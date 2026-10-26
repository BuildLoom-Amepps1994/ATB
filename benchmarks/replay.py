"""Deterministic replay check for decision records. No exchange is contacted.

Run from the repository root:  python benchmarks/replay.py [--symbols N] [--json out.json]

For N synthetic symbols the script builds the completed-candle indicator row
and an observed quote, then checks that:

* evaluating the same inputs twice yields byte-identical decision records,
  including the decision id;
* shuffling and duplicating the raw candle rows (an out-of-order exchange
  response) changes nothing;
* adding the still-forming candle, or later completed candles, changes
  nothing about the decision taken at the earlier close (no look-ahead).

Exit status is 1 when any replay differs, so the check can run in CI.
"""
from __future__ import annotations

import argparse
import json
import random
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import decision  # noqa: E402
import strategy  # noqa: E402
from bench_hot_path import walk  # noqa: E402

CFG = {'min_quote_volume': 5e6, 'max_spread_pct': .85, 'data_max_age_seconds': 120}
DURATION = 3600


def decide(rows, *, now_ms: int, symbol: str, timestamp: int | None = None, rng: random.Random):
    frame = strategy.frame_from_ohlcv(rows, timeframe='1h', now_ms=now_ms)
    table = strategy.indicators(frame)
    last = table.iloc[-1] if timestamp is None else table[table['timestamp'] == timestamp].iloc[-1]
    close = float(last['close'])
    # The quote is derived from the seeded generator so every replay sees the same one.
    price = close * (1 + rng.uniform(-.004, .004))
    return decision.evaluate_entry(symbol=symbol, last=last, regime_ok=True, market_ok=None, rapid=False,
                                   filters=True, price=price, volume=2e7, spread_pct=.03, quote_age_s=.5,
                                   quote_ts=now_ms / 1000 - .5, cfg=CFG, now=now_ms / 1000)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--symbols', type=int, default=50)
    parser.add_argument('--json')
    args = parser.parse_args()
    end = int(time.time() // DURATION) * DURATION - 3 * DURATION
    mismatches, actions, compared = [], Counter(), 0
    started = time.perf_counter()
    for index in range(args.symbols):
        symbol = f'S{index:03d}/USDT'
        rows = walk(140, DURATION, seed=index, end=end)
        close_ms = rows[-1][0] + DURATION * 1000
        now_ms = close_ms + 4_000
        baseline = decide(rows, now_ms=now_ms, symbol=symbol, rng=random.Random(index))
        actions[baseline.action] += 1
        variants = {
            'identical_inputs': decide(list(rows), now_ms=now_ms, symbol=symbol, rng=random.Random(index)),
        }
        shuffled = list(rows) + rows[:7]
        random.Random(index + 1).shuffle(shuffled)
        variants['shuffled_and_duplicated_rows'] = decide(shuffled, now_ms=now_ms, symbol=symbol, rng=random.Random(index))
        forming = [close_ms, rows[-1][4], rows[-1][4] * 1.5, rows[-1][4] * .5, rows[-1][4] * 1.3, 10 ** 7]
        variants['forming_candle_present'] = decide(rows + [forming], now_ms=now_ms, symbol=symbol, rng=random.Random(index))
        later = walk(143, DURATION, seed=index, end=end + 3 * DURATION)
        future = decide(later, now_ms=now_ms + 3 * DURATION * 1000, symbol=symbol, timestamp=rows[-1][0],
                        rng=random.Random(index))
        for name, replay in variants.items():
            compared += 1
            if replay != baseline:
                mismatches.append({'symbol': symbol, 'variant': name})
        compared += 1
        if (future.checks, future.score, future.action, future.inputs['signal_candle_ts_ms']) != (
                baseline.checks, baseline.score, baseline.action, baseline.inputs['signal_candle_ts_ms']):
            mismatches.append({'symbol': symbol, 'variant': 'future_candles_present'})
    report = {'symbols': args.symbols, 'comparisons': compared, 'mismatches': mismatches,
              'actions': dict(actions), 'elapsed_s': round(time.perf_counter() - started, 3),
              'decision_version': decision.DECISION_VERSION}
    print(json.dumps(report, indent=2))
    if args.json:
        Path(args.json).write_text(json.dumps(report, indent=2))
    return 1 if mismatches else 0


if __name__ == '__main__':
    sys.exit(main())
