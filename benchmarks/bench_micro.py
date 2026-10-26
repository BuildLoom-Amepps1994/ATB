"""Latency benchmark for the PAPER micro mode. No exchange is contacted.

Run from the repository root:
    python benchmarks/bench_micro.py [--json out.json] [--repeat N] [--warmup W]

Rows (wall clock, milliseconds; p50/p95 and p99 when a row has at least 100
samples, otherwise the maximum, as in ``bench_latency.py``):

* ``cpu``: one validated 20-level book from a CCXT response; one all-in sizing
  (including the separate smallest-workable-budget search); one exit estimate;
  completed-bar features from 119 one-minute and 59 five-minute bars; one entry
  decision record; one exit decision.
* ``tick``: one ``MicroManager.tick`` for 1, 4 and 8 selected pairs, with no
  holding and with one holding, against a public-data fixture whose every
  request costs 0 ms or 150 ms. Each tick runs just after a completed bar (the
  slow path: every pair's context is due) and between bars (the fast path:
  books and exits only). ``requests_per_tick`` counts exchange reads.
* ``cycle``: the engine's own reservoirs over a run of the mechanics fixture:
  book receipt to entry decision, exit decision, cash reservation, submit to
  fill, and fill to ledger commit, plus the end-to-end entry and exit paths
  timed around ``_enter`` and ``_sell``.

Limitations: a fixture with a fixed round trip is not a real network or a
real MEXC response; a one-minute bar strategy cannot use a faster quote than
its bar close for entries, so the entry-relevant latency is the book-to-order
path once a bar has closed. Numbers depend on this host.
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
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ['CRYPTOPILOT_DATA_DIR'] = tempfile.mkdtemp(prefix='cryptopilot-bench-micro-')
os.environ['PYTHON_KEYRING_BACKEND'] = 'keyring.backends.null.Keyring'
os.environ['CRYPTOPILOT_NO_UPDATE_CHECK'] = '1'

import credentials  # noqa: E402
import micro  # noqa: E402
import micro_book  # noqa: E402
import micro_economics as econ  # noqa: E402
import micro_strategy as strat  # noqa: E402
import observability  # noqa: E402
import storage  # noqa: E402
import strategy  # noqa: E402
import trader  # noqa: E402
import venue_rules  # noqa: E402
from research import micro_fixtures as fx  # noqa: E402

WARMUP = 5
P99_MIN_SAMPLES = 100


def pct(samples: list[float]) -> dict:
    ordered = sorted(samples)
    count = len(ordered)
    if not count:
        return {'count': 0}

    def q(fraction):
        return round(ordered[min(count - 1, max(0, int(round(fraction * (count - 1)))))], 3)
    result = {'count': count, 'p50_ms': q(.5), 'p95_ms': q(.95)}
    if count >= P99_MIN_SAMPLES:
        result['p99_ms'] = q(.99)
    result.update(max_ms=round(ordered[-1], 3), tail='p99' if count >= P99_MIN_SAMPLES else 'max',
                  mean_ms=round(statistics.fmean(ordered), 3))
    return result


def timed(fn, repeat: int) -> dict:
    for _ in range(WARMUP):
        fn()
    samples = []
    for _ in range(repeat):
        start = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - start) * 1000)
    return pct(samples)


def tail_rule(summary):
    if not isinstance(summary, dict) or summary.get('p50_ms') is None:
        return summary
    summary = dict(summary)
    if summary.get('window', summary.get('count', 0)) < P99_MIN_SAMPLES:
        summary.pop('p99_ms', None)
    summary['tail'] = 'p99' if 'p99_ms' in summary else 'max'
    return summary


# ------------------------------------------------------------------ CPU rows
def cpu_rows(repeat: int) -> dict:
    clock = fx.FixtureClock()
    series = fx.SeriesMarket('ETH/USDT', clock.minute() - 300, fx.swings(600, base=2500.0, range_bps=80, rise_bars=18,
                                                                         drop_bars=2, zigzag_bps=4),
                             market=fx.mexc_market('ETH/USDT', amount_step=0.000001))
    exchange = fx.FixtureExchange(clock, [series])
    raw = exchange.fetch_order_book('ETH/USDT', 20)
    now = time.time()

    def parse():
        return micro_book.from_ccxt_order_book(raw, exchange='mexc', symbol='ETH/USDT', received_wall=now,
                                               received_mono=time.monotonic(), max_levels=20)
    snapshot = parse()
    rules = venue_rules.normalize(exchange.markets['ETH/USDT'], exchange='mexc', symbol='ETH/USDT', loaded_at=now)
    cost = econ.cost_model(configured_fee_pct=0.1, configured_slippage_pct=0.2, latency_bps=3, rules=rules)

    def size():
        return econ.size_entry(rules=rules, book=snapshot, cost=cost, budget_cap=Decimal('25'),
                               cash_available=Decimal('10000'), allocation_remaining=Decimal('50'),
                               target_net=Decimal('0.05'), exit_stress_bps=Decimal('80'), now_wall=now)
    sizing = size()

    def estimate():
        return econ.estimate_exit(owned_base=sizing.sellable_base, basis_quote=sizing.max_debit, rules=rules,
                                  book=snapshot, cost=cost, target_net=Decimal('0.05'), uncertainty_bps=Decimal('5'))
    rows_1m = exchange.fetch_ohlcv('ETH/USDT', '1m', limit=120)
    rows_5m = exchange.fetch_ohlcv('ETH/USDT', '5m', limit=60)
    now_ms = int(clock.time() * 1000)

    def features():
        f1 = strategy.frame_from_ohlcv(rows_1m, timeframe='1m', now_ms=now_ms)
        f5 = strategy.frame_from_ohlcv(rows_5m, timeframe='5m', now_ms=now_ms)
        return strat.compute_features('ETH/USDT', f1, f5)
    feats, _ = features()
    params = strat.MicroParams(target_net_bps=20, max_loss_bps=80, max_hold_s=1800, uncertainty_floor_bps=5,
                               max_spread_bps=20, max_vol_bps=60, min_efficiency_trend=.35, max_efficiency_range=.25,
                               edge_ratio=.6)
    summary = {'best_bid': str(snapshot.best_bid), 'best_ask': str(snapshot.best_ask),
               'spread_bps': float(snapshot.spread_bps), 'generation': 1, 'sequence': 1, 'age_s': 0.1,
               'source': 'rest_depth'}
    gates = {'selected': True, 'capacity': True, 'cooldown_remaining_s': 0.0, 'fresh_after_exit': True,
             'review_block': False, 'risk_block': None, 'latched': None}
    sizing_dict = sizing.as_dict()

    def entry_decision():
        regime, reasons = strat.classify_regime(feats, spread_bps=float(snapshot.spread_bps), depth_ok=True,
                                                data_fresh=True, params=params)
        return strat.evaluate_entry(symbol='ETH/USDT', features=feats, regime=regime, regime_reasons=reasons,
                                    sizing=sizing_dict, book_summary=summary, params=params, gates=gates, now=now)
    estimate_dict = estimate().as_dict()

    def exit_decision():
        return strat.evaluate_exit(estimate=estimate_dict, depth_model='book', target_net=Decimal('0.05'),
                                   stop_net=Decimal('0.115'), peak_est_net=None, opened_wall=now - 60,
                                   max_hold_s=1800, regime='ranging', now=now)
    return {'book_parse_20_levels': timed(parse, repeat), 'size_entry_with_minimum_search': timed(size, repeat),
            'estimate_exit': timed(estimate, repeat), 'features_119x1m_59x5m': timed(features, repeat),
            'entry_decision_record': timed(entry_decision, repeat), 'exit_decision': timed(exit_decision, repeat)}


# ------------------------------------------------------------------ engine rows
class SlowExchange(fx.FixtureExchange):
    """The fixture with a fixed round trip on every public read."""

    def __init__(self, clock, markets, rtt: float):
        super().__init__(clock, markets)
        self.rtt = rtt

    def _wait(self):
        if self.rtt:
            time.sleep(self.rtt)

    def fetch_order_book(self, *args, **kwargs):
        self._wait()
        return super().fetch_order_book(*args, **kwargs)

    def fetch_ohlcv(self, *args, **kwargs):
        self._wait()
        return super().fetch_ohlcv(*args, **kwargs)

    def fetch_bids_asks(self, *args, **kwargs):
        self._wait()
        return super().fetch_bids_asks(*args, **kwargs)

    def load_markets(self, reload=False):
        return self.markets


def build_engine(symbols: list[str], rtt: float, *, mechanics: str | None = None):
    data = Path(tempfile.mkdtemp(prefix='cryptopilot-bench-micro-'))
    storage.DATA_DIR, storage.DB_PATH, storage.CONFIG_PATH = data, data / 'portfolio.sqlite3', data / 'settings.json'
    credentials.load = lambda _: None
    clock = fx.FixtureClock()
    fx.install_clock(clock, setattr)
    markets = []
    for index, symbol in enumerate(symbols):
        closes = (fx.swings(900, base=2500.0 + index, range_bps=80, rise_bars=18, drop_bars=2, zigzag_bps=4)
                  if symbol == mechanics else fx.flat(900, base=10.0 + index))
        markets.append(fx.SeriesMarket(symbol, clock.minute() - 300, closes,
                                       market=fx.mexc_market(symbol, amount_step=0.000001)))
    exchange = SlowExchange(clock, markets, rtt)
    trader.TradingEngine._new_exchange = staticmethod(lambda *a, **k: exchange)
    observability.METRICS = observability.Metrics()
    engine = trader.TradingEngine()
    cfg = dict(storage.settings())
    cfg.pop('paper_start_usdt', None)
    cfg.update(time_horizon='micro', watchlist=symbols, auto_discovery=False, streaming_quotes=False,
               max_orders_per_minute=60)
    engine.update_config(cfg)
    engine.data.clock, engine.data.monotonic = clock.time, clock.monotonic
    engine.running = True
    engine.stop_event.clear()
    return engine, exchange, clock


def tick_rows(pairs: int, rtt: float, repeat: int, *, holding: bool) -> dict:
    symbols = [f'C{i:02d}/USDT' for i in range(pairs)]
    if holding:
        symbols[0] = 'ETH/USDT'
    engine, exchange, clock = build_engine(symbols, rtt, mechanics='ETH/USDT' if holding else None)
    if holding:
        for _ in range(400):
            clock.advance(15)
            engine.micro.tick()
            if any(c['state'] == 'HOLDING' for c in micro.open_cycles(engine.exchange_name, 'paper')):
                break
        assert any(c['state'] == 'HOLDING' for c in micro.open_cycles(engine.exchange_name, 'paper')), 'no holding'
        # Hold still from here on so the timed ticks keep one open position.
        series = exchange.series['ETH/USDT']
        cycle = micro.open_cycles(engine.exchange_name, 'paper')[0]
        series.closes[:] = [float(cycle['entry_price'])] * len(series.closes)
        engine.micro.context.clear()
    slow, fast, slow_requests, fast_requests = [], [], [], []
    for index in range(WARMUP + repeat):
        clock.advance(60 - (clock.time() % 60) + 2)            # just after a bar closed: every context is due
        before = len(exchange.calls)
        start = time.perf_counter()
        engine.micro.tick()
        elapsed = (time.perf_counter() - start) * 1000
        if index >= WARMUP:
            slow.append(elapsed)
            slow_requests.append(len(exchange.calls) - before)
        clock.advance(20)                                       # between bars: books and exits only
        before = len(exchange.calls)
        start = time.perf_counter()
        engine.micro.tick()
        elapsed = (time.perf_counter() - start) * 1000
        if index >= WARMUP:
            fast.append(elapsed)
            fast_requests.append(len(exchange.calls) - before)
    if holding:
        assert any(c['state'] == 'HOLDING' for c in micro.open_cycles(engine.exchange_name, 'paper'))
    engine.running = False
    return {'pairs': pairs, 'holding': holding, 'rtt_ms': rtt * 1000,
            'after_bar_close': {**pct(slow), 'requests_per_tick': round(statistics.fmean(slow_requests), 2)},
            'between_bars': {**pct(fast), 'requests_per_tick': round(statistics.fmean(fast_requests), 2)}}


def cycle_rows(minutes: int) -> dict:
    engine, exchange, clock = build_engine(['ETH/USDT', 'BTC/USDT'], 0.0, mechanics='ETH/USDT')
    manager = engine.micro
    entries, exits = [], []
    real_enter, real_sell = manager._enter, manager._sell

    def enter(*args, **kwargs):
        start = time.perf_counter()
        result = real_enter(*args, **kwargs)
        if result:
            entries.append((time.perf_counter() - start) * 1000)
        return result

    def sell(*args, **kwargs):
        start = time.perf_counter()
        result = real_sell(*args, **kwargs)
        if result:
            exits.append((time.perf_counter() - start) * 1000)
        return result
    manager._enter, manager._sell = enter, sell
    for _ in range(int(minutes * 4)):
        clock.advance(15)
        manager.tick()
    closed = [c for c in storage.rows("SELECT state FROM micro_cycles") if c['state'] == 'CLOSED']
    latency = engine.metrics.snapshot()['latency']
    names = ('micro.book_fetch_ms', 'micro.book_to_decision_ms', 'micro.entry_decision_ms', 'micro.exit_decision_ms',
             'micro.reservation_ms', 'execution.submit_total_ms', 'micro.fill_to_commit_ms', 'micro.exit_submit_ms',
             'micro.context_ms', 'micro.tick_ms')
    return {'fixture': 'mechanics fixture (not a market result)', 'simulated_minutes': minutes,
            'closed_cycles': len(closed), 'entry_path_decision_to_commit': pct(entries),
            'exit_path_decision_to_commit': pct(exits),
            'engine_reservoirs': {name: tail_rule(latency.get(name)) for name in names}}


def main() -> int:
    global WARMUP
    parser = argparse.ArgumentParser()
    parser.add_argument('--json')
    parser.add_argument('--repeat', type=int, default=100)
    parser.add_argument('--tick-repeat', type=int, default=30)
    parser.add_argument('--warmup', type=int, default=WARMUP)
    args = parser.parse_args()
    WARMUP = max(0, args.warmup)
    revision = subprocess.run(['git', 'rev-parse', '--short', 'HEAD'], cwd=ROOT, capture_output=True, text=True,
                              check=False).stdout.strip() or 'unknown'
    results = {'revision': revision, 'python': platform.python_version(), 'platform': platform.platform(),
               'repeat': args.repeat, 'tick_repeat': args.tick_repeat, 'warmup': WARMUP,
               'cpu': cpu_rows(args.repeat)}
    results['tick'] = [tick_rows(pairs, rtt, args.tick_repeat, holding=holding)
                       for rtt in (0.0, 0.15) for pairs in (1, 4, 8) for holding in (False, True)]
    results['cycle'] = cycle_rows(240)
    text = json.dumps(results, indent=2, default=str)
    print(text)
    if args.json:
        Path(args.json).write_text(text + '\n', encoding='utf-8')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
