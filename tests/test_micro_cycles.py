"""The micro cycle state machine through the real engine (brief items 12-22, 24 and 25).

Markets are deterministic fixtures from ``research/micro_fixtures.py``. The
**mechanics fixture** (``swings``) is a range shaped so that the real
range-reversion candidate fires after each quick drop and the following rise
reaches the net target. It proves that the buy, hold, sell and rebuy path
works through the real strategy, risk gate, order journal, paper venue and
ledger. It is not a profitable market backtest and says nothing about any
real market; ``docs/ATB_MICRO_RESEARCH.md`` holds the research results.

One ``FixtureClock`` drives ``time.time``, ``time.monotonic`` and the
``datetime.now`` of the engine modules (``install_clock``), so cooldowns, the
order-rate limit, book ages and the authorization lifetime all measure
fixture time while a simulated minute takes a few real milliseconds.

Nothing here stubs the strategy or the risk gate. Where a test needs a fault
(a crash, a dropped response, a late fill) it injects it at the venue or
raises at a named point, and the rest of the path is the production code.
"""
import json
import sqlite3
import sys
import threading
import time
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import credentials
import micro
import observability
import storage
import trader
from research.micro_fixtures import (FixtureExchange, FixtureClock, SeriesMarket, flat, install_clock, mexc_market,
                                     random_walk, swings, trend)

STEP_S = 15.0
REVIEW = micro.REVIEW_PHRASE


class Crash(BaseException):
    """Stands for the process dying at a chosen point: no ``except Exception`` handler catches it."""


def mechanics(minutes: int = 900, base: float = 2500.0) -> list[float]:
    return swings(minutes, base=base, range_bps=80, rise_bars=18, drop_bars=2, zigzag_bps=4)


def fine_market(symbol: str) -> dict:
    # A fine amount step keeps quantity rounding out of these mechanics tests;
    # docs/ATB_MICRO_ECONOMICS.md shows what a coarse step costs.
    return mexc_market(symbol, amount_step=0.000001, price_tick=0.01)


def default_markets(minute: int) -> list[SeriesMarket]:
    return [SeriesMarket('ETH/USDT', minute - 300, mechanics(), market=fine_market('ETH/USDT')),
            SeriesMarket('BTC/USDT', minute - 300, flat(900, base=60000.0), market=fine_market('BTC/USDT'))]


def micro_settings(**changes) -> dict:
    cfg = dict(storage.settings())
    cfg.update({'time_horizon': 'micro', 'watchlist': ['ETH/USDT', 'BTC/USDT'], 'auto_discovery': False,
                'streaming_quotes': False})
    cfg.update(changes)
    cfg.pop('paper_start_usdt', None)
    return cfg


class World:
    def __init__(self, clock: FixtureClock, exchange: FixtureExchange, engine: trader.TradingEngine):
        self.clock, self.exchange, self.engine = clock, exchange, engine
        self.start_cash = Decimal(repr(engine._cash('paper')))

    # ---------------------------------------------------------------- driving
    def tick(self):
        return self.engine.micro.tick()

    def run(self, minutes: float, *, step: float = STEP_S, until=None) -> bool:
        for _ in range(int(round(minutes * 60 / step))):
            self.clock.advance(step)
            self.tick()
            if until is not None and until():
                return True
        return until is None

    def run_until_state(self, state: str, minutes: float = 60, symbol: str | None = None) -> dict:
        assert self.run(minutes, until=lambda: bool(self.cycles(state, symbol))), f'no {state} cycle'
        return self.cycles(state, symbol)[-1]

    def restart(self) -> trader.TradingEngine:
        """A new process on the same data folder: built STOPPED in PAPER, like every launch."""
        self.engine.running = False
        self.engine.stop_event.set()
        engine = trader.TradingEngine()
        engine.data.clock, engine.data.monotonic = self.clock.time, self.clock.monotonic
        self.engine = engine
        return engine

    def go(self):
        self.engine.running = True
        self.engine.stop_event.clear()

    # ---------------------------------------------------------------- reading
    def cycles(self, state: str | None = None, symbol: str | None = None) -> list[dict]:
        rows = storage.rows('SELECT * FROM micro_cycles ORDER BY created_at, rowid')
        return [r for r in rows if (state is None or r['state'] == state) and (symbol is None or r['symbol'] == symbol)]

    def intents(self, cycle_id: str | None = None) -> list[dict]:
        if cycle_id is None:
            return storage.rows("SELECT * FROM order_journal WHERE decision_id LIKE 'cyc_%' ORDER BY created_at")
        return storage.rows('SELECT * FROM order_journal WHERE decision_id=? ORDER BY created_at', (cycle_id,))

    def trades(self) -> list[dict]:
        return storage.rows("SELECT * FROM trades WHERE mode='paper' ORDER BY id")

    def cash(self) -> Decimal:
        return Decimal(repr(self.engine._cash('paper')))

    def venue(self):
        return self.engine.micro.venue

    def set_closes(self, symbol: str, price: float, *, from_minute: int | None = None) -> None:
        """From now on, ``symbol`` trades flat at ``price`` (a sudden repricing)."""
        series = self.exchange.series[symbol]
        start = series.index((from_minute if from_minute is not None else self.clock.minute()) - 1)
        series.closes[start:] = [price] * (len(series.closes) - start)


@pytest.fixture
def world(tmp_path, monkeypatch):
    built = []

    def build(markets=default_markets, **settings) -> World:
        monkeypatch.setattr(storage, 'DATA_DIR', tmp_path)
        monkeypatch.setattr(storage, 'DB_PATH', tmp_path / 'portfolio.sqlite3')
        monkeypatch.setattr(storage, 'CONFIG_PATH', tmp_path / 'settings.json')
        monkeypatch.setattr(credentials, 'load', lambda _: None)
        monkeypatch.setattr(credentials, 'save', lambda *a: None)
        monkeypatch.setattr(credentials, 'delete', lambda *a: None)
        monkeypatch.setattr(observability, 'METRICS', observability.Metrics())   # counters private to this test
        clock = FixtureClock()
        install_clock(clock, monkeypatch.setattr)
        exchange = FixtureExchange(clock, markets(clock.minute()))
        monkeypatch.setattr(trader.TradingEngine, '_new_exchange', staticmethod(lambda *a, **k: exchange))
        engine = trader.TradingEngine()
        engine.update_config(micro_settings(**settings))
        engine.data.clock, engine.data.monotonic = clock.time, clock.monotonic
        made = World(clock, exchange, engine)
        made.go()
        built.append(made)
        return made

    yield build
    for made in built:
        made.engine.running = False
        made.engine.stop_event.set()
        if made.engine.thread:
            made.engine.thread.join(timeout=5)


def money_close(a, b, tolerance=Decimal('1e-9')) -> bool:
    return abs(Decimal(str(a)) - Decimal(str(b))) <= tolerance


def assert_ledger_consistent(w: World) -> None:
    """Cash and holdings reconcile with the cycle records: nothing created, nothing lost."""
    cycles = w.cycles()
    realized = sum((Decimal(c['realized_net']) for c in cycles), Decimal(0))
    held_basis = sum((Decimal(c['basis_quote']) for c in cycles if c['state'] not in micro.TERMINAL), Decimal(0))
    residual_basis = sum((Decimal(c['residual_basis']) for c in cycles if c['state'] == 'CLOSED'), Decimal(0))
    reserved = sum((Decimal(c['reserved_quote']) for c in cycles if c['state'] not in micro.TERMINAL), Decimal(0))
    del reserved   # a reservation is not a debit: cash is only debited by a booked fill
    assert money_close(w.cash(), w.start_cash + realized - held_basis - residual_basis, Decimal('1e-6'))
    for cycle in cycles:
        if cycle['state'] in micro.OWNING_STATES:
            position = storage.rows('SELECT qty FROM positions WHERE symbol=? AND mode=?', (cycle['symbol'], 'paper'))
            assert position and money_close(position[0]['qty'], cycle['owned_base'], Decimal('1e-12'))


# ===================================================================== item 12
def test_mechanics_fixture_runs_buy_hold_sell_and_a_distinct_rebuy(world):
    """MECHANICS FIXTURE, not a profitable market backtest: the market is built to let the cycle complete."""
    w = world()
    assert w.run(60, until=lambda: len(w.cycles('CLOSED')) >= 2)
    first, second = w.cycles('CLOSED')[:2]
    assert first['opportunity_key'] != second['opportunity_key']
    assert second['created_at'] > first['closed_at']                  # the rebuy waited for the settled exit
    for cycle in (first, second):
        assert cycle['symbol'] == 'ETH/USDT' and cycle['strategy'] == 'range_reversion'
        assert Decimal(cycle['entry_debit']) <= Decimal('25')            # all-in, fee included
        assert Decimal(cycle['budget_cap']) == Decimal('25')
        assert cycle['last_exit_trigger'] == 'take_profit' and cycle['realized_status'] == 'confirmed'
        assert Decimal(cycle['realized_net']) > 0                         # true on this fixture by construction
        intents = w.intents(cycle['cycle_id'])
        assert [i['side'] for i in intents] == ['BUY', 'SELL']
        assert all(i['status'] == 'accounted' and i['mode'] == 'paper' and i['client_order_id'] for i in intents)
        for intent in intents:
            record = w.venue().fetch_order(None, 'ETH/USDT', {'clientOrderId': intent['client_order_id']})
            assert money_close(record['filled'], intent['filled_quantity'], Decimal('1e-12'))
            assert any(t['order_id'] == record['id'] for t in w.trades())
        events = [e['to_state'] for e in micro.cycle_events(cycle['cycle_id'])]
        assert events[:3] == ['ENTRY_RESERVED', 'ENTRY_SUBMITTED', 'HOLDING']
        assert events[-2:] == ['EXIT_SUBMITTED', 'CLOSED']
    assert len(w.venue().records()) == len(w.intents())                   # nothing at the venue the journal lacks
    assert_ledger_consistent(w)
    results = w.engine.micro.status()['results']
    assert results['closed_cycles'] >= 2 and results['realized_unconfirmed_count'] == 0


def test_one_start_runs_cycles_with_no_further_input(world, monkeypatch):
    """Start once; the worker enters, exits and re-enters on its own (mechanics fixture)."""
    w = world()
    w.engine.running = False
    real_tick = w.engine.micro.tick

    def paced_tick():
        delay = real_tick()
        w.clock.advance(STEP_S)          # the fixture clock, not the wall clock, paces this run
        assert delay > 0
        return 0.0
    monkeypatch.setattr(w.engine.micro, 'tick', paced_tick)
    w.engine.start()
    deadline = time.perf_counter() + 60
    while time.perf_counter() < deadline and len(w.cycles('CLOSED')) < 2:
        time.sleep(0.05)
    w.engine.stop()
    w.engine.thread.join(timeout=10)
    assert len(w.cycles('CLOSED')) >= 2
    assert w.engine.mode == 'paper'
    assert_ledger_consistent(w)


# ===================================================================== item 13
def test_markets_without_a_setup_abstain_and_nothing_is_forced(world):
    def markets(minute):
        return [SeriesMarket('ETH/USDT', minute - 300, flat(900, base=2500.0), market=fine_market('ETH/USDT')),
                SeriesMarket('BTC/USDT', minute - 300, random_walk(900, base=60000.0, vol_bps=1.5, seed=11),
                             market=fine_market('BTC/USDT')),
                SeriesMarket('SOL/USDT', minute - 300, trend(900, base=150.0, bps_per_minute=-3.0),
                             market=fine_market('SOL/USDT'))]
    w = world(markets, watchlist=['ETH/USDT', 'BTC/USDT', 'SOL/USDT'])
    w.run(90)
    assert w.cycles() == [] and w.intents() == [] and w.venue().records() == []
    status = w.engine.micro.status()
    rows = {row['symbol']: row for row in status['pairs']}
    for symbol in ('ETH/USDT', 'BTC/USDT', 'SOL/USDT'):
        decision = rows[symbol]['decision']
        assert decision['action'] == 'abstain' and decision['code'] and decision['checks']
        assert rows[symbol]['waiting']['code'] == decision['code']
    assert sum(status['counters'].values()) > 0
    assert all(d['action'] == 'abstain' for d in w.engine.micro.recent_decisions(300))


# ===================================================================== item 14
def test_duplicate_ticks_and_ui_retries_create_no_second_order_or_cycle(world):
    w = world()
    held = w.run_until_state('HOLDING')
    before = len(w.intents())
    for _ in range(3):                    # the same moment delivered three times
        w.tick()
    assert len(w.intents()) in (before, before + 1)     # at most the exit; never a second entry
    assert len([c for c in w.cycles() if c['state'] not in micro.TERMINAL]) <= 1
    assert [i['side'] for i in w.intents(held['cycle_id'])][0] == 'BUY'
    assert len([i for i in w.intents(held['cycle_id']) if i['side'] == 'BUY']) == 1
    with pytest.raises(sqlite3.IntegrityError):         # one cycle per opportunity, enforced by the database
        with storage.connection() as db:
            db.execute('INSERT INTO micro_cycles(cycle_id, exchange, mode, symbol, state, created_at, updated_at, '
                       'opportunity_key, budget_cap) VALUES (?,?,?,?,?,?,?,?,?)',
                       ('cyc_' + 'f' * 24, held['exchange'], 'paper', 'BTC/USDT', 'CLOSED', 'x', 'x',
                        held['opportunity_key'], '25'))


def test_repeated_exit_all_requests_send_one_sell(world, monkeypatch):
    import app
    w = world()
    held = w.run_until_state('HOLDING')
    w.engine.running = False                       # stopped: Exit all runs in the request
    monkeypatch.setattr(app, 'engine', w.engine)
    http = TestClient(app.app, base_url='http://127.0.0.1:8765', client=('127.0.0.1', 50000))
    headers = {'x-cryptopilot-token': app.CSRF}
    for _ in range(3):
        assert http.post('/api/micro', headers=headers, json={'action': 'flatten'}).status_code == 200
    sells = [i for i in w.intents(held['cycle_id']) if i['side'] == 'SELL']
    assert len(sells) == 1 and sells[0]['status'] == 'accounted'
    closed = micro.load_cycle(held['cycle_id'])
    assert closed['state'] == 'CLOSED' and closed['last_exit_trigger'] == 'operator'
    assert_ledger_consistent(w)


# ===================================================================== item 15
def test_a_response_lost_after_acceptance_is_resolved_read_only_without_a_resend(world):
    w = world()
    w.venue().drop_response_next = True
    held = w.run_until_state('HOLDING')
    assert w.venue().drop_response_next is False                    # the injection was consumed by the entry
    buys = [i for i in w.intents(held['cycle_id']) if i['side'] == 'BUY']
    assert len(buys) == 1 and buys[0]['status'] == 'accounted'
    assert len(w.venue().records()) == 1                             # found by client order id, never resent
    assert w.engine.metrics.snapshot()['counters'].get('execution.located_after_ambiguity', 0) >= 1
    assert_ledger_consistent(w)


# ===================================================================== item 16
def test_a_partial_entry_owns_exactly_what_filled_and_the_exit_sells_exactly_that(world):
    w = world()
    w.venue().partial_next = Decimal('0.5')
    held = w.run_until_state('HOLDING')
    assert held['entry_partial'] == 1
    record = w.venue().fetch_order(None, 'ETH/USDT', {'clientOrderId': w.intents(held['cycle_id'])[0]['client_order_id']})
    assert record['status'] == 'canceled'
    exact = record['info']['exact']
    assert Decimal(held['entry_filled_base']) == Decimal(exact['filled'])
    assert Decimal(held['entry_debit']) == Decimal(exact['cost']) + Decimal(exact['fee_cost'])
    assert Decimal(held['entry_debit']) < Decimal('13')               # about half of the 25 USDT; never topped up
    assert held['reserved_quote'] == '0'
    closed = w.run_until_state('CLOSED')
    sell = [i for i in w.intents(closed['cycle_id']) if i['side'] == 'SELL'][0]
    assert money_close(sell['filled_quantity'], held['owned_base'], Decimal('1e-12'))
    assert storage.rows("SELECT * FROM positions WHERE symbol='ETH/USDT'") == []
    assert_ledger_consistent(w)


def test_a_late_fill_racing_the_cancel_is_booked_from_the_final_read(world):
    w = world()
    w.venue().late_fill_next = Decimal('1')
    held = w.run_until_state('HOLDING')
    intent = w.intents(held['cycle_id'])[0]
    record = w.venue().fetch_order(None, 'ETH/USDT', {'clientOrderId': intent['client_order_id']})
    assert Decimal(held['entry_filled_base']) == Decimal(record['info']['exact']['filled'])   # not the first answer
    assert money_close(intent['filled_quantity'], record['info']['exact']['filled'], Decimal('1e-12'))
    assert w.engine.metrics.snapshot()['counters'].get('micro.late_fill_reconciled') == 1
    assert w.engine.micro._orphans('ETH/USDT') == []
    assert_ledger_consistent(w)


def test_a_fill_reported_after_booking_sends_the_cycle_to_review_and_recovery_books_only_the_extra(world):
    w = world()
    w.venue().partial_next = Decimal('0.5')
    held = w.run_until_state('HOLDING')
    intent = w.intents(held['cycle_id'])[0]
    stored = w.venue().fetch_order(None, 'ETH/USDT', {'clientOrderId': intent['client_order_id']})['info']['exact']
    extra_base, extra_cost, extra_fee = Decimal('0.0004'), Decimal('1.0004'), Decimal('0.0010004')
    with storage.connection() as db:                # the venue now reports more filled (a delayed late fill)
        db.execute('UPDATE paper_venue_orders SET filled=?, cost=?, fee_cost=? WHERE client_order_id=?',
                   (str(Decimal(stored['filled']) + extra_base), str(Decimal(stored['cost']) + extra_cost),
                    str(Decimal(stored['fee_cost']) + extra_fee), intent['client_order_id']))
    cash_before = w.cash()
    w.set_closes('ETH/USDT', float(Decimal(held['entry_price'])))     # keep the price still: no exit meanwhile
    w.run(1)
    review = micro.load_cycle(held['cycle_id'])
    assert review['state'] == 'REVIEW' and 'late fill' in review['review_reason']
    assert w.engine.micro.review_block()                      # every new micro entry is blocked meanwhile
    with pytest.raises(storage.ConfigValidationError):
        w.engine.micro.apply_recovery(held['cycle_id'], REVIEW)        # the bot must be stopped first
    w.engine.running = False
    with pytest.raises(storage.ConfigValidationError):
        w.engine.micro.apply_recovery(held['cycle_id'], 'yes')
    result = w.engine.micro.apply_recovery(held['cycle_id'], REVIEW)
    assert result['outcome'] == 'late fill booked from the venue record'
    after = micro.load_cycle(held['cycle_id'])
    assert after['state'] == 'HOLDING'
    assert Decimal(after['owned_base']) == Decimal(held['owned_base']) + extra_base
    assert money_close(cash_before - w.cash(), extra_cost + extra_fee)
    assert w.engine.micro._late_fills(after) == [] and w.engine.micro.review_block() == []
    assert_ledger_consistent(w)


def test_an_orphan_venue_order_blocks_new_entries_on_that_pair(world):
    w = world()
    venue = w.venue()
    book = w.exchange.fetch_order_book('ETH/USDT')
    import micro_book
    snapshot = micro_book.from_ccxt_order_book(book, exchange='mexc', symbol='ETH/USDT', received_wall=time.time(),
                                               received_mono=time.monotonic(), source='fixture')
    venue.create_market_buy_order_with_cost('ETH/USDT', 10, {'clientOrderId': 'not-in-any-journal', 'book': snapshot})
    w.run(40)
    assert w.cycles(symbol='ETH/USDT') == []                     # the mechanics fixture would have entered by now
    row = next(r for r in w.engine.micro.status()['pairs'] if r['symbol'] == 'ETH/USDT')
    assert row['waiting']['code'] == 'REVIEW' and 'orphan' in row['waiting']['status']
    assert any('orphan' in item['explanation'] for item in w.engine.micro.recovery)


# ===================================================================== item 17
def test_reentry_needs_a_settled_exit_a_new_bar_and_survives_a_restart(world):
    w = world(micro_cooldown_seconds=0)
    first = w.run_until_state('CLOSED')
    w.engine = w.restart()                      # same minute, new process
    assert w.engine.running is False and w.engine.mode == 'paper'
    w.go()
    for _ in range(3):
        w.tick()                                # no clock movement: the same bar
    assert len(w.cycles()) == 1
    w.run(60, until=lambda: len(w.cycles()) >= 2)
    cycles = w.cycles()
    assert len(cycles) >= 2
    for previous, following in zip(cycles, cycles[1:]):
        bar_ms = int(following['opportunity_key'].rsplit('|', 1)[1])
        assert bar_ms / 1000 + 60 > previous['closed_wall']       # the setup bar completed after the exit
        assert following['opportunity_key'] != previous['opportunity_key']
    del first


def test_cooldown_spaces_a_rebuy_on_the_same_pair(world):
    w = world(micro_cooldown_seconds=600)
    w.run(90, until=lambda: len(w.cycles()) >= 2)
    cycles = w.cycles()
    assert len(cycles) >= 2
    created = [micro.cycle_events(c['cycle_id'])[0] for c in cycles]
    del created
    for previous, following in zip(cycles, cycles[1:]):
        opened_after = storage.rows('SELECT opened_wall FROM micro_cycles WHERE cycle_id=?',
                                    (following['cycle_id'],))[0]['opened_wall']
        assert opened_after - previous['closed_wall'] >= 600 - STEP_S


# ===================================================================== item 18
def test_a_crash_before_the_order_left_releases_the_reservation_at_startup(world, monkeypatch):
    w = world()

    def die(*args, **kwargs):
        raise Crash()
    monkeypatch.setattr(w.engine, '_submit', die)
    with pytest.raises(Crash):
        w.run(60)
    cycle = w.cycles()[-1]
    assert cycle['state'] == 'ENTRY_SUBMITTED' and Decimal(cycle['reserved_quote']) > 0
    cash = w.cash()
    engine = w.restart()
    assert engine.mode == 'paper' and engine.running is False
    assert micro.load_cycle(cycle['cycle_id'])['state'] == 'ABANDONED'
    assert w.cash() == cash and engine.micro.review_block() == []
    assert any('released' in item['explanation'] for item in engine.micro.recovery)


def test_a_crash_after_the_venue_accepted_keeps_the_outcome_blocked_until_reviewed(world, monkeypatch):
    w = world()

    real = micro.MicroManager._book_entry

    def die(self, *args, **kwargs):
        raise Crash()
    monkeypatch.setattr(micro.MicroManager, '_book_entry', die)
    with pytest.raises(Crash):
        w.run(60)
    monkeypatch.setattr(micro.MicroManager, '_book_entry', real)
    cycle = w.cycles()[-1]
    intent = w.intents(cycle['cycle_id'])[0]
    assert intent['status'] == 'filled' and len(w.venue().records()) == 1
    engine = w.restart()
    assert engine.mode == 'paper' and engine.running is False
    unknown = micro.load_cycle(cycle['cycle_id'])
    assert unknown['state'] == 'ENTRY_UNKNOWN'
    explanation = next(item for item in engine.micro.recovery if item.get('cycle_id') == cycle['cycle_id'])
    assert 'Applying recovery books exactly that' in explanation['explanation']
    w.go()
    w.run(5)
    assert len(w.cycles()) == 1                                   # nothing new while the outcome is unresolved
    assert micro.load_cycle(cycle['cycle_id'])['state'] == 'ENTRY_UNKNOWN'
    engine.running = False
    result = engine.micro.apply_recovery(cycle['cycle_id'], REVIEW)
    assert result['outcome'] == 'entry fill booked from the venue record'
    held = micro.load_cycle(cycle['cycle_id'])
    record = w.venue().records()[0]
    assert held['state'] == 'HOLDING' and Decimal(held['entry_filled_base']) == record['filled']
    assert storage.order_intent(intent['intent_id'])['status'] == 'accounted'
    assert_ledger_consistent(w)


def test_a_crash_after_the_ledger_commit_needs_no_recovery(world, monkeypatch):
    w = world()
    real = micro.MicroManager._book_entry

    def commit_then_die(self, *args, **kwargs):
        assert real(self, *args, **kwargs)
        raise Crash()
    monkeypatch.setattr(micro.MicroManager, '_book_entry', commit_then_die)
    with pytest.raises(Crash):
        w.run(60)
    monkeypatch.setattr(micro.MicroManager, '_book_entry', real)
    cycle = w.cycles()[-1]
    engine = w.restart()
    assert micro.load_cycle(cycle['cycle_id'])['state'] == 'HOLDING'
    assert engine.micro.recovery == [] and engine.micro.review_block() == []
    w.go()
    closed = w.run_until_state('CLOSED')
    assert closed['cycle_id'] == cycle['cycle_id']
    assert_ledger_consistent(w)


# ===================================================================== item 19
def two_setups(minute):
    return [SeriesMarket('ETH/USDT', minute - 300, mechanics(), market=fine_market('ETH/USDT')),
            SeriesMarket('BTC/USDT', minute - 300, mechanics(base=60000.0), market=fine_market('BTC/USDT'))]


def captured_entries(w: World, monkeypatch) -> dict:
    """Run the real strategy until both pairs decide to enter; keep the arguments, enter neither."""
    captured = {}
    real_enter = micro.MicroManager._enter

    def capture(self, symbol, *args):
        captured.setdefault(symbol, args)
        return False
    monkeypatch.setattr(micro.MicroManager, '_enter', capture)
    assert w.run(60, until=lambda: len(captured) == 2)
    monkeypatch.setattr(micro.MicroManager, '_enter', real_enter)
    return captured


def enter_together(w: World, captured: dict) -> dict:
    gate = threading.Barrier(len(captured))
    outcomes = {}

    def enter(symbol):
        gate.wait()
        outcomes[symbol] = w.engine.micro._enter(symbol, *captured[symbol])
    threads = [threading.Thread(target=enter, args=(symbol,)) for symbol in captured]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    return outcomes


def test_concurrent_entries_cannot_reserve_the_same_paper_cash(world, monkeypatch):
    w = world(two_setups)
    captured = captured_entries(w, monkeypatch)
    with storage.connection() as db:              # 30 USDT of paper cash: room for one 25 USDT entry, not two
        db.execute('UPDATE meta SET value=? WHERE key=?', (repr(30.0), w.engine._key('paper_cash')))
    enter_together(w, captured)
    reserved = w.cycles()
    assert len(reserved) == 1                     # the other entry was refused before any cycle or order existed
    loser = ({'ETH/USDT', 'BTC/USDT'} - {reserved[0]['symbol']}).pop()
    waiting = next(r for r in w.engine.micro.status()['pairs'] if r['symbol'] == loser)['waiting']
    assert 'already reserved' in waiting['status']
    assert len(w.venue().records()) <= 1 and w.cash() >= Decimal('5')


def test_concurrent_entries_cannot_exceed_the_cycle_capacity(world, monkeypatch):
    w = world(two_setups, micro_max_cycles=1)
    captured = captured_entries(w, monkeypatch)
    outcomes = enter_together(w, captured)
    assert sorted(outcomes.values()) == [False, True]
    held = [c for c in w.cycles() if c['state'] == 'HOLDING']
    assert len(w.cycles()) == 1 and len(held) == 1 and Decimal(held[0]['entry_debit']) <= Decimal('25')
    assert len(w.venue().records()) == 1
    assert_ledger_consistent(w)


def test_capacity_and_allocation_hold_across_pairs(world):
    w = world(two_setups, micro_max_cycles=1)
    w.run(60, until=lambda: len(w.cycles('CLOSED')) >= 2)
    for cycle in w.cycles():
        overlapping = [c for c in w.cycles() if c['cycle_id'] != cycle['cycle_id'] and c['opened_wall']
                       and cycle['opened_wall'] and c['opened_wall'] < (cycle['closed_wall'] or 1e18)
                       and (c['closed_wall'] or 1e18) > cycle['opened_wall']]
        assert overlapping == []                    # never two cycles holding at once with one slot


# ===================================================================== item 20
def test_a_stop_loss_exit_needs_no_profit_no_entry_signal_and_no_depth_book(world):
    w = world()
    held = w.run_until_state('HOLDING')
    w.set_closes('ETH/USDT', float(Decimal(held['entry_price'])) * 0.985)        # a 150 bps drop
    w.exchange.fail_books.add('ETH/USDT')                                         # depth unavailable from now on
    w.exchange.calls.clear()
    closed = w.run_until_state('CLOSED', minutes=10)
    assert closed['last_exit_trigger'] in ('stop_loss', 'regime_shift')
    assert Decimal(closed['realized_net']) < 0 and closed['realized_status'] == 'confirmed'
    assert any(call[0] == 'fetch_bids_asks' for call in w.exchange.calls)        # sold on a top-of-book quote
    decision = next(r for r in w.engine.micro.status()['pairs'] if r['symbol'] == 'ETH/USDT').get('decision') or {}
    assert decision.get('action') != 'enter'
    assert_ledger_consistent(w)


def test_the_maximum_hold_closes_a_flat_position_at_a_labeled_loss(world):
    w = world()
    held = w.run_until_state('HOLDING')
    series = w.exchange.series['ETH/USDT']
    series.closes[:] = [float(Decimal(held['entry_price']))] * len(series.closes)   # a market that stops moving
    w.engine.micro.context.clear()
    closed = w.run_until_state('CLOSED', minutes=40)
    assert closed['last_exit_trigger'] == 'max_hold'
    assert closed['closed_wall'] - closed['opened_wall'] >= 30 * 60
    assert Decimal(closed['realized_net']) < 0                  # flat prices plus costs are a loss, never a profit
    assert_ledger_consistent(w)


def test_a_short_maximum_hold_leaves_no_room_for_the_target_so_nothing_enters(world):
    w = world(micro_max_hold_minutes=3)
    w.run(60)
    assert w.cycles() == []
    codes = [d['code'] for d in w.engine.micro.recent_decisions(300) if d['symbol'] == 'ETH/USDT']
    assert 'NO_ECONOMIC_EDGE' in codes


# ===================================================================== item 21
def test_a_pair_removed_from_the_selection_keeps_its_holding_managed(world):
    w = world()
    held = w.run_until_state('HOLDING')
    w.engine.running = False
    w.engine.update_config(micro_settings(watchlist=['BTC/USDT']))
    assert 'ETH/USDT' in w.engine.micro.owned_symbols()
    w.go()
    monitored = w.engine._monitor_if_due(force=True)
    assert 'ETH/USDT' not in (monitored or set())                 # the legacy monitor leaves micro holdings alone
    closed = w.run_until_state('CLOSED', minutes=40)
    assert closed['cycle_id'] == held['cycle_id']
    w.run(40)
    assert [c['cycle_id'] for c in w.cycles(symbol='ETH/USDT')] == [held['cycle_id']]    # no new ETH entries
    assert_ledger_consistent(w)


# ===================================================================== item 22
def test_consecutive_losses_latch_and_only_the_phrase_clears_it(world):
    w = world(micro_max_consecutive_losses=2, micro_cooldown_seconds=0)
    for _ in range(2):
        held = w.run_until_state('HOLDING', minutes=60)
        w.set_closes('ETH/USDT', float(Decimal(held['entry_price'])))
        w.run(40, until=lambda: micro.load_cycle(held['cycle_id'])['state'] == 'CLOSED')
        w.exchange.series['ETH/USDT'].closes[:] = mechanics()
        if len(w.cycles('CLOSED')) < 2:
            w.run(1)
    assert w.engine.micro.latch()['reason'].startswith('2 consecutive losing')
    count = len(w.cycles())
    w.run(90)
    assert len(w.cycles()) == count                          # no churn while latched
    engine = w.restart()
    assert engine.micro.latch() is not None                  # durable across a restart
    w.go()
    w.run(60)
    assert len(w.cycles()) == count
    with pytest.raises(storage.ConfigValidationError):
        engine.micro.clear_latch('resume')
    engine.micro.clear_latch(micro.RESUME_PHRASE)
    w.run(60, until=lambda: len(w.cycles()) > count)
    assert len(w.cycles()) > count


def test_the_kill_switch_blocks_micro_entries_until_released(world):
    w = world()
    w.engine.risk.engage_kill_switch('test')
    w.run(60)
    assert w.cycles() == []
    row = next(r for r in w.engine.micro.status()['pairs'] if r['symbol'] == 'ETH/USDT')
    assert row['decision']['action'] == 'abstain'
    w.engine.risk.release_kill_switch('RESUME TRADING')
    w.run(60, until=lambda: bool(w.cycles()))
    assert w.cycles()


def test_the_daily_micro_loss_limit_latches(world):
    w = world(micro_daily_loss_limit_usdt=0.1)
    now = time.time()
    with storage.connection() as db:
        for index, loss in enumerate(('-0.06', '0.01', '-0.06')):
            db.execute('INSERT INTO micro_cycles(cycle_id, exchange, mode, symbol, state, created_at, updated_at, '
                       'opportunity_key, budget_cap, realized_net, closed_wall) VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                       (f'cyc_{index:024d}', w.engine.exchange_name, 'paper', 'ETH/USDT', 'CLOSED', 'x', 'x',
                        f'k{index}', '25', loss, now - 60 + index))
    w.engine.micro._check_loss_limits()
    assert 'realized micro loss today' in w.engine.micro.latch()['reason']


# ===================================================================== items 24 and 25
def test_stop_during_an_accepted_order_still_books_its_fill(world, monkeypatch):
    w = world()
    venue = w.venue()
    real = venue.create_market_buy_order_with_cost

    def stop_while_in_flight(symbol, cost, params=None):
        order = real(symbol, cost, params)
        w.engine.stop_event.set()                 # Stop pressed while the venue was answering
        return order
    monkeypatch.setattr(venue, 'create_market_buy_order_with_cost', stop_while_in_flight)
    w.run(60, until=lambda: bool(w.cycles('HOLDING')) or bool(w.cycles('ENTRY_UNKNOWN')))
    held = w.cycles()[-1]
    assert held['state'] == 'HOLDING'
    assert w.intents(held['cycle_id'])[0]['status'] == 'accounted'
    assert_ledger_consistent(w)


def _five_pairs(minute):
    others = [SeriesMarket(f'C{i}/USDT', minute - 300, flat(900, base=10.0 + i), market=fine_market(f'C{i}/USDT'))
              for i in range(4)]
    return [SeriesMarket('ETH/USDT', minute - 300, mechanics(), market=fine_market('ETH/USDT'))] + others


def test_slow_candle_downloads_do_not_starve_the_exit_checks(world):
    """Sequential downloads (an exchange whose pacing allows no overlap, such as Kraken) are the worst case."""
    w = world(_five_pairs, watchlist=['ETH/USDT', 'C0/USDT', 'C1/USDT', 'C2/USDT', 'C3/USDT'])
    w.engine.data.concurrency = 1                              # no background downloads: one request at a time
    w.run_until_state('HOLDING')
    w.clock.advance(60 - (w.clock.time() % 60) + 5)           # just after a bar closes: every context is due
    w.exchange.calls.clear()
    real_fetch = w.exchange.fetch_ohlcv

    def slow_fetch(*args, **kwargs):
        w.clock.advance(4.0)                                   # each download takes 4 s of fixture time
        return real_fetch(*args, **kwargs)
    w.exchange.fetch_ohlcv = slow_fetch
    w.tick()
    calls = w.exchange.calls
    candle_at = [i for i, call in enumerate(calls) if call[0] == 'fetch_ohlcv']
    exit_books = [i for i, call in enumerate(calls) if call == ('fetch_order_book', 'ETH/USDT')]
    assert len(candle_at) >= 5
    between = [i for i in exit_books if candle_at[0] < i < candle_at[-1]]
    assert len(between) >= 2, calls                             # the holding was re-checked between downloads


def test_due_candle_frames_download_together_where_the_rate_limit_allows(world):
    """MEXC-style pacing (rateLimit 50 ms): the due frames overlap their round trips, exits still run."""
    w = world(_five_pairs, watchlist=['ETH/USDT', 'C0/USDT', 'C1/USDT', 'C2/USDT', 'C3/USDT'])
    assert w.engine.data.concurrency > 1
    w.run_until_state('HOLDING')
    w.clock.advance(60 - (w.clock.time() % 60) + 5)           # just after a bar closes: every context is due
    w.exchange.calls.clear()
    real_fetch = w.exchange.fetch_ohlcv
    state = {'inflight': 0, 'peak': 0}
    guard = threading.Lock()

    def slow_fetch(*args, **kwargs):
        with guard:
            state['inflight'] += 1
            state['peak'] = max(state['peak'], state['inflight'])
        try:
            time.sleep(0.05)                                   # a real round trip, so overlap is observable
            return real_fetch(*args, **kwargs)
        finally:
            with guard:
                state['inflight'] -= 1
    w.exchange.fetch_ohlcv = slow_fetch
    w.tick()
    candles = [call for call in w.exchange.calls if call[0] == 'fetch_ohlcv']
    assert len(candles) == len(set(candles))                   # each frame at most once: no duplicate download
    assert len([call for call in candles if call[2] == '1m']) == 5   # every pair's new 1m bar
    assert 2 <= state['peak'] <= w.engine.data.concurrency - 1  # overlapped, inside the bulk-slot limit
    assert ('fetch_order_book', 'ETH/USDT') in w.exchange.calls  # the holding's exit check still ran
    assert all(w.engine.micro.context[s]['current'] for s in ['ETH/USDT', 'C0/USDT', 'C1/USDT', 'C2/USDT', 'C3/USDT'])


def test_status_reports_estimates_apart_from_confirmed_results(world):
    w = world()
    w.run_until_state('HOLDING')
    w.run(0.5)
    status = w.engine.micro.status()
    assert status['paper_only'] is True and status['budget']['absolute_cap'] == '25'
    cycle = status['cycles'][0]
    estimate = cycle['estimate']
    assert estimate['labels']['proceeds'] == 'estimated'
    assert cycle['entry_fee_status'] == 'simulated'
    json.dumps(status, default=str)
    assert status['results']['closed_cycles'] == 0 and status['results']['fees_share_of_gross_gains'] is None


def test_results_report_fees_against_the_gains_before_fees(world):
    w = world()
    assert w.run(60, until=lambda: len(w.cycles('CLOSED')) >= 2)
    results = w.engine.micro.results()
    closed = w.cycles('CLOSED')
    fees = sum(Decimal(c['entry_fee_quote']) + Decimal(c['exit_fees']) for c in closed)
    net = sum(Decimal(c['realized_net']) for c in closed)
    assert Decimal(results['fees_paid']) == fees and Decimal(results['realized_net_total']) == net
    # Every fixture cycle won, so the gains before fees are the net result plus all fees.
    assert results['fees_share_of_gross_gains'] == round(float(fees / (net + fees)), 4)
    assert 0 < results['fees_share_of_gross_gains'] < 1


# ===================================================================== item 17 and 23: the bar that just closed
def _flat_eth(minute: int) -> list[SeriesMarket]:
    return [SeriesMarket('ETH/USDT', minute - 300, flat(900, base=2500.0), market=fine_market('ETH/USDT'))]


def _candle_reads(w: World) -> int:
    return sum(1 for call in w.exchange.calls if call[0] == 'fetch_ohlcv' and call[2] == '1m')


def test_a_cached_frame_from_the_previous_minute_is_not_used_for_the_new_bar(world):
    """Just after a close the candle cache still holds the previous frame: entries wait, then re-read it."""
    w = world(markets=_flat_eth, watchlist=['ETH/USDT'])
    w.clock.advance(60 - (w.clock.time() % 60) + 30)        # mid-bar: the context is read for the last closed bar
    w.tick()
    assert w.engine.micro.context['ETH/USDT']['current'] is True
    reads = _candle_reads(w)
    w.clock.advance(30.5)                                    # half a second after the next bar closed
    w.tick()
    context = w.engine.micro.context['ETH/USDT']
    assert context['current'] is False and context['reason'].startswith('STALE')
    decision = w.engine.micro.pairs['ETH/USDT']['decision']
    assert decision['action'] == 'abstain' and decision['code'] == 'STALE_DATA'
    assert not w.cycles()
    w.clock.advance(1.0)                                     # inside the retry interval: no new candle request
    w.tick()
    assert _candle_reads(w) == reads
    w.clock.advance(micro.CONTEXT_RETRY_S)                   # the cache has expired: read again, now current
    w.tick()
    context = w.engine.micro.context['ETH/USDT']
    assert context['current'] is True and _candle_reads(w) == reads + 1
    assert context['features'].bar_ts_ms // 1000 == w.engine.micro._bar_due()


def test_a_venue_that_publishes_the_bar_late_is_retried_at_a_bounded_rate(world, monkeypatch):
    w = world(markets=_flat_eth, watchlist=['ETH/USDT'])
    original = w.exchange.fetch_ohlcv
    lag = {'on': True}

    def late(symbol, timeframe='1m', since=None, limit=120, params=None):
        rows = original(symbol, timeframe, since, limit, params)
        return rows[:-2] if lag['on'] and timeframe == '1m' else rows   # the bar that just closed is missing

    monkeypatch.setattr(w.exchange, 'fetch_ohlcv', late)
    w.clock.advance(60 - (w.clock.time() % 60) + 10)
    reads = _candle_reads(w)
    for _ in range(20):                                       # 20 seconds of one-second ticks
        w.clock.advance(1.0)
        w.tick()
        assert w.engine.micro.pairs['ETH/USDT']['decision']['code'] == 'STALE_DATA'
    assert 1 <= _candle_reads(w) - reads <= 20 / micro.CONTEXT_RETRY_S + 1
    assert not w.cycles()
    lag['on'] = False
    w.clock.advance(micro.CONTEXT_RETRY_S)
    w.tick()
    assert w.engine.micro.context['ETH/USDT']['current'] is True
    assert w.engine.micro.pairs['ETH/USDT']['decision']['code'] != 'STALE_DATA'
