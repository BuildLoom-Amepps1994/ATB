"""Regression tests for the audit follow-ups: quote regression, shutdown, decision trail, entry errors."""
import json
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import decision
import storage
import trader
from test_decisions import decide, signal_row
from test_engine import FakeExchange, arm_fake_live, engine  # noqa: F401  (fixture)
from test_web_with_mock_exchange import webapp  # noqa: F401  (fixture)


def ticker(price, ts_ms):
    return {'bid': price * .9999, 'ask': price * 1.0001, 'timestamp': ts_ms, 'quoteVolume': 2e7}


def capture_events(monkeypatch):
    events = []
    monkeypatch.setattr(trader.observability, 'event', lambda kind, level='info', **fields: events.append((kind, fields)))
    return events


# --- Quote ordering ----------------------------------------------------------------

def test_an_older_snapshot_never_rolls_a_newer_mark_back(engine):
    now = time.time()
    engine._observe_ticker('BTC/USDT', ticker(100, int(now * 1000)))
    before = engine.metrics.snapshot()['counters'].get('quotes.superseded', 0)
    price, volume, spread = engine._observe_ticker('BTC/USDT', ticker(80, int((now - 5) * 1000)))
    assert price == pytest.approx(100 * .9999) and engine.last_prices['BTC/USDT'] == pytest.approx(100 * .9999)
    assert volume == 2e7 and spread == pytest.approx(.02, abs=1e-6)
    assert engine.metrics.snapshot()['counters']['quotes.superseded'] == before + 1
    # Buy-side reads keep the stored ask as well.
    assert engine._observe_ticker('BTC/USDT', ticker(80, int((now - 5) * 1000)), for_buy=True)[0] == pytest.approx(100 * 1.0001)
    # A newer quote, or one inside the timestamp tolerance, still replaces it.
    engine._observe_ticker('BTC/USDT', ticker(95, int((now + .5) * 1000)))
    assert engine.last_prices['BTC/USDT'] == pytest.approx(95 * .9999)
    engine._observe_ticker('BTC/USDT', ticker(90, int((now + 2) * 1000)))
    assert engine.last_prices['BTC/USDT'] == pytest.approx(90 * .9999)


def test_scan_snapshot_cannot_fire_a_stop_on_a_superseded_bid(engine):
    engine._buy('BTC/USDT', 800, 100, .04)          # stop at 96
    engine.running = True
    now = time.time()
    engine._observe_ticker('BTC/USDT', ticker(97, int(now * 1000)))          # protective poll, newer
    stale_bid, _, _ = engine._observe_ticker('BTC/USDT', ticker(95, int((now - 3) * 1000)))   # bulk snapshot, older
    engine._protect_positions('BTC/USDT', stale_bid)
    assert engine._active_positions('paper')                                  # 97 is the price of record
    fresh_bid, _, _ = engine._observe_ticker('BTC/USDT', ticker(95, int((now + 1) * 1000)))
    engine._protect_positions('BTC/USDT', fresh_bid)
    assert engine._active_positions('paper') == []


# --- Shutdown ---------------------------------------------------------------------

def test_wait_idle_returns_once_the_worker_has_stopped(engine):
    engine.start()
    assert engine.thread.is_alive()
    engine.stop()
    assert engine.wait_idle(5) and not engine.running
    engine._manual_order_active = True
    started = time.monotonic()
    assert engine.wait_idle(.2) is False and time.monotonic() - started >= .2
    engine._manual_order_active = False
    assert engine.wait_idle(.1)


def test_app_shutdown_waits_for_in_flight_work(webapp, monkeypatch):
    waits = []
    monkeypatch.setattr(webapp.engine, 'wait_idle', lambda timeout=30.0: waits.append(timeout) or False)
    with TestClient(webapp.app, base_url='http://127.0.0.1:8765'):
        pass
    assert waits == [30.0]
    assert any('still in flight' in row['message'] for row in storage.rows('SELECT message FROM audit'))


# --- Decision trail -----------------------------------------------------------------

def test_exit_decisions_are_written_to_the_event_log(engine, monkeypatch):
    events = capture_events(monkeypatch)
    engine._buy('BTC/USDT', 800, 100, .04)
    engine.running = True
    engine.public.price = 90
    engine._monitor_if_due(force=True)
    exits = [fields for kind, fields in events if kind == 'decision.exit']
    assert exits and exits[0]['symbol'] == 'BTC/USDT' and exits[0]['source'] == 'protective'
    assert 'stop' in exits[0]['reason'].lower() and exits[0]['stop'] == pytest.approx(100.2 * .96)
    assert engine._active_positions('paper') == []


def test_scan_exit_decisions_name_their_source(engine, monkeypatch):
    events = capture_events(monkeypatch)
    engine._buy('BTC/USDT', 800, 100, .04)
    engine.public.price, engine.running = 90, True
    engine.scan_once(allow_orders=True)
    sources = {fields['source'] for kind, fields in events if kind == 'decision.exit'}
    assert sources and sources <= {'protective', 'scan'}


def test_entry_events_carry_the_full_replayable_record(engine, monkeypatch):
    events = capture_events(monkeypatch)
    engine.scan_once(allow_orders=False)
    entries = [fields for kind, fields in events if kind == 'decision.entry']
    assert len(entries) == 8
    record = entries[0]['record']
    assert record['decision_id'] == entries[0]['decision_id'] and record['checks'] and record['reasons']
    assert record['inputs']['features']['close'] == 100.0 and set(record['inputs']['features']) == set(decision.FEATURE_KEYS)
    json.dumps(record)


def test_decision_id_depends_on_the_indicator_features():
    first, second = decide(), decide(last=signal_row(rsi=61.))
    assert first.decision_id != second.decision_id
    assert first.inputs['features']['rsi'] == 60. and second.inputs['features']['rsi'] == 61.
    assert decide(last=signal_row(efficiency=float('nan'))).inputs['features']['efficiency'] is None


def test_withheld_setups_say_why_instead_of_buy_setup():
    assert decide(quote_age_s=None).status == decision.STATUS_STALE
    assert decide(exited_this_candle=True).status == decision.STATUS_EXITED
    assert decide(held=True).status == 'BUY setup' and decide(held=True).action == 'hold'


# --- Entry pass isolation -------------------------------------------------------------

def candidates():
    return [(1.0, 2e7, 'BTC/USDT', 100.01, 1.0, 123, 100.0, 'dec_a'),
            (0.9, 2e7, 'ETH/USDT', 100.01, 1.0, 123, 100.0, 'dec_b')]


def flaky_buy(engine, monkeypatch):
    calls, original = [], engine._buy

    def buy(symbol, *args, **kwargs):
        calls.append(symbol)
        if symbol == 'BTC/USDT':
            raise RuntimeError('boom')
        return original(symbol, *args, **kwargs)
    monkeypatch.setattr(engine, '_buy', buy)
    return calls


def test_one_candidates_unexpected_failure_does_not_end_a_paper_pass(engine, monkeypatch):
    events = capture_events(monkeypatch)
    engine.running = True
    calls = flaky_buy(engine, monkeypatch)
    engine._enter_candidates(candidates())
    assert calls == ['BTC/USDT', 'ETH/USDT']
    assert [p['symbol'] for p in engine._active_positions('paper')] == ['ETH/USDT']
    assert any('entry failed unexpectedly' in row['message'] for row in storage.rows('SELECT message FROM audit'))
    assert [fields['symbol'] for kind, fields in events if kind == 'execution.entry_error'] == ['BTC/USDT']


def test_one_candidates_unexpected_failure_ends_a_live_pass(engine, monkeypatch):
    client = arm_fake_live(engine)
    calls = flaky_buy(engine, monkeypatch)
    engine._enter_candidates(candidates())
    assert calls == ['BTC/USDT'] and client.orders == []
    assert engine._active_positions('live') == []
