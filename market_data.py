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
    def __init__(self, client, *, clock=time.time, monotonic=time.monotonic, wait=None):
        self.client, self.clock, self.monotonic = client, clock, monotonic
        self.lock = threading.RLock()
        self.wait = wait or time.sleep
        self.metrics, self.cache = Counter(), {}
        self.failures, self.open_until = Counter(), 0.0
        self.endpoint_open_until = {}

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

    def read(self, method, *args, **kwargs):
        if not method.startswith(('fetch_', 'load_')):
            raise ValueError('Only exchange reads may be retried')
        transient = tuple(c for name in ('NetworkError','ExchangeNotAvailable','RateLimitExceeded')
                          if isinstance(c := getattr(ccxt,name,None),type))
        throttled = tuple(c for name in ('RateLimitExceeded','DDoSProtection')
                          if isinstance(c := getattr(ccxt,name,None),type))
        with self.lock:
            if self.monotonic() < max(self.open_until, self.endpoint_open_until.get(method,0)):
                self.metrics['circuit_rejections'] += 1
                raise DataUnavailable('Exchange reads cooling down')
            for attempt in range(3):
                try:
                    self.metrics['requests'] += 1
                    result = getattr(self.client,method)(*args,**kwargs)
                    # A cached load_markets success must not erase a failing
                    # ticker endpoint's transport streak.
                    self.failures[method] = 0
                    return result
                except Exception as exc:
                    self.metrics['errors'] += 1
                    if throttled and isinstance(exc,throttled):
                        self.metrics['rate_limit_cooldowns'] += 1
                        self.open_until = self.monotonic() + self._cooldown()
                        raise
                    if not transient or not isinstance(exc,transient):
                        raise
                    if attempt < 2:
                        self.metrics['retries'] += 1
                        if self.wait(.5 * 2**attempt):
                            raise DataUnavailable('Exchange read interrupted') from None
                    else:
                        self.failures[method] += 1
                        if self.failures[method] >= 3:
                            # A broken candle endpoint must not disable quotes
                            # needed to monitor stored protective exits.
                            self.endpoint_open_until[method] = self.monotonic() + 30
                        raise

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

    def cached_candles(self, symbol, timeframe, limit=175):
        """Return a fresh immutable cache snapshot without making a request."""
        with self.lock:
            saved = self.cache.get((symbol, timeframe, limit))
            stamp = self.clock()
            if saved and saved[2] <= stamp < saved[0] and self.monotonic() < saved[3]:
                return saved[1].copy()
            return None

    def candles(self,symbol,timeframe,limit=175,*,fresh=False):
        if timeframe not in SECONDS:
            raise ValueError('Unsupported timeframe')
        key = (symbol,timeframe,limit)
        with self.lock:
            saved = self.cache.get(key)
            stamp = self.clock()
            if (not fresh and saved and saved[2] <= stamp < saved[0]
                    and self.monotonic() < saved[3]):
                self.metrics['candle_cache_hits'] += 1
                return saved[1].copy()
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
            self.cache[key] = (expires,result,stamp,self.monotonic()+expires-stamp)
            if len(self.cache)>160:
                self.cache.pop(next(iter(self.cache)))
            return result.copy()
