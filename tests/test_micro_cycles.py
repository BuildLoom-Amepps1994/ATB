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
import dataclasses
import json
import math
import sqlite3
import sys
import threading
import types
import time
import uuid
from decimal import Decimal
from pathlib import Path

import ccxt
import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import credentials
import feed
import micro
import money
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


def test_a_pairs_conflict_note_is_replaced_by_its_next_venue_check(world, monkeypatch):
    w = world()
    venue = w.venue()
    real = venue.records
    failed = []

    def flaky(symbol=None, *args, **kwargs):
        if symbol == 'ETH/USDT' and not failed:             # one transient failure of the pair's conflict check
            failed.append(time.time())
            raise sqlite3.OperationalError('database is locked')
        return real(symbol, *args, **kwargs)
    monkeypatch.setattr(venue, 'records', flaky)

    def unreadable() -> list[dict]:
        return [item for item in w.engine.micro.status()['recovery'] if 'unreadable' in str(item.get('explanation'))]
    assert w.run(60, until=lambda: bool(failed))
    found = time.time()
    [note] = unreadable()                                    # shown while it is the pair's latest check
    assert note['symbol'] == 'ETH/USDT'
    assert w.run(600, until=lambda: len(w.cycles('CLOSED', 'ETH/USDT')) >= 2)    # later checks are clean
    assert unreadable() == []
    assert note.get('source') == 'conflict' and failed[0] <= note.get('at', 0) <= found   # dated by its check


def test_start_up_and_interruption_check_findings_carry_the_time_of_their_check(world, monkeypatch):
    w = world()
    real = w.engine._submit

    def die(client, symbol, side, **kwargs):
        if side == 'SELL':
            raise Crash()                                    # the process dies before the exit order leaves
        return real(client, symbol, side, **kwargs)
    monkeypatch.setattr(w.engine, '_submit', die)
    with pytest.raises(Crash):
        w.run(120)
    stranded = w.cycles()[-1]
    assert stranded['state'] == 'EXIT_SUBMITTED'
    before = time.time()
    engine = w.restart()
    checked = time.time()
    [item] = [i for i in engine.micro.status()['recovery'] if i.get('cycle_id') == stranded['cycle_id']]
    assert item['state'] == 'HOLDING' and 'never sent' in item['explanation']
    assert item.get('source') == 'startup' and before <= item.get('at', 0) <= checked
    w.go()
    assert w.run(600, until=lambda: micro.load_cycle(stranded['cycle_id'])['state'] == 'CLOSED')
    [later] = [i for i in engine.micro.status()['recovery'] if i.get('cycle_id') == stranded['cycle_id']]
    assert later['at'] == item['at'] < time.time()            # a dated past event, not a current check
    # A reservation interrupted while running is settled in-process: its finding is dated too.
    authorize = engine._authorize

    def broken(*args, **kwargs):
        if args[1] == 'BUY':
            raise RuntimeError('gate unavailable')
        return authorize(*args, **kwargs)
    monkeypatch.setattr(engine, '_authorize', broken)
    with pytest.raises(RuntimeError):
        w.run(600)
    monkeypatch.setattr(engine, '_authorize', authorize)
    before = time.time()
    w.tick()
    [released] = [i for i in engine.micro.recovery if i.get('state') == 'ABANDONED']
    assert 'released' in released['explanation'] and released['source'] == 'settle'
    assert before <= released['at'] <= time.time()


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


def captured_entries(w: World, monkeypatch, count: int = 2) -> dict:
    """Run the real strategy until ``count`` pairs (both) decide to enter; keep the arguments, enter none."""
    captured = {}
    real_enter = micro.MicroManager._enter

    def capture(self, symbol, *args):
        captured.setdefault(symbol, args)
        return False
    monkeypatch.setattr(micro.MicroManager, '_enter', capture)
    assert w.run(60, until=lambda: len(captured) == count)
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


def test_the_reservation_refuses_while_the_micro_stop_is_latched_or_an_outcome_is_unresolved(world, monkeypatch):
    # The reservation is the authoritative gate: it re-reads the latch and every cycle in its own transaction.
    w = world(two_setups, micro_max_cycles=2)
    captured = captured_entries(w, monkeypatch)
    manager = w.engine.micro

    def waiting(symbol):
        return next(r for r in manager.status()['pairs'] if r['symbol'] == symbol)['waiting']
    manager._engage_latch('test latch')
    assert manager._enter('ETH/USDT', *captured['ETH/USDT']) is False
    assert w.cycles() == [] and 'micro stop' in waiting('ETH/USDT')['status']
    manager.clear_latch(micro.RESUME_PHRASE)
    with storage.connection() as db:
        db.execute('INSERT OR REPLACE INTO meta(key,value) VALUES (?,?)', (manager._key('stop'), '{not json'))
    assert manager._enter('ETH/USDT', *captured['ETH/USDT']) is False and w.cycles() == []
    manager.clear_latch(micro.RESUME_PHRASE)
    assert manager._enter('ETH/USDT', *captured['ETH/USDT']) is True
    held = w.cycles('HOLDING', 'ETH/USDT')[0]
    with storage.connection() as db:
        micro.transition(db, held, 'REVIEW', 'test: outcome needs review', review_reason='test')
    assert manager._enter('BTC/USDT', *captured['BTC/USDT']) is False
    assert w.cycles(symbol='BTC/USDT') == [] and waiting('BTC/USDT')['code'] == 'REVIEW'
    with storage.connection() as db:
        db.execute("UPDATE micro_cycles SET state='CLOSED', late_fill_reason='test late fill' WHERE cycle_id=?",
                   (held['cycle_id'],))
    assert manager._enter('BTC/USDT', *captured['BTC/USDT']) is False and w.cycles(symbol='BTC/USDT') == []
    with storage.connection() as db:
        db.execute('UPDATE micro_cycles SET late_fill_reason=NULL WHERE cycle_id=?', (held['cycle_id'],))
    assert manager._enter('BTC/USDT', *captured['BTC/USDT']) is True          # nothing else was in the way


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
    sale = [r for r in w.venue().records('ETH/USDT') if r['side'] == 'sell'][-1]
    assert sale['info']['book_source'] == 'ticker' and sale['info']['depth_known'] is False
    decision = next(r for r in w.engine.micro.status()['pairs'] if r['symbol'] == 'ETH/USDT').get('decision') or {}
    assert decision.get('action') != 'enter'
    assert_ledger_consistent(w)


def live_quote_feed(w: World, *, sizes: bool):
    """A real QuoteFeed, live, that ``pump`` feeds the fixture's best bid and ask (with or without their sizes)."""
    stream = feed.QuoteFeed('mexc', ['ETH/USDT', 'BTC/USDT'], clock=w.clock.time, heartbeat_seconds=60.0)
    stream.state = 'live'

    def pump():
        now = w.clock.time()
        updates = {}
        for symbol in ('ETH/USDT', 'BTC/USDT'):
            top = w.exchange.series[symbol].book(w.clock.minute(), now_ms=int(now * 1000), sequence=0)
            update = {'bid': top['bids'][0][0], 'ask': top['asks'][0][0], 'timestamp': int(now * 1000)}
            if sizes:
                update.update(bidVolume=top['bids'][0][1], askVolume=top['asks'][0][1])
            updates[symbol] = update
        stream._ingest(updates, now)
    return stream, pump


def stop_loss_with_a_live_stream_and_no_depth(w: World, *, sizes: bool) -> dict | None:
    held = w.run_until_state('HOLDING')
    w.engine.feed, pump = live_quote_feed(w, sizes=sizes)
    pump()
    w.set_closes('ETH/USDT', float(Decimal(held['entry_price'])) * 0.985)        # a 150 bps drop
    w.exchange.fail_books.add('ETH/USDT')                                         # depth unavailable from now on
    w.exchange.calls.clear()
    for _ in range(int(10 * 60 / STEP_S)):
        w.clock.advance(STEP_S)
        pump()
        w.tick()
        if w.cycles('CLOSED', 'ETH/USDT'):
            return w.cycles('CLOSED', 'ETH/USDT')[-1]
    return None


def test_a_stop_loss_with_a_live_stream_and_no_depth_book_sells_against_the_streamed_size(world):
    w = world()
    closed = stop_loss_with_a_live_stream_and_no_depth(w, sizes=True)
    assert closed is not None, [(c['state'], c['last_reason']) for c in w.cycles('HOLDING')]
    assert closed['last_exit_trigger'] in ('stop_loss', 'regime_shift') and closed['realized_status'] == 'confirmed'
    sale = [r for r in w.venue().records('ETH/USDT') if r['side'] == 'sell'][-1]
    assert sale['info']['book_source'] == 'stream_ticker' and sale['info']['depth_known'] is False
    assert_ledger_consistent(w)


def test_a_stop_loss_with_a_live_stream_that_states_no_size_sells_on_the_rest_book_ticker(world):
    w = world()
    closed = stop_loss_with_a_live_stream_and_no_depth(w, sizes=False)
    assert closed is not None, [(c['state'], c['last_reason']) for c in w.cycles('HOLDING')]
    assert closed['last_exit_trigger'] in ('stop_loss', 'regime_shift') and closed['realized_status'] == 'confirmed'
    assert any(call[0] == 'fetch_bids_asks' for call in w.exchange.calls)        # the REST quote states its size
    sale = [r for r in w.venue().records('ETH/USDT') if r['side'] == 'sell'][-1]
    assert sale['info']['book_source'] == 'ticker' and sale['info']['depth_known'] is False
    assert not [i for i in w.intents(closed['cycle_id']) if i['status'] == 'rejected']
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


class ThinBids(SeriesMarket):
    """A market whose best bid holds about 2 USDT, with its real bid depth 160 bps and more below (while ``thin``)."""
    thin = True

    def book_at(self, mid, *, now_ms, sequence, depth_scale=1.0, spread_scale=1.0):
        book = super().book_at(mid, now_ms=now_ms, sequence=sequence, depth_scale=depth_scale,
                               spread_scale=spread_scale)
        if not self.thin:
            return book
        top = book['bids'][0][0]

        def below(bps):
            return round(math.floor(top * (1 - bps / 1e4) / self.tick) * self.tick, 2)
        bids = [[top, round(2.0 / top, 6)], [below(40), round(3.0 / top, 6)], [below(70), round(5.0 / top, 6)]]
        book['bids'] = bids + [[below(bps), round(400.0 / top, 6)] for bps in (160, 170, 180, 190, 200, 210)]
        return book


def test_a_thin_top_bid_is_not_entered_because_selling_into_the_bids_would_pass_the_loss_cap(world):
    def markets(minute):
        return [ThinBids('ETH/USDT', minute - 300, mechanics(), market=fine_market('ETH/USDT')),
                SeriesMarket('BTC/USDT', minute - 300, flat(900, base=60000.0), market=fine_market('BTC/USDT'))]
    w = world(markets)
    refused = {}

    def watch():
        decision = (w.engine.micro.pairs.get('ETH/USDT') or {}).get('decision') or {}
        stop = next((c for c in decision.get('checks') or () if c['check'] == 'stop_beyond_entry_cost'), None)
        if decision.get('candidate') and stop is not None and not stop['passed']:
            refused[decision['decision_id']] = (decision['code'], stop['entry_cost_bps'])
        return bool(w.cycles())
    assert not w.run(60, until=watch)
    assert w.cycles() == [] and w.intents() == []
    assert 'NO_ECONOMIC_EDGE' in {code for code, _ in refused.values()}   # a setup that the stop alone refused
    assert min(cost for _, cost in refused.values()) > 120 - 25    # the walk's cost, not the best bid's 40 bps
    # The same market with its depth back at the touch is entered.
    w.exchange.series['ETH/USDT'].thin = False
    assert w.run_until_state('HOLDING', minutes=60)['symbol'] == 'ETH/USDT'


def _noisy_decline(w: World, held: dict, *, pattern=(-40, 30), bars=40) -> None:
    """From now on ETH falls 40 bps, then recovers 30 bps, bar after bar: a decline that is not a hostile regime."""
    series = w.exchange.series['ETH/USDT']
    start = series.index(w.clock.minute() - 1)
    price, closes = float(Decimal(held['entry_price'])), []
    for k in range(len(series.closes) - start):
        if k < bars:
            price *= math.exp((pattern[0] if k % 2 == 0 else pattern[1]) / 1e4)
        closes.append(price)
    series.closes[start:] = closes


def test_after_a_partial_stop_sale_the_stop_counts_the_realized_loss_and_sells_the_rest(world):
    w = world()
    held = w.run_until_state('HOLDING')
    cycle_id = held['cycle_id']
    _noisy_decline(w, held)
    w.venue().partial_next = Decimal('0.5')                 # the stop sale fills only half
    assert w.run(40, until=lambda: int(micro.load_cycle(cycle_id)['exit_count'] or 0) >= 1)
    first = micro.load_cycle(cycle_id)
    assert (first['state'], first['last_exit_trigger']) == ('HOLDING', 'stop_loss')
    # The stop is an amount for the whole cycle: with half of it lost already, the rest goes within a bar or two.
    assert w.run(3, until=lambda: micro.load_cycle(cycle_id)['state'] == 'CLOSED')
    closed = micro.load_cycle(cycle_id)
    assert closed['last_exit_trigger'] == 'stop_loss' and int(closed['exit_count']) == 2
    cap = money.mul(Decimal(closed['budget_cap']), money.bps(Decimal(str(w.engine.cfg['micro_max_loss_bps']))))
    assert Decimal(closed['realized_net']) > -cap
    assert_ledger_consistent(w)


def _first_sale_fills(w: World, fraction: str) -> tuple[dict, dict]:
    """A HOLDING ETH cycle whose first sale fills only ``fraction``: (the cycle after that sale, the sale's trade)."""
    held = w.run_until_state('HOLDING')
    w.venue().partial_next = Decimal(fraction)
    assert w.run(120, until=lambda: int(micro.load_cycle(held['cycle_id'])['exit_count'] or 0) >= 1)
    sales = [t for t in w.trades() if t['side'] == 'SELL']
    assert len(sales) == 1
    return micro.load_cycle(held['cycle_id']), sales[0]


def test_a_partial_exit_whose_rest_is_below_the_minimum_order_cost_closes_at_once_and_keeps_it_as_residual(world,
                                                                                                         monkeypatch):
    w = world()
    mgr = w.engine.micro
    closes, real_after_close = [], mgr._after_close
    monkeypatch.setattr(mgr, '_after_close', lambda cycle: (closes.append(cycle['cycle_id']), real_after_close(cycle)))
    # 97 % fills: the rest passes the minimum amount, but its value (about 0.75 USDT) is below the 1 USDT market minimum.
    after, sale = _first_sale_fills(w, '0.97')
    rules = mgr.rules('ETH/USDT')
    rest = Decimal(after['owned_base'])
    assert rest >= rules.min_amount and money.mul(rest, Decimal(repr(sale['price']))) < rules.market_min_cost()
    # No sale of that rest can ever be sent: the cycle closes now and keeps it as its residual, as _sell would.
    assert (after['state'], int(after['exit_count'])) == ('CLOSED', 1)
    assert (after['residual_base'], after['residual_basis']) == (after['owned_base'], after['basis_quote'])
    assert Decimal(after['residual_basis']) > 0 and closes == [after['cycle_id']]
    assert money_close(_eth_position()[0]['qty'], rest, Decimal('1e-12'))
    assert [i['status'] for i in w.intents(after['cycle_id']) if i['side'] == 'SELL'] == ['accounted']
    assert 'ETH/USDT' in mgr.owned_symbols() and 'ETH/USDT' not in mgr.open_symbols()
    assert {'symbol': 'ETH/USDT', 'base': after['residual_base'],
            'basis': after['residual_basis']} in mgr.status()['results']['residuals']
    assert_ledger_consistent(w)


def test_a_partial_exit_whose_rest_can_still_be_sold_keeps_it_managed_until_it_is_sold(world):
    w = world()
    # 94 % fills: the rest (about 1.5 USDT) is above the 1 USDT market minimum.
    after, sale = _first_sale_fills(w, '0.94')
    rest = Decimal(after['owned_base'])
    assert money.mul(rest, Decimal(repr(sale['price']))) >= w.engine.micro.rules('ETH/USDT').market_min_cost()
    assert (after['state'], after['residual_base']) == ('HOLDING', '0')
    assert w.run(60, until=lambda: micro.load_cycle(after['cycle_id'])['state'] == 'CLOSED')
    closed = micro.load_cycle(after['cycle_id'])
    assert (int(closed['exit_count']), closed['residual_base']) == (2, '0') and _eth_position() == []
    assert_ledger_consistent(w)


def _last_event(cycle_id: str) -> dict:
    return storage.rows('SELECT to_state, reason FROM micro_cycle_events WHERE cycle_id=? ORDER BY id DESC LIMIT 1',
                        (cycle_id,))[0]


UNSELLABLE_CLOSE = 'only an unsellable remainder is left; retained as residual'


def _one_usdt_markets(minute: int) -> list[SeriesMarket]:
    """MEXC-shaped pairs whose limit-order and market-order minimums are both 1 USDT, in 1e-8 steps."""
    def market(symbol: str) -> dict:
        return mexc_market(symbol, amount_step=0.00000001, price_tick=0.01, limit_min=1.0, market_min='1')
    return [SeriesMarket('ETH/USDT', minute - 300, mechanics(), market=market('ETH/USDT')),
            SeriesMarket('BTC/USDT', minute - 300, flat(900, base=60000.0), market=market('BTC/USDT'))]


def test_a_partial_exit_whose_rest_meets_the_minimum_at_the_sales_best_bid_stays_managed_and_is_sold(world,
                                                                                                    monkeypatch):
    w = world(_one_usdt_markets)
    held = w.run_until_state('HOLDING')
    mgr, venue = w.engine.micro, w.venue()
    real, priced = venue.create_order, {}

    def shaped(symbol, type_, side, amount, price=None, params=None):
        # The first sale leaves a rest worth at least 1 USDT at the best bid it is priced on, less at its fill price.
        if not priced and symbol == 'ETH/USDT':
            bid = Decimal(str(params['book'].best_bid))
            received = money.mul(bid, money.sub(money.ONE, money.bps(Decimal(str(params['adverse_bps'])))))
            rest = ((1 / bid + 1 / received) / 2).quantize(Decimal('1e-8'))
            venue.partial_next = (Decimal(str(amount)) - rest) / Decimal(str(amount))
            priced['bid'] = bid
        return real(symbol, type_, side, amount, price=price, params=params)
    monkeypatch.setattr(venue, 'create_order', shaped)
    assert w.run(120, until=lambda: int(micro.load_cycle(held['cycle_id'])['exit_count'] or 0) >= 1)
    after = micro.load_cycle(held['cycle_id'])
    sale = [t for t in w.trades() if t['side'] == 'SELL'][0]
    rest, bid = Decimal(after['owned_base']), priced['bid']
    minimum = mgr.rules('ETH/USDT').market_min_cost()
    assert money.mul(rest, bid) >= minimum > money.mul(rest, Decimal(repr(sale['price'])))
    assert mgr._sellable('ETH/USDT', rest, bid)            # _sell and the venue would send and accept a sale of it
    # The rest is judged at the sale's best bid: it stays managed, and a later exit sells it.
    assert (after['state'], after['residual_base']) == ('HOLDING', '0')
    # From here ETH keeps swinging, its lows 0.5 % above the bid the sale was priced on.
    series = w.exchange.series['ETH/USDT']
    start = series.index(w.clock.minute())
    swing = mechanics(len(series.closes) - start)
    series.closes[start:] = [c * float(bid) * 1.005 / min(swing) for c in swing]
    refused = []

    def sold() -> bool:
        refused.append('outside the micro cycles' in json.dumps(mgr.pairs.get('ETH/USDT') or {}, default=str))
        return micro.load_cycle(after['cycle_id'])['state'] == 'CLOSED'
    assert w.run(60, until=sold)
    closed = micro.load_cycle(after['cycle_id'])
    assert closed['last_exit_trigger'] != 'unsellable' and _last_event(closed['cycle_id'])['reason'] != UNSELLABLE_CLOSE
    assert (int(closed['exit_count']), closed['residual_base']) == (2, '0')
    assert len([t for t in w.trades() if t['side'] == 'SELL']) == 2
    # Nothing is left on the pair, so no later entry there is refused as 'already held outside the micro cycles'.
    assert _eth_position() == [] and 'ETH/USDT' not in mgr.owned_symbols() and not any(refused)
    assert_ledger_consistent(w)


def test_an_exit_recovery_keeps_a_rest_below_the_minimum_cost_and_the_next_exit_check_closes_it(world, monkeypatch):
    w = world()
    mgr = w.engine.micro
    held = w.run_until_state('HOLDING')
    w.engine.running = False                                  # stopped: no pass will follow
    armed = _fail_sell_booking_once(monkeypatch, held['cycle_id'])
    w.venue().partial_next = Decimal('0.97')                  # the sale fills 97 % and its booking fails
    assert mgr.flatten() == {'queued': False, 'attempted': ['ETH/USDT']}
    assert not armed['on'] and micro.load_cycle(held['cycle_id'])['state'] == 'EXIT_UNKNOWN'
    assert mgr.apply_recovery(held['cycle_id'], REVIEW)['outcome'] == 'exit fill booked from the venue record'
    recovered = micro.load_cycle(held['cycle_id'])
    rules, rest = mgr.rules('ETH/USDT'), Decimal(recovered['owned_base'])
    sales = [t for t in w.trades() if t['side'] == 'SELL']
    # The rest passes the amount rules; its value (about 0.75 USDT) is below the 1 USDT market minimum.
    assert len(sales) == 1 and rest >= rules.min_amount
    assert money.mul(rest, Decimal(repr(sales[0]['price']))) < rules.market_min_cost()
    # The recovery has no current bid: it judges only the amount rules and leaves the rest held.
    assert (recovered['state'], recovered['residual_base'], int(recovered['exit_count'])) == ('HOLDING', '0', 1)
    assert_ledger_consistent(w)
    w.go()
    assert w.run(1, until=lambda: micro.load_cycle(held['cycle_id'])['state'] == 'CLOSED')
    closed = micro.load_cycle(held['cycle_id'])
    # The next exit check closes it through _sell, at the current best bid, without sending anything.
    assert _last_event(closed['cycle_id']) == {'to_state': 'CLOSED', 'reason': UNSELLABLE_CLOSE}
    assert (closed['residual_base'], closed['residual_basis']) == (recovered['owned_base'], recovered['basis_quote'])
    assert Decimal(closed['residual_basis']) > 0 and int(closed['exit_count']) == 1
    assert len([t for t in w.trades() if t['side'] == 'SELL']) == 1
    assert len([i for i in w.intents(closed['cycle_id']) if i['side'] == 'SELL']) == 1
    assert money_close(_eth_position()[0]['qty'], rest, Decimal('1e-12'))
    assert_ledger_consistent(w)


def test_a_held_rest_that_falls_below_the_minimum_at_the_bid_closes_at_the_next_exit_check_without_a_trigger(
        world, monkeypatch):
    w = world()
    mgr = w.engine.micro
    closes, real_after_close = [], mgr._after_close
    monkeypatch.setattr(mgr, '_after_close', lambda cycle: (closes.append(cycle['cycle_id']), real_after_close(cycle)))
    # 95 % fills: the rest (about 1.26 USDT) is above the 1 USDT market minimum and stays managed.
    after, sale = _first_sale_fills(w, '0.95')
    rest = Decimal(after['owned_base'])
    assert (after['state'], after['residual_base'], closes) == ('HOLDING', '0', [])
    real_evaluate = micro.strat.evaluate_exit

    def hold(**kwargs):
        return dataclasses.replace(real_evaluate(**kwargs), action='hold', trigger=None, reason='held by the test')
    monkeypatch.setattr(micro.strat, 'evaluate_exit', hold)        # no exit trigger fires from here on
    w.set_closes('ETH/USDT', float(sale['price']) * 0.7)            # the rest is now below the minimum at the bid
    assert w.run(5, until=lambda: micro.load_cycle(after['cycle_id'])['state'] == 'CLOSED')
    closed = micro.load_cycle(after['cycle_id'])
    assert _last_event(closed['cycle_id']) == {'to_state': 'CLOSED', 'reason': UNSELLABLE_CLOSE}
    assert int(closed['exit_count']) == 1
    assert [i['status'] for i in w.intents(closed['cycle_id']) if i['side'] == 'SELL'] == ['accounted']
    assert len([t for t in w.trades() if t['side'] == 'SELL']) == 1
    assert (closed['residual_base'], closed['residual_basis']) == (after['owned_base'], after['basis_quote'])
    assert Decimal(closed['residual_basis']) > 0 and closes == [closed['cycle_id']]
    assert money_close(_eth_position()[0]['qty'], rest, Decimal('1e-12'))
    assert 'ETH/USDT' in mgr.owned_symbols() and 'ETH/USDT' not in mgr.open_symbols()
    assert_ledger_consistent(w)


@pytest.mark.parametrize('rest, best_bid, state', [
    ('0.000399', '2510', 'HOLDING'),   # 0.9975 USDT at the 2500 fill price, 1.0015 USDT at the sale's best bid
    ('0.000399', '2504', 'CLOSED'),    # 0.9991 USDT at the sale's best bid: no sale of it could be sent
    ('0.000399', None, 'HOLDING'),     # no book (exit recovery): the amount rules only, the cost rule comes later
    ('0.000099', None, 'CLOSED'),      # below the 0.0001 minimum amount
])
def test_a_partial_exit_judges_its_rest_at_the_sales_best_bid_and_without_a_book_by_the_amount_rules(
        world, rest, best_bid, state):
    import types
    w = world()
    held = w.run_until_state('HOLDING')
    rest = Decimal(rest)
    sold = Decimal(held['owned_base']) - rest
    gross = money.mul(sold, Decimal('2500'))
    record = {'id': 'paper-rest-pricing', 'filled': float(sold), 'cost': float(gross),
              'fee': {'cost': float(money.mul(gross, Decimal('0.0005'))), 'currency': 'USDT'}}
    with storage.connection() as db:
        cycle = micro.transition(db, micro.load_cycle(held['cycle_id']), 'EXIT_SUBMITTED', 'test sale')
    known = {} if best_bid is None else {'book': types.SimpleNamespace(best_bid=Decimal(best_bid))}
    assert w.engine.micro._book_exit(cycle, None, record, trigger='test', reason='rest pricing', **known)
    after = micro.load_cycle(held['cycle_id'])
    assert after['state'] == state and Decimal(after['owned_base']) == rest
    assert Decimal(after['residual_base']) == (rest if state == 'CLOSED' else 0)
    assert_ledger_consistent(w)


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


def _falling_eth(minute: int) -> list[SeriesMarket]:
    return [SeriesMarket('ETH/USDT', minute - 300, trend(900, base=2500.0, bps_per_minute=-6.0),
                         market=fine_market('ETH/USDT'))]


def _book_reads(w: World, symbol: str = 'ETH/USDT') -> int:
    return sum(1 for call in w.exchange.calls if call[0] == 'fetch_order_book' and call[1] == symbol)


def test_a_pair_with_no_possible_setup_reads_its_book_for_each_new_decision_and_otherwise_every_30_s(world):
    """The idle cadence: a new bar's decision reads the book even inside the idle interval, never STALE_DATA from it.

    The fixture clock also adds the real time the ticks take, so every boundary below keeps several seconds of
    margin rather than relying on exact steps.
    """
    w = world(markets=_falling_eth, watchlist=['ETH/USDT'])
    mgr = w.engine.micro
    w.clock.advance(60 - (w.clock.time() % 60) + 45)             # 45 s into a bar: the context is current
    w.tick()
    first = mgr.pairs['ETH/USDT']['decision']
    assert mgr.context['ETH/USDT']['current'] is True
    assert first['action'] == 'abstain' and first['code'] not in ('STALE_DATA', 'DEPTH_UNKNOWN')
    assert mgr.pairs['ETH/USDT']['regime'] not in ('trending_up', 'ranging')        # no setup is possible
    reads = _book_reads(w)
    assert reads >= 1
    for _ in range(10):                                        # inside the interval: no read, the decision is kept
        w.clock.advance(1.0)
        w.tick()
        assert mgr.pairs['ETH/USDT']['decision']['decision_id'] == first['decision_id']
    assert _book_reads(w) == reads
    w.clock.advance(60 - (w.clock.time() % 60) + 0.5)          # just after the bar closed: candles not current yet
    w.tick()
    assert mgr.context['ETH/USDT']['current'] is False
    assert mgr.pairs['ETH/USDT']['decision']['code'] == 'STALE_DATA'
    assert _book_reads(w) == reads                             # stale candles decide alone: no book read
    w.clock.advance(micro.CONTEXT_RETRY_S + 1.0)               # the new bar's candles are read
    w.tick()
    assert mgr.context['ETH/USDT']['current'] is True
    decided = mgr.pairs['ETH/USDT']['decision']
    assert _book_reads(w) == reads + 1                         # read now, about 8 s before the idle read was due
    assert decided['code'] not in ('STALE_DATA', 'DEPTH_UNKNOWN') and decided['decision_id'] != first['decision_id']
    reads += 1
    for _ in range(int(micro.IDLE_BOOK_SECONDS) - 6):          # the next idle read is due 30 s after that one
        w.clock.advance(1.0)
        w.tick()
        assert mgr.pairs['ETH/USDT']['decision']['decision_id'] == decided['decision_id']
    assert _book_reads(w) == reads                             # one forced read did not bring a second one
    w.clock.advance(7.0)
    w.tick()
    assert _book_reads(w) == reads + 1
    assert mgr.pairs['ETH/USDT']['decision']['code'] not in ('STALE_DATA', 'DEPTH_UNKNOWN')
    assert not w.cycles()


def test_a_residual_on_a_selected_pair_with_no_setup_keeps_its_mark_inside_the_age_limit(world, monkeypatch):
    """The idle entry cadence (30 s) must not delay a residual's mark, which the risk gate needs within 30 s here.

    The residual is stood in for by the ownership query: a closed cycle's residual makes a pair owned without
    an open cycle (the real path is in the deselected-pair test above).
    """
    # 33 s gives an 11 s mark interval, so the residual reads fall at a different point of each bar and meet the
    # new-bar reads at every offset within a few bars.
    w = world(markets=_falling_eth, watchlist=['ETH/USDT'], data_max_age_seconds=33)
    mgr = w.engine.micro
    monkeypatch.setattr(mgr, 'owned_symbols', lambda: {'ETH/USDT'})
    monkeypatch.setattr(mgr, 'open_symbols', lambda: set())
    interval = 33 / 3
    w.clock.advance(60 - (w.clock.time() % 60) + 1)
    worst = 0.0
    for _ in range(11 * 60):                                   # about eleven bars of one-second ticks
        w.clock.advance(1.0)
        w.tick()
        worst = max(worst, w.engine._price_age('ETH/USDT'))
    # A book read within its age limit counts as the mark's refresh, so the bound is the interval plus that limit.
    assert worst <= interval + float(w.engine.cfg['micro_book_max_age_seconds']) + 2.0 < 33, worst
    w.clock.advance(60 - (w.clock.time() % 60) + 30)           # mid-bar on the fixture clock, whatever the host speed
    w.tick()
    assert mgr.pairs['ETH/USDT']['decision']['code'] not in ('STALE_DATA', 'DEPTH_UNKNOWN')


def test_a_residual_on_a_plausible_pair_leaves_its_3_s_book_cadence_alone(world, monkeypatch):
    """The residual's mark schedule must not hold back the reads of a pair whose setup is plausible."""
    w = world(markets=_flat_eth, watchlist=['ETH/USDT'], data_max_age_seconds=33)
    mgr = w.engine.micro
    monkeypatch.setattr(mgr, 'owned_symbols', lambda: {'ETH/USDT'})
    monkeypatch.setattr(mgr, 'open_symbols', lambda: set())
    w.clock.advance(60 - (w.clock.time() % 60) + 1)
    w.tick()
    assert mgr.pairs['ETH/USDT']['regime'] in ('trending_up', 'ranging')            # a setup is plausible
    last_read, reads, gaps, worst = w.clock.monotonic(), _book_reads(w), [], 0.0
    for step in [1.0, 1.0, 1.5] * 130:                       # uneven steps, as real ticks are
        w.clock.advance(step)
        w.tick()
        if _book_reads(w) > reads:
            gaps.append(w.clock.monotonic() - last_read)
            last_read, reads = w.clock.monotonic(), _book_reads(w)
        worst = max(worst, w.engine._price_age('ETH/USDT'))
    assert not w.cycles()
    assert max(gaps) <= micro.ENTRY_BOOK_SECONDS + 1.6, max(gaps)    # one tick step past the 3 s cadence at most
    assert worst <= 33 / 3 + 2.0, worst                               # the 3 s reads keep the mark fresh too

def test_a_forced_read_reuses_only_a_book_under_half_its_age_limit(world):
    """A reused book must still be fresh when the decision checks it; an older one is read again."""
    w = world(markets=_falling_eth, watchlist=['ETH/USDT'])
    mgr = w.engine.micro
    max_age = float(w.engine.cfg['micro_book_max_age_seconds'])
    first = mgr._book('ETH/USDT', interval=micro.IDLE_BOOK_SECONDS, max_age=max_age, idle=True, force=True)
    reads = _book_reads(w)
    w.clock.advance(max_age / 2 - 1.0)
    assert mgr._book('ETH/USDT', interval=micro.IDLE_BOOK_SECONDS, max_age=max_age, idle=True,
                     force=True).generation == first.generation
    assert _book_reads(w) == reads
    w.clock.advance(1.5)                                       # past half the limit, still inside it
    again = mgr._book('ETH/USDT', interval=micro.IDLE_BOOK_SECONDS, max_age=max_age, idle=True, force=True)
    assert _book_reads(w) == reads + 1 and again.generation > first.generation
    assert mgr._book('ETH/USDT', interval=micro.IDLE_BOOK_SECONDS, max_age=max_age, idle=True) is again

def test_after_a_memory_reset_an_idle_pair_reads_its_book_for_its_next_decision(world):
    w = world(markets=_falling_eth, watchlist=['ETH/USDT'])
    mgr = w.engine.micro
    w.clock.advance(60 - (w.clock.time() % 60) + 20)
    w.tick()
    reads = _book_reads(w)
    w.clock.advance(8.0)                                       # the book is past its 5 s limit; the idle read is 22 s away
    mgr.reset_memory()
    w.tick()
    assert _book_reads(w) == reads + 1
    assert mgr.pairs['ETH/USDT']['decision']['code'] not in ('STALE_DATA', 'DEPTH_UNKNOWN')

# ===================================================================== integration review regressions
def _partial_exit(w: World, fraction: str = '0.4') -> str:
    """A HOLDING cycle that already sold part of its holding (one SELL intent, accounted)."""
    held = w.run_until_state('HOLDING')
    w.venue().partial_next = Decimal(fraction)
    assert w.run(120, until=lambda: int(micro.load_cycle(held['cycle_id'])['exit_count'] or 0) >= 1)
    cycle = micro.load_cycle(held['cycle_id'])
    assert cycle['state'] == 'HOLDING'
    assert [i['status'] for i in w.intents(cycle['cycle_id']) if i['side'] == 'SELL'] == ['accounted']
    return cycle['cycle_id']


def _no_order_booked_twice(w: World) -> None:
    ids = [t['order_id'] for t in w.trades() if t['order_id'] and 'late fill' not in (t['reason'] or '')]
    assert len(ids) == len(set(ids)), 'one venue order was booked twice'


def test_an_exit_that_dies_before_its_intent_never_rebooks_the_earlier_partial_sale(world, monkeypatch):
    w = world()
    cycle_id = _partial_exit(w)
    cash, trades = w.cash(), len(w.trades())
    real = storage.create_order_intent

    def die(*args, **kwargs):
        raise Crash()                         # the process dies before the next exit's intent is written
    monkeypatch.setattr(storage, 'create_order_intent', die)
    with pytest.raises(Crash):
        w.run(60)
    monkeypatch.setattr(storage, 'create_order_intent', real)
    assert micro.load_cycle(cycle_id)['state'] == 'EXIT_SUBMITTED'
    engine = w.restart()
    # The accounted intent belongs to the earlier sale: the interrupted attempt sent nothing.
    after = micro.load_cycle(cycle_id)
    assert after['state'] == 'HOLDING' and engine.micro.review_block() == []
    assert any('never sent' in item['explanation'] for item in engine.micro.recovery if item.get('cycle_id') == cycle_id)
    assert w.cash() == cash and len(w.trades()) == trades
    _no_order_booked_twice(w)
    assert_ledger_consistent(w)


def test_stop_between_the_gate_and_the_submit_of_a_second_exit_returns_to_holding(world, monkeypatch):
    w = world()
    cycle_id = _partial_exit(w)
    cash, trades = w.cash(), len(w.trades())
    real = w.engine._authorize

    def authorize_then_stop(*args, **kwargs):
        result = real(*args, **kwargs)
        if args[1] == 'SELL' and result.allowed:
            w.engine.stop_event.set()         # Stop pressed while the worker is between the gate and the submit
        return result
    monkeypatch.setattr(w.engine, '_authorize', authorize_then_stop)
    w.run(60, until=lambda: w.engine.stop_event.is_set())
    after = micro.load_cycle(cycle_id)
    assert after['state'] == 'HOLDING' and w.engine.micro.review_block() == []
    assert w.cash() == cash and len(w.trades()) == trades
    _no_order_booked_twice(w)
    assert_ledger_consistent(w)


def test_booking_refuses_a_venue_order_that_is_already_in_the_ledger(world):
    w = world()
    cycle_id = _partial_exit(w)
    first_sale = next(i for i in w.intents(cycle_id) if i['side'] == 'SELL')
    record = w.venue().fetch_order(None, 'ETH/USDT', {'clientOrderId': first_sale['client_order_id']})
    cash, trades = w.cash(), len(w.trades())
    assert w.engine.micro._book_exit(micro.load_cycle(cycle_id), None, record, trigger='test', reason='again') is False
    assert w.cash() == cash and len(w.trades()) == trades
    assert 'MICRO SELL' in (w.engine.risk_block or '')


def _fail_sell_booking_once(monkeypatch, cycle_id: str) -> dict:
    real = storage.update_order_intent
    armed = {'on': True}

    def flaky(intent_id, status, *args, **kwargs):
        db = kwargs.get('db')
        if armed['on'] and status == 'accounted' and db is not None:
            row = db.execute('SELECT side, decision_id FROM order_journal WHERE intent_id=?', (intent_id,)).fetchone()
            if row['decision_id'] == cycle_id and row['side'] == 'SELL':
                armed['on'] = False
                raise sqlite3.OperationalError('disk I/O error')     # the booking transaction rolls back
        return real(intent_id, status, *args, **kwargs)
    monkeypatch.setattr(storage, 'update_order_intent', flaky)
    return armed


def test_a_booking_failure_while_running_is_settled_on_the_next_pass_and_blocks_entries(world, monkeypatch):
    w = world(two_setups, micro_max_cycles=2)
    held = w.run_until_state('HOLDING', symbol='ETH/USDT')
    armed = _fail_sell_booking_once(monkeypatch, held['cycle_id'])
    w.run(120, until=lambda: not armed['on'])
    assert micro.load_cycle(held['cycle_id'])['state'] == 'EXIT_SUBMITTED'
    # Contained: the worker keeps running, so every other cycle keeps its exits.
    assert not w.engine.stop_event.is_set() and 'MICRO SELL' in w.engine.risk_block
    before = len(w.cycles())
    w.run(30)
    unknown = micro.load_cycle(held['cycle_id'])
    assert unknown['state'] == 'EXIT_UNKNOWN'
    assert [b['cycle_id'] for b in w.engine.micro.review_block()] == [held['cycle_id']]
    assert len(w.cycles()) == before                         # no new cycle while a filled sale is unbooked
    w.engine.running = False
    result = w.engine.micro.apply_recovery(held['cycle_id'], REVIEW)
    assert result['outcome'] == 'exit fill booked from the venue record'
    assert micro.load_cycle(held['cycle_id'])['state'] in ('CLOSED', 'HOLDING')
    _no_order_booked_twice(w)
    assert_ledger_consistent(w)


def _record_buys(w: World, monkeypatch) -> list:
    """Each BUY sent, as (pass, symbol, other cycles mid-operation or blocking, micro stop engaged)."""
    sent = []
    real = w.engine._submit

    def recording(client, symbol, side, **kwargs):
        if side == 'BUY':
            others = [(c['symbol'], c['state']) for c in micro.open_cycles(w.engine.exchange_name, 'paper')
                      if c['symbol'] != symbol and c['state'] in micro.BLOCKING_STATES + micro.TRANSIENT_STATES]
            sent.append((w.engine.micro.tick_count, symbol, others, w.engine.micro.latch() is not None))
        return real(client, symbol, side, **kwargs)
    monkeypatch.setattr(w.engine, '_submit', recording)
    return sent


def test_a_booking_failure_in_an_entry_pass_stops_the_next_pair_entering_in_that_pass(world, monkeypatch):
    w = world(two_setups, micro_max_cycles=2)
    sent = _record_buys(w, monkeypatch)
    real = storage.update_order_intent
    armed = {'on': True}

    def flaky(intent_id, status, *args, **kwargs):
        db = kwargs.get('db')
        if armed['on'] and status == 'accounted' and db is not None:
            if db.execute('SELECT side FROM order_journal WHERE intent_id=?', (intent_id,)).fetchone()['side'] == 'BUY':
                armed['on'] = False
                raise sqlite3.OperationalError('disk I/O error')     # the first entry's booking rolls back
        return real(intent_id, status, *args, **kwargs)
    monkeypatch.setattr(storage, 'update_order_intent', flaky)
    w.run(120, until=lambda: not armed['on'])
    stranded = w.cycles()[0]
    assert stranded['state'] == 'ENTRY_SUBMITTED' and not w.engine.stop_event.is_set()
    assert len(w.cycles()) == 1                  # no other pair entered in the pass that left a fill unbooked
    assert [buy for buy in sent if buy[2]] == []
    other = ({'ETH/USDT', 'BTC/USDT'} - {stranded['symbol']}).pop()
    assert w.engine.micro.pairs[other]['decision']['code'] == 'REVIEW'      # its gates showed the block
    w.run(30)
    assert micro.load_cycle(stranded['cycle_id'])['state'] == 'ENTRY_UNKNOWN' and len(w.cycles()) == 1


def test_an_unknown_entry_outcome_stops_the_next_pair_entering_in_that_pass(world, monkeypatch):
    w = world(two_setups, micro_max_cycles=2)
    sent = _record_buys(w, monkeypatch)
    w.venue().drop_response_next = True                       # the first order fills, its answer is lost ...
    monkeypatch.setattr(w.engine, '_locate_after_ambiguity', lambda *a, **k: None)    # ... and is not found
    assert w.run(120, until=lambda: bool(w.cycles('ENTRY_UNKNOWN')))
    assert len(w.cycles()) == 1 and len(w.venue().records()) == 1
    assert [buy for buy in sent if buy[2]] == []


def test_a_micro_stop_latched_by_the_exit_check_between_pairs_blocks_the_next_pair_in_that_pass(world, monkeypatch):
    def eth_flat_btc_setup(minute):
        return [SeriesMarket('ETH/USDT', minute - 300, flat(900, base=2500.0), market=fine_market('ETH/USDT')),
                SeriesMarket('BTC/USDT', minute - 300, mechanics(base=60000.0), market=fine_market('BTC/USDT'))]
    w = world(eth_flat_btc_setup, micro_max_cycles=2)
    assert list(captured_entries(w, monkeypatch, 1)) == ['BTC/USDT']    # BTC decides to enter on this bar
    real_entries, real_exits = micro.MicroManager._entries, micro.MicroManager._manage_exits
    inside = {'entries': False}

    def entries(self, params, selected):
        inside['entries'] = True
        try:
            return real_entries(self, params, selected)
        finally:
            inside['entries'] = False

    def exits(self, params, *, only_due=False):
        real_exits(self, params, only_due=only_due)
        if inside['entries']:
            # Stands for a held cycle's losing close, booked by the check between pairs, reaching a loss limit.
            self._engage_latch('test: a losing close reached the micro loss limit')
    monkeypatch.setattr(micro.MicroManager, '_entries', entries)
    monkeypatch.setattr(micro.MicroManager, '_manage_exits', exits)
    sent = _record_buys(w, monkeypatch)
    w.tick()                                                   # the same bar, BTC's setup unchanged
    assert w.engine.micro.latch() is not None
    assert w.cycles() == [] and sent == []
    assert w.engine.micro.pairs['BTC/USDT']['decision']['code'] == 'RISK'   # decided on the latched gate


def test_a_cycle_interrupted_mid_exit_can_be_recovered_while_stopped(world, monkeypatch):
    w = world()
    held = w.run_until_state('HOLDING')
    armed = _fail_sell_booking_once(monkeypatch, held['cycle_id'])
    w.run(120, until=lambda: not armed['on'])
    assert micro.load_cycle(held['cycle_id'])['state'] == 'EXIT_SUBMITTED'
    w.engine.running = False                                  # stopped; no further pass has run
    result = w.engine.micro.apply_recovery(held['cycle_id'], REVIEW)
    assert result['outcome'] == 'exit fill booked from the venue record'
    assert micro.load_cycle(held['cycle_id'])['state'] == 'CLOSED'
    _no_order_booked_twice(w)
    assert_ledger_consistent(w)


def test_a_booking_failure_during_a_stopped_exit_all_is_explained_at_once_and_recoverable(world, monkeypatch):
    w = world()
    held = w.run_until_state('HOLDING')
    w.engine.running = False                                  # stopped: no pass will follow
    armed = _fail_sell_booking_once(monkeypatch, held['cycle_id'])
    assert w.engine.micro.flatten() == {'queued': False, 'attempted': ['ETH/USDT']}
    assert not armed['on'] and [r['side'] for r in w.venue().records()] == ['buy', 'sell']
    assert micro.load_cycle(held['cycle_id'])['state'] == 'EXIT_UNKNOWN'      # not left as "Exit sent"
    assert [b['cycle_id'] for b in w.engine.micro.review_block()] == [held['cycle_id']]
    result = w.engine.micro.apply_recovery(held['cycle_id'], REVIEW)
    assert result['outcome'] == 'exit fill booked from the venue record'
    assert micro.load_cycle(held['cycle_id'])['state'] == 'CLOSED'
    _no_order_booked_twice(w)
    assert_ledger_consistent(w)


def test_a_failure_escaping_a_stopped_exit_all_after_the_fill_is_explained_at_once(world, monkeypatch):
    w = world()
    held = w.run_until_state('HOLDING')
    w.engine.running = False
    real = storage.order_intent
    armed = {'on': True}

    def flaky(intent_id, *args, **kwargs):
        if armed['on']:                         # the first read is _sell's, after the venue fill
            armed['on'] = False
            raise RuntimeError('journal read failed')
        return real(intent_id, *args, **kwargs)
    monkeypatch.setattr(storage, 'order_intent', flaky)
    with pytest.raises(RuntimeError):
        w.engine.micro.flatten()
    monkeypatch.setattr(storage, 'order_intent', real)
    assert [r['side'] for r in w.venue().records()] == ['buy', 'sell']
    assert micro.load_cycle(held['cycle_id'])['state'] == 'EXIT_UNKNOWN'
    assert [b['cycle_id'] for b in w.engine.micro.review_block()] == [held['cycle_id']]
    assert w.engine.micro.apply_recovery(held['cycle_id'], REVIEW)['outcome'] == 'exit fill booked from the venue record'
    _no_order_booked_twice(w)
    assert_ledger_consistent(w)


def test_an_interrupted_cycle_the_check_cannot_explain_is_listed_for_recovery_while_stopped(world, monkeypatch):
    w = world()
    held = w.run_until_state('HOLDING')
    w.engine.running = False
    _fail_sell_booking_once(monkeypatch, held['cycle_id'])
    real = micro.MicroManager._recover_cycle

    def broken(self, cycle):
        raise RuntimeError('recovery check failed')
    monkeypatch.setattr(micro.MicroManager, '_recover_cycle', broken)
    w.engine.micro.flatten()
    assert micro.load_cycle(held['cycle_id'])['state'] == 'EXIT_SUBMITTED'
    blockers = w.engine.micro.review_block()                 # stopped and idle: nothing can still be running
    assert [(b['cycle_id'], b.get('interrupted')) for b in blockers] == [(held['cycle_id'], True)]
    w.go()
    assert w.engine.micro.review_block() == []               # while running, a mid-operation state is normal
    w.engine.running = False
    with pytest.raises(storage.ConfigValidationError, match='could not be checked'):
        w.engine.micro.apply_recovery(held['cycle_id'], REVIEW)
    assert micro.load_cycle(held['cycle_id'])['state'] == 'EXIT_SUBMITTED'
    monkeypatch.setattr(micro.MicroManager, '_recover_cycle', real)
    assert w.engine.micro.apply_recovery(held['cycle_id'], REVIEW)['outcome'] == 'exit fill booked from the venue record'
    _no_order_booked_twice(w)
    assert_ledger_consistent(w)


def test_an_interrupted_cycle_is_listed_with_the_venue_record_its_one_step_recovery_books(world, monkeypatch):
    w = world()
    held = w.run_until_state('HOLDING')
    w.engine.running = False
    _fail_sell_booking_once(monkeypatch, held['cycle_id'])
    real = micro.MicroManager._recover_cycle

    def broken(self, cycle):
        raise RuntimeError('recovery check failed')
    monkeypatch.setattr(micro.MicroManager, '_recover_cycle', broken)
    w.engine.micro.flatten()
    monkeypatch.setattr(micro.MicroManager, '_recover_cycle', real)
    assert micro.load_cycle(held['cycle_id'])['state'] == 'EXIT_SUBMITTED'
    sale = [i for i in w.intents(held['cycle_id']) if i['side'] == 'SELL'][-1]
    exact = w.venue().fetch_order(None, 'ETH/USDT', {'clientOrderId': sale['client_order_id']})['info']['exact']

    def shown() -> str:
        [item] = [b for b in w.engine.micro.status()['review_block'] if b['cycle_id'] == held['cycle_id']]
        assert item.get('interrupted') is True
        return item['explanation']
    venue = w.venue()
    real_fetch = venue.fetch_order
    monkeypatch.setattr(venue, 'fetch_order', _unreadable)
    assert 'could not be read' in shown() and 'refused' in shown()       # never shown as no fill
    monkeypatch.setattr(venue, 'fetch_order', real_fetch)
    # Before the one press of Apply recovery, the panel (and its confirmation) states the record that press books.
    assert f'{exact["filled"]} filled for {exact["cost"]}' in shown() and 'books exactly that' in shown()
    assert micro.load_cycle(held['cycle_id'])['state'] == 'EXIT_SUBMITTED'   # reading it changed nothing
    assert w.engine.micro.apply_recovery(held['cycle_id'], REVIEW)['outcome'] == 'exit fill booked from the venue record'
    assert Decimal(micro.load_cycle(held['cycle_id'])['closed_base']) == Decimal(exact['filled'])
    _no_order_booked_twice(w)
    assert_ledger_consistent(w)


def test_an_interrupted_cycle_that_sent_no_order_is_listed_with_what_its_recovery_does(world, monkeypatch):
    w = world()
    real = w.engine._authorize

    def broken(*args, **kwargs):
        if args[1] == 'BUY':
            raise RuntimeError('gate unavailable')
        return real(*args, **kwargs)
    monkeypatch.setattr(w.engine, '_authorize', broken)
    with pytest.raises(RuntimeError):
        w.run(60)
    monkeypatch.setattr(w.engine, '_authorize', real)
    stranded = w.cycles()[-1]
    assert stranded['state'] == 'ENTRY_RESERVED'
    w.engine.running = False                                  # stopped; no further pass has run
    [item] = w.engine.micro.review_block()
    assert item['interrupted'] is True and 'releases the reservation' in item['explanation']
    result = w.engine.micro.apply_recovery(stranded['cycle_id'], REVIEW)
    assert micro.load_cycle(stranded['cycle_id'])['state'] == 'ABANDONED' and 'released' in result['outcome']


def test_the_worker_exit_settles_interrupted_cycles_and_ends_an_exit_all_request(world, monkeypatch):
    w = world()
    held = w.run_until_state('HOLDING')
    armed = _fail_sell_booking_once(monkeypatch, held['cycle_id'])
    w.run(120, until=lambda: not armed['on'])
    w.engine.micro.request_flatten()
    assert w.engine.micro.flatten_pending() > 0
    w.engine.stop_event.set()
    w.engine._loop()                        # returns at once; its cleanup is what a real worker runs on exit
    assert micro.load_cycle(held['cycle_id'])['state'] == 'EXIT_UNKNOWN'
    assert w.engine.micro.flatten_pending() == 0
    assert any(item.get('cycle_id') == held['cycle_id'] for item in w.engine.micro.recovery)


def test_a_reservation_stranded_by_a_failure_is_released_on_the_next_pass(world, monkeypatch):
    w = world()
    real = w.engine._authorize

    def broken(*args, **kwargs):
        if args[1] == 'BUY':
            raise RuntimeError('gate unavailable')
        return real(*args, **kwargs)
    monkeypatch.setattr(w.engine, '_authorize', broken)
    with pytest.raises(RuntimeError):
        w.run(60)
    stranded = w.cycles()[-1]
    assert stranded['state'] == 'ENTRY_RESERVED' and Decimal(stranded['reserved_quote']) > 0
    monkeypatch.setattr(w.engine, '_authorize', real)
    w.tick()
    assert micro.load_cycle(stranded['cycle_id'])['state'] == 'ABANDONED'
    assert Decimal(micro.load_cycle(stranded['cycle_id'])['reserved_quote']) == 0


def _raise_entry_fill(w: World, cycle_id: str, extra_base=Decimal('0.0004'), extra_cost=Decimal('1.0004'),
                      extra_fee=Decimal('0.0010004')) -> None:
    buy = next(i for i in w.intents(cycle_id) if i['side'] == 'BUY')
    stored = w.venue().fetch_order(None, 'ETH/USDT', {'clientOrderId': buy['client_order_id']})['info']['exact']
    with storage.connection() as db:                # the venue now reports a larger fill on that entry
        db.execute('UPDATE paper_venue_orders SET filled=?, cost=?, fee_cost=? WHERE client_order_id=?',
                   (str(Decimal(stored['filled']) + extra_base), str(Decimal(stored['cost']) + extra_cost),
                    str(Decimal(stored['fee_cost']) + extra_fee), buy['client_order_id']))


def test_a_late_fill_after_the_cycle_closed_blocks_until_booked_and_never_ages_out(world):
    w = world(micro_cooldown_seconds=0)
    w.venue().partial_next = Decimal('0.5')
    held = w.run_until_state('HOLDING')
    closed = w.run_until_state('CLOSED')
    assert closed['cycle_id'] == held['cycle_id']
    _raise_entry_fill(w, closed['cycle_id'])
    cash = w.cash()
    count = len(w.cycles())
    w.run(30)
    assert len(w.cycles()) == count
    blockers = w.engine.micro.review_block()
    assert [(b['cycle_id'], b.get('late_fill')) for b in blockers] == [(closed['cycle_id'], True)]
    # The venue record ages out of the reconciliation window; the block does not.
    w.clock.advance(micro.RECONCILE_WINDOW_S + 60)
    w.run(60)
    assert len(w.cycles()) == count and w.engine.micro.review_block()
    engine = w.restart()                                      # and it survives a restart
    assert engine.micro.review_block()
    result = engine.micro.apply_recovery(closed['cycle_id'], REVIEW)
    assert 'late fill booked' in result['outcome']
    assert engine.micro.review_block() == []
    assert money_close(cash - w.cash(), Decimal('1.0004') + Decimal('0.0010004'))
    after = micro.load_cycle(closed['cycle_id'])
    assert after['state'] == 'CLOSED' and after['late_fill_reason'] is None
    assert money_close(Decimal(after['entry_debit']) - Decimal(closed['entry_debit']), Decimal('1.0014004'))
    adopted = [c for c in w.cycles() if c['strategy'] == 'late_fill_recovery']
    assert len(adopted) == 1 and adopted[0]['state'] == 'HOLDING' and adopted[0]['decision_id'] == closed['cycle_id']
    position = storage.rows("SELECT qty FROM positions WHERE symbol='ETH/USDT' AND mode='paper'")
    assert money_close(position[0]['qty'], adopted[0]['owned_base'], Decimal('1e-12'))
    _no_order_booked_twice(w)
    assert_ledger_consistent(w)
    # The adopted holding is managed like any other: its maximum hold closes it.
    w.go()
    w.run(45, until=lambda: micro.load_cycle(adopted[0]['cycle_id'])['state'] == 'CLOSED')
    assert micro.load_cycle(adopted[0]['cycle_id'])['state'] == 'CLOSED'
    assert_ledger_consistent(w)


def _eth_position() -> list[dict]:
    return storage.rows("SELECT qty, cost FROM positions WHERE symbol='ETH/USDT' AND mode='paper'")


def _manual(engine, side: str, amount=None) -> dict:
    return engine.manual_trade('ETH/USDT', side, amount, request_id=str(uuid.uuid4()),
                               expected_context=engine.manual_order_context)


def test_a_late_fill_after_close_is_never_booked_into_a_position_bought_outside_the_cycles(world):
    w = world(micro_cooldown_seconds=0)
    closed = w.run_until_state('CLOSED')
    assert _eth_position() == []                                  # the cycle sold everything it held
    w.engine.running = False
    w.engine.stop_event.set()
    w.engine.update_config(micro_settings(watchlist=['BTC/USDT']))     # ETH leaves the selection ...
    _manual(w.engine, 'BUY', 50)                                       # ... and is bought by hand
    manual = _eth_position()
    _raise_entry_fill(w, closed['cycle_id'])                 # then the venue reports a late fill on the closed entry
    engine = w.restart()
    assert [b['cycle_id'] for b in engine.micro.review_block()] == [closed['cycle_id']]
    cash, cycles = w.cash(), w.cycles()
    with pytest.raises(storage.ConfigValidationError, match='outside the micro cycles'):
        engine.micro.apply_recovery(closed['cycle_id'], REVIEW)
    assert w.cycles() == cycles and w.cash() == cash             # nothing adopted, booked or changed
    assert _eth_position() == manual and 'ETH/USDT' not in engine.micro.owned_symbols()
    assert engine.micro.review_block()                            # the late fill still blocks new entries
    # Once the outside position is closed, the recovery books the late quantity alone.
    _manual(engine, 'SELL')
    assert _eth_position() == []
    engine.micro.apply_recovery(closed['cycle_id'], REVIEW)
    adopted = [c for c in w.cycles() if c['strategy'] == 'late_fill_recovery']
    assert len(adopted) == 1 and Decimal(adopted[0]['owned_base']) == Decimal('0.0004')
    assert money_close(adopted[0]['basis_quote'], Decimal('1.0004') + Decimal('0.0010004'))
    assert money_close(_eth_position()[0]['qty'], adopted[0]['owned_base'], Decimal('1e-12'))


def test_a_late_fill_after_close_is_booked_onto_the_remainder_the_closed_cycle_retained(world):
    w = world(micro_cooldown_seconds=0)
    held = w.run_until_state('HOLDING')
    venue = w.venue()
    original = venue.create_order

    def sale_fills_97_percent(symbol, *args, **kwargs):
        venue.partial_next = Decimal('0.97')                # the rest is below the 1 USDT market minimum
        return original(symbol, *args, **kwargs)
    venue.create_order = sale_fills_97_percent
    assert w.run(120, until=lambda: micro.load_cycle(held['cycle_id'])['state'] == 'CLOSED')
    venue.create_order = original
    retained = Decimal(micro.load_cycle(held['cycle_id'])['residual_base'])
    assert retained > 0 and money_close(_eth_position()[0]['qty'], retained, Decimal('1e-12'))
    _raise_entry_fill(w, held['cycle_id'])
    engine = w.restart()
    assert 'late fill booked' in engine.micro.apply_recovery(held['cycle_id'], REVIEW)['outcome']
    adopted = [c for c in w.cycles() if c['strategy'] == 'late_fill_recovery']
    assert len(adopted) == 1 and money_close(adopted[0]['owned_base'], retained + Decimal('0.0004'), Decimal('1e-12'))
    assert_ledger_consistent(w)


def _raise_sale_fill(w: World, cycle_id: str, extra_base=Decimal('0.0004'), extra_gross=Decimal('1.0'),
                     extra_fee=Decimal('0.001')) -> None:
    sale = [i for i in w.intents(cycle_id) if i['side'] == 'SELL'][-1]
    stored = w.venue().fetch_order(None, 'ETH/USDT', {'clientOrderId': sale['client_order_id']})['info']['exact']
    with storage.connection() as db:                # the venue now reports a larger fill on that exit
        db.execute('UPDATE paper_venue_orders SET filled=?, cost=?, fee_cost=? WHERE client_order_id=?',
                   (str(Decimal(stored['filled']) + extra_base), str(Decimal(stored['cost']) + extra_gross),
                    str(Decimal(stored['fee_cost']) + extra_fee), sale['client_order_id']))


def test_a_late_sale_after_close_beside_an_outside_position_is_refused_without_advising_a_manual_sale(world):
    w = world(micro_cooldown_seconds=0)
    closed = w.run_until_state('CLOSED')
    assert _eth_position() == []                                  # the cycle sold everything: nothing retained
    w.engine.running = False
    w.engine.stop_event.set()
    w.engine.update_config(micro_settings(watchlist=['BTC/USDT']))
    _manual(w.engine, 'BUY', 50)                                  # ETH bought by hand outside the cycles
    manual = _eth_position()
    _raise_sale_fill(w, closed['cycle_id'])                       # then the venue reports a late sale on the exit
    engine = w.restart()
    assert [b['cycle_id'] for b in engine.micro.review_block()] == [closed['cycle_id']]
    cash, cycles = w.cash(), w.cycles()
    with pytest.raises(storage.ConfigValidationError) as refused:
        engine.micro.apply_recovery(closed['cycle_id'], REVIEW)
    # Selling the manual position can never let a late sale the cycles do not cover be booked: no such advice.
    assert 'more than the micro cycles retain (0); this cannot be booked automatically' in str(refused.value)
    assert 'close that manual' not in str(refused.value) and 'first' not in str(refused.value)
    assert _eth_position() == manual and w.cash() == cash and w.cycles() == cycles
    assert engine.micro.review_block()                            # it stays blocked (register item 4)


def test_a_covered_late_sale_after_close_beside_an_outside_position_is_refused_without_advising_a_manual_sale(world):
    w = world(micro_cooldown_seconds=0)
    held = w.run_until_state('HOLDING')
    venue = w.venue()
    original = venue.create_order

    def sale_fills_97_percent(symbol, *args, **kwargs):
        venue.partial_next = Decimal('0.97')                # the rest is below the 1 USDT market minimum
        return original(symbol, *args, **kwargs)
    venue.create_order = sale_fills_97_percent
    assert w.run(120, until=lambda: micro.load_cycle(held['cycle_id'])['state'] == 'CLOSED')
    venue.create_order = original
    retained = Decimal(micro.load_cycle(held['cycle_id'])['residual_base'])
    with storage.connection() as db:                 # stands for a position added outside the cycles
        db.execute("UPDATE positions SET qty=qty+0.02, cost=cost+50 WHERE symbol='ETH/USDT' AND mode='paper'")
    mixed = _eth_position()
    _raise_sale_fill(w, held['cycle_id'], extra_base=retained / 2, extra_gross=Decimal('0.5'),
                     extra_fee=Decimal('0.0005'))
    engine = w.restart()
    cash, cycles = w.cash(), w.cycles()
    with pytest.raises(storage.ConfigValidationError) as refused:
        engine.micro.apply_recovery(held['cycle_id'], REVIEW)
    # A manual sale would sell the retained remainder too, turning a covered late sale into an uncovered one.
    assert 'outside the micro cycles' in str(refused.value) and 'cannot be booked automatically' in str(refused.value)
    assert 'close that manual' not in str(refused.value) and 'first' not in str(refused.value)
    assert _eth_position() == mixed and w.cash() == cash and w.cycles() == cycles
    assert engine.micro.review_block()


def test_a_manual_buy_waits_while_a_late_fill_on_a_closed_cycle_of_the_pair_is_unbooked(world):
    w = world(micro_cooldown_seconds=0)
    closed = w.run_until_state('CLOSED')
    _raise_entry_fill(w, closed['cycle_id'])
    w.run(5)
    assert [b['cycle_id'] for b in w.engine.micro.review_block()] == [closed['cycle_id']]
    w.engine.running = False
    w.engine.stop_event.set()
    w.engine.update_config(micro_settings(watchlist=['BTC/USDT']))
    with pytest.raises(storage.ConfigValidationError, match='late micro fill'):
        _manual(w.engine, 'BUY', 50)
    assert _eth_position() == []
    w.engine.micro.apply_recovery(closed['cycle_id'], REVIEW)
    assert w.engine.micro.review_block() == []


def test_a_late_sale_fill_corrects_the_cycle_totals_and_the_results(world):
    w = world()
    cycle_id = _partial_exit(w, '0.5')
    cycle = micro.load_cycle(cycle_id)
    sale = next(i for i in w.intents(cycle_id) if i['side'] == 'SELL')
    record = w.venue().fetch_order(None, 'ETH/USDT', {'clientOrderId': sale['client_order_id']})['info']['exact']
    extra_base, price = Decimal('0.001'), Decimal(record['average'])
    extra_gross = extra_base * price
    extra_fee = extra_gross * Decimal('0.001')
    with storage.connection() as db:
        db.execute('UPDATE paper_venue_orders SET filled=?, cost=?, fee_cost=? WHERE client_order_id=?',
                   (str(Decimal(record['filled']) + extra_base), str(Decimal(record['cost']) + extra_gross),
                    str(Decimal(record['fee_cost']) + extra_fee), sale['client_order_id']))
    w.set_closes('ETH/USDT', float(Decimal(cycle['basis_quote']) / Decimal(cycle['owned_base'])) * 0.999)
    w.run(1)
    assert micro.load_cycle(cycle_id)['state'] == 'REVIEW'
    w.engine.running = False
    cash = w.cash()
    w.engine.micro.apply_recovery(cycle_id, REVIEW)
    after = micro.load_cycle(cycle_id)
    assert money_close(w.cash() - cash, extra_gross - extra_fee)
    assert money_close(Decimal(after['exit_gross']) - Decimal(cycle['exit_gross']), extra_gross)
    assert money_close(Decimal(after['exit_fees']) - Decimal(cycle['exit_fees']), extra_fee)
    assert money_close(Decimal(after['closed_base']) - Decimal(cycle['closed_base']), extra_base)
    assert money_close(Decimal(after['exit_proceeds_net']) - Decimal(cycle['exit_proceeds_net']),
                       extra_gross - extra_fee)
    assert_ledger_consistent(w)


def test_exit_all_pressed_while_stopping_waits_for_the_worker_and_sells_or_refuses(world):
    w = world()
    held = w.run_until_state('HOLDING')
    release = threading.Event()

    def worker():
        release.wait(5)
        w.engine.running = w.engine.stopping = False          # what the real worker's cleanup does
    thread = threading.Thread(target=worker, name='TradingEngine')
    thread.start()
    w.engine.thread = thread
    w.engine.stop()                                           # returns while the worker is still finishing
    assert thread.is_alive() and w.engine.stopping
    threading.Timer(0.3, release.set).start()
    answer = w.engine.micro.flatten()
    assert answer == {'queued': False, 'attempted': ['ETH/USDT']}
    closed = micro.load_cycle(held['cycle_id'])
    assert closed['state'] == 'CLOSED' and closed['last_exit_trigger'] == 'operator'
    assert w.engine.micro.flatten_pending() == 0
    # A worker that does not finish in time gets a refusal, never a request left queued for the next start.
    unstick = threading.Event()
    stuck = threading.Thread(target=unstick.wait, args=(30,), daemon=True, name='TradingEngine')
    stuck.start()
    w.engine.thread, w.engine.running = stuck, True
    w.engine.stop_event.set()
    monkeypatched = micro.FLATTEN_STOP_WAIT_S
    micro.FLATTEN_STOP_WAIT_S = 0.2
    try:
        with pytest.raises(storage.ConfigValidationError):
            w.engine.micro.flatten()
    finally:
        micro.FLATTEN_STOP_WAIT_S = monkeypatched
        unstick.set()                       # the stand-in worker ends, so teardown does not wait for it
        stuck.join(timeout=2)
    assert w.engine.micro.flatten_pending() == 0


def test_a_queued_exit_all_retries_within_its_window_then_ends(world, monkeypatch):
    w = world()
    held = w.run_until_state('HOLDING')
    w.set_closes('ETH/USDT', float(Decimal(held['entry_price'])))      # no exit of its own meanwhile
    with pytest.MonkeyPatch.context() as no_quotes:
        no_quotes.setattr(micro.MicroManager, '_book', lambda self, *a, **k: None)
        no_quotes.setattr(micro.MicroManager, '_ticker_snapshot', lambda self, symbol: None)
        w.engine.micro.request_flatten()
        w.run(1)
        assert w.engine.micro.flatten_pending() > 0                     # no quote yet: still trying
        w.run(3)
        assert w.engine.micro.flatten_pending() == 0                    # the window passed
        assert micro.load_cycle(held['cycle_id'])['state'] == 'HOLDING'
        assert any('still held' in row['message'] for row in storage.rows('SELECT message FROM audit'))
    w.run(5)                                  # quotes return: only the cycle's own exit rules may sell now
    assert micro.load_cycle(held['cycle_id'])['last_exit_trigger'] != 'operator'


def test_open_cycles_block_an_exchange_change_and_leaving_micro(world):
    w = world()
    held = w.run_until_state('HOLDING')
    w.engine.running = False
    for change in ({'time_horizon': 'rapid'}, {'exchange': 'kraken'}, {'exchange': 'kraken', 'time_horizon': 'rapid'}):
        with pytest.raises(storage.ConfigValidationError, match='Micro cycles are still open'):
            w.engine.update_config(micro_settings(**change))
    assert w.engine.exchange_name == 'mexc' and micro.load_cycle(held['cycle_id'])['state'] == 'HOLDING'


def test_the_micro_stop_is_carried_to_another_exchange(world):
    w = world()
    w.engine.running = False
    w.engine.micro._engage_latch('2 consecutive losing micro cycles')
    w.engine.update_config(micro_settings(exchange='kraken'))
    assert w.engine.exchange_name == 'kraken'
    assert 'Carried over from MEXC' in w.engine.micro.latch()['reason']


def test_a_held_cycle_defers_the_catalogue_refresh_then_makes_one_attempt_after_its_exit_check(world):
    import ccxt
    w = world(micro_max_hold_minutes=240)
    w.run_until_state('HOLDING')
    w.engine.data.wait = lambda seconds: False
    w.clock.advance(3601)                                     # the hourly refresh is due
    real_load, real_book = w.exchange.load_markets, w.exchange.fetch_order_book
    events = []

    def slow_failing(reload=False):
        if reload:
            events.append(('catalogue', w.clock.time()))
            w.clock.advance(18.0)                             # the client timeout, then a transport error
            raise ccxt.RequestTimeout('timed out')
        return real_load(reload)

    def book(symbol, *args, **kwargs):
        events.append((symbol, w.clock.time()))
        return real_book(symbol, *args, **kwargs)
    w.exchange.load_markets, w.exchange.fetch_order_book = slow_failing, book
    w.clock.advance(1.0)
    w.tick()
    kinds = [kind for kind, _ in events]
    assert 'ETH/USDT' in kinds and 'catalogue' not in kinds       # deferred while the cycle is held
    assert w.cycles('HOLDING')
    # Once the catalogue nears the age at which entries refuse it, one attempt is made, after the exit check.
    w.engine.micro.rules_loaded_at -= micro.CATALOGUE_HOLD_REFRESH_S
    events.clear()
    w.clock.advance(1.0)
    w.tick()
    kinds = [kind for kind, _ in events]
    assert kinds.count('catalogue') == 1                          # one attempt, not the client's three
    assert kinds.index('ETH/USDT') < kinds.index('catalogue')     # the holding was checked first
    for _ in range(20):
        w.clock.advance(5.0)
        w.tick()
    assert [kind for kind, _ in events].count('catalogue') == 1   # not retried for the next five minutes
    w.clock.advance(trader.TradingEngine.CATALOGUE_RETRY_SECONDS)
    w.tick()
    assert [kind for kind, _ in events].count('catalogue') == 2


def test_after_a_held_cycle_catalogue_attempt_the_held_cycle_is_checked_before_a_slow_outside_position(world):
    """A manual position outside the watchlist whose quote reads time out must not add its monitor run to the
    catalogue attempt the held cycle already waited for; it is checked right after the held cycle instead."""
    import ccxt
    w = world(_five_pairs_and_one_outside_them, watchlist=['ETH/USDT'], micro_max_hold_minutes=240)
    held = w.run_until_state('HOLDING', symbol='ETH/USDT')
    w.engine.running = False
    w.engine.stop_event.set()
    assert w.engine.manual_trade('L/USDT', 'BUY', 20)['status'] == 'filled'
    w.go()
    w.set_closes('ETH/USDT', float(Decimal(held['entry_price'])))    # no exit of its own meanwhile
    w.engine.data.wait = lambda seconds: False
    w.tick()
    timeout_s = 18.0
    events = []
    real_load, real_book = w.exchange.load_markets, w.exchange.fetch_order_book
    real_quotes, real_ticker = w.exchange.fetch_bids_asks, w.exchange.fetch_ticker

    def slow_failing(kind):
        events.append((kind, w.clock.monotonic()))
        w.clock.advance(timeout_s)                                    # the client timeout, then a transport error
        raise ccxt.RequestTimeout('timed out')

    def load(reload=False):
        return slow_failing('catalogue') if reload else real_load(reload)

    def book(symbol, *args, **kwargs):
        events.append((symbol, w.clock.monotonic()))
        return real_book(symbol, *args, **kwargs)

    def quotes(symbols=None, params=None):
        return slow_failing('L/USDT quotes') if symbols and 'L/USDT' in symbols else real_quotes(symbols, params)

    def ticker(symbol, params=None):
        return slow_failing('L/USDT ticker') if symbol == 'L/USDT' else real_ticker(symbol, params)
    w.exchange.load_markets, w.exchange.fetch_order_book = load, book
    w.exchange.fetch_bids_asks, w.exchange.fetch_ticker = quotes, ticker
    w.clock.advance(3601)
    w.engine.micro.rules_loaded_at -= micro.CATALOGUE_HOLD_REFRESH_S   # the held cycle's one attempt is due
    w.engine._monitor_if_due()                                        # the worker loop's call before the tick
    events.clear()
    w.tick()
    kinds = [kind for kind, _ in events]
    attempt = kinds.index('catalogue')
    after = [(kind, round(at - events[attempt][1], 1)) for kind, at in events[attempt:]]
    # The fixture clock also moves with real time, so the bounds leave half a timeout for a slow runner.
    assert after[1][0] == 'ETH/USDT' and after[1][1] < timeout_s * 1.5, after     # the held cycle: one attempt
    assert after[2][0] == 'L/USDT quotes' and after[2][1] < timeout_s * 1.5, after   # then the outside position
    assert micro.load_cycle(held['cycle_id'])['state'] == 'HOLDING'


def test_without_a_holding_a_failing_catalogue_refresh_keeps_its_retries_and_waits_before_the_next(world):
    import ccxt
    w = world()
    w.run(1)
    assert not w.cycles()
    w.engine.data.wait = lambda seconds: False
    w.clock.advance(3601)
    real_load = w.exchange.load_markets
    reloads = []

    def failing(reload=False):
        if reload:
            reloads.append(w.clock.time())
            raise ccxt.RequestTimeout('timed out')
        return real_load(reload)
    w.exchange.load_markets = failing
    w.tick()
    assert len(reloads) == 3                                      # the normal client retries, nothing held
    for _ in range(10):
        w.clock.advance(5.0)
        w.tick()
    assert len(reloads) == 3


class _Answer:
    """A canned HTTP response for the real exchange clients (the request itself never leaves the process)."""

    def __init__(self, body):
        self.text = json.dumps(body)
        self.content = self.text.encode()
        self.headers = {'Content-Type': 'application/json'}
        self.status_code, self.reason, self.encoding = 200, 'OK', None

    def raise_for_status(self):
        pass


def test_the_stated_delay_of_a_held_cycle_catalogue_attempt_covers_every_request_of_one_load():
    """One attempt is one ``load_markets(reload=True)`` call of the real client, whose requests run in a row."""
    import re
    import types
    import market_data
    worst, pacing = {}, {}
    for name in ('mexc', 'kraken'):
        client = trader.TradingEngine._new_exchange(name)          # the production options (no network here)
        # ccxt paces each request from the previous one's start, so a slow request absorbs the next one's
        # pacing: only the wait before the first request adds to the attempt, at most one rate-limit interval.
        pacing[name] = client.rateLimit / 1000 if client.enableRateLimit else 0.0
        client.enableRateLimit = False
        urls = []

        def request(method, url, *args, _urls=urls, **kwargs):
            _urls.append(url.split('?')[0])
            now_ms = int(time.time() * 1000)
            if '/api/v3/time' in url:
                return _Answer({'serverTime': now_ms})
            if '/api/v3/exchangeInfo' in url:
                return _Answer({'timezone': 'CST', 'serverTime': now_ms, 'symbols': []})
            if 'contract' in url:
                return _Answer({'success': True, 'code': 0, 'data': []})
            return _Answer({'error': [], 'result': {}})                   # Kraken's public Assets and AssetPairs
        client.session.request = request
        held = types.SimpleNamespace(markets_loaded_at=time.monotonic() - micro.CATALOGUE_HOLD_REFRESH_S,
                                     markets_retry_at=0.0, data=market_data.MarketData(client),
                                     CATALOGUE_RETRY_SECONDS=trader.TradingEngine.CATALOGUE_RETRY_SECONDS,
                                     _catalogue=lambda: {})
        assert isinstance(trader.TradingEngine._fetch_markets(held, attempts=1), dict)   # the held cycle's attempt
        assert urls, name
        worst[name] = len(urls) * client.timeout / 1000          # each request may take the whole client timeout
    assert worst['mexc'] == 3 * 18.0                             # server time, spot markets, contract markets
    assert worst['kraken'] == 2 * 18.0                           # assets, asset pairs
    assert pacing['mexc'] <= 0.1 and pacing['kraken'] == 1.0     # MEXC's is within "about"; Kraken's is stated

    def doc(name):
        return ' '.join((ROOT / 'docs' / name).read_text(encoding='utf-8').split())

    def found(pattern, text, where):
        match = re.search(pattern, text)
        assert match, (where, pattern)
        return match.group(1)
    row = r'\| {} \|(.+?)\| (?:\d+ \| |$)'
    validation = doc('ATB_MICRO_VALIDATION.md')
    statements = {  # the architecture, the accepted-risk register and the validation record state one bound
        'architecture': found(r'can delay an exit check by at most (.+?)(?:\. |$)', doc('ATB_MICRO_ARCHITECTURE.md'),
                              'architecture'),
        'register item 12': found(row.format(12), doc('ATB_MICRO_RELEASE_GATES.md'), 'register item 12'),
        'validation review': found(r'register item 12 for its remainder(.+?)(?:\. |$)', validation, 'validation review'),
        'validation row 25': found(row.format(25), validation, 'validation row 25'),
    }
    for where, stated in statements.items():
        assert float(found(r'MEXC\b.*?(\d+(?:\.\d+)?) s\b', stated, where)) == worst['mexc'], (where, stated)
        assert float(found(r'Kraken\b.*?(\d+(?:\.\d+)?) s\b', stated, where)) == worst['kraken'], (where, stated)
        assert float(found(r'Kraken\b.*?(\d+(?:\.\d+)?) s of pacing before the first request', stated,
                           where)) == pacing['kraken'], (where, stated)


def test_a_loss_limit_reached_just_before_a_crash_latches_at_the_next_start(world, monkeypatch):
    w = world(micro_daily_loss_limit_usdt=0.1, micro_cooldown_seconds=0)
    held = w.run_until_state('HOLDING')
    real = micro.MicroManager._after_close

    def die(self, cycle):
        raise Crash()                         # the process dies right after the exit transaction committed
    monkeypatch.setattr(micro.MicroManager, '_after_close', die)
    w.set_closes('ETH/USDT', float(Decimal(held['entry_price'])) * 0.985)
    with pytest.raises(Crash):
        w.run(30)
    monkeypatch.setattr(micro.MicroManager, '_after_close', real)
    closed = micro.load_cycle(held['cycle_id'])
    assert closed['state'] == 'CLOSED' and Decimal(closed['realized_net']) <= Decimal('-0.1')
    engine = w.restart()
    assert 'realized micro loss today' in engine.micro.latch()['reason']
    w.go()
    w.exchange.series['ETH/USDT'].closes[:] = mechanics(base=float(Decimal(held['entry_price'])))
    count = len(w.cycles())
    w.run(90)
    assert len(w.cycles()) == count


def test_a_loss_limit_check_that_failed_after_a_close_runs_before_the_next_entry(world, monkeypatch):
    w = world(micro_daily_loss_limit_usdt=0.1, micro_cooldown_seconds=0)
    held = w.run_until_state('HOLDING')
    real = micro.MicroManager._check_loss_limits
    calls = {'n': 0}

    def flaky(self):
        calls['n'] += 1
        if calls['n'] == 1:
            raise sqlite3.OperationalError('database is locked')
        return real(self)
    w.set_closes('ETH/USDT', float(Decimal(held['entry_price'])) * 0.985)
    monkeypatch.setattr(micro.MicroManager, '_check_loss_limits', lambda self: None)
    w.run(30, until=lambda: micro.load_cycle(held['cycle_id'])['state'] == 'CLOSED')
    monkeypatch.setattr(micro.MicroManager, '_check_loss_limits', flaky)
    w.exchange.series['ETH/USDT'].closes[:] = mechanics(base=float(Decimal(held['entry_price'])))
    count = len(w.cycles())
    w.run(90)
    assert len(w.cycles()) == count and 'realized micro loss today' in w.engine.micro.latch()['reason']


def test_an_uncertain_entry_blocks_new_entries_but_other_cycles_keep_their_exits(world):
    import ccxt
    w = world(two_setups, micro_max_cycles=2)
    held = w.run_until_state('HOLDING', symbol='ETH/USDT')
    w.set_closes('ETH/USDT', float(Decimal(held['entry_price'])))     # ETH holds still meanwhile
    w.venue().reject_next = ccxt.RequestTimeout('response lost')        # the next order's outcome is unknown
    original = w.venue().fetch_order

    def unreachable(order_id, symbol=None, params=None):
        raise ccxt.NetworkError('lookup failed')
    w.venue().fetch_order = unreachable
    assert w.run(120, until=lambda: bool(w.cycles('ENTRY_UNKNOWN', 'BTC/USDT')))
    w.venue().fetch_order = original
    assert not w.engine.stop_event.is_set()                   # the worker keeps running
    assert w.engine.micro.review_block()
    count = len(w.cycles())
    w.set_closes('ETH/USDT', float(Decimal(held['entry_price'])) * 0.95)   # far through ETH's stop
    w.run(10, until=lambda: micro.load_cycle(held['cycle_id'])['state'] == 'CLOSED')
    closed = micro.load_cycle(held['cycle_id'])
    assert closed['state'] == 'CLOSED' and closed['last_exit_trigger'] == 'stop_loss'
    assert len(w.cycles()) == count                           # and still nothing new
    assert_ledger_consistent(w)


def test_a_residual_on_a_pair_that_left_the_selection_keeps_a_fresh_mark(world):
    w = world(two_setups, micro_cooldown_seconds=0, data_max_age_seconds=30)
    held = w.run_until_state('HOLDING', symbol='ETH/USDT')
    venue = w.venue()
    original = venue.create_order

    def eth_sale_fills_97_percent(symbol, *args, **kwargs):
        if symbol == 'ETH/USDT':
            venue.partial_next = Decimal('0.97')            # the rest is below the 1 USDT market minimum
        return original(symbol, *args, **kwargs)
    venue.create_order = eth_sale_fills_97_percent
    assert w.run(120, until=lambda: micro.load_cycle(held['cycle_id'])['state'] == 'CLOSED')
    venue.create_order = original
    closed = micro.load_cycle(held['cycle_id'])
    assert Decimal(closed['residual_base']) > 0 and 'ETH/USDT' in w.engine.micro.owned_symbols()
    w.engine.running = False
    w.engine.update_config(micro_settings(watchlist=['BTC/USDT'], micro_cooldown_seconds=0,
                                          data_max_age_seconds=30))
    w.go()
    count = len(w.cycles(symbol='BTC/USDT'))
    w.run(120, until=lambda: len(w.cycles(symbol='BTC/USDT')) > count)
    fresh = w.cycles(symbol='BTC/USDT')[count:]
    assert fresh and fresh[0]['state'] != 'ABANDONED', fresh[0]['last_reason'] if fresh else 'no BTC entry'
    refusals = [c['last_reason'] for c in w.cycles() if 'stale or missing price' in (c['last_reason'] or '')]
    assert refusals == []


def offset_depth_ids(w: World, symbol: str, offset: dict) -> None:
    """The venue's depth ids for ``symbol`` carry ``offset['ids']``; setting it to 0 stands for a counter restart."""
    real = w.exchange.fetch_order_book

    def fetch(name, limit=None, params=None):
        book = real(name, limit, params)
        if name == symbol:
            book['nonce'] += offset['ids']
        return book
    w.exchange.fetch_order_book = fetch


def test_a_venue_id_restart_while_held_recovers_the_book_the_profit_exit_and_the_next_entry(world):
    w = world()
    offset = {'ids': 7_000_000_000}
    offset_depth_ids(w, 'ETH/USDT', offset)
    held = w.run_until_state('HOLDING', symbol='ETH/USDT')
    offset['ids'] = 0                                    # the venue restarted its lastUpdateId counter
    assert w.run(60, until=lambda: micro.load_cycle(held['cycle_id'])['state'] == 'CLOSED')
    closed = micro.load_cycle(held['cycle_id'])
    assert closed['last_exit_trigger'] == 'take_profit', closed['last_exit_trigger']   # depth came back
    row = w.engine.micro.books.status(w.clock.monotonic())['ETH/USDT']
    assert row['sequence_resets'] == 1 and row['sequence'] < 7_000_000_000
    assert any('restarted its order-book update ids' in a['message'] for a in storage.rows('SELECT * FROM audit'))
    # the pair enters again without a process restart
    assert w.run(120, until=lambda: any(c['state'] in micro.OWNING_STATES and c['cycle_id'] != held['cycle_id']
                                        for c in w.cycles(symbol='ETH/USDT')))
    assert w.engine.micro.books.status(w.clock.monotonic())['ETH/USDT']['sequence_resets'] == 1
    assert_ledger_consistent(w)


def test_a_venue_id_restart_while_held_recovers_the_profit_exit_under_a_lagging_replica(world):
    w = world()
    offset = {'ids': 7_000_000_000}
    offset_depth_ids(w, 'ETH/USDT', offset)
    held = w.run_until_state('HOLDING', symbol='ETH/USDT')
    served, leading = [], w.exchange.fetch_order_book

    def replicas(name, limit=None, params=None):
        book = leading(name, limit, params)
        if name == 'ETH/USDT' and offset['ids'] == 0:
            if len(served) % 2:
                # every other read comes from a replica that lags by more than the time between reads: its id is
                # lower than the previous read's (only the id lags here, the prices and stamp are the leading ones)
                book['nonce'] = served[-1] - 1
            served.append(book['nonce'])
        return book
    w.exchange.fetch_order_book = replicas
    offset['ids'] = 0                                    # the venue restarted its lastUpdateId counter
    assert w.run(60, until=lambda: micro.load_cycle(held['cycle_id'])['state'] == 'CLOSED')
    closed = micro.load_cycle(held['cycle_id'])
    assert closed['last_exit_trigger'] == 'take_profit', closed['last_exit_trigger']   # depth came back
    assert len(served) >= 4 and any(b < a for a, b in zip(served, served[1:]))
    row = w.engine.micro.books.status(w.clock.monotonic())['ETH/USDT']
    assert row['sequence_resets'] == 1 and row['sequence'] < 7_000_000_000
    assert_ledger_consistent(w)


def test_a_venue_id_restart_while_held_recovers_after_one_read_from_a_replica_behind_the_restart(world):
    w = world()
    offset = {'ids': 7_000_000_000}
    offset_depth_ids(w, 'ETH/USDT', offset)
    held = w.run_until_state('HOLDING', symbol='ETH/USDT')
    books = w.engine.micro.books
    last = books.status(w.clock.monotonic())['ETH/USDT']['sequence']
    served, leading = [], w.exchange.fetch_order_book

    def replicas(name, limit=None, params=None):
        book = leading(name, limit, params)
        if name == 'ETH/USDT' and offset['ids'] == 0:
            if len(served) == 1:
                # once, right after the first live read, a replica a few seconds behind answers with an id from
                # the old counter: below the last accepted one, above every new one (prices and stamp are live)
                book['nonce'] = last - 5
            served.append(book['nonce'])
        return book
    w.exchange.fetch_order_book = replicas
    offset['ids'] = 0                                    # the venue restarted its lastUpdateId counter
    assert w.run(120, until=lambda: micro.load_cycle(held['cycle_id'])['state'] == 'CLOSED')
    closed = micro.load_cycle(held['cycle_id'])
    assert closed['last_exit_trigger'] == 'take_profit', closed['last_exit_trigger']   # depth came back
    assert last - 5 in served
    row = books.status(w.clock.monotonic())['ETH/USDT']
    assert row['sequence_resets'] == 1 and row['sequence'] < 7_000_000_000 and row['state'] == 'fresh'
    # the pair enters again without a process restart
    assert w.run(120, until=lambda: any(c['state'] in micro.OWNING_STATES and c['cycle_id'] != held['cycle_id']
                                        for c in w.cycles(symbol='ETH/USDT')))
    assert_ledger_consistent(w)


@pytest.mark.parametrize('stamped', [False, True])
def test_a_replica_frozen_before_a_venue_id_restart_never_prices_a_held_cycle(world, stamped):
    w = world()
    offset = {'ids': 7_000_000_000}
    offset_depth_ids(w, 'ETH/USDT', offset)
    held = w.run_until_state('HOLDING', symbol='ETH/USDT')
    books = w.engine.micro.books
    last = books.status(w.clock.monotonic())['ETH/USDT']['sequence']
    leading, served, frozen = w.exchange.fetch_order_book, [], {}
    accepted, accept = [], books.accept

    def recorded(snapshot):
        stored = accept(snapshot)
        if stored.symbol == 'ETH/USDT':
            accepted.append(stored.venue_sequence)
        return stored
    books.accept = recorded

    def replicas(name, limit=None, params=None):
        book = leading(name, limit, params)
        if name == 'ETH/USDT' and offset['ids'] == 0:
            if not frozen:
                # a replica froze before the last accepted read: an older id, prices 10 % lower, an older stamp
                frozen.update(json.loads(json.dumps(book)), nonce=last - 5, timestamp=book['timestamp'] - 120_000,
                              bids=[[round(p * 0.9, 2), q] for p, q in book['bids']],
                              asks=[[round(p * 0.9, 2), q] for p, q in book['asks']])
            if not stamped:
                book['timestamp'] = frozen['timestamp'] = None       # depth without a venue timestamp
            served.append(book['nonce'])
            if len(served) % 2 == 0:                              # every other read comes from the frozen replica
                served[-1] = frozen['nonce']
                return json.loads(json.dumps(frozen))
        return book
    w.exchange.fetch_order_book = replicas
    offset['ids'] = 0                                    # the venue restarted its lastUpdateId counter
    w.run(60, until=lambda: micro.load_cycle(held['cycle_id'])['state'] == 'CLOSED')
    assert len(served) >= 4 and frozen['nonce'] in served
    assert frozen['nonce'] not in accepted                # the frozen book is never served
    cycle = micro.load_cycle(held['cycle_id'])
    assert cycle['last_exit_trigger'] != 'stop_loss', cycle['last_exit_trigger']
    live_bid = leading('ETH/USDT')['bids'][0][0]
    sales = [t for t in w.trades() if t['symbol'] == 'ETH/USDT' and t['side'].lower() == 'sell']
    assert all(t['price'] > (frozen['bids'][0][0] + live_bid) / 2 for t in sales), (sales, frozen['bids'][0])
    assert w.engine.micro.latch() is None                  # no false loss latch
    row = books.status(w.clock.monotonic())['ETH/USDT']
    if stamped:
        # the venue's clock places the frozen book 120 s before the live ones, so it is not the live counter's
        # newest: the restart is accepted on the live reads, depth prices the exit, and the frozen id stays refused
        assert row['sequence_resets'] == 1 and row['sequence'] < 7_000_000_000
        assert cycle['last_exit_trigger'] == 'take_profit', cycle['last_exit_trigger']
    else:
        # ids alone cannot show the frozen replica is not the live counter while it answers this often
        assert row['sequence_resets'] == 0 and row['sequence'] == last
    assert_ledger_consistent(w)


@pytest.mark.parametrize('again', [False, True])
def test_a_frozen_replica_seen_before_a_restart_run_started_over_never_prices_a_held_cycle(world, again):
    w = world()
    offset = {'ids': 7_000_000_000}
    offset_depth_ids(w, 'ETH/USDT', offset)
    held = w.run_until_state('HOLDING', symbol='ETH/USDT')
    books = w.engine.micro.books
    last = books.status(w.clock.monotonic())['ETH/USDT']['sequence']
    leading, served, frozen = w.exchange.fetch_order_book, [], {}
    accepted, accept = [], books.accept

    def recorded(snapshot):
        stored = accept(snapshot)
        if stored.symbol == 'ETH/USDT':
            accepted.append(stored.venue_sequence)
        return stored
    books.accept = recorded

    def replicas(name, limit=None, params=None):
        book = leading(name, limit, params)
        if name == 'ETH/USDT' and offset['ids'] == 0:
            if not frozen:
                # a replica froze before the last accepted read: an older id, prices 10 % lower
                frozen.update(json.loads(json.dumps(book)), nonce=last - 5,
                              bids=[[round(p * 0.9, 2), q] for p, q in book['bids']],
                              asks=[[round(p * 0.9, 2), q] for p, q in book['asks']])
            book['timestamp'] = frozen['timestamp'] = None       # depth without a venue timestamp
            read = len(served)
            served.append(book['nonce'])
            if read == 2:
                # a replica behind the first read answers an id below the run's first: the run starts over
                book['nonce'] = served[-1] = served[0] - 2
            elif read in (1, 4) or (again and read > 4 and read % 2 == 0):
                # the frozen replica answers before the run starts over, then on the first read after a live rise
                # once the new run is older than the age limit (reads are 15 s apart), and with again on every
                # other read after that
                served[-1] = frozen['nonce']
                return json.loads(json.dumps(frozen))
        return book
    w.exchange.fetch_order_book = replicas
    offset['ids'] = 0                                    # the venue restarted its lastUpdateId counter
    w.run(60, until=lambda: micro.load_cycle(held['cycle_id'])['state'] == 'CLOSED')
    assert len(served) >= 5 and served[1] == served[4] == frozen['nonce'] and served[2] < served[0]
    assert frozen['nonce'] not in accepted                # the frozen book is never served
    cycle = micro.load_cycle(held['cycle_id'])
    assert cycle['last_exit_trigger'] != 'stop_loss', cycle['last_exit_trigger']
    live_bid = leading('ETH/USDT')['bids'][0][0]
    sales = [t for t in w.trades() if t['symbol'] == 'ETH/USDT' and t['side'].lower() == 'sell']
    assert all(t['price'] > (frozen['bids'][0][0] + live_bid) / 2 for t in sales), (sales, frozen['bids'][0])
    assert w.engine.micro.latch() is None                  # no false loss latch
    row = books.status(w.clock.monotonic())['ETH/USDT']
    if again:
        # the carried id holds the run while it answers this often: depth stays refused, as documented
        assert row['sequence_resets'] == 0 and row['sequence'] == last
    else:
        # once it stops answering the run sets it aside, the restart is accepted on live reads and depth prices the exit
        assert row['sequence_resets'] == 1 and row['sequence'] < 7_000_000_000
        assert cycle['last_exit_trigger'] == 'take_profit', cycle['last_exit_trigger']
    assert_ledger_consistent(w)


def test_a_venue_id_restart_on_a_residual_pair_does_not_block_entries_on_other_pairs(world):
    w = world(two_setups, micro_cooldown_seconds=0, data_max_age_seconds=30)
    offset = {'ids': 7_000_000_000}
    offset_depth_ids(w, 'ETH/USDT', offset)
    held = w.run_until_state('HOLDING', symbol='ETH/USDT')
    venue = w.venue()
    original = venue.create_order

    def eth_sale_fills_97_percent(symbol, *args, **kwargs):
        if symbol == 'ETH/USDT':
            venue.partial_next = Decimal('0.97')            # the rest is below the 1 USDT market minimum
        return original(symbol, *args, **kwargs)
    venue.create_order = eth_sale_fills_97_percent
    assert w.run(120, until=lambda: micro.load_cycle(held['cycle_id'])['state'] == 'CLOSED')
    venue.create_order = original
    assert 'ETH/USDT' in w.engine.micro.owned_symbols()
    offset['ids'] = 0                                    # the venue restarted its lastUpdateId counter
    w.engine.running = False
    w.engine.update_config(micro_settings(watchlist=['BTC/USDT'], micro_cooldown_seconds=0,
                                          data_max_age_seconds=30))
    w.go()
    count = len(w.cycles(symbol='BTC/USDT'))
    assert w.run(120, until=lambda: any(c['state'] in micro.OWNING_STATES
                                        for c in w.cycles(symbol='BTC/USDT')[count:])), \
        [(c['state'], c['last_reason']) for c in w.cycles(symbol='BTC/USDT')[count:]]
    assert w.engine.micro.books.status(w.clock.monotonic())['ETH/USDT']['sequence_resets'] == 1
    assert w.engine._fresh('ETH/USDT')                   # the residual's mark is kept fresh again


def test_recovery_explanations_follow_the_selected_exchange(world):
    w = world()
    w.engine.running = False
    w.engine.micro.recovery = [{'cycle_id': 'cyc_other', 'symbol': 'ETH/USDT', 'state': 'REVIEW',
                                'explanation': 'an explanation for MEXC', 'action': 'apply_recovery'}]
    w.engine.update_config(micro_settings(exchange='kraken'))
    assert w.engine.micro.status()['recovery'] == []


def test_owned_symbols_are_open_cycles_and_closed_residuals_still_in_the_ledger_and_fail_closed(world, monkeypatch):
    w = world()
    held = w.run_until_state('HOLDING')
    assert w.engine.micro.owned_symbols() == {'ETH/USDT'}                 # an open cycle owns its pair
    with storage.connection() as db:
        db.execute("UPDATE micro_cycles SET state='CLOSED', residual_base='0.000001' WHERE cycle_id=?",
                   (held['cycle_id'],))
        db.execute("UPDATE positions SET qty=0.000001 WHERE symbol='ETH/USDT'")
    assert w.engine.micro.owned_symbols() == {'ETH/USDT'}                 # a closed remainder still in the ledger
    with storage.connection() as db:
        db.execute("UPDATE positions SET qty=0.008 WHERE symbol='ETH/USDT'")
    assert w.engine.micro.owned_symbols() == set()                        # the ledger holds something else
    with storage.connection() as db:
        db.execute("UPDATE micro_cycles SET residual_base='0' WHERE cycle_id=?", (held['cycle_id'],))
    assert w.engine.micro.owned_symbols() == set()                        # nothing left: the legacy monitor's again
    with storage.connection() as db:
        db.execute("UPDATE micro_cycles SET residual_base='0.000001' WHERE cycle_id=?", (held['cycle_id'],))
        db.execute("DELETE FROM positions WHERE symbol='ETH/USDT'")
    assert w.engine.micro.owned_symbols() == set()                        # no ledger row: nothing to own
    real_rows = storage.rows

    def broken(sql, args=()):
        if 'micro_cycles' in sql:
            raise sqlite3.OperationalError('no such table: micro_cycles')
        return real_rows(sql, args)
    monkeypatch.setattr(storage, 'rows', broken)
    with storage.connection() as db:
        db.execute("INSERT INTO positions(exchange, mode, symbol, qty, entry_price, cost, highest, stop, target, "
                   "opened_at) VALUES (?, 'paper', 'SOL/USDT', 1, 1, 1, 1, 0.9, 1.1, '2026-10-26T00:00:00+00:00')",
                   (w.engine.exchange_name,))
    assert w.engine.micro.owned_symbols() == {'SOL/USDT'}                 # unreadable cycles: every paper position


def test_a_sizing_failure_or_a_missing_book_reports_its_own_cause_not_an_unnamed_sizing_result(world, monkeypatch):
    w = world(watchlist=['ETH/USDT'])
    real_size_entry = micro.econ.size_entry

    def broken(**kwargs):
        raise money.MoneyError('injected rules defect')
    monkeypatch.setattr(micro.econ, 'size_entry', broken)
    seen = []

    def watch():
        row = w.engine.micro.pairs.get('ETH/USDT') or {}
        waiting = row.get('waiting') or {}
        seen.append((waiting.get('code'), waiting.get('status'), tuple(waiting.get('reasons') or ()),
                     bool(row.get('book'))))
        return False
    w.run(40, until=watch)
    assert w.cycles() == []
    assert not any('does not name its symbol' in reason for _, _, reasons, _ in seen for reason in reasons)
    # A fresh book was read and only the sizing failed: the pair says so.
    assert any(status == 'Cannot evaluate: sizing failed: MoneyError' for _, status, _, book in seen if book)
    # No book at all: the data is stale, and the sizing reason says there was no book to size on.
    monkeypatch.setattr(micro.econ, 'size_entry', real_size_entry)
    monkeypatch.setattr(type(w.engine.micro), '_book', lambda self, *args, **kwargs: None)
    seen.clear()
    w.run(10, until=watch)
    stale = [entry for entry in seen if not entry[3] and entry[0]]
    assert stale and {code for code, _, _, _ in stale} == {'STALE_DATA'}
    assert all('No fresh order book' in reasons for _, _, reasons, _ in stale)
    assert not any('does not name its symbol' in reason for _, _, reasons, _ in seen for reason in reasons)
    assert w.cycles() == []


def _carry_a_residual_then_sell_it_all(w) -> tuple[dict, dict]:
    """ETH's first sale fills 97 % (the rest is below the market minimum); the next cycle carries that rest
    and sells everything."""
    held = w.run_until_state('HOLDING', symbol='ETH/USDT')
    venue = w.venue()
    original = venue.create_order

    def eth_sale_fills_97_percent(symbol, *args, **kwargs):
        if symbol == 'ETH/USDT':
            venue.partial_next = Decimal('0.97')
        return original(symbol, *args, **kwargs)
    venue.create_order = eth_sale_fills_97_percent
    assert w.run(120, until=lambda: micro.load_cycle(held['cycle_id'])['state'] == 'CLOSED')
    venue.create_order = original
    first = micro.load_cycle(held['cycle_id'])
    assert Decimal(first['residual_base']) > 0

    def later_closed():
        return any(c['cycle_id'] != first['cycle_id'] and c['state'] == 'CLOSED' for c in w.cycles(symbol='ETH/USDT'))
    assert w.run(300, until=later_closed)
    later = [c for c in w.cycles(symbol='ETH/USDT') if c['cycle_id'] != first['cycle_id']][0]
    assert Decimal(later['carried_base']) == Decimal(first['residual_base']) and Decimal(later['residual_base']) == 0
    assert storage.rows("SELECT qty FROM positions WHERE symbol='ETH/USDT' AND mode='paper'") == []
    return first, later


def _manual_eth_position_is_the_legacy_monitors(w) -> None:
    w.engine.running = False
    w.engine.update_config(micro_settings(watchlist=['BTC/USDT'], micro_cooldown_seconds=0))
    assert w.engine.manual_trade('ETH/USDT', 'BUY', 20)['status'] == 'filled'
    assert 'ETH/USDT' not in w.engine.micro.owned_symbols()          # not the micro cycles' position
    position = storage.rows("SELECT qty, stop FROM positions WHERE symbol='ETH/USDT' AND mode='paper'")[0]
    w.set_closes('ETH/USDT', float(position['stop']) * 0.97)          # through the manual position's stored stop
    w.go()
    for _ in range(4):
        w.clock.advance(15)
        w.engine._monitor_if_due(force=True)
    assert storage.rows("SELECT qty FROM positions WHERE symbol='ETH/USDT' AND mode='paper'") == []


def test_a_residual_that_a_later_cycle_carried_and_sold_is_no_longer_recorded_on_the_earlier_cycle(world):
    w = world(micro_cooldown_seconds=0)
    first, _ = _carry_a_residual_then_sell_it_all(w)
    earlier = micro.load_cycle(first['cycle_id'])
    assert (earlier['residual_base'], earlier['residual_basis']) == ('0', '0')     # the later cycle took it over
    assert w.engine.micro.status()['results']['residuals'] == []
    assert_ledger_consistent(w)                                                   # its basis is not counted twice
    _manual_eth_position_is_the_legacy_monitors(w)


def test_owned_symbols_never_claim_a_later_position_for_a_residual_the_ledger_no_longer_holds(world):
    w = world(micro_cooldown_seconds=0)
    first, _ = _carry_a_residual_then_sell_it_all(w)
    # A database written before carried residuals were cleared keeps the earlier record.
    with storage.connection() as db:
        db.execute('UPDATE micro_cycles SET residual_base=?, residual_basis=? WHERE cycle_id=?',
                   (first['residual_base'], first['residual_basis'], first['cycle_id']))
    assert w.engine.micro.owned_symbols() == set()
    _manual_eth_position_is_the_legacy_monitors(w)


def test_owned_symbols_cost_does_not_grow_with_the_closed_cycle_history(world, monkeypatch):
    """The legacy monitor, every micro tick and the dashboard snapshot ask for the micro-owned pairs while
    they hold the storage lock. Weeks of closed and abandoned cycles must not make that slower, also with
    paper positions outside the open cycles, on pairs the cycles traded and on pairs they never traded: the
    cost is counted in SQLite virtual-machine steps, which do not depend on the machine's speed."""
    import contextlib
    w = world()
    w.run_until_state('HOLDING', symbol='ETH/USDT')
    exchange, steps, cap = w.engine.exchange_name, [0], 2_000_000
    real_connection = storage.connection

    @contextlib.contextmanager
    def counted():
        with real_connection() as db:
            def step():
                steps[0] += 1
                return 1 if steps[0] == cap else 0       # far past any bound: interrupt rather than wait
            db.set_progress_handler(step, 1)
            yield db

    def owned_and_steps():
        steps[0] = 0
        return w.engine.micro.owned_symbols(), steps[0]
    monkeypatch.setattr(storage, 'connection', counted)
    owned, short_history = owned_and_steps()
    assert owned == {'ETH/USDT'}
    pairs, start = ('ETH/USDT', 'BTC/USDT', 'C0/USDT', 'C1/USDT', 'C2/USDT'), time.time() - 30 * 86400

    def add_history(first: int, count: int) -> None:
        with real_connection() as db:
            db.executemany('INSERT INTO micro_cycles(cycle_id, exchange, mode, symbol, state, created_at, updated_at, '
                           'opportunity_key, budget_cap, closed_wall, residual_base) VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                           [(f'cyc_hist{index:020d}', exchange, 'paper', pairs[index % 5],
                             'ABANDONED' if index % 4 == 0 else 'CLOSED', 'x', 'x', f'history|{index}', '25',
                             start + 60 * index, '0.5' if index == 3 else '0')
                            for index in range(first, first + count)])
    add_history(0, 3000)
    owned, long_history = owned_and_steps()
    assert owned == {'ETH/USDT'}
    assert long_history <= 2 * short_history, (short_history, long_history)   # the held cycle's own position
    # Positions outside every open cycle: the oldest closed C1 cycle retains 0.5 C1, BTC and C0 have closed
    # cycles with no remainder, and the cycles never traded L0 to L2.
    with real_connection() as db:
        for symbol, qty in (('C0/USDT', 2.0), ('C1/USDT', 0.5), ('BTC/USDT', 0.01),
                            ('L0/USDT', 1.0), ('L1/USDT', 3.0), ('L2/USDT', 7.0)):
            db.execute("INSERT INTO positions(exchange, mode, symbol, qty, entry_price, cost, highest, stop, target, "
                       "opened_at) VALUES (?, 'paper', ?, ?, 10, ?, 10, 9, 11, '2026-10-26T00:00:00+00:00')",
                       (exchange, symbol, qty, 10 * qty))
    owned, outside = owned_and_steps()
    assert owned == {'ETH/USDT', 'C1/USDT'}
    add_history(3000, 12000)                                      # five times the history, none of it a remainder
    owned, outside_longer = owned_and_steps()
    assert owned == {'ETH/USDT', 'C1/USDT'}
    assert outside_longer <= outside + 100, (outside, outside_longer)   # each remainder lookup is one index seek


def test_start_up_adds_the_closed_remainder_index_to_a_database_written_without_it(tmp_path, monkeypatch):
    """A version-4 database from a build before the closed-remainder index gets it at the next start, built
    from the rows already there: no row changes and the schema version stays 4."""
    monkeypatch.setattr(storage, 'DATA_DIR', tmp_path)
    monkeypatch.setattr(storage, 'DB_PATH', tmp_path / 'portfolio.sqlite3')
    monkeypatch.setattr(storage, 'CONFIG_PATH', tmp_path / 'settings.json')
    storage.init_db()
    with storage.connection() as db:
        db.execute('DROP INDEX micro_cycles_closed_residual')
        db.executemany('INSERT INTO micro_cycles(cycle_id, exchange, mode, symbol, state, created_at, updated_at, '
                       "opportunity_key, budget_cap, closed_wall, residual_base) VALUES (?, 'mexc', 'paper', "
                       "'C1/USDT', ?, 'x', 'x', ?, '25', ?, ?)",
                       [('cyc_old0', 'CLOSED', 'old|0', 1.0, '0.5'), ('cyc_old1', 'CLOSED', 'old|1', 2.0, '0'),
                        ('cyc_old2', 'ABANDONED', 'old|2', 3.0, '0.25')])
        before = [tuple(row) for row in db.execute('SELECT * FROM micro_cycles ORDER BY cycle_id')]
    storage.init_db()
    remainder = ("SELECT cycle_id FROM micro_cycles INDEXED BY micro_cycles_closed_residual WHERE exchange='mexc' "
                 "AND mode='paper' AND symbol='C1/USDT' AND state='CLOSED' AND residual_base IS NOT NULL "
                 "AND residual_base NOT IN ('0', '')")     # INDEXED BY raises when the index cannot answer it
    with storage.connection() as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 4
        assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert [tuple(row) for row in db.execute('SELECT * FROM micro_cycles ORDER BY cycle_id')] == before
        assert [row['cycle_id'] for row in db.execute(remainder)] == ['cyc_old0']


def _five_pairs_and_one_outside_them(minute: int) -> list[SeriesMarket]:
    others = [SeriesMarket(f'C{i}/USDT', minute - 300, flat(900, base=10.0 + i), market=fine_market(f'C{i}/USDT'))
              for i in range(4)]
    outside = SeriesMarket('L/USDT', minute - 300, flat(900, base=100.0), market=fine_market('L/USDT'))
    return [SeriesMarket('ETH/USDT', minute - 300, mechanics(), market=fine_market('ETH/USDT'))] + others + [outside]


def test_a_position_outside_the_micro_cycles_keeps_its_stop_checks_between_micro_pairs(world):
    download_s = 4.0
    w = world(_five_pairs_and_one_outside_them, watchlist=['ETH/USDT', 'C0/USDT', 'C1/USDT', 'C2/USDT', 'C3/USDT'])
    w.run(3)                                                          # the micro manager has its context
    w.engine.running = False
    w.engine.stop_event.set()
    assert w.engine.manual_trade('L/USDT', 'BUY', 20)['status'] == 'filled'   # a pair outside the watchlist
    w.go()
    assert 'L/USDT' not in w.engine.micro.owned_symbols()
    w.engine.data.concurrency = 1                                     # sequential candle downloads (Kraken-like)
    w.clock.advance(60 - w.clock.time() % 60 + 5)                     # just after a close: every pair's bars are due
    w.engine._monitor_if_due(force=True)                              # the worker loop's call before the tick
    position = [p for p in w.engine._active_positions('paper') if p['symbol'] == 'L/USDT'][0]
    w.set_closes('L/USDT', float(position['stop']) * 0.97)            # the price falls through its stored stop now
    dropped = w.clock.time()
    sold, real_sell, real_fetch = [], w.engine._sell, w.exchange.fetch_ohlcv

    def sell(position, *args, **kwargs):
        if position['symbol'] == 'L/USDT':
            sold.append(w.clock.time())
        return real_sell(position, *args, **kwargs)

    def slow_fetch(*args, **kwargs):
        w.clock.advance(download_s)
        return real_fetch(*args, **kwargs)
    w.engine._sell, w.exchange.fetch_ohlcv = sell, slow_fetch
    w.engine.micro.tick()                                             # one pass of the micro branch of the loop
    w.engine._monitor_if_due()
    interval = max(2.0, min(60.0, float(w.engine.cfg.get('protective_poll_seconds', 5))))
    # Within one protective interval plus one pair's two candle reads, not after the whole pass.
    assert sold and sold[0] - dropped <= interval + 2 * download_s, (sold, dropped)
    assert not [p for p in w.engine._active_positions('paper') if p['symbol'] == 'L/USDT']


def test_a_catalogue_reload_made_outside_the_micro_pass_rebuilds_the_rules_from_its_load_time(world):
    w = world()
    w.run(2)
    w.engine.running = False
    w.engine.stop_event.set()
    w.clock.advance(3 * 3600)
    w.engine.history('ETH/USDT', '1m')                    # a chart while stopped reloads the hour-old catalogue
    loaded_wall = w.clock.time() - (w.clock.monotonic() - w.engine.markets_loaded_at)
    w.go()
    w.tick()
    mgr = w.engine.micro
    assert mgr.rules_loaded_at == pytest.approx(loaded_wall, abs=1.0)    # aged from that load, not from before Stop
    assert mgr.rules('ETH/USDT').loaded_at == pytest.approx(loaded_wall, abs=1.0)
    count = len(w.cycles())
    assert w.run(10, until=lambda: len(w.cycles()) > count)               # entries are not refused as stale data
    held = w.run_until_state('HOLDING')
    # While a cycle is held: a reload made by another caller (a scan, a manual order) is also adopted.
    w.clock.advance(3601)
    w.engine._fetch_markets()
    loaded_wall = w.clock.time() - (w.clock.monotonic() - w.engine.markets_loaded_at)
    assert micro.load_cycle(held['cycle_id'])['state'] == 'HOLDING'
    w.tick()
    assert mgr.rules_loaded_at == pytest.approx(loaded_wall, abs=1.0)


# ===================================================================== what the panel shows
def _fail_entry_journal_once(monkeypatch) -> dict:
    """One failed journal write after the paper venue filled the buy: the outcome is unknown in-process."""
    real = storage.update_order_intent
    armed = {'on': True}

    def flaky(intent_id, status, *args, **kwargs):
        if armed['on'] and status in ('acked', 'submitted'):
            armed['on'] = False
            raise sqlite3.OperationalError('database is locked')
        return real(intent_id, status, *args, **kwargs)
    monkeypatch.setattr(storage, 'update_order_intent', flaky)
    return armed


def _unreadable(*args, **kwargs):
    raise sqlite3.OperationalError('disk I/O error')


def test_an_unknown_outcome_is_explained_by_the_venue_record_it_will_book(world, monkeypatch):
    w = world()
    armed = _fail_entry_journal_once(monkeypatch)
    assert w.run(60, until=lambda: bool(w.cycles('ENTRY_UNKNOWN')))
    assert not armed['on']
    cycle = w.cycles('ENTRY_UNKNOWN')[0]
    intent = [i for i in w.intents(cycle['cycle_id']) if i['side'] == 'BUY'][0]
    exact = w.venue().fetch_order(None, 'ETH/USDT', {'clientOrderId': intent['client_order_id']})['info']['exact']
    booked = f'{exact["filled"]} filled for {exact["cost"]}'

    def shown(engine) -> str:
        [item] = [b for b in engine.micro.status()['review_block'] if b['cycle_id'] == cycle['cycle_id']]
        return item['explanation']
    assert booked in shown(w.engine)                                   # in-process, while the bot still runs
    engine = w.restart()
    assert booked in shown(engine)                                     # and after a restart
    assert booked in micro.load_cycle(cycle['cycle_id'])['review_reason']   # the cycle's own record says so too
    result = engine.micro.apply_recovery(cycle['cycle_id'], REVIEW)
    assert result['outcome'] == 'entry fill booked from the venue record'
    assert Decimal(micro.load_cycle(cycle['cycle_id'])['entry_filled_base']) == Decimal(exact['filled'])
    assert_ledger_consistent(w)


def test_a_venue_record_that_cannot_be_read_is_never_taken_for_no_fill(world, monkeypatch):
    w = world()
    _fail_entry_journal_once(monkeypatch)
    assert w.run(60, until=lambda: bool(w.cycles('ENTRY_UNKNOWN')))
    cycle = w.cycles('ENTRY_UNKNOWN')[0]
    cash = w.cash()
    real_fetch = micro.micro_venue.MicroPaperVenue.fetch_order
    monkeypatch.setattr(micro.micro_venue.MicroPaperVenue, 'fetch_order', _unreadable)
    engine = w.restart()                                               # the start-up check cannot read the venue
    assert 'could not be read' in micro.load_cycle(cycle['cycle_id'])['review_reason']
    [item] = engine.micro.status()['review_block']
    assert 'could not be read' in item['explanation'] and 'no fill' not in item['explanation']
    with pytest.raises(storage.ConfigValidationError, match='could not be read'):
        engine.micro.apply_recovery(cycle['cycle_id'], REVIEW)
    assert micro.load_cycle(cycle['cycle_id'])['state'] == 'ENTRY_UNKNOWN'    # not released while the venue filled
    assert w.cash() == cash and len(w.venue().records()) == 1
    monkeypatch.setattr(micro.micro_venue.MicroPaperVenue, 'fetch_order', real_fetch)
    assert 'filled for' in engine.micro.status()['review_block'][0]['explanation']
    assert engine.micro.apply_recovery(cycle['cycle_id'], REVIEW)['outcome'] == 'entry fill booked from the venue record'
    assert_ledger_consistent(w)


def test_an_exit_whose_venue_record_cannot_be_read_is_not_returned_to_holding(world, monkeypatch):
    w = world()
    held = w.run_until_state('HOLDING')
    armed = _fail_sell_booking_once(monkeypatch, held['cycle_id'])
    w.run(120, until=lambda: not armed['on'])
    assert micro.load_cycle(held['cycle_id'])['state'] == 'EXIT_SUBMITTED'
    w.engine.running = False                                  # stopped; the venue sold, the ledger did not book it
    venue = w.venue()
    real_fetch = venue.fetch_order
    monkeypatch.setattr(venue, 'fetch_order', _unreadable)
    with pytest.raises(storage.ConfigValidationError, match='could not be read'):
        w.engine.micro.apply_recovery(held['cycle_id'], REVIEW)
    after = micro.load_cycle(held['cycle_id'])
    assert after['state'] == 'EXIT_UNKNOWN' and 'could not be read' in after['review_reason']
    monkeypatch.setattr(venue, 'fetch_order', real_fetch)
    assert w.engine.micro.apply_recovery(held['cycle_id'], REVIEW)['outcome'] == 'exit fill booked from the venue record'
    assert micro.load_cycle(held['cycle_id'])['state'] == 'CLOSED'
    _no_order_booked_twice(w)
    assert_ledger_consistent(w)


def _venue_record_fails(monkeypatch, failure: str):
    """The paper venue record cannot be read ('read') or its fill cannot be parsed ('parse'); returns an undo."""
    if failure == 'read':
        real = micro.micro_venue.MicroPaperVenue.fetch_order
        monkeypatch.setattr(micro.micro_venue.MicroPaperVenue, 'fetch_order', _unreadable)
        return lambda: monkeypatch.setattr(micro.micro_venue.MicroPaperVenue, 'fetch_order', real)
    real = micro.MicroManager.__dict__['_fill']

    def unparseable(order):
        raise money.MoneyError('the fill has no positive quantity and cost')
    monkeypatch.setattr(micro.MicroManager, '_fill', staticmethod(unparseable))
    return lambda: monkeypatch.setattr(micro.MicroManager, '_fill', real)


@pytest.mark.parametrize('failure', ['read', 'parse'])
def test_a_closed_cycles_late_fill_is_never_cleared_while_its_venue_record_cannot_be_read(world, monkeypatch, failure):
    w = world(micro_cooldown_seconds=0)
    w.venue().partial_next = Decimal('0.5')
    w.run_until_state('HOLDING')
    closed = w.run_until_state('CLOSED')
    _raise_entry_fill(w, closed['cycle_id'])
    w.run(30)
    engine = w.restart()
    blocked = micro.load_cycle(closed['cycle_id'])
    assert blocked['late_fill_reason'] and [b['cycle_id'] for b in engine.micro.review_block()] == [closed['cycle_id']]
    cash, cycles, position = w.cash(), w.cycles(), _eth_position()
    restore = _venue_record_fails(monkeypatch, failure)
    with pytest.raises(storage.ConfigValidationError, match='could not be read'):
        engine.micro.apply_recovery(closed['cycle_id'], REVIEW)
    assert micro.load_cycle(closed['cycle_id']) == blocked          # the late fill is not cleared
    assert w.cycles() == cycles and w.cash() == cash and _eth_position() == position
    assert [b['cycle_id'] for b in engine.micro.review_block()] == [closed['cycle_id']]   # still blocks entries
    restore()
    assert 'late fill booked' in engine.micro.apply_recovery(closed['cycle_id'], REVIEW)['outcome']
    assert engine.micro.review_block() == []
    _no_order_booked_twice(w)
    assert_ledger_consistent(w)


@pytest.mark.parametrize('failure', ['read', 'parse'])
def test_a_review_recovery_never_adopts_the_ledger_while_the_venue_record_cannot_be_read(world, monkeypatch, failure):
    w = world()
    w.venue().partial_next = Decimal('0.5')
    held = w.run_until_state('HOLDING')
    _raise_entry_fill(w, held['cycle_id'])
    w.set_closes('ETH/USDT', float(Decimal(held['entry_price'])))     # keep the price still: no exit meanwhile
    w.run(1)
    review = micro.load_cycle(held['cycle_id'])
    assert review['state'] == 'REVIEW' and 'late fill' in review['review_reason']
    w.engine.running = False
    cash, position = w.cash(), _eth_position()
    restore = _venue_record_fails(monkeypatch, failure)
    with pytest.raises(storage.ConfigValidationError, match='could not be read'):
        w.engine.micro.apply_recovery(held['cycle_id'], REVIEW)
    assert micro.load_cycle(held['cycle_id']) == review             # still REVIEW, with its reason
    assert w.cash() == cash and _eth_position() == position
    assert [b['state'] for b in w.engine.micro.review_block()] == ['REVIEW']
    restore()
    assert w.engine.micro.apply_recovery(held['cycle_id'], REVIEW)['outcome'] == 'late fill booked from the venue record'
    after = micro.load_cycle(held['cycle_id'])
    assert after['state'] == 'HOLDING' and Decimal(after['owned_base']) == Decimal(held['owned_base']) + Decimal('0.0004')
    assert_ledger_consistent(w)


def _status_cycle(w: World, cycle_id: str) -> dict:
    [found] = [c for c in w.engine.micro.status()['cycles'] if c['cycle_id'] == cycle_id]
    return found


@pytest.mark.parametrize('readable_at_restart', [False, True])
def test_an_unknown_outcomes_row_states_the_venue_record_its_recovery_item_states(world, monkeypatch, readable_at_restart):
    w = world()
    _fail_entry_journal_once(monkeypatch)
    assert w.run(60, until=lambda: bool(w.cycles('ENTRY_UNKNOWN')))
    cycle = w.cycles('ENTRY_UNKNOWN')[0]
    real_fetch = micro.micro_venue.MicroPaperVenue.fetch_order
    reads = []

    def counted(*args, **kwargs):
        reads.append(1)
        return real_fetch(*args, **kwargs)

    def reading(readable: bool) -> None:
        monkeypatch.setattr(micro.micro_venue.MicroPaperVenue, 'fetch_order', counted if readable else _unreadable)
    reading(readable_at_restart)
    engine = w.restart()                       # the start-up check stores what it could read in review_reason
    stored = micro.load_cycle(cycle['cycle_id'])['review_reason']
    assert ('could not be read' in stored) is not readable_at_restart
    reading(not readable_at_restart)           # then the venue becomes readable, or unreadable
    reads.clear()
    status = engine.micro.status()
    [row] = [c for c in status['cycles'] if c['cycle_id'] == cycle['cycle_id']]
    [item] = [b for b in status['review_block'] if b['cycle_id'] == cycle['cycle_id']]
    assert row['review_reason'] == item['explanation']       # the row's Next text is the recovery's text
    assert ('could not be read' in row['review_reason']) is readable_at_restart
    assert ('filled for' in row['review_reason']) is not readable_at_restart
    assert len(reads) == (0 if readable_at_restart else 1)    # one venue read per refresh, not one per place shown


def test_a_closed_cycles_last_estimate_never_shows_on_the_next_cycle_of_the_pair(world):
    import ccxt
    w = world()
    assert w.run(60, until=lambda: len(w.cycles('CLOSED')) >= 1)
    assert (w.engine.micro.pairs['ETH/USDT'].get('exit') or {}).get('estimate')   # the closed cycle's last estimate
    held = w.run_until_state('HOLDING')                       # entered at the end of a pass: no exit check yet
    fresh = _status_cycle(w, held['cycle_id'])
    assert fresh['estimate'] is None and fresh['exit_verdict'] is None
    w.run(0.25)
    assert _status_cycle(w, held['cycle_id'])['estimate'] is not None          # its own estimate is shown
    assert w.run(60, until=lambda: micro.load_cycle(held['cycle_id'])['state'] == 'CLOSED')
    w.venue().reject_next = ccxt.RequestTimeout('injected timeout')
    assert w.run(60, until=lambda: bool(w.cycles('ENTRY_UNKNOWN')))
    unknown = w.cycles('ENTRY_UNKNOWN')[0]
    shown = _status_cycle(w, unknown['cycle_id'])
    assert shown['estimate'] is None and shown['exit_verdict'] is None and shown['depth_model'] is None


def test_the_panel_reports_the_current_book_and_the_estimate_age_not_decision_time_snapshots(world):
    w = world()
    held = w.run_until_state('HOLDING', symbol='ETH/USDT')
    w.run(1)
    assert micro.load_cycle(held['cycle_id'])['state'] == 'HOLDING'
    status = w.engine.micro.status()
    row = next(r for r in status['pairs'] if r['symbol'] == 'ETH/USDT')
    live = status['books']['ETH/USDT']
    # While held, the entry loop skips the pair: its row shows the book the exit path reads now.
    assert row['book']['generation'] == live['generation'] and row['book']['age_s'] == live['age_s']
    assert row['book']['spread_bps'] is not None and row['book']['state'] == 'fresh'
    w.engine.running = False
    w.engine.stop_event.set()
    w.clock.advance(7200)
    status = w.engine.micro.status()
    row = next(r for r in status['pairs'] if r['symbol'] == 'ETH/USDT')
    assert row['book']['age_s'] >= 7200 and row['book']['state'] == 'stale'
    [cycle] = [c for c in status['cycles'] if c['cycle_id'] == held['cycle_id']]
    assert cycle['estimate'] is not None and cycle['estimate_age_s'] >= 7200


def test_an_unfilled_cycle_reports_how_long_its_outcome_has_been_pending(world):
    import ccxt
    w = world()
    w.venue().reject_next = ccxt.RequestTimeout('injected timeout')
    assert w.run(60, until=lambda: bool(w.cycles('ENTRY_UNKNOWN')))
    unknown = w.cycles('ENTRY_UNKNOWN')[0]
    w.engine.running = False
    w.engine.stop_event.set()
    w.clock.advance(3 * 3600)
    shown = _status_cycle(w, unknown['cycle_id'])
    assert shown['age_s'] is None                              # never held: no hold time
    assert shown['pending_s'] >= 3 * 3600


def test_the_volume_gate_reads_the_ticker_sparingly_and_blocks_thin_or_unknown_pairs(monkeypatch):
    calls = []

    class Data:
        volume = 4e6

        def ticker(self, symbol):
            calls.append(symbol)
            if self.volume is None:
                raise ccxt.NetworkError('down')
            return {'quoteVolume': self.volume}

    data = Data()
    manager = object.__new__(micro.MicroManager)
    manager.engine = types.SimpleNamespace(data=data, cfg={'min_quote_volume': 5e6})
    manager._volumes = {}
    clock = [1000.0]
    monkeypatch.setattr(micro.time, 'monotonic', lambda: clock[0])
    assert manager._volume_gate('FLOKI/USDT') == '24h quote volume 4,000,000 USDT is below the 5,000,000 USDT minimum'
    data.volume = 6e6
    assert manager._volume_gate('FLOKI/USDT').startswith('24h quote volume 4,000,000')   # cached
    clock[0] += micro.VOLUME_REFRESH_S
    assert manager._volume_gate('FLOKI/USDT') is None and len(calls) == 2
    data.volume = None
    clock[0] += micro.VOLUME_REFRESH_S
    assert manager._volume_gate('FLOKI/USDT') == 'unknown 24h quote volume'
    clock[0] += micro.VOLUME_RETRY_S
    data.volume = 7e6
    assert manager._volume_gate('FLOKI/USDT') is None and len(calls) == 4
