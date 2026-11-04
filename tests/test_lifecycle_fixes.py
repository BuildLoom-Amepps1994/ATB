"""Regression tests for the order-lifecycle lens of the 2026-11-04 audit.

* Any exception escaping the worker loop wrote the durable live halt, so a
  locked database during a protective poll switched off every stored exit
  and refused Start although no live order outcome was open.
* A settings save that changed the exchange committed the settings file,
  the configuration and the ledger scope before its one database write, so
  a failure there left the engine quoting and simulating on the old venue
  under the new scope.
* The scan's trailing pass resubmitted a stored exit at the bid the
  protective poll had just tried.
* Trend assessments reported every pair's regime as 'weak' and halved the
  recorded confidence for a confirmation gate the Trend rule does not read.

All exchanges are fakes; no network, no credentials.
"""

import sqlite3
import threading
import time

import ccxt
import numpy as np
import pytest

import storage
from test_engine import arm_fake_live, engine  # noqa: F401  (fixture)


def _on_the_worker() -> bool:
    return threading.current_thread().name == 'TradingEngine'


def _fault_on_the_worker(engine, monkeypatch, before_fault=None):
    original = engine._active_positions
    state = {'armed': True}

    def flaky(mode):
        if state['armed'] and _on_the_worker():
            if before_fault:
                before_fault()
            raise sqlite3.OperationalError('database is locked')
        return original(mode)
    monkeypatch.setattr(engine, '_active_positions', flaky)
    return state


def _run_until_the_worker_stops(engine):
    engine.start()
    engine.thread.join(timeout=5)
    assert not engine.thread.is_alive() and not engine.running


# ------------------------------------------------------------ worker faults
def test_a_fault_outside_the_order_path_stops_the_live_worker_without_a_durable_halt(engine, monkeypatch):
    arm_fake_live(engine)
    engine.running = False
    state = _fault_on_the_worker(engine, monkeypatch)
    _run_until_the_worker_stops(engine)
    assert engine.live_halted is False and not storage.get_meta('live_halt.mexc')
    assert 'Trading worker stopped: OperationalError' in engine.last_error
    assert storage.unresolved_orders() == []
    messages = [r['message'] for r in storage.rows('SELECT message FROM audit ORDER BY id DESC LIMIT 3')]
    assert any('No live order is open, so the halt is not durable' in m for m in messages)
    assert not any(m.startswith('LIVE HALT') for m in messages)
    # Start is allowed again once the cause has gone: the stored exits resume.
    state['armed'] = False
    engine.start()
    assert engine.running
    engine.stop()
    engine.thread.join(timeout=5)


def test_the_same_fault_halts_durably_while_a_live_order_outcome_is_open(engine, monkeypatch):
    arm_fake_live(engine)
    engine.running = False

    def open_intent():   # an order the venue acknowledged moments before the fault
        intent = storage.create_order_intent('mexc', 'BTC/USDT', 'BUY', cost=100, mode='live', decision_id='d1')
        storage.update_order_intent(intent, 'acked', order_id='venue-1')
    _fault_on_the_worker(engine, monkeypatch, before_fault=open_intent)
    _run_until_the_worker_stops(engine)
    assert engine.live_halted is True and storage.get_meta('live_halt.mexc') == '1'
    with pytest.raises(storage.ConfigValidationError, match='halted'):
        engine.start()


def test_an_unreadable_journal_keeps_the_durable_halt(engine, monkeypatch):
    arm_fake_live(engine)
    engine.running = False
    _fault_on_the_worker(engine, monkeypatch)
    original = storage.unresolved_orders

    def unreadable(exchange=None):
        if _on_the_worker():
            raise sqlite3.OperationalError('locked')
        return original(exchange)
    monkeypatch.setattr(storage, 'unresolved_orders', unreadable)
    _run_until_the_worker_stops(engine)
    assert engine.live_halted is True


def test_a_worker_fault_in_paper_mode_stops_and_reports_as_before(engine, monkeypatch):
    _fault_on_the_worker(engine, monkeypatch)
    _run_until_the_worker_stops(engine)
    assert engine.live_halted is False and not storage.get_meta('live_halt.mexc')
    assert 'Trading worker stopped: OperationalError' in engine.last_error
    assert any('Trading worker stopped: OperationalError' in r['message']
               for r in storage.rows('SELECT message FROM audit ORDER BY id DESC LIMIT 3'))


# ---------------------------------------------------------- exchange switch
def test_a_failed_exchange_switch_changes_nothing(engine, monkeypatch):
    old_public, old_data, old_paper = engine.public, engine.data, engine.paper
    engine.api_key, engine.api_secret = 'mexc-key', 'mexc-secret'

    def locked(exchange):
        raise sqlite3.OperationalError('database is locked')
    monkeypatch.setattr(storage, 'abandon_stale_paper_intents', locked)
    with pytest.raises(sqlite3.OperationalError):
        engine.update_config({'exchange': 'kraken'})
    assert engine.exchange_name == engine.cfg['exchange'] == storage.settings()['exchange'] == 'mexc'
    assert engine.public is old_public and engine.data is old_data and engine.paper is old_paper
    assert engine.api_key == 'mexc-key' and not engine._busy()
    assert engine._buy('BTC/USDT', 800, 100, .04)
    assert {r['exchange'] for r in storage.rows('SELECT exchange FROM positions')} == {'mexc'}
    assert {r['exchange'] for r in storage.rows('SELECT exchange FROM order_journal')} == {'mexc'}


def test_a_successful_exchange_switch_still_closes_the_stale_intents_of_the_new_exchange(engine, monkeypatch):
    seen = []
    monkeypatch.setattr(storage, 'abandon_stale_paper_intents', lambda exchange: seen.append(exchange))
    engine.update_config({'exchange': 'kraken'})
    assert seen == ['kraken'] and engine.exchange_name == 'kraken' and engine.paper.exchange_name == 'kraken'


# ------------------------------------------------------------- stuck exits
def test_the_scan_does_not_resubmit_a_stored_exit_at_the_bid_the_poll_tried(engine, monkeypatch):
    engine.update_config({'watchlist': ['BTC/USDT']})
    assert engine._buy('BTC/USDT', 800, 100, .04)
    stop = float(engine._active_positions('paper')[0]['stop'])

    def refuse(*a, **k):
        raise ccxt.InsufficientFunds('refused')
    monkeypatch.setattr(engine.paper, 'create_order', refuse)
    seen = []
    prices = iter(round(stop * .99 - .01 * i, 4) for i in range(50))

    def fetch_ticker(symbol):
        p = next(prices)
        seen.append(p)
        return {'last': p, 'timestamp': int(time.time() * 1000), 'bid': p, 'ask': p * 1.0002, 'quoteVolume': 2e7}
    monkeypatch.setattr(engine.public, 'fetch_ticker', fetch_ticker)
    original = engine._monitor_if_due
    calls = []

    def monitor(**kw):
        calls.append(1)
        if len(calls) == 3:          # the poll scan_once makes right after the candle download
            engine._next_monitor = 0.0
        return original(**kw)
    monkeypatch.setattr(engine, '_monitor_if_due', monitor)
    engine._next_monitor = float('inf')
    engine.running = True
    engine.stop_event.clear()
    engine.scan_once(allow_orders=True)
    rows = storage.rows("SELECT intended_price, status FROM order_journal WHERE symbol='BTC/USDT' AND side='SELL' ORDER BY rowid")
    assert len(calls) >= 3
    # One exit per bid read: the scan's protective check, then the poll's; the trailing pass adds nothing.
    assert [(r['intended_price'], r['status']) for r in rows] == [(seen[0], 'rejected'), (seen[1], 'rejected')]


# ------------------------------------------------------------------- Trend
def test_trend_assessments_carry_no_regime_gate(engine, monkeypatch):
    def fetch_ohlcv(symbol, tf='1d', limit=175):
        duration = {'1h': 3600, '4h': 14400, '1d': 86400, '1w': 604800}[tf]
        end = int(time.time() // duration) * duration
        closes = list(np.linspace(100, 200, limit))          # a clean uptrend: close well above SMA100 * 1.05
        return [[(end - (limit - i) * duration) * 1000, c, c * 1.01, c * .99, c, 10000] for i, c in enumerate(closes)]
    monkeypatch.setattr(engine.public, 'fetch_ohlcv', fetch_ohlcv)
    engine.public.price = 200.0
    engine.update_config({'time_horizon': 'trend', 'watchlist': ['BTC/USDT']})
    row = engine.scan_once(allow_orders=False)[0]
    assert row['status'] == 'BUY setup' and row['candidate'] is True and row['score'] == 100
    assert row['regime'] is None and row['market'] is None
    assert row['confidence'] is not None and row['confidence'] > 0.5
    assert engine.decisions.latest['BTC/USDT'].inputs['regime_ok'] is True
