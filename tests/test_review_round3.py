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


def simulated_mexc_class():
    import ccxt.pro

    class SimulatedMexc(ccxt.pro.mexc):
        """The real CCXT MEXC client; only the socket is simulated. Each subscription
        request starts a stream of protobuf book-ticker pushes for that market, fed
        through CCXT's handle_message exactly as received frames are."""
        subscriptions: list = []

        async def load_markets(self, reload=False, params={}):
            if not self.markets:
                self.set_markets([mexc_market('BTCUSDT', 'BTC/USDT'), mexc_market('ETHUSDT', 'ETH/USDT')])
            return self.markets

        async def watch(self, url, message_hash, message=None, subscribe_hash=None, subscription=None):
            client = self.client(url)
            if subscribe_hash not in SimulatedMexc.subscriptions:
                SimulatedMexc.subscriptions.append(subscribe_hash)
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
