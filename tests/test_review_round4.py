"""Regression tests for the final review round: manual-exit latch, withheld entries, feed start, clock-capped hold."""
import asyncio
import json
import sys
import time
from pathlib import Path

import ccxt
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import decision
import feed
import risk
import storage
import trader
from test_engine import engine  # noqa: F401  (fixture)
from test_hardening_fixes import capture_events
from test_review_round3 import end_slippage_hold, trip_slippage
from test_risk_gate import observe


# --- A loss booked by any exit latches the realised-loss stop at once ---------------------

def test_a_loss_booked_by_a_manual_exit_latches_before_a_later_profit_hides_it(engine):
    assert engine._buy('BTC/USDT', 700, 100, .04)
    observe(engine, 'BTC/USDT', 'ETH/USDT')
    assert engine._buy('ETH/USDT', 700, 100, .04)
    engine.public.price = 40.                                    # the bot is stopped: a manual close
    engine.manual_trade('BTC/USDT', 'SELL')
    assert engine.risk.realized_stop('paper'), 'the crossing must latch when the exit books'
    engine.public.price = 160.                                   # a later profit brings the net back over
    engine.manual_trade('ETH/USDT', 'SELL')
    assert engine.risk._realized_today('paper') > -engine.cfg['daily_realized_loss_limit_pct'] * 10
    assert engine.risk.realized_stop('paper')                    # still latched: only the release phrase lifts it


def test_a_small_manual_loss_does_not_latch(engine):
    assert engine._buy('BTC/USDT', 200, 100, .04)
    engine.public.price = 98.
    engine.manual_trade('BTC/USDT', 'SELL')
    assert engine.risk._realized_today('paper') < 0
    assert engine.risk.realized_stop('paper') is None


# --- Every 'enter' decision reaches the journal or says why it did not ---------------------

def candidates(*symbols):
    return [(2.0 - index, 2e7, symbol, 100.01, 1.0, 123, 100.0, f'dec_{index}') for index, symbol in enumerate(symbols)]


def test_a_transport_failure_on_a_requote_ends_the_pass_and_is_reported(engine, monkeypatch):
    # Each further re-quote would wait out the same retries while no protective check runs.
    events = capture_events(monkeypatch)
    engine.running = True
    calls = []

    def ticker(symbol):
        calls.append(symbol)
        raise ccxt.RequestTimeout('timed out')
    monkeypatch.setattr(engine.data, 'ticker', ticker)
    try:
        engine._enter_candidates(candidates('BTC/USDT', 'ETH/USDT', 'SOL/USDT'))
    except ccxt.NetworkError:
        pass
    else:
        raise AssertionError('the transport failure must reach the scan handler')
    withheld = {fields['decision_id']: fields['reason'] for kind, fields in events if kind == 'decision.withheld'}
    assert calls == ['BTC/USDT']
    assert withheld == {'dec_0': 'requote_unavailable', 'dec_1': 'requote_unavailable', 'dec_2': 'requote_unavailable'}


def test_bad_data_on_one_requote_withholds_it_and_the_pass_goes_on(engine, monkeypatch):
    events = capture_events(monkeypatch)
    engine.running = True
    real = engine.data.ticker

    def ticker(symbol):
        if symbol == 'BTC/USDT':
            raise ccxt.BadSymbol('not quoted')
        return real(symbol)
    monkeypatch.setattr(engine.data, 'ticker', ticker)
    engine._enter_candidates(candidates('BTC/USDT', 'ETH/USDT'))
    withheld = {fields['decision_id']: fields['reason'] for kind, fields in events if kind == 'decision.withheld'}
    assert withheld == {'dec_0': 'requote_unavailable'}
    assert [p['symbol'] for p in engine._active_positions('paper')] == ['ETH/USDT']


def test_an_error_that_ends_the_pass_still_withholds_the_rest(engine, monkeypatch):
    events = capture_events(monkeypatch)
    engine.running = True
    real = engine._cash

    def cash(mode):
        # The pass's balance read for its second candidate fails, after the first one bought.
        if sys._getframe(1).f_code.co_name == '_enter_pass' and engine._active_positions('paper'):
            raise ccxt.NetworkError('balance read failed')
        return real(mode)
    monkeypatch.setattr(engine, '_cash', cash)
    try:
        engine._enter_candidates(candidates('BTC/USDT', 'ETH/USDT', 'SOL/USDT'))
    except ccxt.NetworkError:
        pass
    else:
        raise AssertionError('the pass error must still reach the scan handler')
    withheld = {fields['decision_id']: fields['reason'] for kind, fields in events if kind == 'decision.withheld'}
    assert [p['symbol'] for p in engine._active_positions('paper')] == ['BTC/USDT']
    assert withheld == {'dec_1': 'entry_pass_error', 'dec_2': 'entry_pass_error'}


def test_a_stop_during_the_scan_withholds_its_enter_decisions(engine, monkeypatch):
    events = capture_events(monkeypatch)
    monkeypatch.setattr(trader, 'entry_signal', lambda *a, **k: (True, 'BUY setup', 100))
    original, seen = decision.evaluate_entry, []

    def evaluate(**kwargs):
        verdict = original(**kwargs)
        seen.append(verdict)
        if len(seen) == 8:
            engine.stop_event.set()                               # Stop lands as the scan finishes its last symbol
        return verdict
    monkeypatch.setattr(trader.decision, 'evaluate_entry', evaluate)
    engine.running = True
    engine.stop_event.clear()
    engine.scan_once(allow_orders=True)
    enters = {verdict.decision_id for verdict in seen if verdict.action == 'enter'}
    withheld = {fields['decision_id']: fields['reason'] for kind, fields in events if kind == 'decision.withheld'}
    assert enters and set(withheld) == enters
    assert set(withheld.values()) == {'orders_not_allowed'}


def test_a_live_halt_withholds_the_candidates_after_it(engine, monkeypatch):
    from test_engine import arm_fake_live
    events = capture_events(monkeypatch)
    arm_fake_live(engine)
    engine.running = True
    observe(engine, 'BTC/USDT', 'ETH/USDT', 'SOL/USDT')

    def halting_buy(symbol, *args, **kwargs):
        engine.live_halted = True
        return False
    monkeypatch.setattr(engine, '_buy', halting_buy)
    engine._enter_candidates(candidates('BTC/USDT', 'ETH/USDT', 'SOL/USDT'))
    withheld = {fields['decision_id']: fields['reason'] for kind, fields in events if kind == 'decision.withheld'}
    assert withheld == {'dec_0': 'not_submitted', 'dec_1': 'live_halted', 'dec_2': 'live_halted'}


# --- A Stop inside the feed's own start cannot leave an unreachable stream -----------------

class NeverClient:
    has = {'watchBidsAsks': True}
    markets, tickers = {}, {}

    async def watch_bids_asks(self, symbols):
        await asyncio.sleep(3600)

    async def close(self):
        pass


def test_a_stop_between_the_feeds_creation_and_its_start_still_stops_it(engine, monkeypatch):
    engine.cfg = {**engine.cfg, 'streaming_quotes': True}
    created = []

    class RacingFeed(feed.QuoteFeed):
        def __init__(self, *args, **kwargs):
            kwargs['client_factory'] = NeverClient
            super().__init__(*args, **kwargs)
            created.append(self)

        def start(self):
            engine.stop()                    # the operator's Stop runs after self.feed is set, before the thread starts
            super().start()

    monkeypatch.setattr(feed, 'QuoteFeed', RacingFeed)
    engine.start()
    stream = created[0]
    try:
        stream._thread.join(timeout=3)
        assert engine.feed is None
        assert not stream._thread.is_alive(), 'the stream must not outlive the Stop'
    finally:
        stream.stop(timeout=2)


# --- A backward clock step cannot stretch the durable slippage hold -----------------------

def test_a_backward_clock_step_cannot_stretch_the_slippage_hold(engine):
    trip_slippage(engine)
    key = engine.risk._key('slippage_breaker', engine.mode)
    record = json.loads(storage.get_meta(key))
    # Seen from a clock corrected back six hours, the stored end time is six hours further away.
    storage.set_meta(key, json.dumps({**record, 'until': record['until'] + 6 * 3600}))
    remaining = engine.risk.status()['breakers_open']['slippage']
    assert 0 < remaining <= risk.BREAKER_HOLD_S
    assert json.loads(storage.get_meta(key))['until'] <= time.time() + risk.BREAKER_HOLD_S
    assert engine.risk.status()['breakers_open']['slippage'] <= remaining   # it counts down from there
    end_slippage_hold(engine)
    assert 'slippage' not in engine.risk.status()['breakers_open']


# --- Changing exchange carries the stricter kill switch ------------------------------------

def test_an_exits_blocked_switch_tightens_an_entries_only_switch_on_the_target(engine):
    engine.update_config({'exchange': 'kraken'})
    engine.risk.engage_kill_switch('kraken drill', allow_exits=True)
    engine.update_config({'exchange': 'mexc'})
    engine.risk.release_kill_switch(risk.RELEASE_PHRASE)
    engine.risk.engage_kill_switch('incident: block everything', allow_exits=False)
    engine.update_config({'exchange': 'kraken'})
    switch = engine.risk.kill_switch()
    assert switch['engaged'] and not switch['allow_exits']
    assert 'Carried over from MEXC' in switch['reason']


def test_an_entries_only_switch_does_not_loosen_an_exits_blocked_target(engine):
    engine.update_config({'exchange': 'kraken'})
    engine.risk.engage_kill_switch('kraken incident', allow_exits=False)
    engine.update_config({'exchange': 'mexc'})
    engine.risk.release_kill_switch(risk.RELEASE_PHRASE)
    engine.risk.engage_kill_switch('mexc drill', allow_exits=True)
    engine.update_config({'exchange': 'kraken'})
    switch = engine.risk.kill_switch()
    assert switch['engaged'] and not switch['allow_exits'] and switch['reason'] == 'kraken incident'


# --- A rollback is not blocked by a paper intent it discards anyway ------------------------

from test_review_round3 import data_folder, legacy_insert  # noqa: E402,F401  (fixture)


def test_an_open_paper_intent_does_not_block_the_rollback_that_drops_it(data_folder):
    import sqlite3
    import recovery
    paper = storage.create_order_intent('kraken', 'ETH/USDT', 'BUY', cost=50, mode='paper')
    storage.update_order_intent(paper, 'uncertain', note='paper submit raised')   # left open on another exchange
    assert recovery.prepare_downgrade(data_folder, recovery.DOWNGRADE_PHRASE)['paper_rows_removed'] == 1
    database = data_folder / 'portfolio.sqlite3'
    legacy_insert(database)
    with sqlite3.connect(str(database)) as db:
        assert db.execute('SELECT COUNT(*) FROM order_journal WHERE intent_id=?', (paper,)).fetchone()[0] == 0


# --- Every exit decision names its trigger; gate refusals are withheld; slippage at the fill ---

def test_a_protective_exit_decision_names_its_trigger(engine, monkeypatch):
    events = capture_events(monkeypatch)
    assert engine._buy('BTC/USDT', 800, 100, .04)
    engine.running = True
    engine.public.price = 90.                                    # under the stored stop
    engine._monitor_if_due(force=True)
    exits = [fields for kind, fields in events if kind == 'decision.exit']
    assert exits and exits[0]['source'] == 'protective' and exits[0]['trigger'] == 'stored'
    assert not engine._active_positions('paper')


def test_entries_the_risk_gate_refuses_are_withheld_as_such(engine, monkeypatch):
    events = capture_events(monkeypatch)
    engine.running = True
    engine.risk.engage_kill_switch('drill', allow_exits=True)
    engine._enter_candidates(candidates('BTC/USDT', 'ETH/USDT'))
    withheld = {fields['decision_id']: fields['reason'] for kind, fields in events if kind == 'decision.withheld'}
    blocked = {fields.get('decision_id') for kind, fields in events if kind == 'risk.blocked'}
    assert withheld == {'dec_0': 'risk_gate', 'dec_1': 'risk_gate'} and blocked == {'dec_0', 'dec_1'}


def test_the_slippage_hold_opens_and_is_reported_when_the_bad_fill_lands(engine):
    client = arm_fake_live(engine)
    engine.cfg['max_positions'] = 8
    client.fill = {**client.fill, 'average': 101.0, 'cost': 101.0, 'filled': 1.0}   # 1 % worse every time
    for symbol in ('BTC/USDT', 'ETH/USDT', 'SOL/USDT'):
        assert engine._buy(symbol, 150, 100, .04)
        observe(engine, symbol)
    # No further entry attempt: the state view already shows the full hold.
    assert engine.risk.status()['breakers_open']['slippage'] > risk.BREAKER_HOLD_S - 60


# --- Coverage gaps the final test review found (each test fails on the mutant it names) -----

from test_engine import arm_fake_live  # noqa: E402
from test_review_round3 import LifecycleFeed, book_sell, mexc_market  # noqa: E402


def journal(mode):
    return storage.rows('SELECT * FROM order_journal WHERE mode=? ORDER BY created_at', (mode,))


def test_a_clear_floor_from_an_earlier_day_does_not_shield_todays_loss(engine):
    # Mutant: the clear floor applied on any day.
    storage.set_meta(engine.risk._key('realized_stop_cleared', 'paper'),
                     json.dumps({'date': '2000-01-01', 'realized': -400}))
    book_sell(engine, -350.)
    engine._daily_allowed('paper', engine._equity('paper', engine._cash('paper'), []))
    assert engine.risk.realized_stop('paper')


def test_the_launcher_runs_the_engine_first_server_and_its_exit_stops_the_engine(monkeypatch, tmp_path):
    # Mutants: main() building a plain uvicorn.Server; request_exit not stopping the engine.
    import socket
    import uvicorn
    import app as webapp
    import launcher
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        port = probe.getsockname()[1]
    monkeypatch.setenv('CRYPTOPILOT_PORT', str(port))
    monkeypatch.setenv('CRYPTOPILOT_DATA_DIR', str(tmp_path))
    monkeypatch.setenv('CRYPTOPILOT_NO_BROWSER', '1')
    stops, seen = [], {}
    monkeypatch.setattr(webapp.engine, 'stop', lambda: stops.append(1))

    def fake_run(self, sockets=None):
        seen['class'] = type(self).__name__
        webapp.app.state.request_exit()
        seen['should_exit'] = self.should_exit
    monkeypatch.setattr(uvicorn.Server, 'run', fake_run)
    try:
        assert launcher.main() == 0
    finally:
        webapp.app.state.request_exit = None
    deadline = time.monotonic() + 3
    while not stops and time.monotonic() < deadline:
        time.sleep(.02)
    assert seen['class'] == 'EngineFirstServer'
    assert seen['should_exit'] and stops == [1]


@pytest.mark.parametrize('side', ['BUY', 'SELL'])
def test_a_live_fill_accounting_failure_names_its_intent(engine, monkeypatch, side):
    # Mutants: intent ids dropped from the failure sites; live fill failures back to a bare halt.
    client = arm_fake_live(engine)
    if side == 'SELL':
        assert engine._buy('BTC/USDT', 800, 100, .04)
    events = capture_events(monkeypatch)
    client.fill = {**client.fill, 'fee': None, 'fees': 'garbage'}
    if side == 'BUY':
        assert not engine._buy('ETH/USDT', 800, 100, .04)
    else:
        assert not engine._sell(engine._active_positions('live')[0], 100, 'test exit')
    row = journal('live')[-1]
    failure = next(fields for kind, fields in events if kind == 'ledger.failure')
    halt = next(fields for kind, fields in events if kind == 'execution.halt')
    assert failure['intent_id'] == row['intent_id'] == halt['intent_id']


def test_an_unfilled_order_whose_journal_write_fails_names_its_intent(engine, monkeypatch):
    import orders
    events = capture_events(monkeypatch)
    arm_fake_live(engine)
    monkeypatch.setattr(engine, '_confirm_filled', lambda *a, **k: (_ for _ in ()).throw(orders.Unfilled('none')))
    original = storage.update_order_intent

    def failing(intent, status, **kwargs):
        if status == 'rejected':
            raise OSError('disk')
        return original(intent, status, **kwargs)
    monkeypatch.setattr(storage, 'update_order_intent', failing)
    assert not engine._buy('BTC/USDT', 800, 100, .04)
    failure = next(fields for kind, fields in events if kind == 'ledger.failure')
    assert failure['intent_id'] == journal('live')[-1]['intent_id']


def test_a_paper_fill_accounting_failure_names_its_intent(engine, monkeypatch):
    events = capture_events(monkeypatch)
    monkeypatch.setattr(engine, '_fill_details', lambda *a: (_ for _ in ()).throw(ValueError('bad')))
    assert not engine._buy('BTC/USDT', 800, 100, .04)
    failure = next(fields for kind, fields in events if kind == 'ledger.failure')
    assert failure['intent_id'] == journal('paper')[-1]['intent_id']


def test_a_usdt_fee_above_the_configured_rate_is_booked_in_full(engine):
    # Mutant: the third-asset estimate not clamped at zero.
    client = arm_fake_live(engine)
    client.fill = {**client.fill, 'fee': None, 'fees': [{'currency': 'USDT', 'cost': .3}, {'currency': 'MX', 'cost': .01}]}
    assert engine._buy('BTC/USDT', 800, 100, .04)
    trade = storage.rows("SELECT gross,fee FROM trades WHERE mode='live' AND side='BUY'")[0]
    assert trade['fee'] == pytest.approx(.3)
    assert engine._active_positions('live')[0]['cost'] == pytest.approx(trade['gross'] + .3)


def test_a_worker_exit_while_start_prepares_the_stream_stops_it(engine, monkeypatch):
    # Mutant: the abandon check ignoring ``running``.
    engine.cfg = {**engine.cfg, 'streaming_quotes': True}
    created = []
    monkeypatch.setattr(feed, 'QuoteFeed', lambda *a, **k: created.append(LifecycleFeed()) or created[-1])
    monkeypatch.setattr(engine, '_loop', lambda: None)
    prepare = engine._feed_symbols

    def worker_exited(symbols=None):
        with engine.lock:
            engine.running = False                                 # as the worker's finally does
        return prepare(symbols)
    monkeypatch.setattr(engine, '_feed_symbols', worker_exited)
    engine.start()
    assert created and created[0].started and created[0].stopped and engine.feed is None


def test_a_scan_pushes_only_listed_pairs_to_the_stream(engine):
    # Mutant: scan_once passing its raw symbol list to set_symbols.
    engine.cfg['watchlist'] = list(engine.cfg['watchlist']) + ['NOTLISTED/USDT']

    class Sink:
        state = 'live'

        def __init__(self):
            self.calls = []

        def set_symbols(self, symbols):
            self.calls.append(list(symbols))

        def ticker(self, *args, **kwargs):
            return None

        def status(self):
            return {'state': 'live', 'enabled': True}

        def stop(self, timeout=None):
            pass
    engine.feed = sink = Sink()
    engine.scan_once(allow_orders=False)
    assert sink.calls and all('NOTLISTED/USDT' not in call for call in sink.calls)


def test_a_dust_merge_carries_the_third_asset_fee_estimate(engine):
    # Mutant: the merged cost basis without the estimate.
    engine.paper.fee_currency = 'MX'
    with storage.connection() as db:
        db.execute('''INSERT INTO positions(exchange,mode,symbol,qty,entry_price,cost,highest,stop,target,opened_at)
                      VALUES (?,?,?,?,?,?,?,?,?,?)''',
                   (engine.exchange_name, 'paper', 'BTC/USDT', .05, 100, 5., 100, 96, 111, storage._now()))
    observe(engine, 'BTC/USDT')
    cash_before = engine._cash('paper')
    assert engine._buy('BTC/USDT', 200, 100, .04, merge_dust=True), engine.risk_block
    spent = cash_before - engine._cash('paper')
    trade = storage.rows("SELECT fee FROM trades WHERE mode='paper' AND side='BUY'")[0]
    assert trade['fee'] > 0
    assert engine._active_positions('paper')[0]['cost'] == pytest.approx(5. + spent + trade['fee'])


def _withhold_setup(name, engine, monkeypatch):
    if name == 'stale_marks':
        monkeypatch.setattr(engine, '_marks_fresh', lambda positions: False)
    elif name == 'max_positions':
        engine.cfg['max_positions'] = 0
    elif name == 'same_candle':
        monkeypatch.setattr(engine, '_same_candle_blocked', lambda *a: True)
    elif name == 'requote_moved':
        monkeypatch.setattr(trader, 'entry_price_ok', lambda *a, **k: False)
    elif name == 'requote_liquidity':
        engine.cfg['min_quote_volume'] = 1e15
    elif name == 'requote_unavailable':
        monkeypatch.setattr(engine, '_observe_ticker', lambda *a, **k: (_ for _ in ()).throw(ValueError('x')))
    elif name == 'budget':
        monkeypatch.setattr(engine, '_entry_budget', lambda *a: (5., 100., .04))
    elif name == 'daily_loss_guard':
        monkeypatch.setattr(engine, '_daily_allowed', lambda *a: (False, 9.0))


@pytest.mark.parametrize('reason', ['stale_marks', 'max_positions', 'same_candle', 'requote_moved',
                                    'requote_liquidity', 'requote_unavailable', 'budget', 'daily_loss_guard'])
def test_each_withheld_reason_is_recorded_with_its_decision(engine, monkeypatch, reason):
    # Mutant: every other _withhold call removed.
    events = capture_events(monkeypatch)
    engine.running = True
    _withhold_setup(reason, engine, monkeypatch)
    engine._enter_candidates(candidates('BTC/USDT'))
    withheld = {fields['decision_id']: fields['reason'] for kind, fields in events if kind == 'decision.withheld'}
    assert withheld == {'dec_0': reason}


def test_a_manual_sell_journals_its_own_request_id(engine):
    # Mutant: the manual SELL without its decision id.
    buy_id = '0f1e2d3c-4b5a-4968-8776-655443322110'
    sell_id = '1f1e2d3c-4b5a-4968-8776-655443322111'
    assert engine.manual_trade('BTC/USDT', 'BUY', quote_amount=100, request_id=buy_id)['status'] == 'filled'
    assert engine.manual_trade('BTC/USDT', 'SELL', request_id=sell_id)['status'] in ('filled', 'partial')
    row = storage.rows("SELECT decision_id FROM order_journal WHERE mode='paper' AND side='SELL'")[0]
    assert row['decision_id'] == f'manual_{sell_id}'


def test_a_carried_switch_keeps_protective_exits_allowed(engine):
    # Mutant: the carried switch engaged with exits blocked.
    engine.risk.engage_kill_switch('incident', allow_exits=True)
    engine.update_config({'exchange': 'kraken'})
    switch = engine.risk.kill_switch()
    assert switch['engaged'] and switch['allow_exits']


def test_the_rejection_spike_closes_when_the_second_oldest_counted_rejection_ages_out(engine):
    # Mutant: the countdown taken from the oldest row.
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    for age in (500, 400, 300, 200):
        intent = storage.create_order_intent(engine.exchange_name, 'BTC/USDT', 'BUY', cost=100, mode='paper')
        stamp = (now - timedelta(seconds=age)).isoformat(timespec='milliseconds')
        with storage.connection() as db:
            db.execute("UPDATE order_journal SET status='rejected', submitted_at=?, updated_at=? WHERE intent_id=?",
                       (stamp, stamp, intent))
    assert engine.risk._reject_spike_remaining('paper') == pytest.approx(risk.REJECT_SPIKE_WINDOW_S - 400, abs=5)


def test_the_rollback_preparation_runs_from_the_command_line(data_folder, capsys):
    # Mutant: the --prepare-downgrade flag not wired to the step.
    import sqlite3
    import recovery
    live = storage.create_order_intent('mexc', 'BTC/USDT', 'BUY', cost=100, mode='live')
    storage.update_order_intent(live, 'rejected', note='test')
    assert recovery.main(['--prepare-downgrade', '--confirm', recovery.DOWNGRADE_PHRASE]) == 0
    assert json.loads(capsys.readouterr().out)['changed'] is True
    with sqlite3.connect(str(data_folder / 'portfolio.sqlite3')) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 2


def test_a_single_mexc_pair_falls_back_to_the_bulk_read_when_the_market_is_unknown():
    from market_data import MarketData
    client = ccxt.mexc({'enableRateLimit': False})
    client.set_markets([mexc_market('BTCUSDT', 'BTC/USDT')])
    client.market = lambda symbol: (_ for _ in ()).throw(ccxt.ExchangeError('mexc markets not loaded'))
    client.fetch_tickers = lambda symbols=None, params={}: {'BTC/USDT': {'symbol': 'BTC/USDT', 'bid': 100., 'ask': 100.1}}
    assert MarketData(client).quotes(['BTC/USDT'])['BTC/USDT']['bid'] == 100.


# --- The last independent check: booked fills stay booked, every enter has an outcome --------

@pytest.mark.parametrize('side', ['BUY', 'SELL'])
def test_a_report_failing_after_the_ledger_commit_leaves_the_fill_booked(engine, monkeypatch, side):
    client = arm_fake_live(engine)
    if side == 'SELL':
        assert engine._buy('BTC/USDT', 800, 100, .04)
    real_audit = storage.audit

    def audit(level, message, *args, **kwargs):
        if level in ('trade', 'warning'):
            raise storage.sqlite3.OperationalError('database is locked')
        return real_audit(level, message, *args, **kwargs)
    monkeypatch.setattr(storage, 'audit', audit)
    if side == 'BUY':
        assert engine._buy('ETH/USDT', 800, 100, .04)
    else:
        assert engine._sell(engine._active_positions('live')[0], 100, 'test exit')
    assert not engine.live_halted and not engine._durable_live_block()
    assert journal('live')[-1]['status'] == 'accounted'
    assert client is engine.private


@pytest.mark.parametrize('mode', ['paper', 'live'])
def test_a_failed_trade_audit_line_does_not_skip_the_loss_and_slippage_checks(engine, monkeypatch, mode):
    events = capture_events(monkeypatch)
    if mode == 'live':
        client = arm_fake_live(engine)
        client.fill = {**client.fill, 'filled': 8., 'cost': 800., 'average': 100.}
    assert engine._buy('BTC/USDT', 800, 100, .04)
    if mode == 'live':
        client.fill = {**client.fill, 'filled': 8., 'cost': 320., 'average': 40.}
    real_audit, checked = storage.audit, []

    def audit(level, message, *args, **kwargs):
        if level in ('trade', 'warning'):
            raise storage.sqlite3.OperationalError('database is locked')
        return real_audit(level, message, *args, **kwargs)
    monkeypatch.setattr(storage, 'audit', audit)
    real_fills = engine.risk.observe_fills
    monkeypatch.setattr(engine.risk, 'observe_fills', lambda m: checked.append(m) or real_fills(m))
    assert engine._sell(engine._active_positions(mode)[0], 40, 'test exit')
    assert engine.risk._realized_today(mode) < -300
    assert engine.risk.realized_stop(mode), 'the latch must not depend on the audit line'
    assert checked == [mode]
    kinds = [kind for kind, fields in events if fields.get('intent_id') == journal(mode)[-1]['intent_id']]
    assert 'ledger.accounted' in kinds and 'ledger.post_commit_error' in kinds



def failing_audit(*levels):
    real_audit = storage.audit

    def audit(level, message, *args, **kwargs):
        if level in levels:
            raise storage.sqlite3.OperationalError('database is locked')
        return real_audit(level, message, *args, **kwargs)
    return audit


@pytest.mark.parametrize('mode', ['paper', 'live'])
def test_a_failed_trade_audit_line_after_a_buy_keeps_its_accounted_line_and_judges_slippage(engine, monkeypatch, mode):
    # Mutants: the buy's reports not contained on their own (the slippage
    # check skipped), or the trade audit written before ledger.accounted.
    events = capture_events(monkeypatch)
    if mode == 'live':
        arm_fake_live(engine)
    checked = []
    monkeypatch.setattr(storage, 'audit', failing_audit('trade', 'warning'))
    real_fills = engine.risk.observe_fills
    monkeypatch.setattr(engine.risk, 'observe_fills', lambda m: checked.append(m) or real_fills(m))
    assert engine._buy('ETH/USDT', 800, 100, .04)
    assert checked == [mode]
    assert not engine.live_halted and journal(mode)[-1]['status'] == 'accounted'
    kinds = [kind for kind, fields in events if fields.get('intent_id') == journal(mode)[-1]['intent_id']]
    assert 'ledger.accounted' in kinds and 'ledger.post_commit_error' in kinds


@pytest.mark.parametrize('mode', ['paper', 'live'])
def test_a_failed_loss_check_whose_warning_also_fails_still_judges_slippage(engine, monkeypatch, mode):
    # Mutant: the realised-loss fallback written with storage.audit, which
    # can raise past the slippage check that follows it.
    if mode == 'live':
        arm_fake_live(engine)
    assert engine._buy('BTC/USDT', 800, 100, .04)
    checked = []
    monkeypatch.setattr(storage, 'audit', failing_audit('warning'))
    monkeypatch.setattr(engine.risk, 'observe_realized',
                        lambda *a: (_ for _ in ()).throw(storage.sqlite3.OperationalError('database is locked')))
    real_fills = engine.risk.observe_fills
    monkeypatch.setattr(engine.risk, 'observe_fills', lambda m: checked.append(m) or real_fills(m))
    assert engine._sell(engine._active_positions(mode)[0], 100, 'test exit')
    assert checked == [mode]
    assert not engine.live_halted and journal(mode)[-1]['status'] == 'accounted'


@pytest.mark.parametrize('side', ['BUY', 'SELL'])
def test_a_failed_ledger_commit_is_a_ledger_failure_not_a_booked_fill(engine, monkeypatch, side):
    # Mutant: booked set inside the transaction, before its commit, so a
    # commit that fails would count as a booked fill with a failed report.
    import contextlib
    client = arm_fake_live(engine)
    if side == 'SELL':
        assert engine._buy('BTC/USDT', 800, 100, .04)
    events = capture_events(monkeypatch)
    real_connection, failed = storage.connection, []

    class Recording:
        def __init__(self, con):
            self.con, self.trade = con, False

        def execute(self, sql, *args):
            self.trade = self.trade or 'INSERT INTO trades' in sql
            return self.con.execute(sql, *args)

        def __getattr__(self, name):
            return getattr(self.con, name)

    @contextlib.contextmanager
    def connection():
        with real_connection() as con:
            db = Recording(con)
            yield db
            if db.trade and not failed:       # the body ran; the commit is what fails
                failed.append(True)
                raise storage.sqlite3.OperationalError('disk I/O error')
    monkeypatch.setattr(storage, 'connection', connection)
    held = len(engine._active_positions('live'))
    if side == 'BUY':
        assert not engine._buy('ETH/USDT', 800, 100, .04)
    else:
        assert not engine._sell(engine._active_positions('live')[0], 100, 'test exit')
    assert failed and engine.live_halted
    assert len(engine._active_positions('live')) == held      # rolled back: nothing booked
    row = journal('live')[-1]
    assert row['side'] == side and row['status'] != 'accounted'
    kinds = [kind for kind, fields in events if fields.get('intent_id') == row['intent_id']]
    assert 'ledger.failure' in kinds and 'ledger.post_commit_error' not in kinds
    assert 'ledger.accounted' not in kinds
    assert client is engine.private

def test_a_live_exit_that_crosses_the_realised_loss_limit_latches_at_once(engine):
    client = arm_fake_live(engine)
    client.fill = {**client.fill, 'filled': 8., 'cost': 800., 'average': 100.}
    assert engine._buy('BTC/USDT', 800, 100, .04)
    client.fill = {**client.fill, 'filled': 8., 'cost': 320., 'average': 40.}
    assert engine._sell(engine._active_positions('live')[0], 40, 'test exit')
    assert engine.risk._realized_today('live') < -400
    assert engine.risk.realized_stop('live')


def test_the_slippage_hold_opens_when_the_third_bad_fill_is_a_sell(engine):
    client = arm_fake_live(engine)
    engine.cfg['max_positions'] = 8
    client.fill = {**client.fill, 'average': 101.0, 'cost': 101.0, 'filled': 1.0}   # buys 1 % worse
    for symbol in ('BTC/USDT', 'ETH/USDT'):
        assert engine._buy(symbol, 150, 100, .04)
        observe(engine, symbol)
    assert 'slippage' not in engine.risk.status()['breakers_open']
    client.fill = {**client.fill, 'average': 99.0, 'cost': 99.0, 'filled': 1.0}     # the sell 1 % worse
    position = next(p for p in engine._active_positions('live') if p['symbol'] == 'BTC/USDT')
    assert engine._sell(position, 100, 'test exit')
    assert engine.risk.status()['breakers_open']['slippage'] > risk.BREAKER_HOLD_S - 60


@pytest.mark.parametrize('reason', ['held', 'protective_exit_pending', 'settings_review', 'orders_not_allowed'])
def test_the_remaining_withheld_reasons_are_recorded_with_their_decision(engine, monkeypatch, reason):
    events = capture_events(monkeypatch)
    engine.running = True
    symbol = 'ETH/USDT'
    if reason == 'held':
        assert engine._buy('BTC/USDT', 200, 100, .04)
        observe(engine, 'BTC/USDT')
        symbol = 'BTC/USDT'
    elif reason == 'protective_exit_pending':
        assert engine._buy('BTC/USDT', 200, 100, .04)
        observe(engine, 'BTC/USDT')
        engine.last_prices['BTC/USDT'] = engine._active_positions('paper')[0]['stop'] - 1
    elif reason == 'settings_review':
        arm_fake_live(engine)
        monkeypatch.setattr(storage, 'settings_health', lambda: {'review_required': True})
    elif reason == 'orders_not_allowed':
        engine.stop_event.set()
    engine._enter_candidates(candidates(symbol))
    withheld = {fields['decision_id']: fields['reason'] for kind, fields in events if kind == 'decision.withheld'}
    assert withheld == {'dec_0': reason}


@pytest.mark.parametrize('mode', ['paper', 'live'])
def test_an_entry_that_raises_before_the_journal_is_withheld(engine, monkeypatch, mode):
    events = capture_events(monkeypatch)
    engine.running = True
    if mode == 'live':
        arm_fake_live(engine)
        observe(engine, 'BTC/USDT', 'ETH/USDT')
    real = engine._buy

    def buy(symbol, *args, **kwargs):
        if symbol == 'BTC/USDT':
            raise RuntimeError('unexpected')
        return real(symbol, *args, **kwargs)
    monkeypatch.setattr(engine, '_buy', buy)
    engine._enter_candidates(candidates('BTC/USDT', 'ETH/USDT'))
    withheld = {fields['decision_id']: fields['reason'] for kind, fields in events if kind == 'decision.withheld'}
    if mode == 'paper':                                           # paper goes on with the next candidate
        assert withheld == {'dec_0': 'entry_error'}
        assert [p['symbol'] for p in engine._active_positions('paper')] == ['ETH/USDT']
    else:                                                         # live ends the pass
        assert withheld == {'dec_0': 'entry_error', 'dec_1': 'entry_error'}


def test_an_entry_that_reached_the_journal_is_not_also_withheld(engine, monkeypatch):
    events = capture_events(monkeypatch)
    client = arm_fake_live(engine)
    client.order_error = ccxt.InvalidOrder('rejected')
    engine._enter_candidates(candidates('BTC/USDT'))
    assert [row['decision_id'] for row in journal('live')] == ['dec_0']
    assert not [fields for kind, fields in events if kind == 'decision.withheld']


@pytest.mark.parametrize('side', ['BUY', 'SELL'])
def test_a_ledger_commit_failure_names_its_intent(engine, monkeypatch, side):
    if side == 'SELL':
        assert engine._buy('BTC/USDT', 800, 100, .04)
    events = capture_events(monkeypatch)
    original = storage.update_order_intent

    def failing(intent, status, **kwargs):
        if status == 'accounted':
            raise storage.sqlite3.OperationalError('disk I/O error')
        return original(intent, status, **kwargs)
    monkeypatch.setattr(storage, 'update_order_intent', failing)
    if side == 'BUY':
        assert not engine._buy('ETH/USDT', 800, 100, .04)
    else:
        assert not engine._sell(engine._active_positions('paper')[0], 100, 'test exit')
    failure = next(fields for kind, fields in events if kind == 'ledger.failure')
    assert failure['intent_id'] == journal('paper')[-1]['intent_id']


def test_a_paper_sell_fill_accounting_failure_names_its_intent(engine, monkeypatch):
    assert engine._buy('BTC/USDT', 800, 100, .04)
    events = capture_events(monkeypatch)
    monkeypatch.setattr(engine, '_fill_details', lambda *a: (_ for _ in ()).throw(ValueError('bad')))
    assert not engine._sell(engine._active_positions('paper')[0], 100, 'test exit')
    failure = next(fields for kind, fields in events if kind == 'ledger.failure')
    row = journal('paper')[-1]
    assert row['side'] == 'SELL' and failure['intent_id'] == row['intent_id']
