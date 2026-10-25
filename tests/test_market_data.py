"""Offline transport/cache checks with controlled time and no network."""
import sys
import threading
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import ccxt
import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from market_data import DataUnavailable, MarketData


class Exchange:
    has = {'fetchTickers': True}
    def __init__(self):
        self.calls = 0
        self.failure = None
    def fetch_ticker(self, symbol):
        self.calls += 1
        if self.failure:
            raise self.failure
        return {'last':100}
    def fetch_ohlcv(self, symbol, tf, limit):
        self.calls += 1
        return [[i*3600000,100,102,98,101,1000] for i in range(1,limit+1)]


def test_transport_retries_are_bounded_and_order_methods_rejected():
    exchange = Exchange()
    waits = []
    data = MarketData(exchange, wait=waits.append)
    exchange.failure = ccxt.RequestTimeout('signed-request-not-for-logging')
    with pytest.raises(ccxt.RequestTimeout):
        data.ticker('BTC/USDT')
    assert exchange.calls == 3 and waits == [.5,1]
    with pytest.raises(ValueError):
        data.read('create_order','BTC/USDT')
    assert exchange.calls == 3


def test_permanent_exchange_errors_are_not_retried():
    exchange = Exchange()
    exchange.failure = ccxt.BadSymbol('bad pair')
    with pytest.raises(ccxt.BadSymbol):
        MarketData(exchange,wait=lambda _:False).ticker('BAD/USDT')
    assert exchange.calls == 1


def test_circuit_breaker_cools_down_then_allows_a_probe():
    exchange = Exchange()
    exchange.failure = ccxt.RequestTimeout('timeout')
    clock = [100.]
    data = MarketData(exchange,monotonic=lambda:clock[0],wait=lambda _:False)
    for _ in range(3):
        with pytest.raises(ccxt.RequestTimeout):
            data.ticker('BTC/USDT')
    assert exchange.calls == 9
    with pytest.raises(DataUnavailable):
        data.ticker('BTC/USDT')
    assert exchange.calls == 9
    clock[0] += 31
    exchange.failure = None
    assert data.ticker('BTC/USDT')['last'] == 100


def test_cached_market_success_does_not_clear_ticker_failure_streak():
    exchange = Exchange()
    exchange.load_markets = lambda: {}
    exchange.failure = ccxt.RequestTimeout('timeout')
    data = MarketData(exchange,monotonic=lambda:100.,wait=lambda _:False)
    for _ in range(3):
        data.markets()
        with pytest.raises(ccxt.RequestTimeout):
            data.ticker('BTC/USDT')
    with pytest.raises(DataUnavailable):
        data.ticker('ETH/USDT')
    assert exchange.calls == 9


def test_candle_transport_circuit_does_not_suppress_protective_quotes():
    exchange = Exchange()
    def unavailable(*args,**kwargs):
        raise ccxt.RequestTimeout('candle service unavailable')
    exchange.fetch_ohlcv = unavailable
    data = MarketData(exchange,monotonic=lambda:100.,wait=lambda _:False)
    for _ in range(3):
        with pytest.raises(ccxt.RequestTimeout):
            data.read('fetch_ohlcv','BTC/USDT','1h',limit=175)
    with pytest.raises(DataUnavailable):
        data.read('fetch_ohlcv','BTC/USDT','1h',limit=175)
    assert data.ticker('BTC/USDT')['last'] == 100


@pytest.mark.parametrize('header,expected', [('120',120),('999999',300),('NaN',30),('-1',30)])
def test_rate_limit_enters_cooldown_without_retry_burst(header,expected):
    exchange = Exchange()
    exchange.failure = ccxt.RateLimitExceeded('429')
    exchange.last_response_headers = {'Retry-After':header}
    data = MarketData(exchange,monotonic=lambda:100.,wait=lambda _:pytest.fail('429 must not retry immediately'))
    with pytest.raises(ccxt.RateLimitExceeded):
        data.ticker('BTC/USDT')
    assert exchange.calls == 1 and data.open_until == 100+expected
    with pytest.raises(DataUnavailable):
        data.ticker('ETH/USDT')


def test_candle_cache_expires_at_next_close_and_returns_independent_frames():
    exchange = Exchange()
    clock = [181*3600+1.]
    data = MarketData(exchange,clock=lambda:clock[0])
    frame = data.candles('BTC/USDT','1h',180)
    frame.loc[0,'close'] = 999
    assert data.candles('BTC/USDT','1h',180).iloc[0]['close'] == 101
    assert exchange.calls == 1
    clock[0] = 182*3600+3
    data.candles('BTC/USDT','1h',180)
    assert exchange.calls == 2


def test_stale_and_gapped_history_never_enters_cache():
    exchange = Exchange()
    data = MarketData(exchange,clock=lambda:300*3600)
    with pytest.raises(DataUnavailable,match='stale'):
        data.candles('BTC/USDT','1h',180)
    assert not data.cache
    original = exchange.fetch_ohlcv
    exchange.fetch_ohlcv = lambda *a,**kw: original(*a,**kw)[::2]
    data.clock = lambda:181*3600
    with pytest.raises(DataUnavailable,match='missing'):
        data.candles('BTC/USDT','1h',180)
    assert not data.cache


def test_clock_rollback_cannot_return_a_now_incomplete_cached_candle():
    exchange = Exchange()
    clock = [181*3600+1.]
    data = MarketData(exchange,clock=lambda:clock[0])
    assert data.candles('BTC/USDT','1h',180).iloc[-1]['timestamp'] == 180*3600000
    clock[0] = 180.5*3600
    result = data.candles('BTC/USDT','1h',180)
    assert exchange.calls == 2
    assert result.iloc[-1]['timestamp'] + 3600000 <= clock[0]*1000


def test_public_client_is_serialized_between_threads():
    exchange = Exchange()
    active, seen = 0, []
    barrier = threading.Event()
    def fetch(symbol):
        nonlocal active
        active += 1
        seen.append(active)
        barrier.wait(.01)
        active -= 1
        return {'last':100}
    exchange.fetch_ticker = fetch
    data = MarketData(exchange)
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(data.ticker,['BTC/USDT']*8))
    assert max(seen) == 1
