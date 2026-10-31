"""Short-horizon dip-buying rules on real MEXC 1-hour candles (offline research).

Why: CryptoPilot's trend strategies trade too rarely to show a result inside a
24-hour paper cycle, and its fast trend strategies (Rapid, Balanced) showed no
edge after costs. This tests a different, documented idea, short-term reversal
after a sharp drop, before any of it is built into the app.

PROTOCOL (fixed before any result; see ``PROTOCOL``)
* Same date splits as research/classic_walkforward.py: development before
  2024-01-01, validation to 2025-05-01, holdout after. The holdout is read only
  for a rule chosen on development and validation.
* Long only, one position per pair, at most one entry per bar. A signal on a
  completed bar buys at the next bar's open; exits fill at the next open, or
  at the stop when a bar's low reaches it (checked first, pessimistically).
* Costs per fill: realistic 0.05 % fee + 0.05 % slippage; doubled 0.10 + 0.10.
* Rules (the only ones tested):
    A  RSI(2) < 5 and close > EMA(200): exit on a close above SMA(5), after 24
       bars, or at entry - 3 ATR(14).
    B  20-bar z-score < -2.5 and close > EMA(200): exit on a close at or above
       SMA(20), after 24 bars, or at entry - 3 ATR(14).
    C  rule A without the EMA(200) filter.
* Account figure: each trade sized at 8 % of the pair's sleeve (the app's
  largest position), compounding within the pair; pairs equal-weighted.
* Selection: among rules with a positive development mean trade at realistic
  cost and t > 2, the highest validation mean trade with at least 100 trades.
"""
from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from research.classic_walkforward import fill_gaps  # noqa: E402

PROTOCOL = {
    'version': 'dip-research-v1',
    'splits': {'development': [None, '2024-01-01'], 'validation': ['2024-01-01', '2025-05-01'],
               'holdout': ['2025-05-01', None]},
    'costs': {'realistic': [0.05, 0.05], 'doubled': [0.10, 0.10]},
    'rules': {'A': {'rsi2_below': 5, 'trend': True, 'exit': 'close>sma5'},
              'B': {'z20_below': -2.5, 'trend': True, 'exit': 'close>=sma20'},
              'C': {'rsi2_below': 5, 'trend': False, 'exit': 'close>sma5'}},
    'max_hold_bars': 24, 'stop_atr': 3.0, 'position_fraction': 0.08,
    'selection': 'positive development mean trade with t > 2; highest validation mean trade, >= 100 trades',
}


def _ms(text):
    return int(datetime.fromisoformat(text).replace(tzinfo=timezone.utc).timestamp() * 1000) if text else None


def features(rows) -> pd.DataFrame:
    df = pd.DataFrame(rows, columns=['ts', 'open', 'high', 'low', 'close', 'volume'])
    close = df['close']
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / 2, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / 2, adjust=False).mean()
    df['rsi2'] = 100 - 100 / (1 + gain / loss.replace(0, np.nan))
    df['ema200'] = close.ewm(span=200, adjust=False).mean()
    df['sma5'] = close.rolling(5).mean()
    df['sma20'] = close.rolling(20).mean()
    df['z20'] = (close - df['sma20']) / close.rolling(20).std()
    tr = pd.concat([df['high'] - df['low'], (df['high'] - close.shift()).abs(), (df['low'] - close.shift()).abs()],
                   axis=1).max(axis=1)
    df['atr'] = tr.ewm(alpha=1 / 14, adjust=False).mean()
    df['flat'] = df['volume'] <= 0          # a bar filled across a gap: never a signal
    return df


def simulate(df: pd.DataFrame, rule: str, start_ms, end_ms, fee: float, slip: float) -> list[float]:
    spec = PROTOCOL['rules'][rule]
    hold, stop_atr = PROTOCOL['max_hold_bars'], PROTOCOL['stop_atr']
    ts, o, h, l, c = (df[k].to_numpy() for k in ('ts', 'open', 'high', 'low', 'close'))
    rsi, ema, sma5, sma20, z, atr, flat = (df[k].to_numpy() for k in ('rsi2', 'ema200', 'sma5', 'sma20', 'z20', 'atr',
                                                                       'flat'))
    trades, i, n = [], 220, len(df)
    while i < n - 1:
        if (start_ms is not None and ts[i] < start_ms) or flat[i]:
            i += 1
            continue
        if end_ms is not None and ts[i + 1] >= end_ms:
            break
        if 'rsi2_below' in spec:
            signal = rsi[i] < spec['rsi2_below']
        else:
            signal = z[i] < spec['z20_below']
        if signal and spec['trend']:
            signal = c[i] > ema[i]
        if not signal or not math.isfinite(atr[i]):
            i += 1
            continue
        entry_bar = i + 1
        entry = o[entry_bar] * (1 + slip)
        stop = entry - stop_atr * atr[i]
        exit_price, j = None, entry_bar
        while j < n:
            if l[j] <= stop:
                exit_price = min(o[j], stop) * (1 - slip)
                break
            done = (c[j] > sma5[j]) if spec['exit'] == 'close>sma5' else (c[j] >= sma20[j])
            if done or j - entry_bar + 1 >= hold or (end_ms is not None and j + 1 < n and ts[j + 1] >= end_ms):
                if j + 1 < n:
                    exit_price = o[j + 1] * (1 - slip)
                    j += 1
                else:
                    exit_price = c[j] * (1 - slip)
                break
            j += 1
        if exit_price is None:
            break
        trades.append(float(exit_price * (1 - fee) / (entry * (1 + fee)) - 1))
        i = j + 1
    return trades


def summarize(per_pair: dict[str, list[float]]) -> dict:
    trades = [t for ts in per_pair.values() for t in ts]
    out = {'pairs': len(per_pair), 'trades': len(trades), 'wins': sum(t > 0 for t in trades)}
    if trades:
        mean = statistics.mean(trades)
        sd = statistics.stdev(trades) if len(trades) > 1 else 0.0
        out['mean_trade_pct'] = round(100 * mean, 4)
        out['t_stat'] = round(mean / (sd / math.sqrt(len(trades))), 2) if sd > 0 else None
        fraction = PROTOCOL['position_fraction']
        accounts = [100 * (math.prod(1 + fraction * t for t in ts) - 1) for ts in per_pair.values()]
        out['account_mean_pct'] = round(statistics.mean(accounts), 3)
    return out


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description='Dip-buying research on 1-hour candles (offline)')
    parser.add_argument('--data', required=True)
    parser.add_argument('--pairs', required=True)
    parser.add_argument('--rules', default='A,B,C')
    parser.add_argument('--splits', default='development,validation')
    parser.add_argument('--costs', default='realistic,doubled')
    parser.add_argument('--out', required=True)
    args = parser.parse_args(argv)
    if 'holdout' in args.splits:
        print('Reading the holdout. By protocol this is done once, for the chosen rule.', flush=True)
    frames = {}
    for base in args.pairs.split(','):
        payload = json.loads((Path(args.data) / f'{base}_USDT_1h_long.json').read_text(encoding='utf-8'))
        rows, _ = fill_gaps(payload['ohlcv'], 3_600_000)
        frames[base] = features(rows)
    result = {'protocol': PROTOCOL, 'results': {}}
    for rule in args.rules.split(','):
        for cost in args.costs.split(','):
            fee, slip = (x / 100 for x in PROTOCOL['costs'][cost])
            for split in args.splits.split(','):
                start, end = (_ms(x) for x in PROTOCOL['splits'][split])
                per_pair = {b: simulate(df, rule, start, end, fee, slip) for b, df in frames.items()}
                key = f'{rule}/{cost}/{split}'
                result['results'][key] = {'summary': summarize(per_pair),
                                          'per_pair': {b: [round(t, 6) for t in ts] for b, ts in per_pair.items()}}
                print(key, json.dumps(result['results'][key]['summary']), flush=True)
    Path(args.out).write_text(json.dumps(result, indent=1), encoding='utf-8')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
