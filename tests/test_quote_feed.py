"""Websocket quote feed: continuity checks, reconnects, lifecycle and engine fallback.

Every exchange here is a scripted in-process fake; no socket is opened.
"""
import asyncio
import random
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import feed
import observability
import storage
import trader
from test_engine import FakeExchange, engine  # noqa: F401  (fixture)


class FakeStreamClient:
    """Plays a script: each item is an update dict, an exception to raise, or ('block', seconds)."""
    has = {'watchBidsAsks': True, 'watchTickers': True}
    options = {}

    def __init__(self, script):
        self.script = list(script)
        self.calls: list[list[str]] = []
        self.closed = False

    async def watch_bids_asks(self, symbols):
        self.calls.append(list(symbols))
        if not self.script:
            await asyncio.sleep(3600)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        if isinstance(item, tuple):
            await asyncio.sleep(item[1])
            return {}
        return item

    async def close(self):
        self.closed = True


def quote(bid, ask, timestamp=None):
    return {'bid': bid, 'ask': ask, 'timestamp': timestamp}


def wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


@pytest.fixture
def metrics():
    return observability.Metrics()


def make_feed(script_or_factory, *, symbols=('BTC/USDT', 'ETH/USDT'), metrics=None, **kwargs):
    factory = script_or_factory if callable(script_or_factory) else (lambda: FakeStreamClient(script_or_factory))
    kwargs.setdefault('heartbeat_seconds', 2.0)
    kwargs.setdefault('max_backoff', 0.05)
    kwargs.setdefault('rng', random.Random(1))
    return feed.QuoteFeed('mexc', symbols, client_factory=factory, metrics=metrics or observability.Metrics(), **kwargs)


# --- Continuity checks -----------------------------------------------------------

def test_feed_serves_fresh_quotes_and_drops_out_of_order_updates(metrics):
    stream = make_feed([{'BTC/USDT': quote(100., 100.1, 1_000_000)},
                        {'BTC/USDT': quote(99., 99.1, 900_000)},          # older than the held quote
                        {'ETH/USDT': quote(10., 10.01)}], metrics=metrics)  # Kraken-style: no timestamp
    stream.start()
    try:
        assert wait_for(lambda: stream.counters['messages'] == 3)
        assert stream.healthy()
        btc = stream.ticker('BTC/USDT')
        assert btc['bid'] == 100. and btc['ask'] == 100.1 and btc['timestamp'] == 1_000_000 and btc['_source'] == 'stream'
        assert 0 <= time.time() - btc['_received_at'] < 5
        eth = stream.ticker('ETH/USDT')
        assert eth['timestamp'] is None and eth['_received_at'] > 0
        assert stream.counters['out_of_order'] == 1 and stream.counters['quotes'] == 2
        assert metrics.snapshot()['counters']['feed.out_of_order'] == 1
        assert stream.ticker('DOGE/USDT') is None
        assert stream.status()['state'] == 'live' and stream.status()['quoted'] == 2
    finally:
        stream.stop()


def test_crossed_or_incomplete_quotes_are_rejected():
    stream = make_feed([{'BTC/USDT': quote(101., 100.)}, {'BTC/USDT': quote(100., None)},
                        {'BTC/USDT': quote(float('nan'), 1.)}, {'BTC/USDT': 'garbage'}, {'BTC/USDT': quote(1., 1.)}])
    stream.start()
    try:
        assert wait_for(lambda: stream.counters['messages'] == 5)
        assert stream.counters['rejected'] == 3 and stream.counters['quotes'] == 1
        assert stream.ticker('BTC/USDT')['bid'] == 1.
    finally:
        stream.stop()


def test_a_stream_quote_keeps_only_the_sizes_sent_with_its_prices():
    # a safety exit with no depth book may sell at the top of book only against a stated size
    now = [1_000.0]
    stream = feed.QuoteFeed('mexc', ['BTC/USDT', 'ETH/USDT'], clock=lambda: now[0], metrics=observability.Metrics())
    stream.state = 'live'
    stream._ingest({'BTC/USDT': {**quote(100., 100.1, 1_000_000), 'bidVolume': 0.5, 'askVolume': '2'},
                    'ETH/USDT': {**quote(10., 10.01), 'bidVolume': 0, 'askVolume': 'lots'}}, now[0])
    btc = stream.ticker('BTC/USDT')
    assert btc['bidVolume'] == 0.5 and btc['askVolume'] == 2.0
    eth = stream.ticker('ETH/USDT')
    assert eth['bidVolume'] is None and eth['askVolume'] is None          # zero or garbage is unknown, not a size
    now[0] += 1
    stream._ingest({'BTC/USDT': quote(100.2, 100.3, 1_000_100)}, now[0])
    btc = stream.ticker('BTC/USDT')
    assert btc['bid'] == 100.2 and btc['bidVolume'] is None and btc['askVolume'] is None   # never an older size


def test_unread_quotes_are_coalesced_not_queued():
    stream = make_feed([{'BTC/USDT': quote(1., 1.1, 1_000)} for _ in range(6)])
    stream.start()
    try:
        assert wait_for(lambda: stream.counters['messages'] == 6)
        assert stream.counters['coalesced'] == 5 and stream.ticker('BTC/USDT')['_stream_sequence'] == 6
    finally:
        stream.stop()


def test_feed_is_unhealthy_after_a_heartbeat_lapse_and_refuses_old_quotes():
    now = [1_000.0]
    stream = make_feed([{'BTC/USDT': quote(1., 1.1)}], clock=lambda: now[0], heartbeat_seconds=5.0)
    stream.start()
    try:
        assert wait_for(lambda: stream.counters['messages'] == 1)
        assert stream.ticker('BTC/USDT', max_age=1.0) is not None
        now[0] += 3.0
        assert stream.healthy() and stream.ticker('BTC/USDT', max_age=1.0) is None
        assert stream.ticker('BTC/USDT', max_age=10.0) is not None
        now[0] += 3.0
        assert not stream.healthy() and stream.ticker('BTC/USDT') is None
        assert not stream.covers(['BTC/USDT'], max_age=60)
    finally:
        stream.stop()


# --- Reconnects and lifecycle ----------------------------------------------------

def test_feed_reconnects_after_errors_and_stalls_with_backoff(metrics):
    clients = []

    def factory():
        scripts = [[ConnectionError('socket closed')], [('block', 3600)], [{'BTC/USDT': quote(2., 2.1)}]]
        client = FakeStreamClient(scripts[min(len(clients), 2)])
        clients.append(client)
        return client

    stream = make_feed(factory, heartbeat_seconds=0.2, metrics=metrics)
    stream.start()
    try:
        assert wait_for(lambda: stream.counters['quotes'] == 1, timeout=10)
        assert stream.counters['reconnects'] == 2 and stream.counters['stalls'] == 1
        assert clients[0].closed and clients[1].closed
        status = stream.status()
        assert status['state'] == 'live' and 'FeedStalled' in status['last_error'] and status['backoff_s'] == 1.0
        assert metrics.snapshot()['counters']['feed.reconnects'] == 2
    finally:
        stream.stop()


def test_stop_terminates_the_thread_and_closes_the_client():
    client = FakeStreamClient([('block', 3600)])
    stream = make_feed(lambda: client)
    stream.start()
    assert wait_for(lambda: client.calls)
    started = time.monotonic()
    stream.stop(timeout=3)
    assert time.monotonic() - started < 3
    assert not stream._thread.is_alive() and stream.status()['state'] == 'stopped' and client.closed
    assert stream.ticker('BTC/USDT') is None


def test_changing_the_symbol_set_resubscribes_without_counting_a_failure():
    clients = []

    def factory():
        client = FakeStreamClient([{'BTC/USDT': quote(1., 1.1)}] * 3)
        clients.append(client)
        return client

    stream = make_feed(factory, symbols=['BTC/USDT'])
    stream.start()
    try:
        assert wait_for(lambda: stream.counters['messages'] >= 1)
        stream.set_symbols(['BTC/USDT', 'ETH/USDT'])
        assert wait_for(lambda: len(clients) == 2 and clients[1].calls)
        assert clients[1].calls[0] == ['BTC/USDT', 'ETH/USDT'] and clients[0].closed
        assert stream.counters['reconnects'] == 0
        stream.set_symbols(['ETH/USDT'])
        assert wait_for(lambda: len(clients) == 3)
        assert stream.ticker('BTC/USDT') is None
    finally:
        stream.stop()


def test_feed_falls_back_to_watch_tickers_when_bid_ask_channel_is_missing():
    class TickerOnly(FakeStreamClient):
        has = {'watchBidsAsks': False, 'watchTickers': True}

        async def watch_tickers(self, symbols):
            return await self.watch_bids_asks(symbols)

    stream = make_feed(lambda: TickerOnly([{'BTC/USDT': quote(5., 5.1)}]))
    stream.start()
    try:
        assert wait_for(lambda: stream.counters['quotes'] == 1)
    finally:
        stream.stop()


# --- Engine integration -----------------------------------------------------------

class FakeFeed:
    def __init__(self, quotes=None, healthy=True):
        self.quotes, self._healthy, self.state = dict(quotes or {}), healthy, 'live'
        self.symbols_seen, self.stopped = [], False

    def ticker(self, symbol, *, max_age=None):
        q = self.quotes.get(symbol)
        return dict(q, symbol=symbol, _source='stream') if (self._healthy and q) else None

    def covers(self, symbols, *, max_age):
        return bool(symbols) and all(self.ticker(s, max_age=max_age) for s in symbols)

    def set_symbols(self, symbols):
        self.symbols_seen.append(list(symbols))

    def status(self):
        return {'state': self.state}

    def stop(self, timeout=None):
        self.stopped = True


def test_engine_starts_no_feed_unless_the_setting_is_on(engine):
    engine.start()
    try:
        assert engine.feed is None and engine.snapshot()['feed'] == {'state': 'off', 'enabled': False}
    finally:
        engine.stop()
    assert not storage.settings()['streaming_quotes']


def test_engine_starts_and_stops_the_feed_with_the_bot(engine, monkeypatch):
    client = FakeStreamClient([{'BTC/USDT': quote(100., 100.1)}])
    monkeypatch.setattr(feed, 'default_client_factory', lambda name: client)
    engine.cfg['streaming_quotes'] = True
    engine.start()
    try:
        assert wait_for(lambda: engine.feed is not None and engine.feed.counters['messages'] == 1)
        assert 'BTC/USDT' in client.calls[0] and engine.snapshot()['feed']['state'] == 'live'
        worker = engine.feed._thread
    finally:
        engine.stop()
    assert engine.feed is None and not worker.is_alive() and client.closed
    assert [t.name for t in threading.enumerate() if t.name == 'QuoteFeed'] == []


def test_feed_start_failure_leaves_rest_polling_in_charge(engine, monkeypatch):
    def broken(name):
        raise RuntimeError('no websocket support here')
    monkeypatch.setattr(feed, 'default_client_factory', broken)
    engine.cfg['streaming_quotes'] = True
    engine.start()
    try:
        # The factory runs inside the feed thread; the engine stays up and the
        # feed reports its failure state while REST polling continues.
        assert wait_for(lambda: engine.feed is not None and engine.feed.counters['reconnects'] >= 1)
        assert engine.running
    finally:
        engine.stop()


def test_stream_quote_overrides_rest_bid_ask_but_keeps_turnover(engine):
    received = time.time()
    engine.feed = FakeFeed({'BTC/USDT': {'bid': 101., 'ask': 101.1, 'timestamp': None, '_received_at': received}})
    rest = {'bid': 100., 'ask': 100.2, 'timestamp': int((received - 2) * 1000), 'quoteVolume': 5e7, '_received_at': received - 2}
    merged = engine._quote('BTC/USDT', rest)
    assert merged['bid'] == 101. and merged['ask'] == 101.1 and merged['quoteVolume'] == 5e7 and merged['_source'] == 'stream'
    assert engine._quote('ETH/USDT', rest) is rest          # no stream quote for ETH
    engine.feed = FakeFeed(healthy=False)
    assert engine._quote('BTC/USDT', rest) is rest          # unhealthy stream: REST untouched
    assert engine._quote('BTC/USDT', None) is None
    # A REST quote that is newer than the stream's wins.
    engine.feed = FakeFeed({'BTC/USDT': {'bid': 101., 'ask': 101.1, 'timestamp': None, '_received_at': received - 5}})
    assert engine._quote('BTC/USDT', rest) is rest


def test_protective_monitor_uses_the_stream_and_skips_rest_when_every_holding_streams(engine, monkeypatch):
    engine._buy('BTC/USDT', 800, 100, .04)
    engine.running = True
    rest_calls = []
    monkeypatch.setattr(engine.data, 'tickers', lambda symbols: rest_calls.append(symbols) or {})
    engine.feed = FakeFeed({'BTC/USDT': {'bid': 90., 'ask': 90.1, 'timestamp': None, '_received_at': time.time()}})
    engine._monitor_if_due(force=True)
    assert rest_calls == []
    assert engine._active_positions('paper') == []
    sale = storage.rows("SELECT price FROM trades WHERE side='SELL'")[0]['price']
    assert sale == pytest.approx(90 * (1 - engine.cfg['slippage_pct'] / 100))
    assert engine.metrics.snapshot()['counters'].get('monitor.source.stream', 0) >= 1
    assert engine._next_monitor - time.monotonic() <= engine.STREAM_MONITOR_SECONDS + .05


def test_protective_monitor_falls_back_to_rest_when_the_stream_cannot_cover_a_holding(engine, monkeypatch):
    engine._buy('BTC/USDT', 800, 100, .04)
    engine.running = True
    rest_calls = []
    original = engine.data.tickers
    monkeypatch.setattr(engine.data, 'tickers', lambda symbols: rest_calls.append(symbols) or original(symbols))
    engine.feed = FakeFeed(healthy=False)
    engine.public.price = 90
    engine._monitor_if_due(force=True)
    assert rest_calls == [['BTC/USDT']] and engine._active_positions('paper') == []
    assert engine._next_monitor - time.monotonic() > engine.STREAM_MONITOR_SECONDS


def test_scan_subscribes_the_universe_and_prefers_stream_prices(engine, monkeypatch):
    # REST tickers are two seconds old (still fresh); the stream quote is newer.
    rest = engine.public.fetch_ticker
    monkeypatch.setattr(engine.public, 'fetch_ticker', lambda symbol: {**rest(symbol), 'timestamp': int((time.time() - 2) * 1000)})
    now = time.time()
    engine.feed = FakeFeed({s: {'bid': 100.5, 'ask': 100.6, 'timestamp': None, '_received_at': now} for s in ('BTC/USDT',)})
    rows = engine.scan_once(allow_orders=False)
    assert engine.feed.symbols_seen and set(engine.feed.symbols_seen[0]) >= set(engine.cfg['watchlist'])
    by_symbol = {row['symbol']: row for row in rows}
    assert by_symbol['BTC/USDT']['bid'] == 100.5 and by_symbol['BTC/USDT']['ask'] == 100.6
    assert by_symbol['ETH/USDT']['bid'] == pytest.approx(100 * .9999)   # REST for the rest
    assert by_symbol['BTC/USDT']['volume'] == 20_000_000.               # turnover still from REST


def test_setting_round_trips_and_defaults_off(engine):
    assert storage.DEFAULTS['streaming_quotes'] is False
    editable = {key: value for key, value in engine.cfg.items() if key != 'paper_start_usdt'}
    engine.update_config({**editable, 'streaming_quotes': True})
    assert engine.cfg['streaming_quotes'] is True and storage.settings()['streaming_quotes'] is True
    with pytest.raises(storage.ConfigValidationError):
        engine.update_config({**editable, 'streaming_quotes': 'yes'})
