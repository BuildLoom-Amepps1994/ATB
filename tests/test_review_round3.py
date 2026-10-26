"""Regression tests for the third review round: MEXC streaming, exit retries, quote clocks, thread safety."""
import asyncio
import random
import sys
import threading
import time
from pathlib import Path

import ccxt
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import decision
import feed
import observability
import storage
from test_decisions import decide, position
from test_engine import FakeExchange, arm_fake_live, engine  # noqa: F401  (fixture)


# --- MEXC streaming through CCXT's own protobuf decoder ---------------------------------

def mexc_market(market_id, symbol):
    base = symbol.split('/')[0]
    return {'id': market_id, 'symbol': symbol, 'base': base, 'quote': 'USDT', 'baseId': base, 'quoteId': 'USDT',
            'spot': True, 'type': 'spot', 'active': True, 'contract': False, 'precision': {}, 'limits': {}, 'info': {}}


def book_ticker_frame(market_id, bid, ask):
    from ccxt.protobuf.mexc import PushDataV3ApiWrapper_pb2 as pb
    frame = pb.PushDataV3ApiWrapper()
    frame.channel = f'spot@public.aggre.bookTicker.v3.api.pb@100ms@{market_id}'
    frame.symbol, frame.sendTime = market_id, int(time.time() * 1000)
    book = frame.publicAggreBookTicker
    book.bidPrice, book.askPrice, book.bidQuantity, book.askQuantity = str(bid), str(ask), '1', '1'
    return frame.SerializeToString()


def simulated_mexc_class(auto_push=True):
    import ccxt.pro

    class SimulatedMexc(ccxt.pro.mexc):
        """The real CCXT MEXC client; only the socket is simulated. Each subscription
        request starts a stream of protobuf book-ticker pushes for that market, fed
        through CCXT's handle_message exactly as received frames are."""
        subscriptions: list = []
        instances: list = []

        def __init__(self, config=None):
            super().__init__(config or {'newUpdates': True})
            SimulatedMexc.instances.append(self)

        def burst(self, frames):
            """Deliver several frames in one callback, as one socket read would."""
            client = self.client(self.urls['api']['ws']['spot'])
            for market_id, bid in frames:
                self.handle_message(client, book_ticker_frame(market_id, bid, bid + .1))

        async def load_markets(self, reload=False, params={}):
            if not self.markets:
                self.set_markets([mexc_market('BTCUSDT', 'BTC/USDT'), mexc_market('ETHUSDT', 'ETH/USDT')])
            return self.markets

        async def watch(self, url, message_hash, message=None, subscribe_hash=None, subscription=None):
            client = self.client(url)
            if subscribe_hash not in SimulatedMexc.subscriptions:
                SimulatedMexc.subscriptions.append(subscribe_hash)
                if not auto_push:
                    return await client.future(message_hash)
                market_id = message['params'][0].rsplit('@', 1)[-1]
                loop, count = asyncio.get_running_loop(), [0]

                def push():
                    count[0] += 1
                    self.handle_message(client, book_ticker_frame(market_id, 100 + count[0] * .01, 100.1 + count[0] * .01))
                    loop.call_later(.02, push)
                loop.call_later(.02, push)
            return await client.future(message_hash)

        async def close(self):
            await super().close()
    return SimulatedMexc


def wait_for(condition, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(.02)
    return False


def test_mexc_stream_goes_live_through_ccxts_protobuf_ticker_channel():
    pytest.importorskip('google.protobuf')
    cls = simulated_mexc_class()
    cls.subscriptions = []
    stream = feed.QuoteFeed('mexc', ['BTC/USDT', 'ETH/USDT'], client_factory=lambda: cls({'newUpdates': True}),
                            heartbeat_seconds=1.0, max_backoff=.1, metrics=observability.Metrics(), rng=random.Random(1))
    try:
        stream.start()
        assert wait_for(lambda: stream.covers(['BTC/USDT', 'ETH/USDT'], max_age=2.0)), stream.status()
        status = stream.status()
        assert status['state'] == 'live' and status['stalls'] == 0 and status['reconnects'] == 0
        assert sorted(cls.subscriptions) == ['ticker:BTC/USDT', 'ticker:ETH/USDT']   # one per symbol, sent once
        quote = stream.ticker('ETH/USDT', max_age=2.0)
        assert 100 < quote['bid'] < quote['ask'] and quote['_source'] == 'stream'
        # Both symbols keep updating: a busy one cannot starve the other.
        first = {s: stream.ticker(s)['_stream_sequence'] for s in ('BTC/USDT', 'ETH/USDT')}
        assert wait_for(lambda: all(stream.ticker(s)['_stream_sequence'] > first[s] for s in first))
    finally:
        stream.stop()


def test_combined_bid_ask_watch_is_still_used_where_it_resolves():
    client = type('KrakenLike', (), {'has': {'watchBidsAsks': True, 'watchTicker': True, 'watchTickers': True}})()
    assert feed.QuoteFeed('kraken', ['BTC/USD'], client_factory=lambda: client)._watch_method(client) == 'watch_bids_asks'
    assert feed.QuoteFeed('mexc', ['BTC/USDT'], client_factory=lambda: client)._watch_method(client) == 'watch_ticker'


def test_mexc_subscriptions_are_capped_at_thirty_with_held_symbols_first():
    symbols = [f'S{index}/USDT' for index in range(45)]
    stream = feed.QuoteFeed('mexc', symbols, client_factory=lambda: None)
    assert stream.symbols == tuple(symbols[:30])
    assert stream.status()['symbols'] == 30 and stream.status()['unsubscribed'] == 15
    stream.set_symbols(['HELD/USDT'] + symbols)
    assert stream.symbols[0] == 'HELD/USDT' and len(stream.symbols) == 30 and stream.status()['unsubscribed'] == 16
    uncapped = feed.QuoteFeed('kraken', symbols, client_factory=lambda: None)
    assert len(uncapped.symbols) == 45 and uncapped.status()['unsubscribed'] == 0


def test_runtime_dependencies_name_protobuf():
    root = Path(__file__).resolve().parents[1]
    assert 'protobuf' in (root / 'requirements.txt').read_text()
    assert any(line.startswith('protobuf==') for line in (root / 'requirements-lock.txt').read_text().splitlines())
    spec = (root / 'CryptoPilotEditable.spec').read_text()
    assert "collect_submodules('ccxt.protobuf')" in spec and "collect_submodules('google.protobuf'" in spec


# --- Quote ordering across two clocks --------------------------------------------------

def test_an_exchange_clock_running_ahead_cannot_suppress_a_later_receipt_stamped_bid(engine):
    engine._buy('BTC/USDT', 800, 100, .04)                    # stop at 100.2 * .96
    engine.running = True
    now = time.time()
    ahead = {'bid': 97 * .9999, 'ask': 97 * 1.0001, 'timestamp': int((now + 5) * 1000), '_received_at': now - .5}
    engine._observe_ticker('BTC/USDT', ahead)
    # MEXC's book ticker carries no timestamp: it is aged from receipt, which is later.
    book = {'bid': 95., 'ask': 95.01, 'timestamp': None, '_received_at': time.time()}
    bid, _, _ = engine._observe_ticker('BTC/USDT', book)
    assert bid == 95. and engine.last_prices['BTC/USDT'] == 95.
    engine._protect_positions('BTC/USDT', bid)
    assert engine._active_positions('paper') == []


def test_a_receipt_stamped_quote_that_arrived_earlier_is_still_superseded(engine):
    now = time.time()
    engine._observe_ticker('BTC/USDT', {'bid': 100., 'ask': 100.01, 'timestamp': int(now * 1000), '_received_at': now})
    bid, _, _ = engine._observe_ticker('BTC/USDT', {'bid': 90., 'ask': 90.01, 'timestamp': None, '_received_at': now - 3})
    assert bid == 100. and engine.last_prices['BTC/USDT'] == 100.
    # Two exchange-stamped quotes still compare on the exchange clock.
    bid, _, _ = engine._observe_ticker('BTC/USDT', {'bid': 80., 'ask': 80.01, 'timestamp': int((now - 5) * 1000),
                                                    '_received_at': time.time()})
    assert bid == 100.


# --- Protective exits: one attempt per tick, levels ratchet regardless ---------------------

def test_a_rejected_live_target_exit_is_submitted_once_per_poll_and_still_ratchets(engine):
    client = arm_fake_live(engine)
    assert engine._buy('BTC/USDT', 800, 100, .04)
    client.order_error = ccxt.InvalidOrder('rejected by exchange')
    engine.data.candles('BTC/USDT', '1h', limit=175)          # cached ATR (2.0) so the monitor trails
    engine.public.price = 120.                                 # above the stored target
    for poll in range(1, 4):
        engine._monitor_if_due(force=True)
        rows = storage.rows("SELECT status FROM order_journal WHERE side='SELL' AND mode='live'")
        assert len(rows) == poll, rows
    held = engine._active_positions('live')[0]
    assert held['highest'] == pytest.approx(120 * .9999) and held['stop'] == pytest.approx(120 * .9999 - 5)


def test_a_different_bid_after_the_protective_check_is_a_new_exit_decision(engine):
    client = arm_fake_live(engine)
    assert engine._buy('BTC/USDT', 800, 100, .04)
    client.order_error = ccxt.InvalidOrder('rejected by exchange')
    stop = engine._active_positions('live')[0]['stop']
    sells = lambda: len(storage.rows("SELECT status FROM order_journal WHERE side='SELL' AND mode='live'"))
    engine._mark_positions('BTC/USDT', stop - 1, 2., False, stored_checked_at=stop - 1)
    assert sells() == 0                     # the protective check already tried this bid
    engine._mark_positions('BTC/USDT', stop - 2, 2., False, stored_checked_at=stop - 1)
    assert sells() == 1                     # a newer bid between the two calls is judged again
    engine._mark_positions('BTC/USDT', stop - 2, 2., False)
    assert sells() == 2                     # no protective check ran: the trail exits itself


def test_exit_policy_ratchets_on_every_outcome_and_names_its_trigger():
    target = decision.evaluate_exit(position(), 120., atr=2.)
    assert target['trigger'] == 'stored' and target['highest'] == 120. and target['stop'] == 115.
    dust = decision.evaluate_exit(position(), 110., atr=2., dust=True)
    assert dust['action'] == 'skip' and dust['trigger'] is None and dust['highest'] == 110. and dust['stop'] == 105.
    assert decision.evaluate_exit(position(highest=110.), 104., atr=2.)['trigger'] == 'trailing'
    assert decision.evaluate_exit(position(), 101., atr=2., trend_broken=True)['trigger'] == 'trend'
    assert decision.evaluate_exit(position(), 101., atr=2.)['trigger'] is None
    # Without ATR the stop never moves, whatever the outcome.
    assert decision.evaluate_exit(position(), 120.)['stop'] == 97.


def test_dust_positions_keep_ratcheting_their_trailing_levels(engine, monkeypatch):
    engine._buy('BTC/USDT', 800, 100, .04)
    engine.running = True
    monkeypatch.setattr(engine, '_is_dust', lambda *a, **k: True)
    engine._mark_positions('BTC/USDT', 110., 2., False)
    held = engine._active_positions('paper')[0]
    assert held['highest'] == 110. and held['stop'] == 105.


# --- Dashboard status and thread safety ------------------------------------------------

def test_a_signal_that_ran_away_reports_the_move_first():
    assert decide(volume=10., price=120.).status == decision.STATUS_MOVED


def test_decision_log_tolerates_reads_while_the_scan_appends():
    log, errors, done = decision.DecisionLog(limit=200), [], threading.Event()
    sample = decide()

    def writer():
        for _ in range(20000):
            log.add(sample)
        done.set()

    def reader():
        try:
            while not done.is_set():
                log.recent(limit=200)
        except Exception as exc:           # deque mutated during iteration without the lock
            errors.append(exc)
    threads = [threading.Thread(target=writer)] + [threading.Thread(target=reader) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert errors == [] and len(log) == 200


def test_monitor_survives_the_feed_being_cleared_mid_poll(engine, monkeypatch):
    engine._buy('BTC/USDT', 800, 100, .04)
    engine.running = True

    class VanishingFeed:
        def covers(self, symbols, *, max_age):
            engine.feed = None                 # the settings thread stops streaming meanwhile
            return False

        def ticker(self, symbol, *, max_age=None):
            return None
    engine.feed = VanishingFeed()
    engine.public.price = 90
    engine._monitor_if_due(force=True)
    assert engine._active_positions('paper') == []


# --- Final review: realised-loss latch, fees, correlation, manual orders --------------------

from test_hardening_fixes import capture_events  # noqa: E402


def book_sell(engine, realized, symbol='BTC/USDT'):
    with storage.connection() as db:
        db.execute('''INSERT INTO trades(created_at,exchange,mode,symbol,side,price,quantity,gross,fee,realized,reason,order_id)
                      VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''',
                   (storage._now(), engine.exchange_name, 'paper', symbol, 'SELL', 100, 1, 100, 0, realized, 'test', 'x'))


def test_the_realised_loss_stop_latches_when_the_loss_is_booked_not_at_the_next_entry(engine):
    book_sell(engine, -400.)                      # limit is a few % of 10 000 USDT
    assert engine.risk.realized_stop('paper') is None
    engine._daily_allowed('paper', engine._equity('paper', engine._cash('paper'), []))   # every poll and scan
    assert engine.risk.realized_stop('paper')
    book_sell(engine, 300.)                       # a later profit cannot lift it
    assert not engine._buy('BTC/USDT', 200, 100, .04)
    assert engine.risk.status()['realized_stop']


def test_clearing_the_realised_stop_holds_until_a_further_loss_the_same_day(engine):
    book_sell(engine, -400.)
    equity = engine._equity('paper', engine._cash('paper'), [])
    engine._daily_allowed('paper', equity)
    engine.risk.clear_realized_stop('paper', 'RESUME TRADING')
    assert engine._buy('BTC/USDT', 200, 100, .04)                  # the reviewed loss does not re-latch
    assert engine.risk.realized_stop('paper') is None
    book_sell(engine, -1., symbol='ETH/USDT')                      # a further loss today does
    engine._daily_allowed('paper', equity)
    assert engine.risk.realized_stop('paper')
    # A paper reset forgets both the latch and the clear point.
    storage.reset_paper(engine.exchange_name)
    assert engine.risk.realized_stop('paper') is None and engine.risk._cleared_floor('paper') is None


def test_a_third_asset_fee_reaches_realised_pnl_on_both_legs(engine):
    engine.paper.fee_currency = 'MX'
    cash_before = engine._cash('paper')
    assert engine._buy('BTC/USDT', 800, 100, .04)
    position = engine._active_positions('paper')[0]
    assert engine._sell(position, position['entry_price'], 'flat exit')
    sell = storage.rows("SELECT gross,realized FROM trades WHERE mode='paper' AND side='SELL'")[0]
    buy = storage.rows("SELECT gross FROM trades WHERE mode='paper' AND side='BUY'")[0]
    rate = engine.cfg['fee_pct'] / 100
    assert sell['realized'] < 0
    assert sell['realized'] == pytest.approx(sell['gross'] - buy['gross'] - rate * (buy['gross'] + sell['gross']), abs=1e-6)
    # Cash is exact: the third-asset fee never touched USDT.
    assert engine._cash('paper') == pytest.approx(cash_before - buy['gross'] + sell['gross'])


def test_a_fee_split_between_usdt_and_a_third_asset_is_not_counted_twice(engine):
    client = arm_fake_live(engine)
    client.fill = {**client.fill, 'fee': None, 'fees': [{'currency': 'USDT', 'cost': .1}, {'currency': 'MX', 'cost': .01}]}
    assert engine._buy('BTC/USDT', 800, 100, .04)
    trade = storage.rows("SELECT fee FROM trades WHERE mode='live' AND side='BUY'")[0]
    assert trade['fee'] == pytest.approx(max(.1, 100 * engine.cfg['fee_pct'] / 100))


def test_an_uncertain_submit_names_its_intent_on_the_failure_and_halt_lines(engine, monkeypatch):
    events = capture_events(monkeypatch)
    client = arm_fake_live(engine)
    client.order_error = ccxt.RequestTimeout('synthetic timeout')
    monkeypatch.setattr(engine, '_locate_after_ambiguity', lambda *a, **k: None)
    assert not engine._buy('BTC/USDT', 800, 100, .04)
    row = storage.rows("SELECT intent_id,status FROM order_journal WHERE mode='live'")[0]
    assert row['status'] == 'uncertain'
    failure = next(fields for kind, fields in events if kind == 'ledger.failure')
    halt = next(fields for kind, fields in events if kind == 'execution.halt')
    assert failure['intent_id'] == row['intent_id'] == halt['intent_id']


def test_a_filled_manual_order_is_never_reported_as_failed(engine, monkeypatch):
    events = capture_events(monkeypatch)
    original = storage.audit

    def audit(kind, message):
        if kind == 'trade':
            raise storage.sqlite3.OperationalError('database is locked')
        return original(kind, message)
    monkeypatch.setattr(storage, 'audit', audit)
    result = engine.manual_trade('BTC/USDT', 'BUY', quote_amount=100)
    assert result['status'] == 'filled'
    kinds = [kind for kind, _ in events]
    assert 'manual_order.failed' not in kinds and kinds.count('manual_order.completed') == 1


# --- Final review: clock steps, stream bursts, MEXC reads, unlisted pairs -------------------

def test_a_backward_clock_step_cannot_freeze_the_bid_or_fake_freshness(engine, monkeypatch):
    engine._buy('BTC/USDT', 800, 100, .04)                    # stop near 96
    engine.running = True
    real = time.time
    engine._observe_ticker('BTC/USDT', {'bid': 99.99, 'ask': 100.01, 'timestamp': None, '_received_at': real()})
    stepped = lambda: real() - 20                              # Windows time sync steps the clock back 20 s
    monkeypatch.setattr(trader_module().time, 'time', stepped)
    assert not engine._fresh('BTC/USDT')                       # the stored quote's age is now unknown
    bid, _, _ = engine._observe_ticker('BTC/USDT', {'bid': 90., 'ask': 90.01, 'timestamp': None, '_received_at': stepped()})
    assert bid == 90. and engine.last_prices['BTC/USDT'] == 90. and engine._fresh('BTC/USDT')
    engine._protect_positions('BTC/USDT', bid)
    assert engine._active_positions('paper') == []


def test_an_exchange_timestamp_ahead_of_arrival_does_not_make_a_quote_look_younger(engine):
    now = time.time()
    engine._observe_ticker('BTC/USDT', {'bid': 100., 'ask': 100.01, 'timestamp': int((now + 8) * 1000), '_received_at': now - 6})
    assert engine._price_age('BTC/USDT') == pytest.approx(6, abs=.5)


def trader_module():
    import trader
    return trader


def test_a_burst_read_in_one_go_serves_the_newest_quote_for_every_symbol():
    pytest.importorskip('google.protobuf')
    cls = simulated_mexc_class(auto_push=False)
    cls.subscriptions, cls.instances = [], []
    stream = feed.QuoteFeed('mexc', ['BTC/USDT', 'ETH/USDT'], client_factory=lambda: cls({'newUpdates': True}),
                            heartbeat_seconds=30.0, max_backoff=.1, metrics=observability.Metrics(), rng=random.Random(1))
    try:
        stream.start()
        assert wait_for(lambda: len(cls.subscriptions) == 2)
        loop = stream._loop
        loop.call_soon_threadsafe(cls.instances[-1].burst, [('BTCUSDT', 100.), ('ETHUSDT', 50.)])
        assert wait_for(lambda: stream.covers(['BTC/USDT', 'ETH/USDT'], max_age=5))
        # A sharp move: every frame lands in one read; only the first resolves a watch.
        loop.call_soon_threadsafe(cls.instances[-1].burst, [('BTCUSDT', 90.), ('BTCUSDT', 89.), ('ETHUSDT', 45.),
                                                             ('ETHUSDT', 44.)])
        assert wait_for(lambda: stream.ticker('BTC/USDT')['bid'] == 89. and stream.ticker('ETH/USDT')['bid'] == 44.,
                        timeout=3), (stream.ticker('BTC/USDT'), stream.ticker('ETH/USDT'))
    finally:
        stream.stop()


class ScriptedClient:
    """A combined-channel client whose watch returns one symbol while CCXT's cache holds more."""
    has = {'watchBidsAsks': True}
    options = {}

    def __init__(self, script, markets=None):
        self.script, self.calls, self.tickers, self.closed = list(script), [], {}, False
        self.markets = markets

    async def watch_bids_asks(self, symbols):
        self.calls.append(list(symbols))
        if not self.script:
            await asyncio.sleep(3600)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        returned, cached = item
        self.tickers.update(cached)
        return returned

    async def close(self):
        self.closed = True


def quote_dict(bid):
    return {'bid': bid, 'ask': bid + .1, 'timestamp': None}


def test_cache_entries_a_combined_watch_did_not_return_are_ingested():
    cached = {'BTC/USDT': quote_dict(90.), 'ETH/USDT': quote_dict(45.)}
    client = ScriptedClient([({'BTC/USDT': cached['BTC/USDT']}, cached)])
    stream = feed.QuoteFeed('kraken', ['BTC/USDT', 'ETH/USDT'], client_factory=lambda: client, heartbeat_seconds=30.0,
                            max_backoff=.05, metrics=observability.Metrics(), rng=random.Random(1))
    try:
        stream.start()
        assert wait_for(lambda: stream.ticker('ETH/USDT') is not None)
        assert stream.ticker('ETH/USDT')['bid'] == 45. and stream.ticker('BTC/USDT')['bid'] == 90.
    finally:
        stream.stop()


def test_quotes_from_a_closed_connection_are_not_served_after_reconnecting():
    clients = []

    def factory():
        if not clients:
            script = [({'BTC/USDT': quote_dict(100.), 'ETH/USDT': quote_dict(50.)}, {}), ConnectionError('socket closed')]
        else:
            script = [({'BTC/USDT': quote_dict(99.)}, {})]
        clients.append(ScriptedClient(script))
        return clients[-1]
    stream = feed.QuoteFeed('kraken', ['BTC/USDT', 'ETH/USDT'], client_factory=factory, heartbeat_seconds=30.0,
                            max_backoff=.05, metrics=observability.Metrics(), rng=random.Random(1))
    try:
        stream.start()
        assert wait_for(lambda: len(clients) == 2 and stream.ticker('BTC/USDT') is not None
                        and stream.ticker('BTC/USDT')['bid'] == 99.)
        assert stream.ticker('ETH/USDT', max_age=60) is None      # pre-outage quote, though only seconds old
    finally:
        stream.stop()


def test_an_unlisted_watchlist_pair_does_not_keep_the_stream_down():
    markets = {'BTC/USDT': {}, 'ETH/USDT': {}}
    clients = []

    def factory():
        script = [ccxt.BadSymbol('kraken does not have market symbol BONK/USDT')] if not clients else \
            [({'BTC/USDT': quote_dict(100.), 'ETH/USDT': quote_dict(50.)}, {})]
        clients.append(ScriptedClient(script, markets))
        return clients[-1]
    stream = feed.QuoteFeed('kraken', ['BTC/USDT', 'BONK/USDT', 'ETH/USDT'], client_factory=factory,
                            heartbeat_seconds=30.0, max_backoff=.05, metrics=observability.Metrics(), rng=random.Random(1))
    try:
        stream.start()
        assert wait_for(lambda: stream.covers(['BTC/USDT', 'ETH/USDT'], max_age=5), timeout=8)
        assert clients[-1].calls[0] == ['BTC/USDT', 'ETH/USDT'] and stream.status()['unlisted'] == ['BONK/USDT']
        stream.set_symbols(['BTC/USDT', 'BONK/USDT', 'ETH/USDT'])     # the next scan cannot bring it back
        assert stream.symbols == ('BTC/USDT', 'ETH/USDT') and len(clients) == 2
    finally:
        stream.stop()


def test_one_held_mexc_pair_reads_only_its_own_book_ticker():
    from market_data import MarketData
    client = ccxt.mexc({'enableRateLimit': False})
    client.set_markets([mexc_market('BTCUSDT', 'BTC/USDT'), mexc_market('ETHUSDT', 'ETH/USDT')])
    book = {'BTCUSDT': {'symbol': 'BTCUSDT', 'bidPrice': '100.0', 'bidQty': '1', 'askPrice': '100.1', 'askQty': '1'},
            'ETHUSDT': {'symbol': 'ETHUSDT', 'bidPrice': '50.0', 'bidQty': '1', 'askPrice': '50.1', 'askQty': '1'}}
    urls = []

    def fetch(url, method='GET', headers=None, body=None):     # CCXT's HTTP layer; nothing leaves the process
        urls.append(url)
        return book[url.split('symbol=')[1].split('&')[0]] if 'symbol=' in url else list(book.values())
    client.fetch = fetch
    data = MarketData(client)
    one = data.quotes(['BTC/USDT'])
    assert one['BTC/USDT']['bid'] == 100. and urls == ['https://api.mexc.com/api/v3/ticker/bookTicker?symbol=BTCUSDT']
    both = data.quotes(['BTC/USDT', 'ETH/USDT'])
    assert set(both) == {'BTC/USDT', 'ETH/USDT'} and len(urls) == 2


def test_the_engine_streams_only_pairs_the_catalogue_lists(engine):
    engine.cfg['watchlist'] = list(engine.cfg['watchlist']) + ['NOTLISTED/USDT']
    assert engine._catalogue()
    assert 'NOTLISTED/USDT' not in engine._feed_symbols() and 'BTC/USDT' in engine._feed_symbols()


def test_ctrl_c_stops_the_engine_before_uvicorn_drains_connections():
    import signal
    import launcher
    uvicorn = pytest.importorskip('uvicorn')

    class Engine:
        stops = 0

        def stop(self):
            Engine.stops += 1
    lines = []
    server = launcher.engine_first_server(uvicorn.Server, Engine(), lines.append)(uvicorn.Config(app=None))
    server.handle_exit(signal.SIGINT, None)
    assert wait_for(lambda: Engine.stops == 1)
    assert server.should_exit and not server.force_exit and 'stops scanning and placing orders now' in lines[0]
    server.handle_exit(signal.SIGINT, None)              # a second Ctrl+C forces exit and stops nothing twice
    time.sleep(.1)
    assert server.force_exit and Engine.stops == 1


# --- Final review: decision trail --------------------------------------------------------

def test_a_symbol_exited_on_this_candle_is_recorded_as_withheld_not_enter(engine, monkeypatch):
    import trader
    monkeypatch.setattr(trader, 'entry_signal', lambda *a, **k: (True, 'BUY setup', 100))
    assert engine._buy('SHIB/USDT', 200, 100, .04)
    position = next(p for p in engine._active_positions('paper') if p['symbol'] == 'SHIB/USDT')
    assert engine._sell(position, 100, 'test exit')              # writes the durable last_exit record
    rows = {row['symbol']: row for row in engine.scan_once(allow_orders=False)}
    record = next(d for d in engine.decisions.recent('SHIB/USDT', limit=1))
    assert record['action'] == 'abstain' and record['status'] == decision.STATUS_EXITED
    assert rows['SHIB/USDT']['status'] == decision.STATUS_EXITED and not rows['SHIB/USDT']['candidate']


def test_unchanged_abstentions_are_counted_not_rewritten_every_scan(engine, monkeypatch):
    events = capture_events(monkeypatch)
    engine.scan_once(allow_orders=False)
    first = [fields for kind, fields in events if kind == 'decision.entry']
    engine.scan_once(allow_orders=False)
    second = [fields for kind, fields in events if kind == 'decision.entry'][len(first):]
    assert len(first) == 8 and second == []
    assert engine.metrics.snapshot()['counters']['decision.entry_unchanged'] >= 8
    assert len(engine.decisions) == 16                          # the in-memory log keeps every record


def test_enter_decisions_that_do_not_become_orders_say_why(engine, monkeypatch):
    events = capture_events(monkeypatch)
    engine.running = True
    candidates = [(1.0, 2e7, symbol, 100.01, 1.0, 123, 100.0, f'dec_{index}')
                  for index, symbol in enumerate(('BTC/USDT', 'ETH/USDT', 'SOL/USDT', 'DOGE/USDT'))]
    engine._enter_candidates(candidates)
    withheld = {fields['decision_id']: fields['reason'] for kind, fields in events if kind == 'decision.withheld'}
    assert withheld == {'dec_2': 'per_scan_cap', 'dec_3': 'per_scan_cap'}
    assert len(engine._active_positions('paper')) == 2


def test_exit_decisions_from_the_monitor_trail_are_labelled_monitor(engine, monkeypatch):
    events = capture_events(monkeypatch)
    engine._buy('BTC/USDT', 800, 100, .04)
    engine.running = True
    engine.data.candles('BTC/USDT', '1h', limit=175)              # ATR 2: the trail sits 5 under the high
    with storage.connection() as db:                              # levels left by a wider ATR earlier
        db.execute("UPDATE positions SET highest=110, stop=100 WHERE symbol='BTC/USDT'")
    engine.public.price = 104.                                    # above the stored stop, under 110 - 5
    engine._monitor_if_due(force=True)
    exits = [fields for kind, fields in events if kind == 'decision.exit']
    assert exits and exits[-1]['source'] == 'monitor' and exits[-1]['trigger'] == 'trailing'
