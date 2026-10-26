"""Second adversarial review round: latch semantics, exit freshness, fee assets, lookups, correlation, redaction."""
import json
import sys
import time
from pathlib import Path

import ccxt
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import observability
import orders
import recovery
import storage
import trader
from test_engine import FakeExchange, arm_fake_live, engine  # noqa: F401  (fixture)
from test_hardening_fixes import capture_events, ticker
from test_web_with_mock_exchange import webapp  # noqa: F401  (fixture)


def book_realized(amount, exchange='mexc'):
    with storage.connection() as db:
        db.execute('INSERT INTO trades(created_at,exchange,mode,symbol,side,price,quantity,gross,fee,realized,reason) VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                   (trader.now(), exchange, 'paper', 'SOL/USDT', 'SELL', 100, 1, 100, .1, amount, 'test'))


def audit_messages():
    return [row['message'] for row in storage.rows('SELECT message FROM audit')]


# --- Daily realised-loss stop is a latch ------------------------------------------------

def test_realised_loss_stop_latches_until_the_release_phrase(engine, monkeypatch):
    events = capture_events(monkeypatch)
    book_realized(-400)
    assert not engine._buy('BTC/USDT', 200, 100, .04)
    assert engine.risk.realized_stop('paper') and 'Realised-loss stop latched' in engine.risk_block
    assert any(kind == 'risk.realized_stop' for kind, _ in events)
    assert any('realised-loss stop latched' in message for message in audit_messages())
    engine.cfg['daily_realized_loss_limit_pct'] = 50          # a settings change does not lift it
    assert not engine._buy('BTC/USDT', 200, 100, .04)
    book_realized(600)                                        # nor does a later profit
    assert not engine._buy('BTC/USDT', 200, 100, .04)
    assert engine.risk.status()['realized_stop']
    with pytest.raises(storage.ConfigValidationError):
        engine.risk.clear_realized_stop('paper', 'resume trading')
    assert engine.risk.realized_stop('paper')
    engine.risk.clear_realized_stop('paper', 'RESUME TRADING')
    assert engine.risk.realized_stop('paper') is None
    assert engine._buy('BTC/USDT', 200, 100, .04)
    assert any(kind == 'risk.realized_stop_cleared' for kind, _ in events)


def test_paper_reset_clears_the_realised_loss_stop(engine):
    book_realized(-400)
    assert not engine._buy('BTC/USDT', 200, 100, .04)
    assert engine.risk.realized_stop('paper')
    engine.reset_paper_portfolio()
    assert engine.risk.realized_stop('paper') is None


def test_risk_api_clears_the_realised_loss_stop_with_the_phrase(webapp):
    webapp.engine.risk._latch_realized_stop('paper', -400, 300)
    assert webapp.engine.risk.status()['realized_stop']
    with pytest.raises(Exception):
        webapp.risk_action(webapp.RiskAction(action='clear_realized_stop', confirmation='wrong'))
    assert webapp.engine.risk.status()['realized_stop']
    status = webapp.risk_action(webapp.RiskAction(action='clear_realized_stop', confirmation='RESUME TRADING'))
    assert status['realized_stop'] is None


# --- Exit freshness after an invalidated quote -------------------------------------------

def test_an_invalidated_quote_leaves_the_symbol_stale_for_the_gate(engine):
    assert engine._buy('BTC/USDT', 800, 100, .04)
    now_ms = int(time.time() * 1000)
    engine._observe_ticker('BTC/USDT', ticker(100, now_ms))
    with pytest.raises(ValueError):
        engine._observe_ticker('BTC/USDT', {'bid': 101, 'ask': 100, 'timestamp': now_ms, 'quoteVolume': 2e7})
    assert 'BTC/USDT' not in engine.last_price_times and engine.last_prices['BTC/USDT'] > 0
    authorization = engine._authorize('BTC/USDT', 'SELL', 'exit', 100, 100, positions=engine._active_positions('paper'), cash=None)
    assert not authorization.allowed and 'No fresh quote' in authorization.reason
    # A symbol that was never observed at all is still judged on the supplied price.
    unobserved = engine._authorize('ETH/USDT', 'SELL', 'exit', 100, 100, positions=[], cash=None)
    assert next(check for check in unobserved.checks if check['check'] == 'symbol_quote_fresh')['passed'] is True


def test_clear_halt_reports_intents_still_open_on_another_exchange(engine):
    storage.create_order_intent('kraken', 'BTC/USD', 'BUY', cost=50, mode='live')
    storage.set_meta('live_halt.mexc', '1')
    result = recovery.clear_halt(storage.DATA_DIR, 'mexc', recovery.confirmation_phrase('mexc'))
    assert result['halt_flag_removed'] and result['other_exchange_unresolved'] == 1
    assert 'another exchange still block live trading' in result['notice']
    assert engine._durable_live_block()


# --- Fees billed in a third asset ----------------------------------------------------------

def test_a_live_fill_with_a_fee_in_another_asset_is_booked_not_halted(engine, monkeypatch):
    events = capture_events(monkeypatch)
    client = arm_fake_live(engine)
    client.fill = {**client.fill, 'fee': {'currency': 'MX', 'cost': .05}}
    assert engine._buy('BTC/USDT', 800, 100, .04)
    assert not engine.live_halted
    position = engine._active_positions('live')[0]
    estimate = 100 * engine.cfg['fee_pct'] / 100
    # Quantity is exact; the cost basis carries the fee estimate so realised P/L bears it.
    assert position['qty'] == pytest.approx(1.0) and position['cost'] == pytest.approx(100.0 + estimate)
    trade = storage.rows("SELECT fee FROM trades WHERE mode='live' AND side='BUY'")[0]
    assert trade['fee'] == pytest.approx(estimate)
    assert any('fee charged in another asset (0.05 MX)' in message for message in audit_messages())
    unvalued = [fields for kind, fields in events if kind == 'execution.fee_unvalued']
    assert unvalued and unvalued[0]['fees'] == {'MX': .05}


def test_paper_exchange_can_bill_the_fee_in_another_asset_for_parity(engine):
    engine.paper.fee_currency = 'MX'
    assert engine._buy('BTC/USDT', 800, 100, .04)
    position = engine._active_positions('paper')[0]
    assert engine._sell(position, 100, 'test exit')
    assert engine._active_positions('paper') == [] and engine.risk_block is None
    assert sum('fee charged in another asset' in message for message in audit_messages()) >= 1


# --- Order lookup on MEXC -----------------------------------------------------------------

class MexcLike:
    id, has = 'mexc', {'fetchOrder': True, 'fetchOpenOrders': True, 'fetchClosedOrders': True}
    error = ccxt.ExchangeError('mexc {"code":-2013,"msg":"Order does not exist."}')

    def fetch_order(self, order_id, symbol=None, params=None):
        raise self.error

    def fetch_open_orders(self, symbol=None, since=None, limit=None, params=None):
        return []

    def fetch_closed_orders(self, symbol=None, since=None, limit=None, params=None):
        return [{'id': 'x2', 'clientOrderId': 'listed', 'status': 'closed', 'filled': 2}]


def test_locate_treats_the_mexc_unknown_order_code_as_absent_and_consults_the_lists():
    assert orders.locate(MexcLike(), 'BTC/USDT', 'listed')['id'] == 'x2'
    assert orders.locate(MexcLike(), 'BTC/USDT', 'absent') is None
    broken = MexcLike()
    broken.error = ccxt.ExchangeError('mexc {"code":-1000,"msg":"internal error"}')
    with pytest.raises(ccxt.ExchangeError):
        orders.locate(broken, 'BTC/USDT', 'listed')
    assert not orders.unknown_order_error(type('Kraken', (), {'id': 'kraken'})(), MexcLike.error)


# --- Recovery report and live-block messages ----------------------------------------------

def test_recovery_statuses_match_the_journal_state_machine():
    assert recovery.STATUSES == set(storage.ORDER_STATUSES)


def test_live_refusal_names_the_exchange_holding_unresolved_intents(engine):
    storage.create_order_intent('kraken', 'BTC/USD', 'BUY', cost=50, mode='live')
    with pytest.raises(storage.ConfigValidationError, match='kraken: 1 unresolved live order intent'):
        engine.set_mode('live', 'ENABLE LIVE TRADING', eligible=True)


# --- Correlation and operator events in the event log --------------------------------------

def test_exit_decision_authorization_submit_and_ledger_share_one_id(engine, monkeypatch):
    events = capture_events(monkeypatch)
    assert engine._buy('BTC/USDT', 800, 100, .04)
    bought = next(fields for kind, fields in events if kind == 'ledger.accounted' and fields['side'] == 'BUY')
    assert bought['intent_id']
    engine.running = True
    engine.public.price = 90
    engine._monitor_if_due(force=True)
    exit_event = next(fields for kind, fields in events if kind == 'decision.exit')
    authorized = next(fields for kind, fields in events if kind == 'risk.authorize' and fields['side'] == 'SELL')
    accounted = next(fields for kind, fields in events if kind == 'ledger.accounted' and fields['side'] == 'SELL')
    assert exit_event['decision_id'] and authorized['decision_id'] == exit_event['decision_id'] == accounted['decision_id']
    assert accounted['intent_id'] and accounted['realized'] is not None
    row = storage.rows("SELECT decision_id,status FROM order_journal WHERE side='SELL' ORDER BY created_at DESC")[0]
    assert row['decision_id'] == exit_event['decision_id'] and row['status'] == 'accounted'


def test_operator_and_lifecycle_actions_are_events(engine, monkeypatch):
    events = capture_events(monkeypatch)
    engine.start()
    engine.stop()
    assert engine.wait_idle(5)
    settings = {key: value for key, value in engine.cfg.items() if key != 'paper_start_usdt'}
    engine.update_config({**settings, 'max_positions': 4})
    engine.set_mode('paper')
    engine.reset_paper_portfolio()
    kinds = [kind for kind, _ in events]
    for kind in ('engine.started', 'engine.stopped', 'settings.saved', 'mode.changed', 'paper.reset'):
        assert kind in kinds, kind
    saved = next(fields for kind, fields in events if kind == 'settings.saved')
    assert saved['changed'] == ['max_positions']
    started = next(fields for kind, fields in events if kind == 'engine.started')
    assert started['mode'] == 'paper' and len(started['settings_sha256']) == 16


def test_manual_orders_are_events(engine, monkeypatch):
    events = capture_events(monkeypatch)
    result = engine.manual_trade('BTC/USDT', 'BUY', quote_amount=100)
    assert result['status'] == 'filled'
    kinds = [kind for kind, _ in events]
    assert 'manual_order.requested' in kinds and 'manual_order.completed' in kinds
    # A refusal inside the recorded request (no tracked ETH position) is one failed event with its id.
    request_id = '6f1d3c3e-8c55-4c1f-9d6c-1f0a6b2e9b11'
    with pytest.raises(storage.ConfigValidationError, match='No tracked position'):
        engine.manual_trade('ETH/USDT', 'SELL', request_id=request_id)
    failed = [fields for kind, fields in events if kind == 'manual_order.failed']
    assert len(failed) == 1 and failed[0]['request_id'] == request_id and failed[0]['error'] == 'ConfigValidationError'


# --- Redaction keeps event lines valid JSON ---------------------------------------------------

def test_redaction_inside_strings_keeps_the_event_line_valid_json(engine, tmp_path, monkeypatch):
    monkeypatch.setattr(storage, 'DATA_DIR', tmp_path / 'data')
    observability.event('test.redact', error='mexc GET /api/v3/order?timestamp=1&signature=abcdef"}',
                        nested={'url': 'https://x/y?token=secrettoken', 'items': ['password=hunter2']}, reason="token='t0k'")
    line = observability.log_path().read_text().splitlines()[-1]
    record = json.loads(line)
    assert 'abcdef' not in line and 'secrettoken' not in line and 'hunter2' not in line and 't0k' not in line
    assert record['error'].endswith('signature=[REDACTED]"}') and record['nested']['url'].endswith('token=[REDACTED]')
    assert json.loads(storage.redact('{"a":"signature=abc","b":1}')) == {'a': 'signature=[REDACTED]', 'b': 1}


# --- Start-up failures leave a usable trace -------------------------------------------------

def test_startup_failure_is_reported_safely_and_logged(tmp_path, monkeypatch, capsys):
    import launcher
    monkeypatch.setenv('CRYPTOPILOT_DATA_DIR', str(tmp_path / 'portfolio'))
    try:
        raise RuntimeError('database locked; token=abc123')
    except RuntimeError as exc:
        launcher.report_startup_failure(exc)
    out = capsys.readouterr().out
    assert 'could not start: RuntimeError' in out and 'abc123' not in out and str(tmp_path / 'portfolio') in out
    log = (tmp_path / 'portfolio' / 'logs' / 'startup.log').read_text()
    assert 'RuntimeError' in log and 'test_review_round2.py' in log and 'abc123' not in log and '[REDACTED]' in log
