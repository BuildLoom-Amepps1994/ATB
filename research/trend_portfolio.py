"""Portfolio rules for the banded Trend strategy (offline research, no holdout).

Why: the app runs Trend over a watchlist with a fixed number of position
slots, filling them in scan order as pairs qualify, each sized the same. This
tests two documented refinements on development and validation data only:
which qualifying pair gets a free slot (momentum rank instead of scan order),
and how much to size it (volatility-scaled instead of fixed). The holdout is
not read; the forward paper run is the test.

PROTOCOL (fixed before any result; see ``PROTOCOL``)
* Signal as the app's Trend: enter on a completed daily close more than 5 %
  above the 100-day average, exit after a close more than 5 % below it. Fills
  at the next open, 0.05 % fee + 0.05 % slippage per fill.
* Portfolio: 32 pairs, ``k`` slots, each slot a fixed fraction ``1/k`` of the
  account at entry (the app's equal sizing), cash otherwise, no rebalancing of
  an open position. A freed slot is filled on the next day that a pair
  qualifies.
* Variants, nothing else varied:
    fill in {scan, momentum}: among the pairs qualifying on a day, scan order
      (the watchlist order) or the highest 90-day return first;
    size in {fixed, vol}: the slot fraction, or the slot fraction times
      min(1, 60 % / annualised 20-day volatility), with the remainder in cash;
    k in {5, 8}.
* Splits as research/classic_walkforward.py: development before 2024-01-01,
  validation 2024-01-01 to 2025-05-01. Each split starts flat.
* Metrics: account return, annualised Sharpe of daily returns, maximum
  drawdown, trades.
* Selection: among variants whose development return is at least 90 % of the
  base (scan, fixed, k = 8), the highest validation Sharpe; ties go to the
  higher validation return.
"""
from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from research.classic_walkforward import fill_gaps  # noqa: E402

PROTOCOL = {
    'version': 'trend-portfolio-v1',
    'lookback': 100, 'band': 0.05, 'momentum_days': 90, 'vol_days': 20, 'vol_target': 0.60,
    'fill': ['scan', 'momentum'], 'size': ['fixed', 'vol'], 'slots': [5, 8],
    'splits': {'development': [None, '2024-01-01'], 'validation': ['2024-01-01', '2025-05-01']},
    'costs': {'fee': 0.0005, 'slip': 0.0005},
    'selection': 'development return >= 90 % of base (scan, fixed, k=8); highest validation Sharpe; higher validation return on ties',
}
DAY = 86_400_000


def _ms(text):
    return int(datetime.fromisoformat(text).replace(tzinfo=timezone.utc).timestamp() * 1000) if text else None


def prepare(rows, n, momentum_days, vol_days):
    closes = [r[4] for r in rows]
    cumulative = [0.0]
    for c in closes:
        cumulative.append(cumulative[-1] + c)
    sma = [None] * len(rows)
    for i in range(n - 1, len(rows)):
        sma[i] = (cumulative[i + 1] - cumulative[i + 1 - n]) / n
    momentum = [None] * len(rows)
    for i in range(momentum_days, len(rows)):
        momentum[i] = closes[i] / closes[i - momentum_days] - 1
    vol = [None] * len(rows)
    rets = [None] + [math.log(closes[i] / closes[i - 1]) if closes[i - 1] > 0 else 0.0 for i in range(1, len(rows))]
    for i in range(vol_days, len(rows)):
        window = rets[i - vol_days + 1:i + 1]
        mean = sum(window) / len(window)
        vol[i] = math.sqrt(sum((x - mean) ** 2 for x in window) / (len(window) - 1)) * math.sqrt(365)
    by_day = {r[0]: i for i, r in enumerate(rows)}
    return {'rows': rows, 'sma': sma, 'momentum': momentum, 'vol': vol, 'by_day': by_day}


def simulate(data: dict, order: list[str], *, k: int, fill: str, size: str, start, end, p=PROTOCOL) -> dict:
    fee, slip, band = p['costs']['fee'], p['costs']['slip'], p['band']
    days = sorted({day for d in data.values() for day in d['by_day']})
    days = [d for d in days if (start is None or d >= start) and (end is None or d < end)]
    cash, held = 1.0, {}          # symbol -> {'units', 'cost'}
    equity_path, trades = [], []
    first = True
    for day in days:
        if first:
            first = False
            continue   # a position opens at this day's open from the previous close: skip the split's first day
        # 1. exits and entries decided on the previous completed day, filled at today's open
        signals_exit, candidates = [], []
        for sym in order:
            d = data[sym]
            i = d['by_day'].get(day)
            if i is None or i < 1:
                continue
            prev = i - 1
            close, sma = d['rows'][prev][4], d['sma'][prev]
            if sma is None or d['rows'][prev][5] <= 0:
                continue
            if sym in held and close < sma * (1 - band):
                signals_exit.append(sym)
            elif sym not in held and close > sma * (1 + band) and d['momentum'][prev] is not None and d['vol'][prev]:
                candidates.append((sym, d['momentum'][prev], d['vol'][prev]))
        for sym in signals_exit:
            i = data[sym]['by_day'][day]
            price = data[sym]['rows'][i][1] * (1 - slip)
            proceeds = held[sym]['units'] * price * (1 - fee)
            cash += proceeds
            trades.append(proceeds / held[sym]['cost'] - 1)
            del held[sym]
        if fill == 'momentum':
            candidates.sort(key=lambda c: -c[1])
        equity = cash + sum(h['units'] * data[s]['rows'][data[s]['by_day'][day]][1] for s, h in held.items())
        for sym, _mom, vol in candidates:
            if len(held) >= k:
                break
            fraction = 1.0 / k
            if size == 'vol':
                fraction *= min(1.0, p['vol_target'] / vol) if vol > 0 else 1.0
            budget = min(cash, equity * fraction)
            if budget <= 0:
                continue
            i = data[sym]['by_day'][day]
            price = data[sym]['rows'][i][1] * (1 + slip)
            held[sym] = {'units': budget * (1 - fee) / price, 'cost': budget}
            cash -= budget
        # 2. mark at today's close
        equity = cash + sum(h['units'] * data[s]['rows'][data[s]['by_day'][day]][4] for s, h in held.items())
        equity_path.append(equity)
    # close everything at the last close, after exit costs
    last = days[-1]
    for sym, h in list(held.items()):
        i = data[sym]['by_day'].get(last)
        if i is not None:
            proceeds = h['units'] * data[sym]['rows'][i][4] * (1 - slip) * (1 - fee)
            cash += proceeds
            trades.append(proceeds / h['cost'] - 1)
    if equity_path:
        equity_path[-1] = cash
    daily = [equity_path[i] / equity_path[i - 1] - 1 for i in range(1, len(equity_path))]
    peak, dd = 0.0, 0.0
    for e in equity_path:
        peak = max(peak, e)
        dd = max(dd, (peak - e) / peak if peak else 0.0)
    sd = statistics.stdev(daily) if len(daily) > 1 else 0.0
    return {'return_pct': round(100 * (cash - 1), 2),
            'sharpe': round(statistics.mean(daily) / sd * math.sqrt(365), 2) if sd > 0 else None,
            'max_drawdown_pct': round(100 * dd, 2), 'trades': len(trades),
            'wins': sum(t > 0 for t in trades),
            'mean_trade_pct': round(100 * statistics.mean(trades), 2) if trades else None,
            'days': len(equity_path)}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description='Trend portfolio rules (offline, development and validation only)')
    parser.add_argument('--data', required=True)
    parser.add_argument('--pairs', required=True, help='comma-separated bases in watchlist (scan) order')
    parser.add_argument('--out', required=True)
    args = parser.parse_args(argv)
    p = PROTOCOL
    order = args.pairs.split(',')
    data = {}
    for base in order:
        payload = json.loads((Path(args.data) / f'{base}_USDT_1d_long.json').read_text(encoding='utf-8'))
        rows, _ = fill_gaps(payload['ohlcv'], DAY)
        data[base] = prepare(rows, p['lookback'], p['momentum_days'], p['vol_days'])
    result = {'protocol': p, 'results': {}}
    for k in p['slots']:
        for fill in p['fill']:
            for size in p['size']:
                key = f'{fill}/{size}/k{k}'
                result['results'][key] = {}
                for split, (start, end) in p['splits'].items():
                    result['results'][key][split] = simulate(data, order, k=k, fill=fill, size=size,
                                                             start=_ms(start), end=_ms(end))
                dev, val = result['results'][key]['development'], result['results'][key]['validation']
                print(f"{key:18s} dev {dev['return_pct']:8.2f}% sharpe {dev['sharpe']} dd {dev['max_drawdown_pct']}% "
                      f"| val {val['return_pct']:7.2f}% sharpe {val['sharpe']} dd {val['max_drawdown_pct']}% "
                      f"trades {val['trades']}", flush=True)
    base = result['results']['scan/fixed/k8']['development']['return_pct']
    eligible = [(key, r) for key, r in result['results'].items() if r['development']['return_pct'] >= 0.9 * base]
    eligible.sort(key=lambda kv: (-(kv[1]['validation']['sharpe'] or -99), -kv[1]['validation']['return_pct']))
    result['selected'] = eligible[0][0] if eligible else None
    print('selected', result['selected'])
    Path(args.out).write_text(json.dumps(result, indent=1), encoding='utf-8')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
