"""Whipsaw reduction for the daily Trend rule (offline research, no holdout).

Why: the app's Trend strategy (close above its 100-day average) made 109
holdout trades on 11 pairs with a mean of -2.2 % (docs/CLASSIC_WALKFORWARD.md),
most of them whipsaws around the average. The holdout has been read for the
plain rule, so these variants are judged on development and validation only;
the forward paper run is their out-of-sample test.

PROTOCOL (fixed before any result; see ``PROTOCOL``)
* Base rule as research/trend_research.py with N = 100: hold while the daily
  close is above the average; fills at the next open; costs realistic (0.05 %
  fee + 0.05 % slippage per fill).
* Variants, a small fixed grid, nothing else varied:
    band b in {0, 1, 2, 3, 5} %: enter on a close above SMA x (1 + b), exit on
      a close below SMA x (1 - b);
    rising in {no, yes}: enter only while the average is above its value
      10 days earlier (exits are unchanged).
* Pair sets: the 12 selection pairs and the 20 other pairs, 32 in all.
* Splits: development (before 2024-01-01) and validation (2024-01-01 to
  2025-05-01). The holdout is not read.
* Selection: among variants whose development mean sleeve return over the 32
  pairs is at least 90 % of the plain rule's, the highest validation mean
  sleeve return; ties go to fewer trades.
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
    'version': 'trend-variants-v1',
    'lookback': 100, 'rising_days': 10,
    'bands_pct': [0, 1, 2, 3, 5], 'rising': [False, True],
    'splits': {'development': [None, '2024-01-01'], 'validation': ['2024-01-01', '2025-05-01']},
    'costs': {'realistic': [0.05, 0.05]},
    'selection': 'development mean >= 90 % of the plain rule; highest validation mean sleeve return; fewer trades on ties',
}
DAY = 86_400_000


def _ms(text):
    return int(datetime.fromisoformat(text).replace(tzinfo=timezone.utc).timestamp() * 1000) if text else None


def run(rows, n, start, end, fee, slip, *, band=0.0, rising=False, rising_days=10):
    closes = [r[4] for r in rows]
    cumulative = [0.0]
    for c in closes:
        cumulative.append(cumulative[-1] + c)
    sma = [None] * len(rows)
    for i in range(n - 1, len(rows)):
        sma[i] = (cumulative[i + 1] - cumulative[i + 1 - n]) / n
    out = {'trades': [], 'sleeve_pct': None, 'buy_hold_pct': None, 'days': 0, 'days_in_market': 0}
    equity, held, entry_value, units = 1.0, False, None, 0.0
    first_open = last_close = None
    for i in range(n - 1 + rising_days, len(rows) - 1):
        day = rows[i + 1][0]
        if (start is not None and day < start) or (end is not None and day >= end):
            continue
        if first_open is None:
            first_open = rows[i + 1][1]
        last_close = rows[i + 1][4]
        out['days'] += 1
        above = closes[i] > sma[i] * (1 + band)
        below = closes[i] < sma[i] * (1 - band)
        if rising:
            above = above and sma[i] > sma[i - rising_days]
        price = rows[i + 1][1]
        if above and not held:
            entry_value = equity
            units = equity * (1 - fee) / (price * (1 + slip))
            held = True
        elif below and held:
            equity = units * price * (1 - slip) * (1 - fee)
            out['trades'].append(equity / entry_value - 1)
            held = False
        if held:
            out['days_in_market'] += 1
    if held:
        equity = units * last_close * (1 - slip) * (1 - fee)
        out['trades'].append(equity / entry_value - 1)
    if first_open is not None:
        out['sleeve_pct'] = round(100 * (equity - 1), 3)
        out['buy_hold_pct'] = round(100 * (last_close * (1 - slip) * (1 - fee) / (first_open * (1 + slip) * (1 + fee)) - 1), 3)
    return out


def summarize(per):
    per = {b: r for b, r in per.items() if r['sleeve_pct'] is not None and r['days'] >= 60}
    trades = [t for r in per.values() for t in r['trades']]
    out = {'pairs': len(per), 'trades': len(trades), 'wins': sum(t > 0 for t in trades)}
    if per:
        values = [r['sleeve_pct'] for r in per.values()]
        out.update(sleeve_mean_pct=round(statistics.mean(values), 2), sleeve_median_pct=round(statistics.median(values), 2),
                   buy_hold_mean_pct=round(statistics.mean(r['buy_hold_pct'] for r in per.values()), 2),
                   positive_pairs=sum(v > 0 for v in values),
                   in_market_pct=round(100 * sum(r['days_in_market'] for r in per.values())
                                       / max(1, sum(r['days'] for r in per.values())), 1))
    if len(trades) > 1:
        mean, sd = statistics.mean(trades), statistics.stdev(trades)
        out.update(mean_trade_pct=round(100 * mean, 3), t_stat=round(mean / (sd / math.sqrt(len(trades))), 2) if sd else None)
    return out


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description='Trend whipsaw variants (offline, development and validation only)')
    parser.add_argument('--data', required=True)
    parser.add_argument('--pairs', required=True)
    parser.add_argument('--out', required=True)
    args = parser.parse_args(argv)
    data = {}
    for base in args.pairs.split(','):
        payload = json.loads((Path(args.data) / f'{base}_USDT_1d_long.json').read_text(encoding='utf-8'))
        data[base], _ = fill_gaps(payload['ohlcv'], DAY)
    fee, slip = (x / 100 for x in PROTOCOL['costs']['realistic'])
    n = PROTOCOL['lookback']
    result = {'protocol': PROTOCOL, 'results': {}}
    for band in PROTOCOL['bands_pct']:
        for rising in PROTOCOL['rising']:
            key = f'band{band}/{"rising" if rising else "any"}'
            result['results'][key] = {}
            for split, (start, end) in PROTOCOL['splits'].items():
                per = {b: run(rows, n, _ms(start), _ms(end), fee, slip, band=band / 100, rising=rising,
                              rising_days=PROTOCOL['rising_days']) for b, rows in data.items()}
                result['results'][key][split] = {'summary': summarize(per), 'per_pair': per}
            dev, val = result['results'][key]['development']['summary'], result['results'][key]['validation']['summary']
            print(key, 'dev', dev.get('sleeve_mean_pct'), dev.get('sleeve_median_pct'), dev.get('trades'),
                  '| val', val.get('sleeve_mean_pct'), val.get('sleeve_median_pct'), val.get('positive_pairs'), '/', val.get('pairs'),
                  'trades', val.get('trades'), 'mean trade', val.get('mean_trade_pct'), flush=True)
    plain = result['results']['band0/any']['development']['summary']['sleeve_mean_pct']
    eligible = [(k, v) for k, v in result['results'].items()
                if v['development']['summary']['sleeve_mean_pct'] >= 0.9 * plain]
    eligible.sort(key=lambda kv: (-kv[1]['validation']['summary']['sleeve_mean_pct'], kv[1]['validation']['summary']['trades']))
    result['selected'] = eligible[0][0] if eligible else None
    print('selected', result['selected'], flush=True)
    Path(args.out).write_text(json.dumps(result, indent=1), encoding='utf-8')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
