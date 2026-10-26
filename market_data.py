"""Serialized public reads with bounded retries and completed-candle caching.

Order submission must never pass through this retry mechanism.
"""
from __future__ import annotations
import threading
import time
import math
from email.utils import parsedate_to_datetime
from collections import Counter
import ccxt
from strategy import frame_from_ohlcv, indicators

SECONDS = {'1m':60, '5m':300, '1h':3600, '4h':14400, '1d':86400, '1w':604800}


class DataUnavailable(ValueError):
    pass


class MarketData:
    """Public reads. Serialized by default; ``concurrency`` above one lets
    candle downloads overlap their network round trips (low-rate-limit
    exchanges only). Request starts stay paced by the exchange rate limit,
    and quote reads keep a reserved slot and go first in that pacing queue,
    so protective price checks never wait behind queued candle downloads.
    """

    PRIORITY_METHODS = ('fetch_ticker', 'fetch_tickers', 'fetch_bids_asks', 'fetch_order_book')

    def __init__(self, client, *, clock=time.time, monotonic=time.monotonic, wait=None,
                 concurrency: int = 1):
        self.client, self.clock, self.monotonic = client, clock, monotonic
        self.lock = threading.RLock()
        self.wait = wait or time.sleep
        self.metrics, self.cache = Counter(), {}
        self.failures, self.open_until = Counter(), 0.0
        self.endpoint_open_until = {}
        self.concurrency = max(1, int(concurrency))
        self._inflight: dict = {}
        self._prefetch_errors: dict = {}
        if self.concurrency == 1:
            self._bulk_slots = self._quote_slots = threading.Lock()
        else:
            self._bulk_slots = threading.BoundedSemaphore(self.concurrency - 1)
            self._quote_slots = threading.BoundedSemaphore(1)
            self._install_pacing()

    def _install_pacing(self):
        """Make CCXT's per-request throttle safe to call from several threads."""
        original = getattr(self.client, 'throttle', None)
        if not callable(original):
            return
        condition, state, local = threading.Condition(), {'busy': False, 'urgent': 0}, threading.local()
        self._priority = local

        def throttle(cost=None):
            urgent = getattr(local, 'urgent', False)
            with condition:
                state['urgent'] += urgent
                try:
                    while state['busy'] or (not urgent and state['urgent']):
                        condition.wait()
                    state['busy'] = True
                finally:
                    state['urgent'] -= urgent
            try:
                original(cost)
                # Record the start before releasing so the next caller paces from it.
                if hasattr(self.client, 'milliseconds'):
                    self.client.lastRestRequestTimestamp = self.client.milliseconds()
            finally:
                with condition:
                    state['busy'] = False
                    condition.notify_all()
        self.client.throttle = throttle

    def _cooldown(self):
        headers = getattr(self.client, 'last_response_headers', None) or {}
        value = next((v for k,v in headers.items() if k.lower()=='retry-after'), None)
        delay = 30.0
        if value is not None:
            try:
                delay = float(value)
            except (ValueError, TypeError):
                try:
                    delay = parsedate_to_datetime(str(value)).timestamp() - self.clock()
                except (ValueError, TypeError, OverflowError):
                    delay = 30.0
        if not math.isfinite(delay):
            delay = 30.0
        return max(30.0, min(300.0, delay))

    def _call(self, method, args, kwargs):
        urgent = method in self.PRIORITY_METHODS
        slots = self._quote_slots if urgent else self._bulk_slots
        with slots:
            priority = getattr(self, '_priority', None)
            if priority is not None:
                priority.urgent = urgent
            return getattr(self.client, method)(*args, **kwargs)

    def read(self, method, *args, attempts: int = 3, **kwargs):
        if not method.startswith(('fetch_', 'load_')):
            raise ValueError('Only exchange reads may be retried')
        attempts = max(1, int(attempts))
        transient = tuple(c for name in ('NetworkError','ExchangeNotAvailable','RateLimitExceeded')
                          if isinstance(c := getattr(ccxt,name,None),type))
        throttled = tuple(c for name in ('RateLimitExceeded','DDoSProtection')
                          if isinstance(c := getattr(ccxt,name,None),type))
        for attempt in range(attempts):
            # Breaker state is shared; the network call itself runs unlocked
            # inside its request slot.
            with self.lock:
                if self.monotonic() < max(self.open_until, self.endpoint_open_until.get(method,0)):
                    self.metrics['circuit_rejections'] += 1
                    raise DataUnavailable('Exchange reads cooling down')
                self.metrics['requests'] += 1
            try:
                result = self._call(method, args, kwargs)
            except Exception as exc:
                with self.lock:
                    self.metrics['errors'] += 1
                    if throttled and isinstance(exc,throttled):
                        self.metrics['rate_limit_cooldowns'] += 1
                        self.open_until = self.monotonic() + self._cooldown()
                        raise
                    if not transient or not isinstance(exc,transient):
                        raise
                    if attempt == attempts - 1:
                        self.failures[method] += 1
                        if self.failures[method] >= 3:
                            # A broken candle endpoint must not disable quotes
                            # needed to monitor stored protective exits.
                            self.endpoint_open_until[method] = self.monotonic() + 30
                        raise
                    self.metrics['retries'] += 1
                if self.wait(.5 * 2**attempt):
                    raise DataUnavailable('Exchange read interrupted') from None
                continue
            with self.lock:
                # A cached load_markets success must not erase a failing
                # ticker endpoint's transport streak.
                self.failures[method] = 0
            return result

    def markets(self):
        return self.read('load_markets')

    def _clock_offset(self):
        """Local-minus-exchange seconds, as CCXT measured it (MEXC); else zero."""
        options = getattr(self.client, 'options', None)
        difference = options.get('timeDifference') if isinstance(options, dict) else None
        if isinstance(difference, bool) or not isinstance(difference, (int, float)) or not math.isfinite(difference):
            return 0.0
        return difference / 1000

    def _received(self, ticker, received):
        # Exchanges without ticker timestamps are aged from the response time.
        if isinstance(ticker, dict):
            ticker['_received_at'] = received
        return ticker

    def ticker(self,symbol):
        result = self.read('fetch_ticker',symbol)
        return self._received(result, self.clock())

    def order_book(self, symbol, limit=20):
        """A public depth snapshot (MEXC REST depth weight 3 at limit<=100), stamped on receipt.

        Validation is the caller's (``micro_book.from_ccxt_order_book``): this
        only reads, so a malformed book is refused there, never repaired here.
        """
        result = self.read('fetch_order_book', symbol, int(limit))
        return self._received(result, self.clock())

    def tickers(self,symbols):
        if getattr(self.client,'has',{}).get('fetchTickers') is True:
            result = self.read('fetch_tickers',symbols)
            if not isinstance(result,dict):
                return {}
            received = self.clock()
            for ticker in result.values():
                self._received(ticker, received)
            return result
        return {}

    def quotes(self, symbols):
        """Best bid/ask for protective checks, from the lighter endpoint where one exists.

        MEXC's book-ticker endpoint costs 10 rate-limit units against 25 for
        the 24h ticker and returns a far smaller body; it carries no turnover,
        which protective checks do not need. A single MEXC pair is requested
        by name; several pairs read the full book-ticker list once, still
        lighter than the full 24h list. Exchanges without it (Kraken) use the
        ticker read. Tickers without timestamps age from receipt.
        """
        if getattr(self.client,'has',{}).get('fetchBidsAsks') is True:
            symbols = list(symbols)
            if len(symbols) == 1 and getattr(self.client, 'id', None) == 'mexc':
                # CCXT 4.5 sends MEXC no symbol filter: a one-symbol read would
                # download the whole book-ticker list, parse it as one entry and
                # return nothing, forcing a second, throttled ticker read. Name
                # the market so the endpoint returns just that pair.
                try:
                    market_id = self.client.market(symbols[0])['id']
                except Exception:
                    return self.tickers(symbols)
                result = self.read('fetch_bids_asks', symbols, {'symbol': market_id})
            else:
                result = self.read('fetch_bids_asks',symbols)
            if not isinstance(result,dict):
                return {}
            received = self.clock()
            for ticker in result.values():
                self._received(ticker, received)
            return result
        return self.tickers(symbols)

    def cached_candles(self, symbol, timeframe, limit=175):
        """Return a fresh immutable cache snapshot without making a request."""
        with self.lock:
            saved = self.cache.get((symbol, timeframe, limit))
            stamp = self.clock()
            if saved and saved[2] <= stamp < saved[0] and self.monotonic() < saved[3]:
                return saved[1].copy()
            return None

    def candles(self,symbol,timeframe,limit=175,*,fresh=False,background=False):
        if timeframe not in SECONDS:
            raise ValueError('Unsupported timeframe')
        key = (symbol,timeframe,limit)
        while True:
            with self.lock:
                saved = self.cache.get(key)
                stamp = self.clock()
                if (not fresh and saved and saved[2] <= stamp < saved[0]
                        and self.monotonic() < saved[3]):
                    self.metrics['candle_cache_hits'] += 1
                    return saved[1].copy()
                failed = self._prefetch_errors.pop(key, None)
                if not fresh and failed and self.monotonic() < failed[1]:
                    # A background download already failed this scan; do not
                    # count the same outage twice against the breaker.
                    raise failed[0]
                pending = self._inflight.get(key)
                if pending is None:
                    pending = self._inflight[key] = threading.Event()
                    break
                if background:
                    return None  # Already being downloaded in the foreground.
            # Another thread is downloading this frame; reuse its result.
            pending.wait()
            fresh = False
        try:
            return self._download(key, stamp)
        except Exception as exc:
            if background:
                with self.lock:
                    self._prefetch_errors[key] = (exc, self.monotonic() + 60)
            raise
        finally:
            with self.lock:
                self._inflight.pop(key, None)
            pending.set()

    def _download(self, key, stamp):
        symbol, timeframe, limit = key
        # Candle times are exchange time. The completion cut-off is taken
        # before the request: a candle that closed while the response was
        # in flight was still open when the exchange assembled it.
        offset, duration = self._clock_offset(), SECONDS[timeframe]
        cutoff = stamp - offset
        raw = self.read('fetch_ohlcv',symbol,timeframe,limit=limit)
        stamp = self.clock()
        frame = frame_from_ohlcv(raw,timeframe=timeframe,now_ms=int(cutoff*1000))
        if frame.empty:
            raise DataUnavailable('No completed candles')
        latest = float(frame.iloc[-1]['timestamp'])/1000
        if stamp - offset - latest - duration > duration + 90:
            raise DataUnavailable('Historical candles are stale')
        if len(frame)>1 and not frame['timestamp'].diff().iloc[1:].eq(duration*1000).all():
            raise DataUnavailable('Historical candles contain missing periods')
        result = indicators(frame)
        expires = max(stamp+1,latest+offset+2*duration+2)
        with self.lock:
            self.cache[key] = (expires,result,stamp,self.monotonic()+expires-stamp)
            if len(self.cache)>160:
                self.cache.pop(next(iter(self.cache)))
        return result.copy()

    def prefetch(self, requests):
        """Download uncached candle frames in the background, ``concurrency``-1 at a time.

        Returns at once. Results land in the cache; ``candles`` waits for a
        download in flight and re-raises a failure once instead of repeating it.
        """
        if self.concurrency < 2:
            return []
        work = list(dict.fromkeys(requests))
        lock = threading.Lock()

        def drain():
            while True:
                with lock:
                    if not work:
                        return
                    symbol, timeframe, limit = work.pop(0)
                if self.cached_candles(symbol, timeframe, limit) is not None:
                    continue
                try:
                    self.candles(symbol, timeframe, limit, background=True)
                except Exception:
                    pass  # Recorded for the scan, which reports it per symbol.
        workers = [threading.Thread(target=drain, daemon=True, name='CandlePrefetch')
                   for _ in range(min(self.concurrency - 1, len(work)))]
        for worker in workers:
            worker.start()
        return workers
