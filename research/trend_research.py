"""Daily time-series trend rule on real MEXC candles (offline research).

Why: the classic Position strategy showed a small edge on its 12 selection
pairs but not on 20 pairs it never saw, and Swing showed none there. This
tests the simplest documented trend rule, with no tuning beyond the three
fixed lookbacks below, before anything is built into the app.

PROTOCOL (fixed before any result; see ``PROTOCOL``)
* Rule T<N>: hold a pair while its daily close is above its N-day simple
  moving average; otherwise hold cash. A close above (below) the average buys
  (sells) at the next day's open. N is 50, 100 or 200; nothing else varies.
* Costs per fill: realistic 0.05 % fee + 0.05 % slippage; doubled 0.10 + 0.10.
* Splits as research/classic_walkforward.py. Two pair sets: the 12 selection
  pairs (development and validation choose N) and 20 other pairs (all splits,
  never used to choose anything).
* Account figure: each pair's sleeve is fully invested while the rule holds
  (a portfolio would split capital across pairs; the per-pair figure is the
  sleeve's return). Buy and hold of the same sleeve is reported beside it.
* Selection: the N with the highest validation mean of sleeve returns at
  realistic cost among those positive in development and validation.
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
    'version': 'trend-research-v1',
    'lookbacks': [50, 100, 200],
    'splits': {'development': [None, '2024-01-01'], 'validation': ['2024-01-01', '2025-05-01'],
               'holdout': ['2025-05-01', None]},
    'costs': {'realistic': [0.05, 0.05], 'doubled': [0.10, 0.10]},
    'selection': 'highest validation mean sleeve return at realistic cost among N positive in development and validation',
}
DAY = 86_400_000


def _ms(text):
    return int(datetime.fromisoformat(text).replace(tzinfo=timezone.utc).timestamp() * 1000) if text else None


def run(rows, n: int, start, end, fee: float, slip: float) -> dict:
    closes = [r[4] for r in rows]
    out = {'trades': [], 'sleeve_pct': None, 'buy_hold_pct': None, 'days_in_market': 0, 'days': 0}
    equity, held, entry_value, units = 1.0, False, None, 0.0
    first_open = last_close = None
    cumulative = [0.0]
    for c in closes:
        cumulative.append(cumulative[-1] + c)
    for i in range(n - 1, len(rows) - 1):
        sma = (cumulative[i + 1] - cumulative[i + 1 - n]) / n     # closes i-n+1 .. i, all completed
        day = rows[i + 1][0]                                      # the day the order fills (next open)
        if (start is not None and day < start) or (end is not None and day >= end):
            continue
        if first_open is None:
            first_open = rows[i + 1][1]
        last_close = rows[i + 1][4]
        out['days'] += 1
        want = closes[i] > sma
        price = rows[i + 1][1]
        if want and not held:
            entry_value = equity
            units = equity * (1 - fee) / (price * (1 + slip))
            held = True
        elif not want and held:
            equity = units * price * (1 - slip) * (1 - fee)
            out['trades'].append(equity / entry_value - 1)
            held = False
        if held:
            out['days_in_market'] += 1
    if held:                                    # mark an open holding at the last close, after exit costs
        equity = units * last_close * (1 - slip) * (1 - fee)
        out['trades'].append(equity / entry_value - 1)
    if first_open is not None:
        out['sleeve_pct'] = round(100 * (equity - 1), 3)
        out['buy_hold_pct'] = round(100 * (last_close * (1 - slip) * (1 - fee) / (first_open * (1 + slip) * (1 + fee)) - 1), 3)
    return out


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description='Daily SMA trend rule research (offline)')
    parser.add_argument('--data', required=True)
    parser.add_argument('--pairs', required=True)
    parser.add_argument('--lookbacks', default='50,100,200')
    parser.add_argument('--splits', default='development,validation')
    parser.add_argument('--costs', default='realistic,doubled')
    parser.add_argument('--out', required=True)
    args = parser.parse_args(argv)
    data = {}
    for base in args.pairs.split(','):
        payload = json.loads((Path(args.data) / f'{base}_USDT_1d_long.json').read_text(encoding='utf-8'))
        data[base], _ = fill_gaps(payload['ohlcv'], DAY)
    result = {'protocol': PROTOCOL, 'results': {}}
    for n in map(int, args.lookbacks.split(',')):
        for cost in args.costs.split(','):
            fee, slip = (x / 100 for x in PROTOCOL['costs'][cost])
            for split in args.splits.split(','):
                start, end = (_ms(x) for x in PROTOCOL['splits'][split])
                per = {b: run(rows, n, start, end, fee, slip) for b, rows in data.items()}
                per = {b: r for b, r in per.items() if r['sleeve_pct'] is not None and r['days'] >= 60}
                trades = [t for r in per.values() for t in r['trades']]
                summary = {'pairs': len(per), 'trades': len(trades), 'wins': sum(t > 0 for t in trades)}
                if per:
                    summary.update(sleeve_mean_pct=round(statistics.mean(r['sleeve_pct'] for r in per.values()), 2),
                                   sleeve_median_pct=round(statistics.median(r['sleeve_pct'] for r in per.values()), 2),
                                   buy_hold_mean_pct=round(statistics.mean(r['buy_hold_pct'] for r in per.values()), 2),
                                   positive_pairs=sum(r['sleeve_pct'] > 0 for r in per.values()),
                                   in_market_pct=round(100 * sum(r['days_in_market'] for r in per.values())
                                                       / max(1, sum(r['days'] for r in per.values())), 1))
                if len(trades) > 1:
                    mean, sd = statistics.mean(trades), statistics.stdev(trades)
                    summary.update(mean_trade_pct=round(100 * mean, 3),
                                   t_stat=round(mean / (sd / math.sqrt(len(trades))), 2) if sd else None)
                key = f'T{n}/{cost}/{split}'
                result['results'][key] = {'summary': summary, 'per_pair': per}
                print(key, json.dumps(summary), flush=True)
    Path(args.out).write_text(json.dumps(result, indent=1), encoding='utf-8')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
