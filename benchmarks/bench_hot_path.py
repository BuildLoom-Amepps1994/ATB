"""Offline hot-path benchmark. No exchange is contacted.

Run from the repository root:  python benchmarks/bench_hot_path.py [--json out.json] [--warmup N]

Measures the CPU cost of the per-symbol indicator pipeline and research
backtests, plus the wall clock of a cold strategy scan and a protective
monitor tick against a fake exchange that sleeps a fixed round-trip time on
every request and declares a CCXT-style ``rateLimit``.

Every ``timed`` row first runs ``--warmup`` discarded iterations (the same
number for every row), so a one-off first call is not part of the median.

The monitor rows open the requested number of paper positions before timing
and assert that they are all open. Each buy is preceded by a fresh bid/ask
mark for every symbol through the engine's own ``_observe_ticker``: the risk
gate on the hardening branch refuses an entry while any held asset has a
stale or missing mark, and the previous release ignores marks for paper
entries, so seeding keeps the two revisions measuring the same portfolio.
The same file runs unchanged on the previous release (``7207e82``) and on
the branch.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import platform
import random
import statistics
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault('CRYPTOPILOT_DATA_DIR', tempfile.mkdtemp(prefix='cryptopilot-bench-'))

import credentials  # noqa: E402
import storage  # noqa: E402
import strategy  # noqa: E402
import trader  # noqa: E402

SECONDS = {'1m': 60, '5m': 300, '1h': 3600, '4h': 14400, '1d': 86400, '1w': 604800}

# Discarded iterations before every timed row; main() can override it.
WARMUP = 5


def walk(count: int, duration: int, *, seed: int = 7, end: int | None = None) -> list[list[float]]:
    rng = random.Random(seed)
    end = end if end is not None else int(time.time() // duration) * duration
    price, rows = 100.0, []
    for i in range(count):
        opened = price
        price *= math.exp(rng.gauss(0.0002, 0.01))
        high = max(opened, price) * (1 + abs(rng.gauss(0, .003)))
        low = min(opened, price) * (1 - abs(rng.gauss(0, .003)))
        rows.append([(end - (count - i) * duration) * 1000, opened, high, low, price,
                     1000 + rng.random() * 500])
    return rows


class LatencyExchange:
    """A public/private fake whose every request costs ``rtt`` seconds."""

    def __init__(self, symbols, rtt: float, rate_limit_ms: float):
        self.rtt, self.rateLimit = rtt, rate_limit_ms
        self.has = {'fetchTickers': True}
        self.markets = {s: {'spot': True, 'active': True, 'contract': False,
                            'limits': {'cost': {'min': 10}, 'amount': {'min': 1e-6}}} for s in symbols}
        self.requests = 0
        self.in_flight = self.max_in_flight = 0
        self._count_lock = threading.Lock()
        self._last_start = 0.0
        self._pace_lock = threading.Lock()

    def _request(self):
        # CCXT's throttle spaces request *starts* by rateLimit milliseconds.
        with self._pace_lock:
            delay = self._last_start + self.rateLimit / 1000 - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            self._last_start = time.monotonic()
        with self._count_lock:
            self.requests += 1
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            time.sleep(self.rtt)
        finally:
            with self._count_lock:
                self.in_flight -= 1

    def load_markets(self, reload=False):
        return self.markets

    def market(self, symbol):
        return self.markets[symbol]

    @staticmethod
    def _quote():
        return {'timestamp': int(time.time() * 1000), 'bid': 99.99, 'ask': 100.01,
                'last': 100.0, 'quoteVolume': 5e7}

    def fetch_ticker(self, symbol):
        self._request()
        return self._quote()

    def fetch_tickers(self, symbols=None):
        self._request()
        return {s: self._quote() for s in (symbols or self.markets)}

    def fetch_ohlcv(self, symbol, timeframe='1h', limit=175):
        self._request()
        return walk(limit, SECONDS[timeframe], seed=hash((symbol, timeframe)) & 0xffff)

    def fetch_balance(self, params=None):
        return {'free': {'USDT': 10000.0}}


def timed(fn, repeat: int, *, warmup: int | None = None) -> dict:
    warmup = WARMUP if warmup is None else warmup
    for _ in range(warmup):
        fn()
    samples = []
    for _ in range(repeat):
        start = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - start) * 1000)
    return {'median_ms': round(statistics.median(samples), 3), 'min_ms': round(min(samples), 3),
            'samples': repeat, 'warmup_discarded': warmup}


def seed_marks(engine, symbols) -> None:
    """Give every symbol a fresh bid/ask mark through the engine's quote path.

    ``_observe_ticker`` exists with the same signature on both revisions. The
    quote is the fake exchange's own (bid 99.99, ask 100.01, timestamp now).
    """
    for symbol in symbols:
        engine._observe_ticker(symbol, LatencyExchange._quote(), for_buy=True)


def open_paper_positions(engine, symbols, *, budget: float = 50, price: float = 100,
                         fraction: float = .04) -> list[dict]:
    """Open one paper position per symbol from an empty ledger and prove they are all open."""
    # Every engine in this process shares one paper ledger.
    storage.reset_paper(engine.exchange_name)
    engine.cfg['max_positions'] = max(8, len(symbols))
    if 'max_orders_per_minute' in engine.cfg:      # branch risk gate; absent on the previous release
        engine.cfg['max_orders_per_minute'] = 10 ** 6
    for symbol in symbols:
        # The branch's gate needs a fresh mark for every held asset (and uses
        # the new symbol's mark once it has one); refresh all of them each time.
        seed_marks(engine, symbols)
        assert engine._buy(symbol, budget, price, fraction), f'paper buy of {symbol} was refused'
    held = engine._active_positions(engine.mode)
    assert engine.mode == 'paper' and len(held) == len(symbols), (
        f'expected {len(symbols)} open paper positions, found {len(held)}')
    return held


def cpu_benchmarks() -> dict:
    primary = walk(176, 3600)
    higher = walk(86, 14400)
    research = walk(900, 3600)
    research_higher = walk(900, 14400, seed=11)
    now_ms = int(time.time() * 1000)

    def pipeline():
        frame = strategy.indicators(strategy.frame_from_ohlcv(primary, timeframe='1h', now_ms=now_ms))
        regime = strategy.regime_signal(strategy.indicators(
            strategy.frame_from_ohlcv(higher, timeframe='4h', now_ms=now_ms)))
        strategy.entry_signal(frame.iloc[-1], regime_ok=regime)

    def report():
        strategy.research_report(research, {}, timeframe='1h', higher_rows=research_higher,
                                 higher_timeframe='4h', now_ms=now_ms)

    return {'symbol_pipeline_175_candles': timed(pipeline, 60),
            'research_report_900_candles': timed(report, 5)}


def make_engine(symbols, rtt, rate_limit_ms, horizon):
    storage.init_db()
    credentials.load = lambda _name: None
    exchange = LatencyExchange(symbols, rtt, rate_limit_ms)
    trader.TradingEngine._new_exchange = staticmethod(lambda *a, **k: exchange)
    engine = trader.TradingEngine()
    engine.cfg = {**engine.cfg, 'watchlist': list(symbols), 'time_horizon': horizon}
    return engine, exchange


def scan_benchmark(symbol_count: int, rtt: float, rate_limit_ms: float, horizon: str) -> dict:
    symbols = ['BTC/USDT'] + [f'C{i:02d}/USDT' for i in range(symbol_count - 1)]
    engine, exchange = make_engine(symbols, rtt, rate_limit_ms, horizon)
    engine.scan_once(allow_orders=False)            # catalogue warm-up
    engine.data.cache.clear()
    exchange.requests = exchange.max_in_flight = 0
    start = time.perf_counter()
    rows = engine.scan_once(allow_orders=False)
    cold = time.perf_counter() - start
    cold_requests, concurrency = exchange.requests, exchange.max_in_flight
    exchange.requests = 0
    start = time.perf_counter()
    engine.scan_once(allow_orders=False)
    warm = time.perf_counter() - start
    assert len(rows) == symbol_count and all('Scan unavailable' not in r['status'] for r in rows), rows[:2]
    return {'symbols': symbol_count, 'rtt_ms': rtt * 1000, 'rate_limit_ms': rate_limit_ms,
            'horizon': horizon, 'cold_scan_s': round(cold, 3), 'cold_requests': cold_requests,
            'max_concurrent_requests': concurrency, 'warm_scan_s': round(warm, 3),
            'warm_requests': exchange.requests}


def monitor_benchmark(positions: int, repeat: int = 100) -> dict:
    symbols = [f'C{i:02d}/USDT' for i in range(positions)]
    engine, exchange = make_engine(symbols, 0.0, 0, 'balanced')
    held = open_paper_positions(engine, symbols)
    engine.running = True
    engine.stop_event.clear()

    def tick():
        engine._monitor_if_due(force=True)
    warmup = WARMUP
    for _ in range(warmup):
        tick()
    exchange.requests = 0
    result = timed(tick, repeat, warmup=0)
    result['warmup_discarded'] = warmup
    requests = exchange.requests
    engine.running = False
    still_open = len(engine._active_positions(engine.mode))
    assert still_open == positions, f'{positions - still_open} position(s) closed during the timed ticks'
    return {'positions': positions, 'open_positions': len(held), 'requests_per_tick': requests / repeat, **result}


def main():
    global WARMUP
    parser = argparse.ArgumentParser()
    parser.add_argument('--json')
    parser.add_argument('--warmup', type=int, default=WARMUP)
    parser.add_argument('--revision', default='', help='label for the measured tree (default: git rev-parse)')
    args = parser.parse_args()
    WARMUP = max(0, args.warmup)
    revision = args.revision
    if not revision:
        try:
            import subprocess
            revision = subprocess.run(['git', 'rev-parse', '--short', 'HEAD'], cwd=ROOT, capture_output=True,
                                      text=True, check=False).stdout.strip()
        except OSError:
            revision = ''
    results = {'tree': str(ROOT), 'revision': revision or 'unknown', 'python': platform.python_version(),
               'warmup': WARMUP,
               'cpu': cpu_benchmarks(),
               'scan': [scan_benchmark(8, .15, 50, 'balanced'),     # MEXC-like
                        scan_benchmark(20, .15, 50, 'rapid'),
                        scan_benchmark(8, .15, 1000, 'balanced')],  # Kraken-like
               'monitor': [monitor_benchmark(1), monitor_benchmark(5)]}
    print(json.dumps(results, indent=2))
    if args.json:
        Path(args.json).write_text(json.dumps(results, indent=2))


if __name__ == '__main__':
    main()
