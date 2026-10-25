"""Offline hot-path benchmark. No exchange is contacted.

Run from the repository root:  python benchmarks/bench_hot_path.py [--json out.json]

Measures the CPU cost of the per-symbol indicator pipeline and research
backtests, plus the wall clock of a cold strategy scan and a protective
monitor tick against a fake exchange that sleeps a fixed round-trip time on
every request and declares a CCXT-style ``rateLimit``.
"""
from __future__ import annotations

import argparse
import json
import math
import os
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


def timed(fn, repeat: int) -> dict:
    samples = []
    for _ in range(repeat):
        start = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - start) * 1000)
    return {'median_ms': round(statistics.median(samples), 3), 'min_ms': round(min(samples), 3)}


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


def monitor_benchmark(positions: int) -> dict:
    symbols = [f'C{i:02d}/USDT' for i in range(positions)]
    engine, exchange = make_engine(symbols, 0.0, 0, 'balanced')
    engine.cfg['max_positions'] = 8
    for symbol in symbols:
        engine._buy(symbol, 50, 100, .04)
    engine.running = True
    engine.stop_event.clear()

    def tick():
        engine._monitor_if_due(force=True)
    result = timed(tick, 40)
    engine.running = False
    return {'positions': positions, **result}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--json')
    args = parser.parse_args()
    results = {'cpu': cpu_benchmarks(),
               'scan': [scan_benchmark(8, .15, 50, 'balanced'),     # MEXC-like
                        scan_benchmark(20, .15, 50, 'rapid'),
                        scan_benchmark(8, .15, 1000, 'balanced')],  # Kraken-like
               'monitor': [monitor_benchmark(1), monitor_benchmark(5)]}
    print(json.dumps(results, indent=2))
    if args.json:
        Path(args.json).write_text(json.dumps(results, indent=2))


if __name__ == '__main__':
    main()
