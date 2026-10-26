"""Hard risk controls: every order path is gated, and the gate cannot be bypassed.

All exchanges are fakes. No network, no credentials.
"""
import json
import time
import uuid

import pytest

import risk
import storage
import trader
from test_engine import arm_fake_live, engine  # noqa: F401  (fixture)


def quote(bid=100, ask=100.01):
    return {'bid': bid, 'ask': ask, 'last': bid, 'timestamp': int(time.time() * 1000), 'quoteVolume': 20000000}


def observe(engine, *symbols):
    for symbol in symbols:
        engine._observe_ticker(symbol, quote())


def candidate(symbol='ETH/USDT'):
    # (score, volume, symbol, observed price, atr, candle, signal close)
    return (100, 20000000, symbol, 100.01, 5, 123, 100)


def journal_rows():
    return storage.rows('SELECT * FROM order_journal ORDER BY created_at')


# ----------------------------------------------------------------- kill switch

def test_kill_switch_blocks_automatic_and_manual_entries_but_allows_protective_exits(engine):
    assert engine._buy('BTC/USDT', 200, 100, .04)
    observe(engine, 'BTC/USDT', 'ETH/USDT')
    engine.risk.engage_kill_switch('test')
    engine.running = True
    engine._enter_candidates([candidate()])
    assert [p['symbol'] for p in engine._active_positions('paper')] == ['BTC/USDT']
    assert engine.risk_block.startswith('Kill switch')
    engine.running = False
    with pytest.raises(storage.ConfigValidationError, match='Kill switch'):
        engine.manual_trade('ETH/USDT', 'buy', 100, request_id=str(uuid.uuid4()))
    # A protective exit is still allowed with the default policy.
    engine.public.price, engine.running = 90, True
    assert engine._monitor_if_due(force=True) == {'BTC/USDT'}
    assert engine._active_positions('paper') == []


def test_kill_switch_can_block_exits_too_and_survives_restart(engine):
    assert engine._buy('BTC/USDT', 200, 100, .04)
    engine.risk.engage_kill_switch('test', allow_exits=False)
    engine.public.price, engine.running = 90, True
    assert engine._monitor_if_due(force=True) == set()
    assert len(engine._active_positions('paper')) == 1
    other = trader.TradingEngine()
    assert other.risk.kill_switch()['engaged'] and not other.risk.kill_switch()['allow_exits']
    other.public.price, other.running = 90, True
    other._protect_positions('BTC/USDT', 90)
    assert len(other._active_positions('paper')) == 1


def test_kill_switch_release_requires_the_exact_phrase(engine):
    engine.risk.engage_kill_switch('test')
    with pytest.raises(storage.ConfigValidationError, match='RESUME TRADING'):
        engine.risk.release_kill_switch('resume trading')
    assert engine.risk.kill_switch()['engaged']
    engine.risk.release_kill_switch('RESUME TRADING')
    assert not engine.risk.kill_switch()['engaged']
    assert engine._buy('BTC/USDT', 200, 100, .04)


def test_unreadable_kill_switch_record_fails_closed(engine):
    storage.set_meta(engine.risk._key('kill_switch'), '{not json')
    assert engine.risk.kill_switch()['engaged']
    assert not engine._buy('BTC/USDT', 200, 100, .04)


def test_kill_switch_works_while_the_worker_is_running(engine):
    engine.running = True
    engine.risk.engage_kill_switch('while running')
    assert engine.risk.kill_switch()['engaged']
    assert engine.running  # Engaging never requires or forces an idle engine.


# ----------------------------------------------------------- bypass resistance

def test_submit_refuses_orders_without_a_valid_authorization(engine):
    client = arm_fake_live(engine)
    assert engine._submit(client, 'BTC/USDT', 'BUY', cost=100) == (None, None)
    assert client.orders == [] and journal_rows() == []
    assert any('no valid risk authorization' in row['message'] for row in storage.rows('SELECT message FROM audit'))
    # An authorization for another symbol, side, or one that has expired is equally refused.
    good = engine._authorize('BTC/USDT', 'BUY', 'entry', 100, 100, positions=[], cash=10000, stop_fraction=.04)
    assert good.allowed
    assert engine._submit(client, 'ETH/USDT', 'BUY', cost=100, authorization=good) == (None, None)
    assert engine._submit(client, 'BTC/USDT', 'SELL', quantity=1, authorization=good) == (None, None)
    expired = risk.Authorization(True, good.request, (), (), 'x', issued_at=time.monotonic() - risk.AUTHORIZATION_TTL_SECONDS - 1)
    assert engine._submit(client, 'BTC/USDT', 'BUY', cost=100, authorization=expired) == (None, None)
    assert client.orders == []
    assert engine._submit(client, 'BTC/USDT', 'BUY', cost=100, authorization=good)[1] is not None
    assert len(client.orders) == 1


def test_every_order_path_is_blocked_by_an_engaged_kill_switch_in_live_mode(engine, monkeypatch):
    client = arm_fake_live(engine)
    assert engine._buy('BTC/USDT', 200, 100, .04)        # one tracked live position
    client.orders.clear()
    engine.risk.engage_kill_switch('bypass test', allow_exits=False)
    observe(engine, 'BTC/USDT', 'ETH/USDT')
    # Automated entries.
    engine._enter_candidates([candidate()])
    # Protective exits through every monitor path.
    engine.public.price = 50
    engine._protect_positions('BTC/USDT', 50)
    engine._mark_positions('BTC/USDT', 50, 1.0, True)
    engine._monitor_if_due(force=True)
    # Direct calls that a future code path might make.
    engine._buy('ETH/USDT', 200, 100, .04)
    engine._sell(engine._active_positions('live')[0], 50, 'direct')
    # Manual ticket.
    engine.running = False
    with pytest.raises(storage.ConfigValidationError):
        engine.manual_trade('BTC/USDT', 'sell', request_id=str(uuid.uuid4()), confirmation='PLACE LIVE SELL',
                            expected_context=engine.manual_order_context)
    assert client.orders == []
    assert all(row['status'] != 'submitted' for row in journal_rows())
    assert engine.metrics.snapshot()['counters']['risk.blocked'] >= 6


def test_live_halt_blocks_entries_through_the_gate_even_if_a_caller_forgets(engine):
    client = arm_fake_live(engine)
    engine.live_halted = True
    authorization = engine._authorize('BTC/USDT', 'BUY', 'entry', 100, 100, positions=[], cash=10000, stop_fraction=.04)
    assert not authorization.allowed and 'halted' in authorization.reason
    assert engine._submit(client, 'BTC/USDT', 'BUY', cost=100, authorization=authorization) == (None, None)
    assert client.orders == []


# -------------------------------------------------------------- entry controls

def test_per_order_and_per_asset_notional_caps(engine):
    engine.cfg['max_order_notional_usdt'] = 150
    assert not engine._buy('BTC/USDT', 200, 100, .04)
    assert 'per-order cap' in engine.risk_block
    engine.cfg['max_order_notional_usdt'] = 1000
    engine.cfg['max_asset_notional_usdt'] = 1000
    assert engine._buy('BTC/USDT', 800, 100, .04)
    observe(engine, 'BTC/USDT')
    # Merging more into the same asset past its cap is refused even for a manual dust merge path.
    engine.cfg['max_positions'] = 8
    with storage.connection() as db:
        db.execute("UPDATE positions SET qty=qty/1000, cost=cost/1000")  # make it dust so a merge is legal
    assert not engine._buy('BTC/USDT', 999.5, 100, .04, merge_dust=True)
    assert 'per-asset cap' in engine.risk_block


def test_notional_caps_are_validated_settings(engine):
    with pytest.raises(storage.ConfigValidationError, match='per-order'):
        storage.validate_settings({'max_order_notional_usdt': 5000, 'max_asset_notional_usdt': 1000})
    with pytest.raises(storage.ConfigValidationError):
        storage.validate_settings({'max_orders_per_minute': 0})
    with pytest.raises(storage.ConfigValidationError):
        storage.validate_settings({'max_drawdown_pct': 0.5})
    assert storage.validate_settings({})['max_orders_per_minute'] == 6


def test_profile_saved_before_the_hard_limits_existed_needs_no_review(engine):
    saved = json.loads(storage.CONFIG_PATH.read_text())
    for key in ('max_order_notional_usdt', 'max_asset_notional_usdt', 'max_orders_per_minute',
                'daily_realized_loss_limit_pct', 'max_drawdown_pct', 'streaming_quotes'):
        saved.pop(key)
    storage.CONFIG_PATH.write_text(json.dumps(saved))
    storage._SETTINGS_CACHE = None
    assert storage.settings()['max_drawdown_pct'] == 10.0
    assert not storage.settings_health()['review_required']


def test_order_rate_limit_blocks_entries_and_only_runaway_exits(engine):
    client = arm_fake_live(engine)
    engine.cfg['max_orders_per_minute'] = 2
    engine.cfg['max_positions'] = 8
    assert engine._buy('BTC/USDT', 100, 100, .04)
    observe(engine, 'BTC/USDT')
    assert engine._buy('ETH/USDT', 100, 100, .04)
    observe(engine, 'ETH/USDT')
    assert not engine._buy('SOL/USDT', 100, 100, .04)
    assert 'rate limit' in engine.risk_block
    assert len(client.orders) == 2
    # Exits are not held to the entry rate; only a runaway multiple pauses them.
    for position in engine._active_positions('live'):
        assert engine._sell(position, 100, 'exit')
    assert len(client.orders) == 4
    engine.cfg['max_orders_per_minute'] = 1
    assert not engine._buy('SOL/USDT', 100, 100, .04)


def test_per_trade_loss_budget_is_enforced_independently_of_sizing(engine):
    # A caller bug passing an oversized stop distance cannot exceed the risk budget.
    assert not engine._buy('BTC/USDT', 800, 100, .12)
    assert 'per-trade risk budget' in engine.risk_block
    assert engine._buy('BTC/USDT', 800, 100, .04)


def test_daily_realized_loss_limit_blocks_new_entries(engine):
    with storage.connection() as db:
        db.execute('INSERT INTO trades(created_at,exchange,mode,symbol,side,price,quantity,gross,fee,realized,reason) VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                   (trader.now(), 'mexc', 'paper', 'SOL/USDT', 'SELL', 100, 1, 100, .1, -400, 'loss'))
    assert not engine._buy('BTC/USDT', 200, 100, .04)
    assert 'realized-loss limit' in engine.risk_block
    engine.cfg['daily_realized_loss_limit_pct'] = 5
    assert engine._buy('BTC/USDT', 200, 100, .04)


def test_drawdown_stop_latches_durably_and_clears_only_with_the_phrase(engine):
    assert engine.risk.observe_equity('paper', 10000)['peak'] == 10000
    assert engine.risk.observe_equity('paper', 9500)['latched'] is False
    assert engine.risk.observe_equity('paper', 8999)['latched'] is True
    assert not engine._buy('BTC/USDT', 200, 100, .04)
    assert 'Drawdown stop' in engine.risk_block
    other = trader.TradingEngine()
    assert other.risk.drawdown_stop('paper')
    with pytest.raises(storage.ConfigValidationError, match='RESUME TRADING'):
        engine.risk.clear_drawdown_stop('paper', 'please')
    engine.risk.clear_drawdown_stop('paper', 'RESUME TRADING')
    assert engine.risk.drawdown_stop('paper') is None
    assert engine._buy('BTC/USDT', 200, 100, .04)


def test_drawdown_stop_never_blocks_a_protective_exit(engine):
    assert engine._buy('BTC/USDT', 200, 100, .04)
    engine.risk.observe_equity('paper', 10000)
    engine.risk.observe_equity('paper', 8000)
    engine._daily_allowed('paper', 10000)
    engine._daily_allowed('paper', 9000)
    engine.public.price, engine.running = 90, True
    assert engine._monitor_if_due(force=True) == {'BTC/USDT'}


def test_reject_spike_breaker_pauses_entries(engine):
    client = arm_fake_live(engine)
    import ccxt
    client.order_error = ccxt.InvalidOrder('bad')
    for _ in range(3):
        assert not engine._buy('BTC/USDT', 100, 100, .04)
    client.order_error = None
    assert not engine._buy('BTC/USDT', 100, 100, .04)
    assert 'repeated exchange rejections' in engine.risk_block or 'repeated execution failures' in engine.risk_block
    assert len(client.orders) == 3


def test_slippage_breaker_pauses_entries_after_repeated_adverse_fills(engine):
    client = arm_fake_live(engine)
    engine.cfg['max_positions'] = 8
    client.fill = {**client.fill, 'average': 101.0, 'cost': 101.0, 'filled': 1.0}
    for symbol in ('BTC/USDT', 'ETH/USDT', 'SOL/USDT'):
        assert engine._buy(symbol, 150, 100, .04)
        observe(engine, symbol)
    assert not engine._buy('DOGE/USDT', 150, 100, .04)
    assert 'slippage' in engine.risk_block
    rows = storage.rows("SELECT intended_price, filled_price FROM order_journal WHERE status='accounted'")
    assert all(row['intended_price'] == 100 and row['filled_price'] == 101 for row in rows)


def test_stale_symbol_quote_blocks_entry_and_exit_through_the_gate(engine):
    assert engine._buy('BTC/USDT', 200, 100, .04)
    engine.last_prices['BTC/USDT'] = 90
    engine.last_price_times['BTC/USDT'] = time.time() - 10000
    assert not engine._sell(engine._active_positions('paper')[0], 90, 'stale')
    assert 'No fresh quote' in engine.risk_block
    assert not engine._buy('ETH/USDT', 200, 100, .04)   # held asset stale


def test_blocked_manual_order_reports_the_gate_reason(engine):
    engine.cfg['max_order_notional_usdt'] = 50
    observe(engine, 'BTC/USDT')
    with pytest.raises(storage.ConfigValidationError, match='per-order cap'):
        engine.manual_trade('BTC/USDT', 'buy', 100, request_id=str(uuid.uuid4()))
    assert storage.rows('SELECT * FROM trades') == []


def test_authorization_records_every_check_and_structured_events(engine, tmp_path):
    authorization = engine._authorize('BTC/USDT', 'BUY', 'entry', 100, 100, positions=[], cash=10000, stop_fraction=.04)
    names = {check['check'] for check in authorization.checks}
    assert {'kill_switch', 'max_order_notional', 'max_asset_notional', 'per_trade_loss_budget', 'order_rate',
            'daily_equity_loss', 'daily_realized_loss', 'drawdown_stop', 'reject_spike', 'slippage_breaker'} <= names
    assert all(check['passed'] for check in authorization.checks)
    log = storage.DATA_DIR / 'logs' / 'events.jsonl'
    lines = [json.loads(line) for line in log.read_text().splitlines() if line.strip()]
    assert any(line['kind'] == 'risk.authorize' and line['allowed'] for line in lines)


def test_paper_reset_clears_paper_risk_latches(engine):
    engine.risk.observe_equity('paper', 10000)
    engine.risk.observe_equity('paper', 5000)
    assert engine.risk.drawdown_stop('paper')
    engine.reset_paper_portfolio()
    assert engine.risk.drawdown_stop('paper') is None
    assert engine.risk.status()['peak_equity'] is None


def test_journal_rejects_illegal_state_transitions(engine):
    intent = storage.create_order_intent('mexc', 'BTC/USDT', 'BUY', cost=100)
    storage.update_order_intent(intent, 'submitted')
    with pytest.raises(ValueError, match='Illegal'):
        storage.update_order_intent(intent, 'pending')
    storage.update_order_intent(intent, 'filled', filled_price=100.5)
    storage.update_order_intent(intent, 'accounted')
    with pytest.raises(ValueError, match='Illegal'):
        storage.update_order_intent(intent, 'uncertain')
    row = storage.order_intent(intent)
    assert row['client_order_id'] == intent[:32] and row['filled_price'] == 100.5 and row['submitted_at']
