"""Latency benchmark for the quote -> decision -> order path. No exchange is contacted.

Run from the repository root:
    python benchmarks/bench_latency.py [--json out.json] [--repeat N] [--warmup W] [--revision LABEL]

The script only uses engine entry points that exist on both the previous
release and this branch, so the same file produces comparable "before" and
"after" numbers when copied into a checkout of either. Sections that need
the new modules (decision records, the websocket feed, the metrics
reservoirs) are skipped where those modules are absent and say so.

Sampling: every row first runs ``--warmup`` iterations (default 5) that are
discarded, then records ``--repeat`` samples (default 100); both numbers are
the same for every row, so no row compares a cold first call with warm
calls. Quantiles are nearest-rank on the sorted samples. A p99 is reported
only when a row has at least 100 samples; below that the tail is reported
as ``max_ms`` and ``tail`` says ``max``.

What is measured (wall clock, milliseconds, p50/p95 and p99-or-max):

* ``decision_cpu``      indicator pipeline + entry rule for one symbol, and
                        (new) one ``decision.evaluate_entry`` record.
* ``scan_warm``         a strategy scan over 8 symbols whose candles are
                        cached: one bulk REST ticker read plus per-symbol
                        decisions. (new) The engine's own quote-to-decision
                        reservoir, cleared after the warm-up so it covers the
                        timed scans only; the set-up scans (one cold) are
                        reported separately.
* ``monitor_rest``      one protective monitor tick with 1 and 5 held
                        positions against a REST fake costing 0 ms and 150 ms
                        per request. The positions are opened with fresh
                        marks and the script asserts they are all open.
* ``monitor_stream``    (new) the same tick when every held asset has a live
                        stream quote, so no REST round trip is made.
* ``feed_ingest``       (new) cost of ingesting one websocket update.
* ``paper_order``       a paper buy and a paper sell from decision to ledger
                        (new code: through the risk gate, journal and the
                        paper exchange adapter; old code: inline simulation).

Limitations: a fake exchange with a fixed round-trip time is not a real
network; CPU numbers depend on the host; the stream figures use a stub feed
that returns fresh quotes and therefore show the engine's cost once a quote
has arrived, not the exchange's publication delay.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ.setdefault('CRYPTOPILOT_DATA_DIR', tempfile.mkdtemp(prefix='cryptopilot-bench-'))
os.environ.setdefault('PYTHON_KEYRING_BACKEND', 'keyring.backends.null.Keyring')
os.environ.setdefault('CRYPTOPILOT_NO_UPDATE_CHECK', '1')

import storage  # noqa: E402
import strategy  # noqa: E402
import bench_hot_path  # noqa: E402
from bench_hot_path import make_engine, open_paper_positions, walk  # noqa: E402

try:
    import decision  # noqa: E402
    import feed  # noqa: E402
    import observability  # noqa: E402
    HAS_NEW = True
except ImportError:                       # previous release
    decision = feed = observability = None
    HAS_NEW = False


# Discarded iterations before every row; main() sets it from --warmup.
WARMUP = 5
# Below this many samples a "p99" is just the maximum, so it is not reported.
P99_MIN_SAMPLES = 100


def pct(samples_ms: list[float], *, warmup: int | None = None) -> dict:
    ordered = sorted(samples_ms)
    count = len(ordered)

    def q(fraction):
        return round(ordered[min(count - 1, max(0, int(round(fraction * (count - 1)))))], 3)
    result = {'count': count, 'warmup_discarded': WARMUP if warmup is None else warmup,
              'p50_ms': q(.5), 'p95_ms': q(.95)}
    if count >= P99_MIN_SAMPLES:
        result['p99_ms'] = q(.99)
    result.update({'max_ms': round(ordered[-1], 3), 'tail': 'p99' if count >= P99_MIN_SAMPLES else 'max',
                   'mean_ms': round(statistics.fmean(ordered), 3)})
    return result


def timed(fn, repeat: int, *, after_warmup=None) -> dict:
    """Run ``WARMUP`` discarded calls, then time ``repeat`` calls."""
    for _ in range(WARMUP):
        fn()
    if after_warmup is not None:
        after_warmup()
    samples = []
    for _ in range(repeat):
        start = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - start) * 1000)
    return pct(samples)


def tail_rule(summary):
    """Apply the p99 rule to an engine reservoir summary (which always reports a p99)."""
    if not isinstance(summary, dict) or summary.get('p50_ms') is None:
        return summary
    summary = dict(summary)
    window = summary.get('window', summary.get('count', 0))
    if window < P99_MIN_SAMPLES:
        summary.pop('p99_ms', None)
    summary['tail'] = 'p99' if 'p99_ms' in summary else 'max'
    return summary


def engine_reservoir(engine, name):
    return tail_rule(engine.metrics.snapshot()['latency'].get(name)) if HAS_NEW else None


def clear_reservoirs(engine, *names) -> dict:
    """Drop the named engine reservoirs (shared by every engine in this process) and return what they held."""
    if not HAS_NEW:
        return {}
    metrics = engine.metrics
    with metrics.lock:
        dropped = {name: metrics.timings.pop(name, None) for name in names}
    return {name: tail_rule(reservoir.summary()) for name, reservoir in dropped.items() if reservoir is not None}


def decision_cpu(repeat: int) -> dict:
    primary, higher = walk(176, 3600), walk(86, 14400)
    now_ms = int(time.time() * 1000)
    frame = strategy.indicators(strategy.frame_from_ohlcv(primary, timeframe='1h', now_ms=now_ms))
    regime = strategy.regime_signal(strategy.indicators(strategy.frame_from_ohlcv(higher, timeframe='4h', now_ms=now_ms)))
    last = frame.iloc[-1]

    def pipeline():
        table = strategy.indicators(strategy.frame_from_ohlcv(primary, timeframe='1h', now_ms=now_ms))
        strategy.entry_signal(table.iloc[-1], regime_ok=regime)

    result = {'indicators_plus_rule': timed(pipeline, repeat)}
    if HAS_NEW:
        cfg = {'min_quote_volume': 5e6, 'max_spread_pct': .85, 'data_max_age_seconds': 120}
        now = time.time()

        def record():
            decision.evaluate_entry(symbol='BTC/USDT', last=last, regime_ok=regime, market_ok=None, rapid=False,
                                    filters=True, price=float(last['close']), volume=5e7, spread_pct=.02,
                                    quote_age_s=.2, quote_ts=now - .2, cfg=cfg, now=now)
        result['decision_record'] = timed(record, repeat)
    else:
        result['decision_record'] = 'not available on this tree'
    return result


def scan_warm(repeat: int, rtt: float = .15) -> dict:
    symbols = ['BTC/USDT'] + [f'C{i:02d}/USDT' for i in range(7)]
    engine, exchange = make_engine(symbols, rtt, 50, 'balanced')
    clear_reservoirs(engine, 'decision.quote_to_decision_ms')
    engine.scan_once(allow_orders=False)          # cold: catalogue and candle downloads
    engine.scan_once(allow_orders=False)          # candles cached from here on
    setup = {}

    def after_warmup():
        exchange.requests = 0
        setup.update(clear_reservoirs(engine, 'decision.quote_to_decision_ms'))
    result = {'symbols': len(symbols), 'rtt_ms': rtt * 1000,
              'scan': timed(lambda: engine.scan_once(allow_orders=False), repeat, after_warmup=after_warmup)}
    result['requests_per_scan'] = exchange.requests / repeat
    if HAS_NEW:
        result['quote_to_decision_per_symbol'] = engine_reservoir(engine, 'decision.quote_to_decision_ms')
        result['quote_to_decision_setup_scans'] = setup.get('decision.quote_to_decision_ms')
        if engine.decisions.latest:
            sample = next(iter(engine.decisions.latest.values()))
            result['sample_decision'] = {'action': sample.action, 'status': sample.status, 'checks': len(sample.checks)}
    else:
        result['quote_to_decision_per_symbol'] = 'not available on this tree'
    return result


class StubFeed:
    """A healthy stream whose quotes are always fresh; stands in for feed.QuoteFeed."""
    state = 'live'

    def __init__(self, symbols, bid=100.0, ask=100.02):
        self.symbols, self.bid, self.ask = list(symbols), bid, ask

    def ticker(self, symbol, *, max_age=None):
        if symbol not in self.symbols:
            return None
        return {'symbol': symbol, 'bid': self.bid, 'ask': self.ask, 'timestamp': None,
                '_received_at': time.time(), '_source': 'stream'}

    def covers(self, symbols, *, max_age):
        return bool(symbols) and all(s in self.symbols for s in symbols)

    def set_symbols(self, symbols):
        self.symbols = list(symbols)

    def status(self):
        return {'state': self.state}

    def healthy(self):
        return True

    def stop(self, timeout=None):
        pass


def fresh_ledger(engine) -> None:
    """Every engine in this process shares one paper ledger; start each run from an empty one."""
    storage.reset_paper(engine.exchange_name)


def monitor_tick(positions: int, rtt: float, repeat: int, *, stream: bool) -> dict:
    symbols = [f'C{i:02d}/USDT' for i in range(positions)]
    engine, exchange = make_engine(symbols, rtt, 0, 'balanced')
    # Resets the shared ledger, seeds fresh marks before each buy and asserts
    # that ``positions`` paper positions are open.
    held = open_paper_positions(engine, symbols)
    engine.running = True
    engine.stop_event.clear()
    if stream:
        engine.feed = StubFeed(symbols)

    def reset_counter():
        exchange.requests = 0
    result = timed(lambda: engine._monitor_if_due(force=True), repeat, after_warmup=reset_counter)
    engine.running = False
    still_open = len(engine._active_positions(engine.mode))
    assert still_open == positions, f'{positions - still_open} position(s) closed during the timed ticks'
    return {'positions': positions, 'open_positions': len(held), 'rtt_ms': rtt * 1000,
            'source': 'stream' if stream else 'rest', 'requests_per_tick': exchange.requests / repeat, **result}


def feed_ingest(repeat: int) -> dict:
    if not HAS_NEW:
        return {'note': 'not available on this tree'}
    symbols = [f'C{i:02d}/USDT' for i in range(40)]
    stream = feed.QuoteFeed('mexc', symbols, client_factory=lambda: None, metrics=observability.Metrics())
    base = int(time.time() * 1000)
    updates = [{symbols[i % 40]: {'bid': 100 + i * 1e-4, 'ask': 100.02 + i * 1e-4, 'timestamp': base + i}}
               for i in range(WARMUP + repeat)]
    samples = []
    for index, update in enumerate(updates):
        start = time.perf_counter()
        stream._ingest(update, time.time())
        if index >= WARMUP:
            samples.append((time.perf_counter() - start) * 1000)
    summary = pct(samples)
    summary['out_of_order'] = stream.counters['out_of_order']
    return summary


def paper_order(repeat: int) -> dict:
    engine, _ = make_engine(['BTC/USDT'], 0.0, 0, 'balanced')
    fresh_ledger(engine)
    if 'max_orders_per_minute' in engine.cfg:
        engine.cfg['max_orders_per_minute'] = 10 ** 6
    buys, sells = [], []
    for index in range(WARMUP + repeat):
        if index == WARMUP:
            clear_reservoirs(engine, 'execution.submit_total_ms', 'execution.submit_to_ack_ms')
        start = time.perf_counter()
        assert engine._buy('BTC/USDT', 50, 100, .04)
        buy_ms = (time.perf_counter() - start) * 1000
        position = engine._active_positions('paper')[0]
        start = time.perf_counter()
        engine._sell(position, 100, 'benchmark exit')
        sell_ms = (time.perf_counter() - start) * 1000
        assert not engine._active_positions('paper'), 'the benchmark sell left a position open'
        if index >= WARMUP:
            buys.append(buy_ms)
            sells.append(sell_ms)
    result = {'buy': pct(buys), 'sell': pct(sells)}
    if HAS_NEW:
        # Buys and sells both submit, so these hold two samples per timed round trip.
        result['engine_submit_total'] = engine_reservoir(engine, 'execution.submit_total_ms')
        result['engine_submit_to_ack'] = engine_reservoir(engine, 'execution.submit_to_ack_ms')
    return result


def main():
    global WARMUP
    parser = argparse.ArgumentParser()
    parser.add_argument('--json')
    parser.add_argument('--repeat', type=int, default=100, help='samples per row (the same for every row)')
    parser.add_argument('--warmup', type=int, default=WARMUP, help='discarded iterations before every row')
    parser.add_argument('--revision', default='', help='label for the measured tree (default: git rev-parse)')
    args = parser.parse_args()
    WARMUP = bench_hot_path.WARMUP = max(0, args.warmup)
    revision = args.revision
    if not revision:
        try:
            revision = subprocess.run(['git', 'rev-parse', '--short', 'HEAD'], cwd=ROOT, capture_output=True,
                                      text=True, check=False).stdout.strip()
        except OSError:
            revision = ''
    results = {'tree': str(ROOT), 'revision': revision or 'unknown', 'python': platform.python_version(),
               'has_new_modules': HAS_NEW, 'repeat': args.repeat, 'warmup': WARMUP,
               'p99_min_samples': P99_MIN_SAMPLES,
               'decision_cpu': decision_cpu(args.repeat),
               'scan_warm': scan_warm(args.repeat),
               'monitor_rest': [monitor_tick(1, 0.0, args.repeat, stream=False),
                                monitor_tick(5, 0.0, args.repeat, stream=False),
                                monitor_tick(1, .15, args.repeat, stream=False),
                                monitor_tick(5, .15, args.repeat, stream=False)],
               'monitor_stream': ([monitor_tick(1, .15, args.repeat, stream=True),
                                   monitor_tick(5, .15, args.repeat, stream=True)] if HAS_NEW
                                  else 'not available on this tree'),
               'feed_ingest': feed_ingest(args.repeat),
               'paper_order': paper_order(args.repeat)}
    print(json.dumps(results, indent=2, default=str))
    if args.json:
        Path(args.json).write_text(json.dumps(results, indent=2, default=str))


if __name__ == '__main__':
    main()
