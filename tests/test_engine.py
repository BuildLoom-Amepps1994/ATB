"""Engine failure scenarios. All exchange and vault calls are fake."""
import math
import sys
import threading
import time
from pathlib import Path

import ccxt
import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import credentials
import storage
import trader


class FakeExchange:
    has = {}
    def __init__(self):
        self.orders = []
        self.markets = {s:{'spot':True,'active':True,'contract':False,
                       'limits':{'cost':{'min':10},'amount':{'min':.000001}}}
                       for s in storage.DEFAULTS['watchlist']}
        self.price = 100.
        self.fail_candles = False
        self.order_error = None
        self.fill = {'id':'fake-only','status':'closed','filled':1.,'cost':100.,'average':100.,'fee':{'currency':'USDT','cost':.1}}
    def load_markets(self):
        return self.markets
    def market(self, symbol):
        return self.markets[symbol]
    def fetch_balance(self, opts=None):
        return {'free':{'USDT':10000.,'BTC':100.,'ETH':100.,'DOGE':100.}}
    def fetch_ticker(self, symbol):
        return {'last':self.price,'timestamp':int(time.time()*1000),'bid':self.price*.9999,'ask':self.price*1.0001,'quoteVolume':20000000.}
    def fetch_ohlcv(self, symbol, tf='1h', limit=175):
        if self.fail_candles:
            raise ccxt.BadResponse('malformed candle response')
        duration = {'1h':3600,'4h':14400,'1d':86400,'1w':604800}[tf]
        end = int(time.time()//duration)*duration
        return [[(end-(limit-i)*duration)*1000,100,101,99,100,10000] for i in range(limit)]
    def cost_to_precision(self, symbol, cost):
        return f'{cost:.2f}'
    def amount_to_precision(self, symbol, qty):
        return f'{math.floor(qty*1e6)/1e6:.6f}'
    def create_market_buy_order_with_cost(self, *args):
        self.orders.append(('buy',args))
        if self.order_error:
            raise self.order_error
        return dict(self.fill)
    def create_order(self, *args):
        self.orders.append(('sell',args))
        if self.order_error:
            raise self.order_error
        return dict(self.fill)


@pytest.fixture
def engine(tmp_path, monkeypatch):
    monkeypatch.setattr(storage,'DATA_DIR',tmp_path)
    monkeypatch.setattr(storage,'DB_PATH',tmp_path/'portfolio.sqlite3')
    monkeypatch.setattr(storage,'CONFIG_PATH',tmp_path/'settings.json')
    monkeypatch.setattr(credentials,'load',lambda _:None)
    monkeypatch.setattr(credentials,'save',lambda *a:None)
    monkeypatch.setattr(credentials,'delete',lambda *a:None)
    monkeypatch.setattr(trader.TradingEngine,'_new_exchange',staticmethod(lambda *a:FakeExchange()))
    instance = trader.TradingEngine()
    yield instance
    instance.stop_event.set()
    if instance.thread:
        instance.thread.join(timeout=3)


def arm_fake_live(engine):
    engine.private = FakeExchange()
    engine.mode, engine.running = 'live', True
    engine.stop_event.clear()
    return engine.private


def test_paper_round_trip_has_exact_cash_and_fee_accounting(engine):
    assert engine.mode == 'paper'
    assert engine._buy('BTC/USDT',800,100,.04)
    position = engine._active_positions('paper')[0]
    assert position['cost'] == 800
    assert engine._cash('paper') == 9200
    expected_qty = 800/(100*1.002*1.001)
    assert position['qty'] == pytest.approx(expected_qty)
    engine._sell(position,110,'test exit')
    expected = 9200+expected_qty*110*.998*.999
    assert engine._cash('paper') == pytest.approx(expected)
    assert engine._active_positions('paper') == []
    assert engine.snapshot()['realized'] == pytest.approx(round(expected-10000,2))
    assert engine.public.orders == []


def test_analysis_scan_never_trades_even_with_forced_signals(engine, monkeypatch):
    monkeypatch.setattr(trader,'entry_signal',lambda *a,**kw:(True,'BUY setup',100))
    rows = engine.scan_once(allow_orders=False)
    assert len(rows) == 8
    assert engine._active_positions('paper') == []
    assert engine.public.orders == []


def test_protective_exit_survives_failed_history_download(engine):
    engine._buy('BTC/USDT',800,100,.04)
    engine.public.price, engine.public.fail_candles = 90, True
    engine.running = True
    engine.scan_once(allow_orders=True)
    assert engine._active_positions('paper') == []
    assert storage.rows("SELECT side FROM trades WHERE side='SELL'")


def test_stale_held_asset_blocks_other_entries(engine):
    engine._buy('BTC/USDT',800,100,.04)
    engine.last_prices.update({'BTC/USDT':100,'ETH/USDT':100})
    engine.price_times['ETH/USDT'] = time.time()
    engine.running = True
    engine._enter_candidates([(100,20000000,'ETH/USDT',100,1,123)])
    assert len(engine._active_positions('paper')) == 1
    assert engine.risk_block


def test_daily_drawdown_remains_latched_after_rebound_and_restart(engine):
    assert engine._daily_allowed('paper',10000)[0]
    assert not engine._daily_allowed('paper',9600)[0]
    assert not engine._daily_allowed('paper',11000)[0]
    other = trader.TradingEngine()
    assert not other._daily_allowed('paper',11000)[0]


def test_exchange_switch_keeps_portfolios_and_cash_separate(engine):
    engine._buy('BTC/USDT',800,100,.04)
    engine.update_config({'exchange':'kraken'})
    assert engine._active_positions('paper') == []
    assert engine._cash('paper') == 10000
    engine._buy('ETH/USDT',400,100,.04)
    engine.update_config({'exchange':'mexc'})
    assert [p['symbol'] for p in engine._active_positions('paper')] == ['BTC/USDT']
    assert engine._cash('paper') == 9200


def test_dashboard_snapshot_performs_no_exchange_requests(engine):
    private = arm_fake_live(engine)
    private.fetch_balance = lambda *a:pytest.fail('state polling must never contact exchange')
    state = engine.snapshot()
    assert state['mode'] == 'live'
    assert state['cash'] is None


def test_order_timeout_is_not_resubmitted_and_blocks_restart(engine):
    private = arm_fake_live(engine)
    private.order_error = ccxt.RequestTimeout('synthetic timeout')
    engine._buy('BTC/USDT',800,100,.04)
    assert len(private.orders) == 1
    assert engine.live_halted and engine.stop_event.is_set()
    assert len(storage.unresolved_orders()) == 1
    other = trader.TradingEngine()
    assert other.mode == 'paper' and other.live_halted
    with pytest.raises(ValueError):
        other.set_mode('live','ENABLE LIVE TRADING',True)


def test_persisted_intent_exists_before_fake_order_submission(engine):
    private = arm_fake_live(engine)
    original = private.create_market_buy_order_with_cost
    def submit(*args):
        assert len(storage.unresolved_orders()) == 1
        assert storage.unresolved_orders()[0]['status'] == 'pending'
        return original(*args)
    private.create_market_buy_order_with_cost = submit
    engine._buy('BTC/USDT',800,100,.04)
    assert len(private.orders) == 1
    assert storage.unresolved_orders() == []
    position = engine._active_positions('live')[0]
    assert position['qty'] == 1 and position['cost'] == pytest.approx(100.1)


def test_live_fill_with_base_fee_tracks_net_received_amount(engine):
    private = arm_fake_live(engine)
    private.fill['fee'] = {'currency':'BTC','cost':.01}
    engine._buy('BTC/USDT',800,100,.04)
    position = engine._active_positions('live')[0]
    assert position['qty'] == pytest.approx(.99)
    assert position['cost'] == pytest.approx(100)


def test_live_partial_exit_keeps_remainder_and_allocates_cost(engine):
    private = arm_fake_live(engine)
    engine._buy('BTC/USDT',800,100,.04)
    original = engine._active_positions('live')[0]
    private.fill = {'id':'fake-sell','status':'canceled','filled':.4,'cost':44,'average':110,'fee':{'currency':'USDT','cost':.044}}
    engine._sell(original,110,'partial test')
    remainder = engine._active_positions('live')[0]
    assert remainder['qty'] == pytest.approx(.6)
    assert remainder['cost'] == pytest.approx(original['cost']*.6)
    assert storage.unresolved_orders() == []


def test_ledger_failure_after_fill_preserves_unresolved_intent(engine):
    private = arm_fake_live(engine)
    with storage.connection() as db:
        db.execute("CREATE TRIGGER fail_insert BEFORE INSERT ON positions BEGIN SELECT RAISE(ABORT,'synthetic disk error'); END")
    try:
        engine._buy('BTC/USDT',800,100,.04)
    except Exception:
        pass
    assert len(private.orders) == 1
    assert engine.live_halted
    assert len(storage.unresolved_orders()) == 1
    assert engine._active_positions('live') == []


def test_stop_in_flight_cannot_start_another_worker(engine, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    def scan(**kwargs):
        entered.set()
        release.wait(5)
    monkeypatch.setattr(engine,'scan_once',scan)
    engine.start()
    assert entered.wait(1)
    engine.stop()
    assert engine.running and engine.snapshot()['stopping']
    with pytest.raises(ValueError):
        engine.start()
    with pytest.raises(ValueError):
        engine.update_config({'scan_seconds':180})
    release.set()
    engine.thread.join(2)
    assert not engine.running


def test_history_warmup_serializes_to_null(engine):
    rows = engine.history('BTC/USDT','1h')
    assert rows and rows[0]['ema50'] is None
    import json
    json.dumps(rows,allow_nan=False)


def test_aggregate_meme_caps_and_total_exposure(engine):
    engine.running = True
    symbols = ['DOGE/USDT','PEPE/USDT','SHIB/USDT','BONK/USDT','FLOKI/USDT']
    engine.last_prices.update({s:100 for s in symbols})
    engine.price_times.update({s:time.time() for s in symbols})
    engine._enter_candidates([(100,20000000,s,100,1,123) for s in symbols])
    positions = engine._active_positions('paper')
    assert sum(p['qty']*100 for p in positions) <= 1000+1e-6
    assert all(p['cost']<=300+1e-6 for p in positions)
    assert len(positions)<=engine.cfg['max_positions']
