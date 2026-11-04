"""Coin rotation and stop distance for the banded Trend portfolio (offline, no holdout).

Why: the operator asked for a bot that trades the most profitable coins and
switches between them freely. The first test (research/trend_portfolio.py)
filled free slots by momentum rank and did worse than scan order. This tests
active rotation: a weekly swap of the weakest holding for a stronger
qualifying coin. It also tests the disaster stop, because the app's
portfolio-risk cap is measured against it and a 25 % stop lets Trend fill
only about four of its eight slots.

PROTOCOL (fixed before any result; see ``PROTOCOL``)
* Signal, fills, costs, pairs, slots (8, 1/8 of the account each), sizing
  (fixed, or volatility-scaled at a 60 % target) and splits as
  research/trend_portfolio.py.
* rotation in {none, 90, 180}: every 7 days, while a qualifying coin that is
  not held has an L-day return at least 10 percentage points above the
  weakest holding's, sell that holding at the next open and buy the coin; at
  most 2 swaps per rotation day. A coin sold by rotation is not re-bought the
  same day.
* stop in {25, 15, 10} %: a day whose low reaches entry x (1 - stop) sells at
  min(open, stop price), checked before the band exit.
* Selection: among variants whose development Sharpe is at least the base's
  (none, 25 %, fixed) minus 0.10, the highest validation Sharpe; ties go to
  the higher validation return.
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
    'version': 'trend-rotation-v1',
    'lookback': 100, 'band': 0.05, 'vol_days': 20, 'vol_target': 0.60, 'slots': 8,
    'rotation': [None, 90, 180], 'rotation_days': 7, 'rotation_margin': 0.10, 'rotation_max_swaps': 2,
    'stops': [0.25, 0.15, 0.10], 'size': ['fixed', 'vol'],
    'splits': {'development': [None, '2024-01-01'], 'validation': ['2024-01-01', '2025-05-01']},
    'costs': {'fee': 0.0005, 'slip': 0.0005},
    'selection': 'development Sharpe >= base - 0.10; highest validation Sharpe; higher validation return on ties',
}
DAY = 86_400_000


def _ms(text):
    return int(datetime.fromisoformat(text).replace(tzinfo=timezone.utc).timestamp() * 1000) if text else None


def prepare(rows, p=PROTOCOL):
    closes = [r[4] for r in rows]
    n = p['lookback']
    cumulative = [0.0]
    for c in closes:
        cumulative.append(cumulative[-1] + c)
    sma = [None] * len(rows)
    for i in range(n - 1, len(rows)):
        sma[i] = (cumulative[i + 1] - cumulative[i + 1 - n]) / n
    momentum = {}
    for days in [d for d in p['rotation'] if d]:
        momentum[days] = [None] * len(rows)
        for i in range(days, len(rows)):
            momentum[days][i] = closes[i] / closes[i - days] - 1 if closes[i - days] > 0 else None
    vol = [None] * len(rows)
    rets = [None] + [math.log(closes[i] / closes[i - 1]) if closes[i - 1] > 0 and closes[i] > 0 else 0.0
                     for i in range(1, len(rows))]
    for i in range(p['vol_days'], len(rows)):
        window = rets[i - p['vol_days'] + 1:i + 1]
        mean = sum(window) / len(window)
        vol[i] = math.sqrt(sum((x - mean) ** 2 for x in window) / (len(window) - 1)) * math.sqrt(365)
    return {'rows': rows, 'sma': sma, 'momentum': momentum, 'vol': vol, 'by_day': {r[0]: i for i, r in enumerate(rows)}}


def simulate(data, order, *, rotation, stop, size, start, end, p=PROTOCOL):
    fee, slip, band, k = p['costs']['fee'], p['costs']['slip'], p['band'], p['slots']
    days = sorted({d for s in data.values() for d in s['by_day']})
    days = [d for d in days if (start is None or d >= start) and (end is None or d < end)]
    cash, held, trades, equity_path = 1.0, {}, [], []
    rotation_count = 0

    def sell(sym, price):
        nonlocal cash
        proceeds = held[sym]['units'] * price * (1 - slip) * (1 - fee)
        cash += proceeds
        trades.append(proceeds / held[sym]['cost'] - 1)
        del held[sym]

    def buy(sym, day, equity, vol):
        nonlocal cash
        fraction = 1.0 / k
        if size == 'vol' and vol:
            fraction *= min(1.0, p['vol_target'] / vol)
        budget = min(cash, equity * fraction)
        if budget <= 0:
            return
        i = data[sym]['by_day'][day]
        price = data[sym]['rows'][i][1] * (1 + slip)
        held[sym] = {'units': budget * (1 - fee) / price, 'cost': budget, 'entry': price}
        cash -= budget

    for n_day, day in enumerate(days):
        if n_day == 0:
            continue
        # 1. stops at today's open or low, judged on today's bar as it unfolds (pessimistic: before the band exit)
        for sym in list(held):
            i = data[sym]['by_day'].get(day)
            if i is None:
                continue
            o, l = data[sym]['rows'][i][1], data[sym]['rows'][i][3]
            level = held[sym]['entry'] * (1 - stop)
            if l <= level:
                sell(sym, min(o, level))
        # 2. band exits and candidates from the previous completed day, filled at today's open
        candidates = []
        for sym in order:
            s = data[sym]
            i = s['by_day'].get(day)
            if i is None or i < 1:
                continue
            prev = i - 1
            close, sma = s['rows'][prev][4], s['sma'][prev]
            if sma is None or s['rows'][prev][5] <= 0:
                continue
            if sym in held and close < sma * (1 - band):
                sell(sym, s['rows'][i][1])
            elif sym not in held and close > sma * (1 + band) and s['vol'][prev]:
                candidates.append(sym)
        equity = cash + sum(h['units'] * data[s]['rows'][data[s]['by_day'][day]][1] for s, h in held.items())
        # 3. rotation: weekly, swap the weakest holding for a stronger qualifying coin
        sold_today = set()
        if rotation and n_day % p['rotation_days'] == 0 and held:
            mom = data[next(iter(held))]['momentum'][rotation]  # noqa: F841  (shape check)
            for _ in range(p['rotation_max_swaps']):
                scored = []
                for sym in candidates:
                    i = data[sym]['by_day'][day] - 1
                    m = data[sym]['momentum'][rotation][i]
                    if m is not None and sym not in held and sym not in sold_today:
                        scored.append((m, sym))
                weakest = None
                for sym in held:
                    i = data[sym]['by_day'].get(day)
                    m = data[sym]['momentum'][rotation][i - 1] if i else None
                    if m is not None and (weakest is None or m < weakest[0]):
                        weakest = (m, sym)
                if not scored or weakest is None:
                    break
                best = max(scored)
                if best[0] - weakest[0] < p['rotation_margin']:
                    break
                sell(weakest[1], data[weakest[1]]['rows'][data[weakest[1]]['by_day'][day]][1])
                sold_today.add(weakest[1])
                i = data[best[1]]['by_day'][day] - 1
                buy(best[1], day, equity, data[best[1]]['vol'][i])
                candidates.remove(best[1])
                rotation_count += 1
        # 4. fill free slots in scan order
        for sym in candidates:
            if len(held) >= k:
                break
            if sym in held or sym in sold_today:
                continue
            i = data[sym]['by_day'][day] - 1
            buy(sym, day, equity, data[sym]['vol'][i])
        equity_path.append(cash + sum(h['units'] * data[s]['rows'][data[s]['by_day'][day]][4] for s, h in held.items()))
    last = days[-1]
    for sym in list(held):
        i = data[sym]['by_day'].get(last)
        if i is not None:
            sell(sym, data[sym]['rows'][i][4])
    if equity_path:
        equity_path[-1] = cash
    daily = [equity_path[i] / equity_path[i - 1] - 1 for i in range(1, len(equity_path))]
    peak = dd = 0.0
    for e in equity_path:
        peak = max(peak, e)
        dd = max(dd, (peak - e) / peak if peak else 0.0)
    sd = statistics.stdev(daily) if len(daily) > 1 else 0.0
    return {'return_pct': round(100 * (cash - 1), 2),
            'sharpe': round(statistics.mean(daily) / sd * math.sqrt(365), 2) if sd > 0 else None,
            'max_drawdown_pct': round(100 * dd, 2), 'trades': len(trades), 'wins': sum(t > 0 for t in trades),
            'rotations': rotation_count,
            'mean_trade_pct': round(100 * statistics.mean(trades), 2) if trades else None}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description='Trend rotation and stop research (offline, development and validation only)')
    parser.add_argument('--data', required=True)
    parser.add_argument('--pairs', required=True)
    parser.add_argument('--out', required=True)
    args = parser.parse_args(argv)
    p = PROTOCOL
    order = args.pairs.split(',')
    data = {}
    for base in order:
        payload = json.loads((Path(args.data) / f'{base}_USDT_1d_long.json').read_text(encoding='utf-8'))
        rows, _ = fill_gaps(payload['ohlcv'], DAY)
        data[base] = prepare(rows)
    result = {'protocol': p, 'results': {}}
    for rotation in p['rotation']:
        for stop in p['stops']:
            for size in p['size']:
                key = f'rot{rotation or 0}/stop{int(stop * 100)}/{size}'
                result['results'][key] = {split: simulate(data, order, rotation=rotation, stop=stop, size=size,
                                                          start=_ms(a), end=_ms(b))
                                          for split, (a, b) in p['splits'].items()}
                dev, val = result['results'][key]['development'], result['results'][key]['validation']
                print(f"{key:22s} dev {dev['return_pct']:8.1f}% sh {dev['sharpe']} dd {dev['max_drawdown_pct']}% "
                      f"| val {val['return_pct']:7.2f}% sh {val['sharpe']} dd {val['max_drawdown_pct']}% "
                      f"trades {val['trades']} rot {val['rotations']}", flush=True)
    base = result['results']['rot0/stop25/fixed']['development']['sharpe'] or 0
    eligible = [(key, r) for key, r in result['results'].items()
                if (r['development']['sharpe'] or -99) >= base - 0.10]
    eligible.sort(key=lambda kv: (-(kv[1]['validation']['sharpe'] or -99), -kv[1]['validation']['return_pct']))
    result['selected'] = eligible[0][0] if eligible else None
    print('selected', result['selected'])
    Path(args.out).write_text(json.dumps(result, indent=1), encoding='utf-8')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
