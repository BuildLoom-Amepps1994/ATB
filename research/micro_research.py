"""Frozen-protocol research harness for the PAPER micro-trading mode.

Run from the repository root (no network, no credentials):

    python -m research.micro_research --out research/results/micro_fixtures.json
    python -m research.micro_research --data DIR      # real public data, see DATA FORMAT

What it does. For each market series it replays completed 1m bars in order
and, at every bar close, asks the production code (``micro_strategy`` for
features, regime and the entry decision; ``micro_economics`` for sizing,
the book walks, exit estimates and fill accounting; ``micro_book`` to
validate every synthetic book) what the micro mode would do. The harness only
supplies time, books and fills; it contains no trading rule of its own, so a
research result and the paper engine cannot drift apart.

Execution model (fixed before any result was seen; see ``PROTOCOL``):

* A decision at the close of bar i executes against the book at the open of
  bar i+1 (the next bar's first price), never at a price known only later.
  Each book level is moved against the order by the latency margin.
* While a cycle is open, each later bar is evaluated three times, in a
  pessimistic order: at the bar's low (where only loss, time and regime exits
  may act), then at its high (where profit exits may act), then at its close.
  A bar that touched both a stop and a target is a stop. A profit exit that
  fires at the high is filled at the LOWEST price in the bar's range at which
  it fires (a ticking engine sells at the first such price on the way up),
  never at the high itself.
* Market orders only, taker fees on both legs (the larger of the configured
  rate and the public schedule), billed in the quote asset. There is no maker
  simulation, so nothing fills because a price was touched.
* One shared cash balance per series run; the wallet is never reset between
  cycles; a holding that cannot be sold at the end is valued at its last
  executable estimate and reported as open, never force-sold at a fictitious
  price. The verdict counts that open holding at its estimate (``net_marked``),
  so a loss still open at the end of a split is not left out.

Baselines: cash (no trade); passive buy-and-hold of the same budget over the
same split with the same costs; random entries (same number of entries as the
strategy in that split, same sizing, costs and exit policy, 200 seeded
repetitions); always-enter (enter whenever flat and sizing allows, same exits:
the pure cost of churning); and the unchanged ATB rapid strategy
(``strategy.backtest`` on the same 1m/5m bars, its own sizing).

Stresses: baseline costs, doubled (fee x2, latency x2, spread x2, depth / 2)
and severe (fee x3, latency 15 bps, spread x4, depth / 10). The fee multiple
applies to the fee actually modeled (the larger of the configured rate and the
venue's public taker rate), in exact decimals. They are replays of the same
bars, correlated with each other, not independent samples.

Budgets: 10, 15 and 25 USDT run through the production sizing component. 50
USDT is above the micro cap: the sizing component refuses it by design
(``BUDGET_INVALID``), so 50 appears only in the cost-algebra table. No
research setting can change ``MICRO_MAX_ALL_IN_USDT``.

Verdict rule (frozen): an edge is reported as DEMONSTRATED only if, on the
untouched holdout of real market data, the strategy's net result is positive,
exceeds the random-entry baseline mean by more than two of its standard
deviations, and stays positive under the doubled stress. Synthetic fixtures
can never demonstrate an edge; their verdict is always MECHANICS ONLY.

DATA FORMAT for ``--data``: one ``<BASE>_<QUOTE>_1m.json`` per pair holding
``{"provenance": {"venue": "mexc" | "kraken", ...}, "market": {...}, "ohlcv":
[[ms, o, h, l, c, v], ...]}`` from a public endpoint
(``research/collect_public_ohlcv.py`` writes this). The CCXT market may
instead sit beside it as ``<BASE>_<QUOTE>_market.json``; a pair with neither,
or with no supported venue, is refused, because the venue's rules and price
tick decide what can be traded. Without recorded depth the harness models the
book from the bar at the market's own tick; the book model, the decision
parameters and the code identity are written into every result. A split too
short to hold a full cycle is reported as not run, never indexed.
"""
from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import math
import random
import statistics
import subprocess
import sys
import time
from dataclasses import dataclass, replace
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import micro_book                      # noqa: E402
import micro_economics as econ         # noqa: E402
import micro_strategy as strat         # noqa: E402
import money                           # noqa: E402
import venue_rules                     # noqa: E402
from research import micro_fixtures as fx   # noqa: E402
from strategy import backtest as atb_backtest, frame_from_ohlcv   # noqa: E402

PROTOCOL = {
    'version': 'micro-research-v2',      # v1 filled profit exits at the bar's high
    'frozen_before_results': True,
    'splits': {'development': 0.6, 'validation': 0.2, 'holdout': 0.2},
    'embargo_bars': 120,
    'warmup_bars_1m': 120,
    'decision': 'every completed 1m bar; execution at the next bar open book',
    'exit_path': 'low (loss, time and regime exits), then high (profit exits), then close of each held bar',
    'profit_exit_fill': 'the lowest price in the bar range at which the profit exit fires, never the high',
    'stress_fee': 'the multiple applies to the modeled fee, max(configured, public taker), in decimals',
    'open_at_end': 'counted at its executable estimate in net_marked, which the verdict reads',
    'min_split_bars': 60,
    'buy_and_hold_exit_stress_bps': 80,
    'random_pick_multiplier': 4,
    'book_model': 'modeled from each bar at the market tick: spread_bps, level_quote and levels per series',
    'orders': 'market only, taker fee both legs, quote-billed',
    'settings': {'micro_budget_usdt': 25, 'micro_max_cycles': 1, 'micro_target_net_bps': 20, 'micro_max_loss_bps': 120,
                 'micro_max_hold_minutes': 30, 'micro_cooldown_seconds': 60, 'micro_latency_bps': 3,
                 'fee_pct': 0.1, 'slippage_pct': 0.2, 'max_spread_pct': 0.85},
    'scenarios': {'baseline': {'fee_mult': 1, 'latency_bps': 3, 'spread_mult': 1, 'depth_mult': 1},
                  'doubled': {'fee_mult': 2, 'latency_bps': 6, 'spread_mult': 2, 'depth_mult': 0.5},
                  'severe': {'fee_mult': 3, 'latency_bps': 15, 'spread_mult': 4, 'depth_mult': 0.1}},
    'budgets_sized': [10, 15, 25],
    'budgets_algebra_only': [50],
    'random_repetitions': 200,
    'random_baseline_budget': 25,
    'verdict_rule': 'DEMONSTRATED only on real holdout data: net > 0, net > random mean + 2 sd, doubled-stress net > 0. '
                    'Synthetic fixtures: MECHANICS ONLY.',
}


def protocol_hash() -> str:
    """The protocol, the two version labels and every decision parameter the run uses (micro.py constants too)."""
    payload = json.dumps({'protocol': PROTOCOL, 'features': strat.FEATURE_VERSION, 'decision': strat.DECISION_VERSION,
                          'params': dataclasses.asdict(params())}, sort_keys=True, default=str).encode()
    return hashlib.sha256(payload).hexdigest()


CODE_FILES = ('micro.py', 'micro_strategy.py', 'micro_economics.py', 'micro_book.py', 'venue_rules.py', 'money.py',
              'strategy.py', 'research/micro_research.py', 'research/micro_fixtures.py')


def code_identity() -> dict:
    """The code a result came from: a SHA-256 of the decision and research files, and the commit when git can say."""
    digest = hashlib.sha256()
    for name in CODE_FILES:
        digest.update(name.encode() + b'\0' + (ROOT / name).read_bytes() + b'\0')
    identity = {'files': list(CODE_FILES), 'files_sha256': digest.hexdigest(), 'commit': None, 'dirty': None}
    try:
        head = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=ROOT, capture_output=True, text=True, timeout=10)
        status = subprocess.run(['git', 'status', '--porcelain', '--untracked-files=no', '--', *CODE_FILES], cwd=ROOT,
                                capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return identity
    if head.returncode == 0 and status.returncode == 0 and head.stdout.strip():
        identity['commit'], identity['dirty'] = head.stdout.strip(), bool(status.stdout.strip())
    return identity


@dataclass(frozen=True)
class Scenario:
    name: str
    fee_mult: float
    latency_bps: float
    spread_mult: float
    depth_mult: float


SCENARIOS = {name: Scenario(name, **values) for name, values in PROTOCOL['scenarios'].items()}


def params() -> strat.MicroParams:
    cfg = PROTOCOL['settings']
    import micro
    return strat.MicroParams(target_net_bps=float(cfg['micro_target_net_bps']), max_loss_bps=float(cfg['micro_max_loss_bps']),
                             max_hold_s=float(cfg['micro_max_hold_minutes']) * 60.0,
                             uncertainty_floor_bps=micro.UNCERTAINTY_FLOOR_BPS,
                             max_spread_bps=min(float(cfg['max_spread_pct']) * 100.0, micro.MAX_SPREAD_BPS),
                             max_vol_bps=micro.MAX_VOL_BPS, min_efficiency_trend=micro.MIN_EFFICIENCY_TREND,
                             max_efficiency_range=micro.MAX_EFFICIENCY_RANGE, edge_ratio=micro.EDGE_RATIO)


# ------------------------------------------------------------- feature cache
def bars_1m(series: fx.SeriesMarket) -> list[list[float]]:
    return [series.bar(series.origin_minute + i) for i in range(len(series.closes))]


def bars_5m(rows_1m: list[list[float]]) -> list[list[float]]:
    out, bucket = [], []
    for row in rows_1m:
        start = int(row[0] // 300000) * 300000
        if bucket and int(bucket[0][0] // 300000) * 300000 != start:
            out.append(_aggregate(bucket))
            bucket = []
        bucket.append(row)
    if bucket and len(bucket) == 5:
        out.append(_aggregate(bucket))
    return out


def _aggregate(rows):
    start = int(rows[0][0] // 300000) * 300000
    return [start, rows[0][1], max(r[2] for r in rows), min(r[3] for r in rows), rows[-1][4], sum(r[5] for r in rows)]


def features_at(symbol, rows_1m, rows_5m, i):
    """Features exactly as the engine computes them at the close of bar i (completed bars only)."""
    end_ms = int(rows_1m[i][0]) + 60000
    window_1m = rows_1m[max(0, i - 118):i + 1]
    window_5m = [r for r in rows_5m if r[0] + 300000 <= end_ms][-59:]
    frame_1m = frame_from_ohlcv(window_1m, timeframe='1m', now_ms=end_ms)
    frame_5m = frame_from_ohlcv(window_5m, timeframe='5m', now_ms=end_ms)
    return strat.compute_features(symbol, frame_1m, frame_5m)


def feature_cache(series: fx.SeriesMarket, rows_1m, rows_5m) -> list:
    cache = []
    for i in range(len(rows_1m)):
        if i < PROTOCOL['warmup_bars_1m'] - 1:
            cache.append((None, 'WARMUP'))
            continue
        try:
            cache.append(features_at(series.symbol, rows_1m, rows_5m, i))
        except Exception as exc:          # a malformed window is a missing observation, never a guess
            cache.append((None, f'ERROR: {type(exc).__name__}'))
    return cache


# ------------------------------------------------------------------ books
def book(series: fx.SeriesMarket, price: float, ms: int, scenario: Scenario, seq: int):
    raw = series.book_at(price, now_ms=ms, sequence=seq, depth_scale=scenario.depth_mult,
                         spread_scale=scenario.spread_mult)
    return micro_book.from_ccxt_order_book(raw, exchange=series.venue, symbol=series.symbol, received_wall=ms / 1000,
                                           received_mono=ms / 1000, source='fixture')


def rules_for(series: fx.SeriesMarket, loaded_at: float) -> venue_rules.VenueRules:
    """The series' venue rules, normalized for its own venue (both CCXT clients use TICK_SIZE precision)."""
    market = series.market or fx.mexc_market(series.symbol)
    return venue_rules.normalize(market, exchange=series.venue, symbol=series.symbol, loaded_at=loaded_at,
                                 source='research catalogue')


def scenario_cost(rules: venue_rules.VenueRules, scenario: Scenario) -> econ.CostModel:
    """The baseline cost model with the scenario's fee multiple applied to the fee actually modeled."""
    cfg = PROTOCOL['settings']
    base = econ.cost_model(configured_fee_pct=cfg['fee_pct'], configured_slippage_pct=cfg['slippage_pct'],
                           latency_bps=scenario.latency_bps, rules=rules)
    if scenario.fee_mult == 1:
        return base
    return replace(base, taker_fee=money.mul(base.taker_fee, money.parse(scenario.fee_mult)),
                   fee_source=f'{base.fee_source} x{scenario.fee_mult} ({scenario.name} stress)')


# -------------------------------------------------------------- simulation
@dataclass
class Cycle:
    symbol: str
    entry_bar: int
    entry_debit: Decimal
    owned: Decimal
    basis: Decimal
    target_net: Decimal
    stop_net: Decimal
    candidate: str | None
    peak: Decimal | None = None
    exit_bar: int | None = None
    trigger: str | None = None
    realized: Decimal | None = None
    fees: Decimal = money.ZERO
    gross_in: Decimal = money.ZERO
    gross_out: Decimal = money.ZERO
    net_out: Decimal = money.ZERO
    open_value: Decimal | None = None
    carried_base: Decimal = money.ZERO      # an unsellable remainder of the previous cycle, carried as the engine does
    residual_base: Decimal = money.ZERO
    residual_basis: Decimal = money.ZERO


def run(series: fx.SeriesMarket, cache: list, rows_1m: list, *, start: int, end: int, scenario: Scenario,
        budget: Decimal, policy: str = 'strategy', entry_bars: set | None = None, max_entries: int | None = None,
        cash: Decimal = Decimal('1000')) -> dict:
    """Replay bars [start, end) and return the cycles and the cash path. Pure apart from ``rng``."""
    p = params()
    cfg = PROTOCOL['settings']
    loaded = rows_1m[start][0] / 1000
    rules = rules_for(series, loaded)
    cost = scenario_cost(rules, scenario)
    target_net = money.mul(budget, money.bps(money.parse(cfg['micro_target_net_bps'])))
    cycles: list[Cycle] = []
    open_cycle: Cycle | None = None
    last_exit_bar, loss_streak, seq = -10**9, 0, 0
    start_cash = cash
    carry_base, carry_basis = money.ZERO, money.ZERO     # a remainder below the venue step stays owned at its cost
    equity_path = []
    for i in range(start, end - 1):
        seq += 1
        close_ms = int(rows_1m[i][0]) + 60000
        nxt = rows_1m[i + 1]
        features, reason = cache[i]
        if open_cycle is not None:
            # Pessimistic intra-bar order on the bar that just completed (bar i): low, high, close.
            prior = cache[i - 1][0] if i > 0 else None

            def assess(price, peak):
                snapshot = book(series, price, close_ms, scenario, seq)
                estimate = econ.estimate_exit(owned_base=open_cycle.owned, basis_quote=open_cycle.basis, rules=rules,
                                              book=snapshot, cost=cost, target_net=open_cycle.target_net,
                                              uncertainty_bps=money.parse(max(p.uncertainty_floor_bps, scenario.latency_bps)))
                regime = None
                if prior is not None:
                    regime, _ = strat.classify_regime(prior, spread_bps=float(snapshot.spread_bps), depth_ok=True,
                                                      data_fresh=True, params=p)
                verdict = strat.evaluate_exit(estimate=estimate.as_dict(), depth_model=estimate.depth_model,
                                              target_net=open_cycle.target_net, stop_net=open_cycle.stop_net,
                                              peak_est_net=peak, opened_wall=open_cycle.entry_bar * 60.0,
                                              max_hold_s=p.max_hold_s, regime=regime, now=(i + 1) * 60.0)
                return verdict, snapshot

            for label, price in (('low', rows_1m[i][3]), ('high', rows_1m[i][2]), ('close', rows_1m[i][4])):
                peak_before = open_cycle.peak
                verdict, snapshot = assess(price, peak_before)
                if verdict.new_peak is not None and (open_cycle.peak is None or verdict.new_peak > open_cycle.peak):
                    open_cycle.peak = verdict.new_peak
                profit_trigger = verdict.trigger in PROFIT_TRIGGERS
                if label == 'high' and profit_trigger:
                    # The engine sells at the first price on the way up where a profit exit fires; the high is only
                    # an upper bound, so fill at the lowest such price in the bar's range.
                    def fires(at, peak=peak_before):
                        found = assess(at, peak)
                        return found if found[0].trigger in PROFIT_TRIGGERS else None
                    verdict, snapshot = first_profit_exit(fires, rows_1m[i][3], price)
                if verdict.action == 'exit' and (label != 'low' or not profit_trigger):
                    _sell(open_cycle, snapshot, rules, cost, verdict.trigger, i)
                    cash = money.add(cash, open_cycle.net_out)
                    carry_base, carry_basis = open_cycle.residual_base, open_cycle.residual_basis
                    cycles.append(open_cycle)
                    loss_streak = loss_streak + 1 if open_cycle.realized < 0 else 0
                    last_exit_bar = i
                    open_cycle = None
                    break
        equity_path.append(cash)
        if open_cycle is not None or features is None:
            continue
        cooldown_bars = math.ceil(cfg['micro_cooldown_seconds'] * 2 ** min(loss_streak, 4) / 60.0)
        if i <= last_exit_bar + cooldown_bars:
            continue
        if max_entries is not None and len(cycles) >= max_entries:
            continue
        if policy == 'random' and i not in (entry_bars or set()):
            continue
        snapshot = book(series, nxt[1], close_ms, scenario, seq)
        if policy == 'strategy':
            regime, reasons = strat.classify_regime(features, spread_bps=float(snapshot.spread_bps),
                                                    depth_ok=snapshot.depth_known, data_fresh=True, params=p)
            if regime not in strat.CANDIDATES:
                continue          # no candidate in this regime: evaluate_entry abstains whatever the sizing says
        sizing = econ.size_entry(rules=rules, book=snapshot, cost=cost, budget_cap=budget, cash_available=cash,
                                 allocation_remaining=budget, target_net=target_net,
                                 exit_stress_bps=money.parse(p.max_loss_bps), now_wall=close_ms / 1000)
        if policy == 'strategy':
            summary = {'best_bid': money.text(snapshot.best_bid), 'best_ask': money.text(snapshot.best_ask),
                       'spread_bps': float(snapshot.spread_bps), 'generation': seq, 'sequence': seq, 'age_s': 0.0,
                       'source': 'fixture'}
            gates = {'selected': True, 'capacity': True, 'cooldown_remaining_s': 0.0, 'fresh_after_exit': i > last_exit_bar,
                     'review_block': False, 'risk_block': None, 'latched': None}
            decision = strat.evaluate_entry(symbol=series.symbol, features=features, regime=regime, regime_reasons=reasons,
                                            sizing=sizing.as_dict(), book_summary=summary, params=p, gates=gates,
                                            now=close_ms / 1000)
            enter = decision.action == 'enter'
            candidate = decision.candidate
            stop_bps = (decision.proposal or {}).get('stop_net_bps')
        else:   # 'random' or 'always': the strategy's stop rule (entry cost plus the ATR move), else the loss cap
            enter = sizing.eligible
            carried = strat.entry_cost_bps(budget_cap=budget, max_debit=sizing.max_debit,
                                           break_even_bid=sizing.break_even_bid, best_bid=snapshot.best_bid)
            candidate = policy
            stop_bps = (strat.stop_net_bps(features, p, carried) if carried is not None else None) or p.max_loss_bps
        if not enter or not sizing.eligible:
            continue
        stop_net = money.mul(budget, money.bps(money.parse(float(stop_bps))))
        open_cycle = _buy(series.symbol, sizing, snapshot, cost, i + 1, target_net, stop_net, candidate,
                          carry_base=carry_base, carry_basis=carry_basis)
        carry_base, carry_basis = money.ZERO, money.ZERO
        cash = money.sub(cash, open_cycle.entry_debit)
    if open_cycle is not None:
        last = rows_1m[end - 1]
        snapshot = book(series, last[4], int(last[0]) + 60000, scenario, seq + 1)
        estimate = econ.estimate_exit(owned_base=open_cycle.owned, basis_quote=open_cycle.basis, rules=rules,
                                      book=snapshot, cost=cost, target_net=open_cycle.target_net,
                                      uncertainty_bps=money.parse(p.uncertainty_floor_bps))
        open_cycle.open_value = estimate.net_proceeds
    return {'cycles': cycles, 'open': open_cycle, 'cash': cash, 'equity_path': equity_path,
            'cash_change': money.sub(cash, start_cash),
            'residual_at_end': None if open_cycle is not None else {'base': carry_base, 'basis': carry_basis}}


PROFIT_TRIGGERS = ('take_profit', 'profit_protection')


def first_profit_exit(fires, low: float, high: float, *, step_bps: float = 2.0, refine: int = 12):
    """The (verdict, book) at the lowest price in [low, high] where ``fires(price)`` returns one.

    The protection band can be a few basis points wide, so the range is walked
    in steps of at most ``step_bps`` before the last step is halved ``refine``
    times; the price found is never above where the exit first fires by more
    than the last half-step. ``fires(high)`` must return a result.
    """
    found = fires(low)
    if found is not None:
        return found
    steps = max(1, min(4000, math.ceil((high - low) / low * 1e4 / step_bps)))
    below = low
    for k in range(1, steps + 1):
        price = high if k == steps else low + (high - low) * k / steps
        found = fires(price)
        if found is not None:
            above = price
            for _ in range(refine):
                middle = (below + above) / 2
                candidate = fires(middle)
                if candidate is not None:
                    above, found = middle, candidate
                else:
                    below = middle
            return found
        below = price
    raise ValueError('first_profit_exit: no profit exit fires at the high')


def _buy(symbol, sizing, snapshot, cost, bar, target_net, stop_net, candidate, *, carry_base=money.ZERO,
         carry_basis=money.ZERO) -> Cycle:
    walk = econ.walk_asks(snapshot, quote_amount=sizing.gross_quote, latency_bps=cost.latency_bps)
    gross = walk.quote
    fee = money.q_up(money.mul(gross, cost.taker_fee), Decimal('0.00000001'))
    acquisition = econ.account_entry(filled_base=walk.base, gross_quote=gross, quote_fee=fee, base_fee=money.ZERO,
                                     other_fees={}, other_fee_value=None, fee_status='simulated',
                                     reserved=sizing.max_debit)
    return Cycle(symbol=symbol, entry_bar=bar, entry_debit=acquisition.debit,
                 owned=money.add(acquisition.owned_base, carry_base), basis=money.add(acquisition.basis, carry_basis),
                 target_net=target_net, stop_net=stop_net, candidate=candidate, fees=fee, gross_in=gross,
                 carried_base=carry_base)


def _sell(cycle: Cycle, snapshot, rules, cost, trigger, bar) -> None:
    step = rules.amount_step or Decimal('0.00000001')
    sellable = money.q_down(cycle.owned, step)
    walk = econ.walk_bids(snapshot, base_amount=sellable, latency_bps=cost.latency_bps)
    fee = money.q_up(money.mul(walk.quote, cost.taker_fee), Decimal('0.00000001'))
    realized = econ.realize_exit(owned_base=cycle.owned, basis_quote=cycle.basis, sold_base=walk.base,
                                 gross_quote=walk.quote, quote_fee=fee, base_fee=money.ZERO, other_fees={},
                                 other_fee_value=None, fee_status='simulated')
    cycle.exit_bar, cycle.trigger, cycle.realized = bar, trigger, realized.realized_net
    cycle.fees = money.add(cycle.fees, fee)
    cycle.gross_out, cycle.net_out = walk.quote, realized.net_proceeds
    cycle.residual_base, cycle.residual_basis = realized.residual_base, realized.residual_basis


# ---------------------------------------------------------------- metrics
def summarize(result: dict, budget: Decimal, bars: int) -> dict:
    cycles = result['cycles']
    nets = [float(c.realized) for c in cycles]
    fees = sum(float(c.fees) for c in cycles)
    gross_gains = sum(n for n in nets if n > 0)
    equity, peak, drawdown = 0.0, 0.0, 0.0
    for n in nets:
        equity += n
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    held = sum((c.exit_bar - c.entry_bar + 1) for c in cycles)
    open_cycle = result['open']
    triggers = {}
    for c in cycles:
        triggers[c.trigger] = triggers.get(c.trigger, 0) + 1
    # Before fees: each closed cycle's net result plus the fees it paid (the price move on what it sold).
    gross_each = [float(c.realized) + float(c.fees) for c in cycles]
    gross_pnl = sum(gross_each)
    gross_wins = sum(g for g in gross_each if g > 0)
    losses = [n for n in nets if n < 0]
    # An open holding counts at its executable estimate; one that cannot be valued counts as lost.
    open_mark = 0.0 if open_cycle is None else (
        float(open_cycle.open_value) - float(open_cycle.basis) if open_cycle.open_value is not None
        else -float(open_cycle.basis))
    return {
        'closed_cycles': len(cycles), 'net': round(sum(nets), 6), 'net_marked': round(sum(nets) + open_mark, 6),
        'open_mark': round(open_mark, 6), 'gross_pnl_before_fees': round(gross_pnl, 6),
        'fees': round(fees, 6), 'net_pct_of_budget': round(sum(nets) / float(budget) * 100, 4) if nets else 0.0,
        'average_net': round(statistics.mean(nets), 6) if nets else None,
        'sd_net': round(statistics.stdev(nets), 6) if len(nets) > 1 else None,
        'wins': sum(1 for n in nets if n > 0), 'losses': len(losses),
        'worst': round(min(nets), 6) if nets else None, 'best': round(max(nets), 6) if nets else None,
        'max_drawdown': round(drawdown, 6),
        'profit_factor': round(gross_gains / -sum(losses), 4) if losses and gross_gains > 0 else None,
        'fees_share_of_gross_gains': round(fees / gross_wins, 4) if gross_wins > 0 else None,
        'turnover': round(sum(float(c.gross_in) + float(c.gross_out) for c in cycles), 4),
        'exposure_pct_of_time': round(held / bars * 100, 2) if bars else 0.0,
        'average_hold_min': round(held / len(cycles), 2) if cycles else None,
        'triggers': triggers, 'stop_outs': triggers.get('stop_loss', 0),
        'near_break_even_exits': sum(1 for n in nets if abs(n) <= float(budget) * 0.0005),
        'cash_change': round(float(result['cash_change']), 6),
        'residual_at_end': None if result.get('residual_at_end') is None else {
            'base': float(result['residual_at_end']['base']), 'basis': float(result['residual_at_end']['basis']),
            'note': 'below the quantity step: still owned, carried at cost into the next cycle'},
        'open_at_end': None if open_cycle is None else {
            'basis': float(open_cycle.basis),
            'estimated_net_proceeds': None if open_cycle.open_value is None else float(open_cycle.open_value)},
    }


def split_bounds(n: int) -> dict:
    """Each split's [start, end), or None when it is shorter than ``min_split_bars`` after its embargo."""
    warm = PROTOCOL['warmup_bars_1m']
    usable = max(0, n - warm)
    dev_end = warm + int(usable * PROTOCOL['splits']['development'])
    val_end = dev_end + int(usable * PROTOCOL['splits']['validation'])
    embargo = PROTOCOL['embargo_bars']
    bounds = {'development': (warm, dev_end), 'validation': (dev_end + embargo, val_end),
              'holdout': (val_end + embargo, n)}
    return {name: (start, end) if end - start >= PROTOCOL['min_split_bars'] else None
            for name, (start, end) in bounds.items()}


def buy_and_hold(series, rows_1m, start, end, scenario, budget) -> float:
    rules = rules_for(series, rows_1m[start][0] / 1000)
    cost = scenario_cost(rules, scenario)
    first = book(series, rows_1m[start][1], int(rows_1m[start][0]), scenario, 1)
    sizing = econ.size_entry(rules=rules, book=first, cost=cost, budget_cap=budget, cash_available=budget,
                             allocation_remaining=budget, target_net=money.ZERO,
                             exit_stress_bps=money.parse(PROTOCOL['buy_and_hold_exit_stress_bps']),
                             now_wall=rows_1m[start][0] / 1000)
    if not sizing.eligible:
        return float('nan')
    cycle = _buy(series.symbol, sizing, first, cost, start, money.ZERO, money.ZERO, 'hold')
    last = book(series, rows_1m[end - 1][4], int(rows_1m[end - 1][0]) + 60000, scenario, 2)
    _sell(cycle, last, rules, cost, 'end', end - 1)
    return float(cycle.realized)


def atb_rapid(rows_1m, rows_5m, start, end) -> dict:
    """The unchanged ATB rapid strategy on the same bars (its own sizing; percent of its equity)."""
    from storage import DEFAULTS
    settings = dict(DEFAULTS)
    settings.update(time_horizon='rapid')
    try:
        window = rows_1m[max(0, start - 200):end]
        higher = [r for r in rows_5m if window[0][0] - 300000 * 70 <= r[0] <= window[-1][0]]
        result = atb_backtest(window, settings, timeframe='1m', higher_rows=higher, higher_timeframe='5m',
                              now_ms=int(window[-1][0]) + 60000, start_ms=int(rows_1m[start][0]))
        return {'return_pct': result.get('return_pct'), 'closed_trades': result.get('closed_trades'),
                'max_drawdown_pct': result.get('max_drawdown_pct'),
                'note': 'percent of a 10,000 USDT simulated equity with its own risk sizing; not comparable in USDT'}
    except Exception as exc:
        return {'unavailable': f'{type(exc).__name__}: {exc}'[:200]}


def evaluate_series(series: fx.SeriesMarket, *, synthetic: bool, budgets=None) -> dict:
    rows_1m = bars_1m(series)
    rows_5m = bars_5m(rows_1m)
    started = time.perf_counter()
    cache = feature_cache(series, rows_1m, rows_5m)
    feature_seconds = time.perf_counter() - started
    bounds = split_bounds(len(rows_1m))
    budgets = budgets or [Decimal(str(b)) for b in PROTOCOL['budgets_sized']]
    report = {'symbol': series.symbol, 'venue': series.venue, 'bars': len(rows_1m),
              'splits': {k: list(v) if v else f'not run: fewer than {PROTOCOL["min_split_bars"]} bars after the embargo'
                         for k, v in bounds.items()},
              'book_model': {'spread_bps': series.spread_bps, 'level_quote': series.level_quote, 'levels': series.levels,
                             'tick': series.tick, 'from': 'each bar (no recorded depth)'},
              'feature_seconds': round(feature_seconds, 2), 'by_budget': {}}
    for budget in budgets:
        per_budget = {}
        for split, span in bounds.items():
            if span is None:
                continue
            start, end = span
            bars = end - start
            per_scenario, results = {}, {}
            for name, scenario in SCENARIOS.items():
                results[name] = run(series, cache, rows_1m, start=start, end=end, scenario=scenario, budget=budget)
                per_scenario[name] = summarize(results[name], budget, bars)
            base = SCENARIOS['baseline']
            strategy_result = results['baseline']
            entries = len(strategy_result['cycles'])
            random_nets = []
            if entries and budget == Decimal('25'):     # the random baseline is run at the micro budget only (cost)
                # Same number of closed entries as the strategy, at random bars, same sizing, costs and exits.
                rng = random.Random(f'{series.symbol}|{split}|{budget}')
                candidates = list(range(start, end - 2))
                for _ in range(PROTOCOL['random_repetitions']):
                    picks = set(rng.sample(candidates, min(len(candidates), entries * PROTOCOL['random_pick_multiplier'])))
                    random_nets.append(summarize(run(series, cache, rows_1m, start=start, end=end, scenario=base,
                                                     budget=budget, policy='random', entry_bars=picks,
                                                     max_entries=entries), budget, bars)['net_marked'])
            always = summarize(run(series, cache, rows_1m, start=start, end=end, scenario=base, budget=budget,
                                   policy='always'), budget, bars)
            per_budget[split] = {
                'strategy': per_scenario,
                'baselines': {
                    'cash': 0.0,
                    'buy_and_hold': round(buy_and_hold(series, rows_1m, start, end, base, budget), 6),
                    'random_entries': {'repetitions': len(random_nets), 'measure': 'net_marked',
                                       'mean': round(statistics.mean(random_nets), 6) if random_nets else None,
                                       'sd': round(statistics.stdev(random_nets), 6) if len(random_nets) > 1 else None,
                                       'strategy_percentile': (round(sum(1 for n in random_nets
                                                                       if n < per_scenario['baseline']['net_marked'])
                                                                   / len(random_nets) * 100, 1) if random_nets else None)},
                    'always_enter': always,
                    'atb_rapid_unchanged': atb_rapid(rows_1m, rows_5m, start, end) if budget == budgets[-1] else 'see 25 USDT row',
                },
            }
        report['by_budget'][str(budget)] = per_budget
    report['verdict'] = verdict(report, synthetic=synthetic)
    return report


def verdict(report: dict, *, synthetic: bool) -> str:
    if synthetic:
        return 'MECHANICS ONLY (synthetic fixture: no market edge can be shown)'
    holdout = report['by_budget'].get('25', {}).get('holdout')
    if not holdout:
        return 'NO DEMONSTRATED EDGE (no holdout)'
    net = holdout['strategy']['baseline']['net_marked']           # closed cycles plus a holding still open
    rnd = holdout['baselines']['random_entries']
    doubled = holdout['strategy']['doubled']['net_marked']
    if (holdout['strategy']['baseline']['closed_cycles'] and net > 0 and rnd['mean'] is not None and rnd['sd']
            and net > rnd['mean'] + 2 * rnd['sd'] and doubled > 0):
        return 'EDGE DEMONSTRATED ON THIS HOLDOUT (prospective paper evidence still required)'
    return 'NO DEMONSTRATED EDGE'


def algebra_table() -> list[dict]:
    rows = econ.sensitivity([Decimal('10'), Decimal('15'), Decimal('25'), Decimal('50')],
                            [Decimal('0.0005'), Decimal('0.001'), Decimal('0.002')],
                            [Decimal('0'), Decimal('0.0003'), Decimal('0.002')], [Decimal('20')])
    return [{k: (money.text(v) if isinstance(v, Decimal) else v) for k, v in row.items()} for row in rows]


def fixture_suite(minutes: int = 2880) -> list[tuple[fx.SeriesMarket, str]]:
    origin = 29_000_000          # a fixed epoch minute (2025-02-19): replays do not depend on today's date

    def market(symbol):
        # Fine quantity steps keep rounding out of the fixtures (docs/ATB_MICRO_ECONOMICS.md shows its cost), and
        # a sub-dollar pair gets a sub-cent tick: a 0.01 tick on a 0.50 price would round every exit bid by 200 bps.
        sub_dollar = symbol.startswith(('XRP', 'ADA'))
        return fx.mexc_market(symbol, amount_step=0.000001, price_tick=0.0001 if sub_dollar else 0.01)
    return [
        (fx.SeriesMarket('ETH/USDT', origin, fx.swings(minutes, base=2500.0, range_bps=80, rise_bars=18, drop_bars=2,
                                                        zigzag_bps=4), market=market('ETH/USDT')),
         'MECHANICS FIXTURE: a saw-tooth range built so the range candidate fires and the rise reaches the target'),
        (fx.SeriesMarket('ADA/USDT', origin, fx.oscillating(minutes, base=0.5, amplitude_bps=70, period=24,
                                                            noise_bps=3, seed=11), market=market('ADA/USDT'),
                         tick=0.0001),
         'smooth sine range with noise'),
        (fx.SeriesMarket('BTC/USDT', origin, fx.random_walk(minutes, base=60000.0, vol_bps=8, seed=21),
                         market=market('BTC/USDT')),
         'random walk, 8 bps per minute (no edge by construction)'),
        (fx.SeriesMarket('SOL/USDT', origin, fx.trend(minutes, base=150.0, bps_per_minute=-0.8, noise_bps=6, seed=31),
                         market=market('SOL/USDT')),
         'steady decline with noise'),
        (fx.SeriesMarket('XRP/USDT', origin, fx.flat(minutes, base=0.6), market=market('XRP/USDT'), tick=0.0001),
         'flat price: costs only'),
    ]


def load_real(directory: Path) -> list:
    """Series from collect_public_ohlcv files. Contents are validated, not trusted by their hash field."""
    suite = []
    for path in sorted(directory.glob('*_1m.json')):
        payload = json.loads(path.read_text(encoding='utf-8'))
        rows = payload['ohlcv']
        frame_from_ohlcv(rows, drop_open_candle=False)          # schema, finiteness and OHLC consistency
        digest = hashlib.sha256(json.dumps(rows).encode()).hexdigest()
        provenance = dict(payload.get('provenance') or {})
        provenance['rows_sha256_verified'] = digest == provenance.get('rows_sha256')
        market = payload.get('market')
        sidecar = path.with_name(path.name[:-len('_1m.json')] + '_market.json')
        if not market and sidecar.exists():
            market = json.loads(sidecar.read_text(encoding='utf-8'))
        if not isinstance(market, dict) or not market:
            raise SystemExit(f'{path.name}: no market metadata (a "market" entry or {sidecar.name}); '
                             'the venue rules and price tick decide what can trade')
        venue = str(provenance.get('venue') or '').lower()
        if venue not in ('mexc', 'kraken'):
            raise SystemExit(f'{path.name}: the provenance names no supported venue (mexc or kraken)')
        try:
            tick = float((market.get('precision') or {})['price'])
        except (KeyError, TypeError, ValueError):
            tick = float('nan')
        if not math.isfinite(tick) or tick <= 0:
            raise SystemExit(f'{path.name}: the market has no usable price tick')
        symbol = str(market.get('symbol') or path.stem.replace('_1m', '').replace('_', '/'))
        series = fx.SeriesMarket.from_rows(symbol, rows, market=market, tick=tick, venue=venue)
        suite.append((series, f'real public data ({provenance.get("venue")}), book modeled from bars', provenance))
    if not suite:
        raise SystemExit(f'No *_1m.json files in {directory}')
    return suite


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description='Micro-mode research harness (offline)')
    parser.add_argument('--out', default=str(ROOT / 'research' / 'results' / 'micro_fixtures.json'))
    parser.add_argument('--minutes', type=int, default=2880)
    parser.add_argument('--data', default=None, help='directory of real public OHLCV files (see DATA FORMAT)')
    args = parser.parse_args(argv)
    started = time.perf_counter()
    output = {'protocol': PROTOCOL, 'protocol_sha256': protocol_hash(), 'feature_version': strat.FEATURE_VERSION,
              'decision_version': strat.DECISION_VERSION, 'params': dataclasses.asdict(params()),
              'code': code_identity(), 'series': [], 'cost_algebra': algebra_table()}
    if args.data:
        suite = load_real(Path(args.data))
        output['real_data'] = [item[2] for item in suite]
        synthetic = False
    else:
        suite = [(series, label, None) for series, label in fixture_suite(args.minutes)]
        output['real_data'] = 'NOT RUN: exchange endpoints are unreachable from the build environment'
        synthetic = True
    for series, label, _ in suite:
        report = evaluate_series(series, synthetic=synthetic)
        report['fixture'] = label
        output['series'].append(report)
        holdout = report['by_budget']['25'].get('holdout')
        print(f"{series.symbol:10s} {label[:48]:48s} cycles(25, holdout, baseline)="
              f"{holdout['strategy']['baseline']['closed_cycles'] if holdout else 'not run'}", flush=True)
    output['runtime_seconds'] = round(time.perf_counter() - started, 1)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(output, indent=1, default=str) + '\n', encoding='utf-8')
    print(f'wrote {out}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
