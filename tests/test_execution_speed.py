"""Offline fast monitoring and explicit manual execution regressions."""
import time
import uuid

import ccxt
import pytest

import storage
import trader
from test_engine import arm_fake_live, engine


def request():
    return str(uuid.uuid4())


def test_fast_monitor_exits_without_candles_or_a_strategy_scan(engine, monkeypatch):
    engine._buy('BTC/USDT', 200, 100, .04)
    engine.public.price, engine.running = 90, True
    monkeypatch.setattr(engine.data, 'candles', lambda *a, **kw: pytest.fail('protective monitor must not download candles'))
    assert engine._monitor_if_due(force=True) == {'BTC/USDT'}
    assert engine._active_positions('paper') == []
    assert engine.last_successful_monitor
    assert engine.last_scan is None
    assert engine.public.orders == []


def test_fast_monitor_ignores_stale_quotes_and_reports_failure(engine):
    engine._buy('BTC/USDT', 200, 100, .04)
    engine.running = True
    engine.public.fetch_ticker = lambda _: {'last': 90, 'timestamp': 1}
    assert engine._monitor_if_due(force=True) == set()
    assert len(engine._active_positions('paper')) == 1
    assert engine.monitor_error and engine.last_successful_monitor is None


def test_fast_monitor_waits_for_its_configured_deadline(engine, monkeypatch):
    engine.running = True
    engine._next_monitor = time.monotonic() + 60
    monkeypatch.setattr(engine, '_active_positions', lambda *_: pytest.fail('not due yet'))
    assert engine._monitor_if_due() == set()


def test_worker_monitors_between_full_strategy_scan_deadlines(engine, monkeypatch):
    clock, scans, monitors = [100.0], [], []
    class Timer:
        def set(self):
            clock[0] = 116
        def is_set(self):
            return clock[0] >= 116
        def wait(self, delay):
            clock[0] += delay
            return self.is_set()
    def monitor(**kwargs):
        if clock[0] >= engine._next_monitor:
            monitors.append(clock[0])
            engine._next_monitor = clock[0] + 5
        return set()
    monkeypatch.setattr(trader.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(engine, '_monitor_if_due', monitor)
    monkeypatch.setattr(engine, 'scan_once', lambda **kw: scans.append(clock[0]))
    engine.stop_event, engine.running = Timer(), True
    engine.cfg['scan_seconds'] = 180
    engine._loop()
    assert scans == [100]
    assert monitors == [100, 105, 110, 115]


def test_manual_paper_buy_sell_buy_can_run_without_waiting_for_signal_candle(engine):
    engine.stop_event.set()  # A normal previous Stop must not prevent a deliberate order.
    first = engine.manual_trade('BTC/USDT', 'buy', 200, request_id=request())
    second = engine.manual_trade('BTC/USDT', 'sell', request_id=request())
    third = engine.manual_trade('BTC/USDT', 'buy', 200, request_id=request())
    assert [first['side'], second['side'], third['side']] == ['BUY', 'SELL', 'BUY']
    assert len(engine._active_positions('paper')) == 1
    assert not engine.running and not engine.control_busy
    assert engine.public.orders == []
    assert len(storage.rows('SELECT * FROM trades')) == 3


def test_manual_request_is_durable_idempotent_and_payload_bound(engine):
    identifier = request()
    first = engine.manual_trade('BTC/USDT', 'buy', 200, request_id=identifier)
    assert engine.manual_trade('BTC/USDT', 'buy', 200, request_id=identifier) == first
    other = trader.TradingEngine()
    assert other.manual_trade('BTC/USDT', 'buy', 200, request_id=identifier) == first
    assert len(storage.rows('SELECT * FROM trades')) == 1
    with pytest.raises(storage.ConfigValidationError, match='different request'):
        engine.manual_trade('ETH/USDT', 'buy', 200, request_id=identifier)


@pytest.mark.parametrize('budget', [True, float('nan'), float('inf'), -1, 0, 11, 801])
def test_manual_buy_refuses_invalid_or_over_cap_budget(engine, budget):
    with pytest.raises(storage.ConfigValidationError):
        engine.manual_trade('BTC/USDT', 'buy', budget, request_id=request())
    assert storage.rows('SELECT * FROM trades') == []


def test_manual_buy_honors_latched_daily_loss_and_leaves_sell_available(engine):
    engine._buy('BTC/USDT', 200, 100, .04)
    engine._daily_allowed('paper', 10000)
    engine._daily_allowed('paper', 9600)
    with pytest.raises(storage.ConfigValidationError, match='daily loss'):
        engine.manual_trade('ETH/USDT', 'buy', 100, request_id=request())
    assert engine.manual_trade('BTC/USDT', 'sell', request_id=request())['status'] == 'filled'


def test_manual_orders_require_stopped_worker_and_tracked_sell(engine):
    engine.running = True
    with pytest.raises(storage.ConfigValidationError, match='Stop the bot'):
        engine.manual_trade('BTC/USDT', 'buy', 100, request_id=request())
    engine.running = False
    with pytest.raises(storage.ConfigValidationError, match='No tracked'):
        engine.manual_trade('BTC/USDT', 'sell', request_id=request())


def test_live_manual_requires_exact_per_order_confirmation_and_accounts_both_sides(engine):
    client = arm_fake_live(engine)
    engine.running = False
    with pytest.raises(storage.ConfigValidationError, match='PLACE LIVE BUY'):
        engine.manual_trade('BTC/USDT', 'buy', 200, request_id=request())
    bought = engine.manual_trade('BTC/USDT', 'buy', 200, expected_context=engine.manual_order_context, confirmation='PLACE LIVE BUY', request_id=request())
    assert bought['mode'] == 'live'
    with pytest.raises(storage.ConfigValidationError, match='PLACE LIVE SELL'):
        engine.manual_trade('BTC/USDT', 'sell', expected_context=engine.manual_order_context, confirmation='PLACE LIVE BUY', request_id=request())
    assert engine.manual_trade('BTC/USDT', 'sell', expected_context=engine.manual_order_context, confirmation='PLACE LIVE SELL', request_id=request())['status'] == 'filled'
    assert [order[0] for order in client.orders] == ['buy', 'sell']
    assert storage.unresolved_orders() == []
    assert engine._active_positions('live') == []


def test_live_manual_timeout_never_repeats_and_durably_halts(engine):
    client = arm_fake_live(engine)
    engine.running = False
    client.order_error = ccxt.RequestTimeout('synthetic transport timeout')
    identifier = request()
    with pytest.raises(storage.ConfigValidationError, match='reconciliation'):
        engine.manual_trade('BTC/USDT', 'buy', 200, expected_context=engine.manual_order_context, confirmation='PLACE LIVE BUY', request_id=identifier)
    with pytest.raises(storage.ConfigValidationError):
        engine.manual_trade('BTC/USDT', 'buy', 200, expected_context=engine.manual_order_context, confirmation='PLACE LIVE BUY', request_id=identifier)
    assert len(client.orders) == 1
    assert len(storage.unresolved_orders()) == 1
    assert trader.TradingEngine().live_halted


def test_definite_exchange_rejection_is_journaled_without_false_uncertainty(engine):
    client = arm_fake_live(engine)
    client.order_error = ccxt.InsufficientFunds('synthetic rejection')
    assert not engine._buy('BTC/USDT', 200, 100, .04)
    assert len(client.orders) == 1 and not engine.live_halted
    assert storage.rows('SELECT status FROM order_journal') == [{'status': 'rejected'}]
    assert storage.unresolved_orders() == []


def test_rejection_during_confirmation_after_acceptance_remains_uncertain(engine, monkeypatch):
    client = arm_fake_live(engine)
    client.fill.update(status='open', filled=0, cost=0)
    client.fetch_order = lambda *a: (_ for _ in ()).throw(ccxt.InvalidOrder('synthetic read rejection'))
    assert not engine._buy('BTC/USDT', 200, 100, .04)
    assert engine.live_halted and len(client.orders) == 1
    assert storage.rows('SELECT status FROM order_journal') == [{'status': 'uncertain'}]


def test_direct_confirmation_error_cannot_be_misclassified_as_create_rejection(engine, monkeypatch):
    client = arm_fake_live(engine)
    monkeypatch.setattr(engine, '_confirm_filled', lambda *a: (_ for _ in ()).throw(ccxt.InvalidOrder('synthetic response parser rejection')))
    assert not engine._buy('BTC/USDT', 200, 100, .04)
    assert engine.live_halted and len(client.orders) == 1
    assert storage.rows('SELECT status FROM order_journal') == [{'status': 'uncertain'}]


def test_manual_terminal_partial_sell_reports_retained_quantity_and_deduplicates(engine):
    client = arm_fake_live(engine)
    engine.running = False
    engine.manual_trade('BTC/USDT', 'buy', 200, expected_context=engine.manual_order_context, confirmation='PLACE LIVE BUY', request_id=request())
    client.fill.update(status='canceled', filled=.4, cost=40, average=100, fee={'currency': 'USDT', 'cost': .04})
    identifier = request()
    result = engine.manual_trade('BTC/USDT', 'sell', expected_context=engine.manual_order_context, confirmation='PLACE LIVE SELL', request_id=identifier)
    assert result['status'] == 'partial'
    assert result['remaining_quantity'] == pytest.approx(.6)
    assert engine.manual_trade('BTC/USDT', 'sell', expected_context=engine.manual_order_context, confirmation='PLACE LIVE SELL', request_id=identifier) == result
    assert len(client.orders) == 2
    assert engine._active_positions('live')[0]['qty'] == pytest.approx(.6)
    assert not storage.unresolved_orders()


def test_manual_rebuy_merges_precision_dust_without_losing_quantity_or_cost(engine):
    client = arm_fake_live(engine)
    engine.running = False
    client.fill.update(filled=1.0000009, cost=100.00009, average=100,
                       fee={'currency': 'USDT', 'cost': .10000009})
    engine.manual_trade('BTC/USDT', 'buy', 200, expected_context=engine.manual_order_context, confirmation='PLACE LIVE BUY', request_id=request())
    client.fill.update(filled=1, cost=100, fee={'currency': 'USDT', 'cost': .1})
    sold = engine.manual_trade('BTC/USDT', 'sell', expected_context=engine.manual_order_context, confirmation='PLACE LIVE SELL', request_id=request())
    assert sold['status'] == 'partial'
    remainder = engine._active_positions('live')[0]
    assert remainder['qty'] == pytest.approx(.0000009)
    assert engine._is_dust(remainder, client.market('BTC/USDT'), 100)
    bought = engine.manual_trade('BTC/USDT', 'buy', 200, expected_context=engine.manual_order_context, confirmation='PLACE LIVE BUY', request_id=request())
    position = engine._active_positions('live')[0]
    assert bought['status'] == 'filled'
    assert position['qty'] == pytest.approx(1 + remainder['qty'])
    assert position['cost'] == pytest.approx(100.1 + remainder['cost'])
    assert position['entry_price'] == pytest.approx(100)
    assert len(client.orders) == 3 and not storage.unresolved_orders()


def test_manual_buy_cannot_merge_a_tradable_position(engine):
    engine.manual_trade('BTC/USDT', 'buy', 200, request_id=request())
    with pytest.raises(storage.ConfigValidationError, match='already tracked'):
        engine.manual_trade('BTC/USDT', 'buy', 200, request_id=request())
    assert len(storage.rows('SELECT * FROM trades')) == 1


def test_unknown_exchange_minimum_never_classifies_a_position_as_dust(engine):
    assert not engine._is_dust({'qty': .00000001}, {'limits': {}}, 100)


def test_fast_monitor_trails_from_cached_atr_without_candle_download(engine, monkeypatch):
    engine.data.candles('BTC/USDT', '1h', limit=175)
    engine._buy('BTC/USDT', 200, 100, .04)
    engine.running, engine.public.price = True, 108
    monkeypatch.setattr(engine.data, 'candles', lambda *a, **kw: pytest.fail('monitor must use cache'))
    assert engine._monitor_if_due(force=True) == set()
    position = engine._active_positions('paper')[0]
    # Long protective marks use the available selling quote, not last trade.
    assert position['highest'] == pytest.approx(108 * .9999)
    assert position['stop'] == pytest.approx(108 * .9999 - 5)
    engine.public.price = 102
    assert engine._monitor_if_due(force=True) == {'BTC/USDT'}


def test_fast_live_monitor_latches_intraday_loss_even_without_strategy_scan(engine):
    client = arm_fake_live(engine)
    assert engine._buy('BTC/USDT', 200, 100, .5)
    engine._daily_allowed('live', 10000)
    client.fetch_balance = lambda *a: {'free': {'USDT': 9500, 'BTC': 1}}
    engine._monitor_if_due(force=True)
    assert not engine._daily_allowed('live', 11000)[0]
    assert 'Daily loss' in engine.risk_block


def test_unsupported_cost_buy_is_blocked_before_intent_or_submission(engine):
    client = arm_fake_live(engine)
    client.has = {'createMarketBuyOrderWithCost': False}
    with pytest.raises(storage.ConfigValidationError, match='bounded-cost'):
        engine._buy('BTC/USDT', 200, 100, .04)
    assert client.orders == [] and storage.unresolved_orders() == []


def test_stop_during_manual_preparation_prevents_submission(engine, monkeypatch):
    original = engine.data.ticker
    def quote(symbol):
        engine.stop()
        return original(symbol)
    monkeypatch.setattr(engine.data, 'ticker', quote)
    with pytest.raises(storage.ConfigValidationError, match='stopped'):
        engine.manual_trade('BTC/USDT', 'buy', 200, request_id=request())
    assert storage.rows('SELECT * FROM trades') == []
    assert not engine.control_busy and not engine.scan_lock.locked()


def test_fast_monitor_exit_cannot_reenter_same_closed_candle(engine):
    engine._buy('BTC/USDT', 200, 100, .04)
    engine.running, engine.public.price = True, 90
    engine._monitor_if_due(force=True)
    candle = (int(time.time() * 1000) // 3600000 - 1) * 3600000
    engine.public.price = 100
    engine.last_prices['BTC/USDT'], engine.last_price_times['BTC/USDT'] = 100, time.time()
    engine._enter_candidates([(100, 20_000_000, 'BTC/USDT', 100, 1, candle)])
    assert engine._active_positions('paper') == []


@pytest.mark.parametrize('change', ['exchange', 'connect', 'disconnect', 'settings', 'mode', 'reset'])
def test_manual_review_context_invalidates_after_account_or_config_change(engine, change):
    if change == 'disconnect':
        engine.connect('synthetic-key', 'synthetic-secret')
    context = engine.snapshot()['manual_order_context']
    if change == 'exchange':
        engine.update_config({'exchange': 'kraken'})
    elif change == 'connect':
        engine.connect('synthetic-key', 'synthetic-secret')
    elif change == 'disconnect':
        engine.disconnect()
    elif change == 'settings':
        engine.update_config({'risk_pct': .5})
    elif change == 'mode':
        engine.set_mode('paper')
    else:
        engine.reset_paper_portfolio()
    assert engine.manual_order_context != context
    with pytest.raises(storage.ConfigValidationError, match='changed'):
        engine.manual_trade('BTC/USDT', 'buy', 200, expected_context=context, request_id=request())
    assert storage.rows('SELECT * FROM trades') == []
    assert engine.public.orders == []


def test_manual_live_cannot_use_a_paper_review_or_omit_context(engine):
    context = engine.snapshot()['manual_order_context']
    client = arm_fake_live(engine)
    engine.running = False
    engine.set_mode('live', 'ENABLE LIVE TRADING', True)
    assert engine.manual_order_context != context
    for expected in ('', context):
        with pytest.raises(storage.ConfigValidationError, match='changed'):
            engine.manual_trade('BTC/USDT', 'buy', 200, expected_context=expected,
                                confirmation='PLACE LIVE BUY', request_id=request())
    assert client.orders == []


def test_manual_request_id_cannot_replay_a_fill_in_a_new_review_context(engine):
    identifier = request()
    engine.manual_trade('BTC/USDT', 'buy', 200, expected_context=engine.manual_order_context, request_id=identifier)
    engine.set_mode('paper')
    with pytest.raises(storage.ConfigValidationError, match='different request'):
        engine.manual_trade('BTC/USDT', 'buy', 200, expected_context=engine.manual_order_context, request_id=identifier)
    assert len(storage.rows('SELECT * FROM trades')) == 1


def test_context_rotates_on_same_account_reconnect_and_contains_no_account_identifier(engine):
    contexts = [engine.manual_order_context]
    for _ in range(2):
        engine.connect('synthetic-account-key', 'synthetic-account-secret')
        contexts.append(engine.snapshot()['manual_order_context'])
    assert len(set(contexts)) == 3
    fingerprint = storage.get_meta(engine._key('live_account_fingerprint'))
    for context in contexts:
        assert uuid.UUID(context).version == 4
        assert fingerprint not in context
        assert 'synthetic' not in context


def test_context_rotates_through_live_paper_live_and_exchange_changes(engine):
    contexts = [engine.manual_order_context]
    engine.private = engine._new_exchange('mexc')
    for mode in ('live', 'paper', 'live', 'paper'):
        engine.set_mode(mode, 'ENABLE LIVE TRADING' if mode == 'live' else '', True)
        contexts.append(engine.manual_order_context)
    for exchange in ('kraken', 'mexc'):
        engine.update_config({'exchange': exchange})
        contexts.append(engine.manual_order_context)
    assert len(set(contexts)) == len(contexts)


@pytest.mark.parametrize('busy', ['manual', 'scan', 'running'])
def test_paper_reset_is_blocked_during_other_operations(engine, busy):
    engine._buy('BTC/USDT', 200, 100, .04)
    if busy == 'manual':
        engine.control_busy = True
    elif busy == 'scan':
        engine.scan_lock.acquire()
    else:
        engine.running = True
    try:
        with pytest.raises(storage.ConfigValidationError, match='active operations'):
            engine.reset_paper_portfolio()
        assert len(engine._active_positions('paper')) == 1
    finally:
        engine.control_busy = engine.running = False
        if busy == 'scan':
            engine.scan_lock.release()


def test_fast_live_monitor_reuses_recent_cash_then_refreshes_after_invalidation(engine):
    client = arm_fake_live(engine)
    engine._buy('BTC/USDT', 200, 100, .04)
    count, original = [], client.fetch_balance
    def balance(*args):
        count.append(1)
        return original(*args)
    client.fetch_balance = balance
    assert engine.balance_updated_at is None
    engine._monitor_if_due(force=True)
    engine._monitor_if_due(force=True)
    assert len(count) == 1
    engine.balance_updated_at = time.time() - 31
    engine._monitor_if_due(force=True)
    assert len(count) == 2
    engine.balance_updated_at = None
    engine._monitor_if_due(force=True)
    assert len(count) == 3
