"""Latency benchmark for the quote -> decision -> order path. No exchange is contacted.

Run from the repository root:  python benchmarks/bench_latency.py [--json out.json] [--repeat N]

The script only uses engine entry points that exist on both the previous
release and this branch, so the same file produces comparable "before" and
"after" numbers when copied into a checkout of either. Sections that need
the new modules (decision records, the websocket feed, the metrics
reservoirs) are skipped where those modules are absent and say so.

What is measured (wall clock, milliseconds, p50/p95/p99 over ``--repeat``):

* ``decision_cpu``      indicator pipeline + entry rule for one symbol, and
                        (new) one ``decision.evaluate_entry`` record.
* ``scan_warm``         a strategy scan over 8 symbols whose candles are
                        cached: one bulk REST ticker read plus per-symbol
                        decisions. (new) The engine's own quote-to-decision
                        reservoir for the same scans.
* ``monitor_rest``      one protective monitor tick with 1 and 5 held
                        positions against a REST fake costing 0 ms and 150 ms
                        per request.
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
from bench_hot_path import make_engine, walk  # noqa: E402

try:
    import decision  # noqa: E402
    import feed  # noqa: E402
    import observability  # noqa: E402
    HAS_NEW = True
except ImportError:                       # previous release
    decision = feed = observability = None
    HAS_NEW = False


def pct(samples_ms: list[float]) -> dict:
    ordered = sorted(samples_ms)

    def q(fraction):
        return round(ordered[min(len(ordered) - 1, max(0, int(round(fraction * (len(ordered) - 1)))))], 3)
    return {'count': len(ordered), 'p50_ms': q(.5), 'p95_ms': q(.95), 'p99_ms': q(.99),
            'max_ms': round(ordered[-1], 3), 'mean_ms': round(statistics.fmean(ordered), 3)}


def timed(fn, repeat: int) -> dict:
    samples = []
    for _ in range(repeat):
        start = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - start) * 1000)
    return pct(samples)


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
        result['decision_record'] = timed(record, repeat * 5)
    else:
        result['decision_record'] = 'not available on this tree'
    return result


def scan_warm(repeat: int, rtt: float = .15) -> dict:
    symbols = ['BTC/USDT'] + [f'C{i:02d}/USDT' for i in range(7)]
    engine, exchange = make_engine(symbols, rtt, 50, 'balanced')
    engine.scan_once(allow_orders=False)
    engine.scan_once(allow_orders=False)          # candles cached from here on
    exchange.requests = 0
    result = {'symbols': len(symbols), 'rtt_ms': rtt * 1000,
              'scan': timed(lambda: engine.scan_once(allow_orders=False), repeat)}
    result['requests_per_scan'] = exchange.requests / repeat
    if HAS_NEW:
        summary = engine.metrics.snapshot()['latency'].get('decision.quote_to_decision_ms')
        result['quote_to_decision_per_symbol'] = summary
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
    fresh_ledger(engine)
    engine.cfg['max_positions'] = 8
    if 'max_orders_per_minute' in engine.cfg:
        engine.cfg['max_orders_per_minute'] = 10 ** 6
    for symbol in symbols:
        assert engine._buy(symbol, 50, 100, .04), symbol
        # A held asset needs a recent mark before the next entry is allowed.
        engine.last_prices[symbol], engine.last_price_times[symbol] = 99.99, time.time()
        getattr(engine, 'last_asks', {})[symbol] = 100.01
    engine.running = True
    engine.stop_event.clear()
    if stream:
        engine.feed = StubFeed(symbols)
    exchange.requests = 0
    result = timed(lambda: engine._monitor_if_due(force=True), repeat)
    engine.running = False
    return {'positions': positions, 'rtt_ms': rtt * 1000, 'source': 'stream' if stream else 'rest',
            'requests_per_tick': exchange.requests / repeat, **result}


def feed_ingest(repeat: int) -> dict:
    if not HAS_NEW:
        return {'note': 'not available on this tree'}
    symbols = [f'C{i:02d}/USDT' for i in range(40)]
    stream = feed.QuoteFeed('mexc', symbols, client_factory=lambda: None, metrics=observability.Metrics())
    base = int(time.time() * 1000)
    updates = [{symbols[i % 40]: {'bid': 100 + i * 1e-4, 'ask': 100.02 + i * 1e-4, 'timestamp': base + i}}
               for i in range(repeat * 20)]
    samples = []
    for update in updates:
        start = time.perf_counter()
        stream._ingest(update, time.time())
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
    for _ in range(repeat):
        start = time.perf_counter()
        assert engine._buy('BTC/USDT', 50, 100, .04)
        buys.append((time.perf_counter() - start) * 1000)
        position = engine._active_positions('paper')[0]
        start = time.perf_counter()
        engine._sell(position, 100, 'benchmark exit')
        sells.append((time.perf_counter() - start) * 1000)
    result = {'buy': pct(buys), 'sell': pct(sells)}
    if HAS_NEW:
        timings = engine.metrics.snapshot()['latency']
        result['engine_submit_total'] = timings.get('execution.submit_total_ms')
        result['engine_submit_to_ack'] = timings.get('execution.submit_to_ack_ms')
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--json')
    parser.add_argument('--repeat', type=int, default=40)
    args = parser.parse_args()
    try:
        revision = subprocess.run(['git', 'rev-parse', '--short', 'HEAD'], cwd=ROOT, capture_output=True,
                                  text=True, check=False).stdout.strip() or 'unknown'
    except OSError:
        revision = 'unknown'
    results = {'tree': str(ROOT), 'revision': revision, 'python': platform.python_version(),
               'has_new_modules': HAS_NEW, 'repeat': args.repeat,
               'decision_cpu': decision_cpu(args.repeat),
               'scan_warm': scan_warm(max(5, args.repeat // 4)),
               'monitor_rest': [monitor_tick(1, 0.0, args.repeat, stream=False),
                                monitor_tick(5, 0.0, args.repeat, stream=False),
                                monitor_tick(1, .15, max(10, args.repeat // 2), stream=False),
                                monitor_tick(5, .15, max(10, args.repeat // 2), stream=False)],
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
