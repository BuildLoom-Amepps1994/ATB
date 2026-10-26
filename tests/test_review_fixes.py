"""Regressions for the code review fixes. All exchange, vault and HTTP calls are fake."""
import json
import sys
import time
from pathlib import Path

import ccxt
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import recovery
import storage
import trader
from market_data import DataUnavailable, MarketData
from test_engine import FakeExchange, arm_fake_live, engine
from test_security_storage import client, db, isolated_paths, local_app


@pytest.fixture(autouse=True)
def instant_confirmation(monkeypatch):
    monkeypatch.setattr(trader.TradingEngine, 'CONFIRM_DELAYS', (0, 0, 0, 0, 0))


# --- Order confirmation and fee handling -------------------------------------

def test_fill_confirmation_keeps_polling_after_a_stop_request(engine, monkeypatch):
    client = arm_fake_live(engine)
    client.fill.update(status='open', filled=0, cost=0)
    looks, sleeps = [], []
    def fetch_order(identifier, symbol):
        looks.append(identifier)
        if len(looks) < 3:
            return {'id': identifier, 'status': 'open', 'filled': 0, 'cost': 0}
        return {'id': identifier, 'status': 'closed', 'filled': 1., 'cost': 100., 'average': 100., 'fee': {'currency': 'USDT', 'cost': .1}}
    client.fetch_order = fetch_order
    original = client.create_market_buy_order_with_cost
    def create(*args):
        engine.stop_event.set()  # Stop arrives while the order is already at the exchange.
        return original(*args)
    client.create_market_buy_order_with_cost = create
    monkeypatch.setattr(trader.TradingEngine, 'CONFIRM_DELAYS', (.01, .01, .01, .01, .01))
    monkeypatch.setattr(trader.time, 'sleep', sleeps.append)
    assert engine._buy('BTC/USDT', 800, 100, .04)
    assert len(looks) == 3 and sleeps == [.01, .01, .01]
    assert not engine.live_halted and storage.unresolved_orders() == []
    assert engine._active_positions('live')[0]['qty'] == 1


def test_fill_without_fee_data_uses_the_orders_own_trades(engine):
    client = arm_fake_live(engine)
    client.has = {'fetchOrderTrades': True}
    client.fill.update(fee=None, fees=[])
    calls = []
    client.fetch_order_trades = lambda *args: calls.append(args) or [
        {'fee': {'currency': 'USDT', 'cost': .3}}, {'fee': {'currency': 'BTC', 'cost': .001}}]
    assert engine._buy('BTC/USDT', 800, 100, .04)
    assert calls == [('fake-only', 'BTC/USDT', None, None, {})]
    position = engine._active_positions('live')[0]
    assert position['qty'] == pytest.approx(.999)
    assert position['cost'] == pytest.approx(100.3)


def test_unavailable_trade_fees_fall_back_to_the_configured_estimate(engine):
    client = arm_fake_live(engine)
    client.has = {'fetchOrderTrades': True}
    client.fill.update(fee=None, fees=[])
    client.fetch_order_trades = lambda *args: (_ for _ in ()).throw(ccxt.NetworkError('fee read failed'))
    assert engine._buy('BTC/USDT', 800, 100, .04)
    assert engine._active_positions('live')[0]['cost'] == pytest.approx(100.1)
    assert not engine.live_halted
    assert any('fee estimate' in row['message'] for row in storage.rows('SELECT message FROM audit'))


def test_kraken_orders_request_quote_currency_fees(engine):
    engine.update_config({'exchange': 'kraken'})
    client = arm_fake_live(engine)
    assert engine._buy('BTC/USDT', 800, 100, .04)
    params = client.orders[-1][1][2]
    assert params['oflags'] == 'fciq' and len(params['clientOrderId']) == 32
    assert engine._sell(engine._active_positions('live')[0], 100, 'test')
    side, (symbol, kind, direction, amount, price, params) = client.orders[-1]
    assert (side, symbol, kind, direction, amount, price) == ('sell', 'BTC/USDT', 'market', 'sell', 1.0, None)
    assert params['oflags'] == 'fciq' and len(params['clientOrderId']) == 32
    # The client order id is the journal intent, so an ambiguous submit can be looked up.
    assert storage.rows('SELECT client_order_id FROM order_journal ORDER BY created_at')[-1]['client_order_id'] == params['clientOrderId']


def test_mexc_orders_send_no_extra_flags_and_spot_balance_type(engine):
    client = arm_fake_live(engine)
    seen = []
    original = client.fetch_balance
    client.fetch_balance = lambda params=None: seen.append(params) or original()
    assert engine._buy('BTC/USDT', 800, 100, .04)
    assert set(client.orders[-1][1][2]) == {'clientOrderId'}   # no Kraken-only flags on MEXC
    assert seen and all(params == {'type': 'spot'} for params in seen)
    engine.running, engine.mode = False, 'paper'
    engine.update_config({'exchange': 'kraken'})
    client = arm_fake_live(engine)
    client.fetch_balance = lambda params=None: seen.append(params) or original()
    engine._read_balance(client)
    assert seen[-1] == {}


def test_exchange_bad_request_is_a_definite_rejection(engine):
    client = arm_fake_live(engine)
    client.order_error = ccxt.BadRequest('synthetic 400 rejection')
    assert not engine._buy('BTC/USDT', 200, 100, .04)
    assert not engine.live_halted
    assert storage.rows('SELECT status FROM order_journal') == [{'status': 'rejected'}]


def test_missing_order_identifier_is_stored_as_null_not_text(engine):
    client = arm_fake_live(engine)
    client.fill['id'] = None
    assert not engine._buy('BTC/USDT', 800, 100, .04)
    assert storage.rows('SELECT order_id FROM order_journal') == [{'order_id': None}]


def test_remainder_below_amount_precision_is_retained_without_an_order(engine):
    client = arm_fake_live(engine)
    assert engine._buy('BTC/USDT', 800, 100, .04)
    client.amount_to_precision = lambda *a: (_ for _ in ()).throw(ccxt.InvalidOrder('below precision'))
    position = engine._active_positions('live')[0]
    assert not engine._sell(position, 100, 'test')
    assert [order[0] for order in client.orders] == ['buy']
    assert len(engine._active_positions('live')) == 1
    assert 'below an exchange limit' in engine.risk_block


def test_short_free_balance_is_reported_before_selling_what_is_available(engine):
    client = arm_fake_live(engine)
    assert engine._buy('BTC/USDT', 800, 100, .04)
    client.fetch_balance = lambda *a: {'free': {'USDT': 10000., 'BTC': .4}}
    client.fill = {'id': 'fake-sell', 'status': 'closed', 'filled': .4, 'cost': 40, 'average': 100, 'fee': {'currency': 'USDT', 'cost': .04}}
    assert engine._sell(engine._active_positions('live')[0], 100, 'test')
    assert client.orders[-1][1][3] == .4
    assert engine._active_positions('live')[0]['qty'] == pytest.approx(.6)
    assert any('below the tracked quantity' in row['message'] for row in storage.rows('SELECT message FROM audit'))


# --- Market catalogue and market data ----------------------------------------

def test_unloaded_catalogue_never_raises_from_scans_or_snapshots(engine):
    engine._buy('BTC/USDT', 200, 100, .04)
    engine.public.markets = None
    rows = engine.scan_once(allow_orders=False)
    assert rows and all(row['status'].startswith('Pair unavailable') for row in rows)
    assert engine.snapshot()['positions'][0]['dust'] is False


@pytest.mark.parametrize('symbol,allowed', [
    ('JUP/USDT', True), ('PUP/USDT', True), ('DOWN/USDT', True),
    ('BTC3L/USDT', False), ('ETHBULL/USDT', False), ('SOLUP/USDT', False),
])
def test_leveraged_token_suffixes_need_a_real_base(symbol, allowed):
    market = {'spot': True, 'active': True, 'contract': False}
    assert trader.TradingEngine._spot(symbol, market) is allowed


def test_limit_only_pairs_are_not_tradable(engine):
    market = {'spot': True, 'active': True, 'contract': False, 'info': {'orderTypes': ['LIMIT', 'LIMIT_MAKER']}}
    assert not trader.TradingEngine._spot('OGN/USDT', market)
    assert trader.TradingEngine._spot('OGN/USDT', {**market, 'info': {'orderTypes': ['LIMIT', 'MARKET']}})
    client = arm_fake_live(engine)
    client.markets['BTC/USDT']['info'] = {'orderTypes': ['LIMIT', 'MARKET'], 'quoteOrderQtyMarketAllowed': False}
    with pytest.raises(storage.ConfigValidationError, match='USDT amount'):
        engine._buy('BTC/USDT', 200, 100, .04)
    assert client.orders == []


def test_catalogue_refreshes_hourly_and_keeps_the_previous_copy_on_failure(engine, monkeypatch):
    loads = []
    original = engine.public.load_markets
    def load_markets(reload=False):
        if loads and loads[-1] is None:
            raise ccxt.NetworkError('catalogue offline')
        loads.append(reload)
        return original()
    engine.public.load_markets = load_markets
    engine._fetch_markets()
    engine._fetch_markets()
    assert loads == [False, False]  # CCXT answers these from its own cache.
    engine.markets_loaded_at = time.monotonic() - 3601
    engine._fetch_markets()
    assert loads == [False, False, True]
    loads.clear()
    engine.markets_loaded_at = time.monotonic() - 3601
    monkeypatch.setattr(trader.time, 'sleep', lambda _: None)
    loads.extend([None, None])  # The next refresh fails on every retry.
    assert engine._fetch_markets() is engine.public.markets
    assert any('catalogue refresh unavailable' in row['message'] for row in storage.rows('SELECT message FROM audit'))


def test_idle_reads_retry_even_after_an_earlier_stop(engine, monkeypatch):
    monkeypatch.setattr(trader.time, 'sleep', lambda _: None)
    engine.stop()
    failures = [ccxt.NetworkError('one'), ccxt.NetworkError('two')]
    original = engine.public.fetch_ticker
    def flaky(symbol):
        if failures:
            raise failures.pop()
        return original(symbol)
    engine.public.fetch_ticker = flaky
    assert engine.data.ticker('BTC/USDT')['bid'] > 0
    assert not failures
    engine.running = True
    failures.extend([ccxt.NetworkError('three'), ccxt.NetworkError('four')])
    with pytest.raises(DataUnavailable, match='interrupted'):
        engine.data.ticker('BTC/USDT')


def test_candle_completion_is_judged_from_the_pre_request_clock():
    clock = [10 * 3600 + 3599.0]
    class Exchange:
        has = {}
        options = {}
        def fetch_ohlcv(self, symbol, tf, limit):
            clock[0] = 11 * 3600 + 1.0  # The hour closes while the response is in flight.
            return [[(hour * 3600) * 1000, 100, 101, 99, 100, 1000] for hour in range(1, 11)]
    data = MarketData(Exchange(), clock=lambda: clock[0], wait=lambda _: False)
    frame = data.candles('BTC/USDT', '1h', limit=10)
    assert int(frame.iloc[-1]['timestamp']) == 9 * 3600 * 1000


def test_exchange_clock_offset_applies_to_ticker_and_candle_ages(engine):
    engine.cfg['data_max_age_seconds'] = 30
    engine.public.options = {'timeDifference': 60_000}  # This PC runs one minute ahead.
    stamp = int((time.time() - 60) * 1000)
    price, _, _ = engine._observe_ticker('BTC/USDT', {'bid': 100, 'ask': 100.1, 'timestamp': stamp})
    assert price == 100 and engine._fresh('BTC/USDT')
    engine.public.options = {}
    with pytest.raises(ValueError, match='stale'):
        engine._observe_ticker('BTC/USDT', {'bid': 100, 'ask': 100.1, 'timestamp': stamp})


def test_tickers_without_timestamps_age_from_their_response_time(engine):
    engine.public.fetch_ticker = lambda symbol: {'bid': 100, 'ask': 100.1}
    ticker = engine.data.ticker('BTC/USDT')
    assert ticker['_received_at'] <= time.time()
    engine._observe_ticker('BTC/USDT', ticker)
    assert engine.last_price_times['BTC/USDT'] == ticker['_received_at']
    engine.cfg['data_max_age_seconds'] = 30
    with pytest.raises(ValueError, match='stale'):
        engine._observe_ticker('BTC/USDT', {'bid': 100, 'ask': 100.1, '_received_at': time.time() - 31})


# --- Entries, reentry and scans -----------------------------------------------

def test_reentry_is_blocked_for_any_candle_up_to_the_exit_candle(engine):
    engine._buy('BTC/USDT', 200, 100, .04)
    engine.running, engine.public.price = True, 90
    engine._monitor_if_due(force=True)
    assert engine._active_positions('paper') == []
    exit_candle = (int(time.time() * 1000) // 3600000 - 1) * 3600000
    engine.public.price = 100
    engine.last_prices['BTC/USDT'], engine.last_price_times['BTC/USDT'] = 100, time.time()
    engine._enter_candidates([(100, 20_000_000, 'BTC/USDT', 100, 1, exit_candle - 3600000)])
    assert engine._active_positions('paper') == []
    engine._enter_candidates([(100, 20_000_000, 'BTC/USDT', 100, 1, exit_candle + 3600000)])
    assert len(engine._active_positions('paper')) == 1


def test_worker_passes_its_protective_exits_into_the_following_scan(engine, monkeypatch):
    seen = {}
    monkeypatch.setattr(engine, '_monitor_if_due', lambda **kw: {'BTC/USDT'})
    def scan(**kwargs):
        seen.update(kwargs)
        engine.stop_event.set()
    monkeypatch.setattr(engine, 'scan_once', scan)
    engine.running = True
    engine.stop_event.clear()
    engine._loop()
    assert seen == {'allow_orders': True, 'exited_symbols': {'BTC/USDT'}}


def test_one_unquotable_candidate_does_not_end_the_entry_pass(engine):
    engine.running = True
    for symbol in ('ETH/USDT', 'SOL/USDT'):
        engine.last_prices[symbol], engine.price_times[symbol] = 100, time.time()
    original = engine.public.fetch_ticker
    engine.public.fetch_ticker = lambda symbol: {'bid': None} if symbol == 'ETH/USDT' else original(symbol)
    engine._enter_candidates([(100, 20_000_000, 'ETH/USDT', 100, 1, 123), (90, 20_000_000, 'SOL/USDT', 100, 1, 123)])
    assert [p['symbol'] for p in engine._active_positions('paper')] == ['SOL/USDT']


def test_manual_scan_is_refused_while_the_worker_or_a_chart_owns_the_lock(engine):
    engine.running = True
    with pytest.raises(storage.ConfigValidationError, match='already active'):
        engine.scan_once(allow_orders=False)
    engine.running = False
    with engine.scan_lock:
        with pytest.raises(storage.ConfigValidationError, match='Wait for'):
            engine.scan_once(allow_orders=False)
    assert len(engine.scan_once(allow_orders=False)) == 8


def test_live_activation_binds_the_account_only_after_a_verified_read(engine):
    engine.api_key = engine.api_secret = 'unverified-key'
    monkeypatch_exchange = FakeExchange()
    monkeypatch_exchange.fetch_balance = lambda *a: (_ for _ in ()).throw(ccxt.AuthenticationError('bad key'))
    trader.TradingEngine._new_exchange = staticmethod(lambda *a: monkeypatch_exchange)
    with pytest.raises(ccxt.AuthenticationError):
        engine.set_mode('live', 'ENABLE LIVE TRADING', True)
    assert storage.get_meta(engine._key('live_account_fingerprint')) == ''
    assert engine.mode == 'paper'


def test_unwritable_halt_flag_stays_pending_until_it_can_be_written(engine, monkeypatch):
    client = arm_fake_live(engine)
    client.order_error = ccxt.RequestTimeout('synthetic timeout')
    original = storage.set_meta
    def failing(name, value):
        if name.startswith('live_halt'):
            raise OSError('disk full')
        return original(name, value)
    monkeypatch.setattr(storage, 'set_meta', failing)
    assert not engine._buy('BTC/USDT', 800, 100, .04)
    assert engine.live_halted and engine._halt_pending == 'mexc'
    with storage.connection() as db:
        db.execute("DELETE FROM order_journal")
    assert engine._durable_live_block()
    monkeypatch.setattr(storage, 'set_meta', original)
    assert engine._durable_live_block() and engine._halt_pending is None
    assert storage.get_meta('live_halt.mexc') == '1'


def test_engine_refusals_reach_the_browser_as_their_own_messages(engine):
    with pytest.raises(storage.ConfigValidationError, match='Connect exchange API credentials'):
        engine.set_mode('live', 'ENABLE LIVE TRADING', True)
    engine.running = True
    with pytest.raises(storage.ConfigValidationError, match='Wait for the bot'):
        engine.update_config({'scan_seconds': 60})
    with pytest.raises(storage.ConfigValidationError, match='cached chart'):
        engine.history('BTC/USDT', '4h')


# --- Storage, settings and audit ----------------------------------------------

def test_repeated_identical_audit_entries_refresh_instead_of_flooding(db):
    for _ in range(5):
        storage.audit('warning', 'quote stale')
    storage.audit('warning', 'other')
    storage.audit('warning', 'quote stale')
    assert [row['message'] for row in storage.rows('SELECT message FROM audit ORDER BY id')] == ['quote stale', 'other', 'quote stale']


def test_damaged_last_good_copy_is_preserved_and_requires_review(db):
    strict = {**storage.DEFAULTS, 'risk_pct': .2}
    storage.save_settings(strict)
    backup = storage.CONFIG_PATH.with_name('settings.last-good.json')
    backup.write_bytes(b'{damaged-backup')
    storage._SETTINGS_CACHE = None
    assert storage.settings() == strict
    health = storage.settings_health()
    assert health['review_required'] and health['reason'] == 'backup_unavailable'
    assert backup.read_bytes() == b'{damaged-backup'
    assert any(p.read_bytes() == b'{damaged-backup' for p in storage.DATA_DIR.glob('settings.invalid-*.json'))
    storage.save_settings(strict)
    assert not storage.settings_health()['review_required']


def test_paper_reset_and_seed_use_the_configured_starting_balance(engine):
    storage.save_settings({**storage.DEFAULTS, 'paper_start_usdt': 2500.0})
    engine.cfg = storage.settings()
    engine.reset_paper_portfolio()
    assert engine._cash('paper') == 2500
    with storage.connection() as db:
        db.execute("DELETE FROM meta WHERE key='paper_cash.kraken'")
    storage.init_db()
    assert storage.get_meta('paper_cash.kraken') == '2500.0'


# --- Local web service ---------------------------------------------------------

def test_non_ascii_action_token_is_rejected_not_crashed(local_app):
    response = client(local_app).post('/api/scan', headers={'x-cryptopilot-token': 'tøken'.encode('utf8')})
    assert response.status_code == 403
    assert response.json() == {'error': 'Invalid local action token.'}


def test_static_scripts_are_served_as_javascript(local_app):
    response = client(local_app).get('/static/dashboard.js')
    assert response.status_code == 200
    assert response.headers['content-type'].startswith('text/javascript')


def test_scan_refusal_message_comes_from_the_engine(local_app):
    def busy(allow_orders):
        raise storage.ConfigValidationError('Automatic scanning is already active.')
    local_app.engine.scan_once = busy
    response = client(local_app).post('/api/scan', headers={'x-cryptopilot-token': local_app.CSRF})
    assert response.status_code == 400 and response.json()['detail'] == 'Automatic scanning is already active.'


# --- Recovery tool -------------------------------------------------------------

@pytest.fixture
def halted_data(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, 'DATA_DIR', tmp_path)
    monkeypatch.setattr(storage, 'DB_PATH', tmp_path / 'portfolio.sqlite3')
    monkeypatch.setattr(storage, 'CONFIG_PATH', tmp_path / 'settings.json')
    monkeypatch.setenv('CRYPTOPILOT_DATA_DIR', str(tmp_path))
    storage.init_db()
    intent = storage.create_order_intent('mexc', 'BTC/USDT', 'BUY', cost=100)
    storage.update_order_intent(intent, 'uncertain', order_id='exchange-order')
    other = storage.create_order_intent('kraken', 'ETH/USDT', 'SELL', quantity=1)
    storage.update_order_intent(other, 'uncertain')
    storage.set_meta('live_halt.mexc', '1')
    return tmp_path


def test_clear_halt_requires_the_exact_phrase_and_changes_nothing_otherwise(halted_data):
    before = storage.DB_PATH.read_bytes()
    with pytest.raises(ValueError, match='I HAVE RECONCILED MEXC ORDERS'):
        recovery.clear_halt(halted_data, 'mexc', 'i have reconciled mexc orders')
    assert recovery.main(['--clear-halt', 'mexc']) == 1
    assert storage.DB_PATH.read_bytes() == before
    assert len(storage.unresolved_orders()) == 2


def test_clear_halt_reconciles_one_exchange_and_records_it(halted_data, capsys):
    assert recovery.main(['--clear-halt', 'mexc', '--confirm', 'I HAVE RECONCILED MEXC ORDERS']) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['reconciled_orders'] == 1 and result['halt_flag_removed'] and result['legacy_live_record_count'] == 0
    assert storage.get_meta('live_halt.mexc') == ''
    assert [row['exchange'] for row in storage.unresolved_orders()] == ['kraken']
    assert storage.rows("SELECT status FROM order_journal WHERE exchange='mexc'") == [{'status': 'reconciled'}]
    assert 'cleared by the recovery tool' in storage.rows('SELECT message FROM audit ORDER BY id DESC LIMIT 1')[0]['message']
    assert recovery.report(halted_data)['unresolved_order_count'] == 1
    assert 'exchange-order' not in json.dumps(recovery.report(halted_data))


def test_recovery_refuses_a_missing_data_folder(tmp_path, monkeypatch, capsys):
    missing = tmp_path / 'never-run'
    monkeypatch.setenv('CRYPTOPILOT_DATA_DIR', str(missing))
    assert recovery.main([]) == 1
    assert 'No CryptoPilot data folder' in capsys.readouterr().err
    assert not missing.exists()


def test_recovery_report_names_the_folder_it_examined(halted_data):
    assert recovery.report(halted_data)['data_folder'] == str(halted_data)
