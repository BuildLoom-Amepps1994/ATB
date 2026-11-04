"""Regression tests for the observability lens of the 2026-11-04 audit.

* A protective exit failing during the scan was shown as "Scan unavailable"
  with no activity line, event or counter.
* A failed trailing-level write in live mode halted live trading durably as
  an order uncertainty although nothing had been sent to the venue.
* A confirmed venue refusal whose report line failed to write became an
  "uncertain" durable halt.
* A paper worker stopped by an exception left no activity line.
* Protective quote failures left no durable trace.
* A failed event-log write was swallowed without a counter, a state field
  or an activity line; an oversized string field produced an invalid JSON
  line.
* The activity table deduplicated only against its latest row, so two
  alternating lines flushed the history.
* The execution-failure breaker lived in memory, so a restart lifted it.
* ``_submit`` accepted any authorization object with ``allowed=True``.

All exchanges are fakes; no network, no credentials.
"""

import contextlib
import dataclasses
import json
import time

import ccxt

import observability
import risk
import storage
import trader
from test_engine import arm_fake_live, engine  # noqa: F401  (fixture)


def observe(engine, *symbols):
    for symbol in symbols:
        engine._observe_ticker(symbol, {'bid': 100, 'ask': 100.01, 'last': 100, 'quoteVolume': 20000000,
                                        'timestamp': int(time.time() * 1000)})


def _audit_messages():
    return [row['message'] for row in storage.rows('SELECT message FROM audit ORDER BY id')]


def _counter(name):
    return observability.METRICS.counters.get(name, 0)


# ------------------------------------------------------------ exit failures
def test_a_protective_exit_failing_during_the_scan_is_reported_as_an_exit_failure(engine, monkeypatch):
    assert engine._buy('BTC/USDT', 800, 100, .04)
    observe(engine, 'BTC/USDT')

    original = engine._protect_positions

    def failing(symbol, price):
        if symbol == 'BTC/USDT':
            raise RuntimeError('adapter refused')
        return original(symbol, price)
    monkeypatch.setattr(engine, '_protect_positions', failing)
    engine.running = True
    engine.stop_event.clear()
    rows = {row['symbol']: row for row in engine.scan_once(allow_orders=True)}
    assert not str(rows['BTC/USDT']['status']).startswith('Scan unavailable')
    assert engine.monitor_error == 'Protective exit failed for BTC/USDT: RuntimeError; review required'
    assert 'Protective exit failed for BTC/USDT: RuntimeError; review required' in _audit_messages()
    assert [p['symbol'] for p in engine._active_positions('paper')] == ['BTC/USDT']


def test_a_failed_trailing_write_in_live_mode_does_not_halt_live_trading(engine, monkeypatch):
    arm_fake_live(engine)
    assert engine._buy('BTC/USDT', 800, 100, .04)
    original = storage.connection

    @contextlib.contextmanager
    def failing_connection():
        with original() as db:
            class Proxy:
                def execute(self, sql, *args):
                    if sql.startswith('UPDATE positions SET highest'):
                        raise storage.sqlite3.OperationalError('disk I/O error')
                    return db.execute(sql, *args)

                def __getattr__(self, name):
                    return getattr(db, name)
            yield Proxy()
    monkeypatch.setattr(storage, 'connection', failing_connection)
    before = _counter('monitor.trail_write_failures')
    engine._mark_positions('BTC/USDT', 130.0, 2.0, False, stored_checked_at=130.0, source='monitor')
    assert not engine.live_halted and not engine._durable_live_block()
    assert _counter('monitor.trail_write_failures') == before + 1
    assert any(m.startswith('Trailing stop for BTC/USDT could not be saved (OperationalError); the stored stop 96 still applies')
               for m in _audit_messages())
    assert [p['symbol'] for p in engine._active_positions('live')] == ['BTC/USDT']


def test_a_confirmed_refusal_whose_report_fails_stays_a_rejection(engine, monkeypatch):
    client = arm_fake_live(engine)
    client.order_error = ccxt.InsufficientFunds('not enough USDT')
    original = storage.audit

    def audit(level, message):
        if 'rejected by the exchange' in message:
            raise RuntimeError('audit write failed')
        return original(level, message)
    monkeypatch.setattr(storage, 'audit', audit)
    before = _counter('execution.rejection_report_failed')
    assert not engine._buy('BTC/USDT', 200, 100, .04)
    assert storage.rows('SELECT status FROM order_journal') == [{'status': 'rejected'}]
    assert not engine.live_halted and not engine._durable_live_block()
    assert _counter('execution.rejection_report_failed') == before + 1


def test_a_paper_worker_stop_by_exception_is_audited(engine):
    engine._ledger_failure('Trading worker stopped: OperationalError; inspect local portfolio and monitoring status')
    assert engine.stop_event.is_set()
    assert 'Trading worker stopped: OperationalError; inspect local portfolio and monitoring status' in _audit_messages()


def test_protective_quote_failures_leave_a_durable_trace(engine, monkeypatch):
    assert engine._buy('BTC/USDT', 800, 100, .04)

    def failing(*args, **kwargs):
        raise ccxt.NetworkError('ticker endpoint down')
    monkeypatch.setattr(engine.data, 'quotes', failing)
    monkeypatch.setattr(engine.data, 'ticker', failing)
    engine.running = True
    before = _counter('monitor.quote_failures')
    engine._monitor_if_due(force=True)
    assert engine.monitor_error == 'Protective quotes unavailable for 1 held asset(s)'
    assert _counter('monitor.quote_failures') == before + 1
    assert 'Protective quotes unavailable for 1 held asset(s); stops were not checked this poll.' in _audit_messages()


# -------------------------------------------------------------- event log
def test_an_event_write_failure_is_counted_shown_and_cleared(engine, monkeypatch):
    original = observability._append

    def failing(text):
        raise OSError('read-only file system')
    monkeypatch.setattr(observability, '_append', failing)
    before = _counter('observability.write_failed')
    observability.event('test.kind', level='info', note='one')
    observability.event('test.kind', level='info', note='two')
    assert observability.last_write_error() == 'OSError'
    assert _counter('observability.write_failed') == before + 2
    assert sum('Event log not being written (OSError)' in m for m in _audit_messages()) == 1   # one line per change of state
    assert engine.snapshot()['event_log_error'] == 'OSError'
    monkeypatch.setattr(observability, '_append', original)
    observability.event('test.kind', level='info', note='three')
    assert observability.last_write_error() is None and engine.snapshot()['event_log_error'] is None


def test_an_oversized_string_field_still_produces_a_valid_json_line(engine):
    observability.event('test.big', level='info', error='x' * 50_000)
    line = observability.log_path().read_text(encoding='utf8').splitlines()[-1]
    record = json.loads(line)
    assert record['error'].endswith('...[truncated]') and len(line) <= observability.MAX_EVENT_BYTES


def test_alternating_audit_lines_collapse_instead_of_flushing_the_history(engine):
    storage.audit('trade', 'PAPER BUY BTC/USDT: 1 at 100')
    for _ in range(200):
        storage.audit('error', 'Protective exit failed for BTC/USDT: NetworkError; review required')
        storage.audit('error', 'Protective exit failed for ETH/USDT: NetworkError; review required')
    messages = _audit_messages()
    assert sum(m.startswith('Protective exit failed') for m in messages) == 2
    assert 'PAPER BUY BTC/USDT: 1 at 100' in messages and len(messages) < 20


# ---------------------------------------------------------- failure breaker
def test_the_execution_failure_breaker_survives_a_restart(engine):
    for _ in range(3):
        engine.risk.record_failure('entry_error')
    assert engine.risk.status()['breakers_open']['failures'] > 0
    other = trader.TradingEngine()
    assert other.risk.status()['breakers_open'].get('failures', 0) > 0
    verdict = other._authorize('BTC/USDT', 'BUY', 'entry', 100, 100, positions=[], cash=10000, stop_fraction=.04)
    assert not verdict.allowed and 'Circuit breaker: repeated execution failures; new entries paused' in verdict.failed


# ---------------------------------------------------------- authorization
def test_a_hand_built_authorization_is_refused_by_submit(engine):
    client = arm_fake_live(engine)
    good = engine._authorize('BTC/USDT', 'BUY', 'entry', 100, 100, positions=[], cash=10000, stop_fraction=.04)
    assert good.allowed
    forged = risk.Authorization(True, good.request, (), (), 'forged-token')
    assert engine._submit(client, 'BTC/USDT', 'BUY', cost=100, authorization=forged) == (None, None)
    resized = risk.Authorization(True, dataclasses.replace(good.request, notional=1000.0), (), (), good.token)
    assert engine._submit(client, 'BTC/USDT', 'BUY', cost=1000, authorization=resized) == (None, None)
    assert client.orders == [] and storage.rows('SELECT status FROM order_journal') == []
    assert sum('no valid risk authorization' in m for m in _audit_messages()) >= 1
    assert engine._submit(client, 'BTC/USDT', 'BUY', cost=100, authorization=good)[1] is not None
    assert len(client.orders) == 1
