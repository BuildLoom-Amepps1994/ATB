"""Streaming best bid/ask quotes over the exchange websocket, with continuity checks.

Off by default (``streaming_quotes`` setting). REST polling stays in place as
the fallback and as the source of anything the stream has not refreshed, so a
silent or lagging stream can only make the engine fall back, never trade on
an old price: every quote carries the exchange timestamp when the exchange
sends one, the local receipt time always, and :meth:`QuoteFeed.ticker`
refuses to hand out a quote older than the caller's limit.

Continuity checks
-----------------
* **Heartbeat**: no message for ``heartbeat_seconds`` marks the feed stalled
  and reconnects it; until the first message after a reconnect the feed is
  not ``healthy`` and callers use REST.
* **Monotonic timestamps**: a quote whose exchange timestamp is older than
  the one already held for that symbol is dropped and counted.
* **Backpressure**: CCXT Pro resolves ``watch_bids_asks`` with the newest
  update per symbol, so a slow consumer sees the latest quote rather than a
  growing queue; the feed keeps exactly one quote per symbol and counts how
  many it overwrote without anyone reading them.
* **Reconnects** back off exponentially with jitter up to ``max_backoff``.

The feed never touches credentials, orders or balances: it builds a public
CCXT Pro client and subscribes to top-of-book channels only.
"""
from __future__ import annotations

import asyncio
import math
import random
import threading
import time
from typing import Callable

import observability


def _finite_positive(value) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    return number if math.isfinite(number) and number > 0 and not isinstance(value, bool) else 0.0


class FeedStalled(RuntimeError):
    """No message arrived within the heartbeat window."""


class Resubscribe(RuntimeError):
    """The wanted symbol set changed; reconnect with the new subscription."""


def default_client_factory(exchange_name: str):
    """A public CCXT Pro client; imported lazily so REST-only runs never load it."""
    import ccxt.pro
    if exchange_name not in ('mexc', 'kraken'):
        raise ValueError('Supported exchanges: MEXC, Kraken')
    options = {'enableRateLimit': True, 'newUpdates': True, 'timeout': 18000,
               'options': {'defaultType': 'spot'}}
    if exchange_name == 'mexc':
        options['options'].update({'adjustForTimeDifference': True})
    return getattr(ccxt.pro, exchange_name)(options)


class QuoteFeed:
    STATES = ('idle', 'connecting', 'live', 'stalled', 'reconnecting', 'stopped', 'failed')

    def __init__(self, exchange_name: str, symbols, *, client_factory: Callable | None = None,
                 clock: Callable[[], float] = time.time, heartbeat_seconds: float = 20.0,
                 max_backoff: float = 60.0, metrics=None, rng: random.Random | None = None):
        self.exchange_name = exchange_name
        self._client_factory = client_factory or (lambda: default_client_factory(exchange_name))
        self.clock = clock
        self.heartbeat_seconds = float(heartbeat_seconds)
        self.max_backoff = float(max_backoff)
        self.metrics = metrics if metrics is not None else observability.METRICS
        self._rng = rng or random.Random()
        self.lock = threading.Lock()
        self.symbols: tuple[str, ...] = tuple(dict.fromkeys(symbols))
        self.quotes: dict[str, dict] = {}
        self.counters = {'messages': 0, 'quotes': 0, 'out_of_order': 0, 'coalesced': 0, 'rejected': 0,
                         'reconnects': 0, 'stalls': 0}
        self.state = 'idle'
        self.last_message_at: float | None = None
        self.connected_at: float | None = None
        self.last_error: str | None = None
        self.backoff = 1.0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task | None = None
        self._wanted_changed = False
        self._changed: asyncio.Event | None = None
        self._sequence = 0

    # ---------------------------------------------------------------- control
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name='QuoteFeed')
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        loop, task = self._loop, self._task
        if loop is not None and task is not None and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(task.cancel)
            except RuntimeError:
                pass
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=timeout)
        with self.lock:
            self.state = 'stopped'

    def set_symbols(self, symbols) -> None:
        wanted = tuple(dict.fromkeys(symbols))
        with self.lock:
            if wanted == self.symbols:
                return
            self.symbols = wanted
            self._wanted_changed = True
            for symbol in list(self.quotes):
                if symbol not in wanted:
                    self.quotes.pop(symbol, None)
            loop, changed = self._loop, self._changed
        if loop is not None and changed is not None and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(changed.set)   # wake a blocked watch
            except RuntimeError:
                pass

    # ----------------------------------------------------------------- reads
    def healthy(self) -> bool:
        with self.lock:
            return (self.state == 'live' and self.last_message_at is not None
                    and self.clock() - self.last_message_at <= self.heartbeat_seconds)

    def ticker(self, symbol: str, *, max_age: float | None = None) -> dict | None:
        """A CCXT-shaped top-of-book quote, or None when the stream cannot vouch for one.

        ``timestamp`` is the exchange's (ms) when it sent one, ``_received_at``
        the local receipt time, ``quoteVolume`` absent: the book channel does
        not carry turnover, so callers keep REST for liquidity checks.
        """
        if not self.healthy():
            return None
        with self.lock:
            quote = self.quotes.get(symbol)
            if quote is None:
                return None
            age = self.clock() - quote['received_at']
            if max_age is not None and not (0 <= age <= max_age):
                return None
            quote['read'] = True
            return {'symbol': symbol, 'bid': quote['bid'], 'ask': quote['ask'], 'timestamp': quote['timestamp'],
                    '_received_at': quote['received_at'], '_stream_sequence': quote['sequence'], '_source': 'stream'}

    def covers(self, symbols, *, max_age: float) -> bool:
        """True when every symbol has a stream quote no older than ``max_age``."""
        return bool(symbols) and all(self.ticker(symbol, max_age=max_age) is not None for symbol in symbols)

    def status(self) -> dict:
        with self.lock:
            now = self.clock()
            return {'state': self.state, 'exchange': self.exchange_name, 'symbols': len(self.symbols),
                    'quoted': len(self.quotes), 'last_message_age_s': (round(now - self.last_message_at, 1)
                                                                       if self.last_message_at is not None else None),
                    'connected_at': self.connected_at, 'last_error': self.last_error, 'backoff_s': self.backoff,
                    'heartbeat_seconds': self.heartbeat_seconds, **self.counters}

    # ------------------------------------------------------------- internals
    def _set_state(self, state: str, error: str | None = None) -> None:
        with self.lock:
            previous, self.state = self.state, state
            if error is not None:
                self.last_error = error
        if previous != state:
            observability.event('feed.state', exchange=self.exchange_name, state=state, previous=previous, error=error)

    def _run(self) -> None:
        try:
            asyncio.run(self._main())
        except asyncio.CancelledError:
            pass
        except Exception as exc:                      # pragma: no cover - defensive
            self._set_state('failed', f'{type(exc).__name__}: {exc}')
        finally:
            self._loop = self._task = None
            if self.state != 'failed':
                self._set_state('stopped')

    async def _main(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._task = asyncio.current_task()
        with self.lock:
            self._changed = asyncio.Event()
        while not self._stop.is_set():
            client = None
            try:
                self._set_state('connecting')
                client = self._client_factory()
                await self._stream(client)
            except asyncio.CancelledError:
                break
            except Resubscribe:
                self.backoff = 1.0
            except Exception as exc:
                self.counters['reconnects'] += 1
                self.metrics.incr('feed.reconnects')
                stalled = isinstance(exc, (FeedStalled, asyncio.TimeoutError))
                if stalled:
                    self.counters['stalls'] += 1
                self._set_state('stalled' if stalled else 'reconnecting', f'{type(exc).__name__}: {exc}'[:200])
            finally:
                await self._close(client)
            if self._stop.is_set():
                break
            delay = min(self.max_backoff, self.backoff) * (0.5 + self._rng.random())
            self.backoff = min(self.max_backoff, self.backoff * 2)
            self.metrics.observe('feed.reconnect_delay_ms', delay * 1000)
            try:
                await self._sleep(delay)
            except asyncio.CancelledError:
                break

    async def _close(self, client) -> None:
        if client is None:
            return
        try:
            result = client.close()
            if asyncio.iscoroutine(result):
                await asyncio.wait_for(result, timeout=5)
        except Exception:
            pass

    async def _sleep(self, seconds: float) -> None:
        deadline = time.monotonic() + seconds
        while not self._stop.is_set() and time.monotonic() < deadline:
            await asyncio.sleep(min(0.2, max(0.0, deadline - time.monotonic())))

    def _watch_method(self, client) -> str:
        has = getattr(client, 'has', {}) or {}
        if has.get('watchBidsAsks'):
            return 'watch_bids_asks'
        if has.get('watchTickers'):
            return 'watch_tickers'
        raise RuntimeError(f'{self.exchange_name} streams neither bid/ask nor tickers')

    async def _stream(self, client) -> None:
        method = self._watch_method(client)
        with self.lock:
            symbols, self._wanted_changed = list(self.symbols), False
            changed = self._changed
            changed.clear()
        if not symbols:
            await self._sleep(1.0)
            raise Resubscribe('no symbols to watch')
        first = True
        while not self._stop.is_set():
            updates = await self._next_update(client, method, symbols, changed)
            received = self.clock()
            if first:
                first = False
                self.backoff = 1.0
                with self.lock:
                    self.connected_at = received
                observability.event('feed.connected', exchange=self.exchange_name, symbols=len(symbols), method=method)
            self._ingest(updates, received, client)
            self._set_state('live')
            with self.lock:
                wanted_changed = self._wanted_changed
            if wanted_changed:
                raise Resubscribe('symbol set changed')

    async def _next_update(self, client, method: str, symbols: list[str], changed: asyncio.Event):
        """The next message, a stall after the heartbeat window, or a wake-up to resubscribe."""
        watch = asyncio.ensure_future(getattr(client, method)(symbols))
        waiter = asyncio.ensure_future(changed.wait())
        try:
            done, _ = await asyncio.wait({watch, waiter}, timeout=self.heartbeat_seconds,
                                         return_when=asyncio.FIRST_COMPLETED)
            if watch in done:
                return watch.result()
            if waiter in done:
                raise Resubscribe('symbol set changed')
            raise FeedStalled(f'no message for {self.heartbeat_seconds:g}s')
        finally:
            for future in (watch, waiter):
                if not future.done():
                    future.cancel()

    def _ingest(self, updates, received: float, client=None) -> None:
        if not isinstance(updates, dict):
            return
        offset = 0.0
        options = getattr(client, 'options', None)
        difference = options.get('timeDifference') if isinstance(options, dict) else None
        if isinstance(difference, (int, float)) and not isinstance(difference, bool) and math.isfinite(difference):
            offset = difference / 1000
        accepted = 0
        for symbol, ticker in updates.items():
            if not isinstance(ticker, dict):
                continue
            bid, ask = _finite_positive(ticker.get('bid')), _finite_positive(ticker.get('ask'))
            if not bid or not ask or bid > ask:
                self.counters['rejected'] += 1
                continue
            raw_ts = ticker.get('timestamp')
            exchange_ts = _finite_positive(raw_ts) / 1000 if raw_ts is not None else None
            with self.lock:
                if symbol not in self.symbols:
                    continue
                previous = self.quotes.get(symbol)
                if (previous is not None and exchange_ts is not None and previous['exchange_ts'] is not None
                        and exchange_ts < previous['exchange_ts']):
                    self.counters['out_of_order'] += 1
                    self.metrics.incr('feed.out_of_order')
                    continue
                if previous is not None:
                    self.metrics.observe('feed.update_gap_ms', max(0.0, received - previous['received_at']) * 1000)
                    if not previous.get('read'):
                        self.counters['coalesced'] += 1
                self._sequence += 1
                self.quotes[symbol] = {'bid': bid, 'ask': ask, 'timestamp': int(raw_ts) if exchange_ts else None,
                                       'exchange_ts': exchange_ts, 'received_at': received, 'sequence': self._sequence,
                                       'read': False}
                self.counters['quotes'] += 1
                accepted += 1
            if exchange_ts is not None:
                self.metrics.observe('feed.exchange_to_receipt_ms', max(0.0, received - (exchange_ts + offset)) * 1000)
        with self.lock:
            self.counters['messages'] += 1
            self.last_message_at = received
        self.metrics.incr('feed.messages')
        if accepted:
            self.metrics.incr('feed.quotes', accepted)
