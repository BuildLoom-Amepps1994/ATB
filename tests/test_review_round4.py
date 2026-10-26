"""Regression tests for the final review round: manual-exit latch, withheld entries, feed start, clock-capped hold."""
import asyncio
import json
import sys
import time
from pathlib import Path

import ccxt

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


def test_a_network_error_on_one_requote_withholds_it_and_the_pass_goes_on(engine, monkeypatch):
    events = capture_events(monkeypatch)
    engine.running = True
    real = engine.data.ticker

    def ticker(symbol):
        if symbol == 'BTC/USDT':
            raise ccxt.NetworkError('reset by peer')
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
    assert withheld == {'dec_1': 'live_halted', 'dec_2': 'live_halted'}


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
