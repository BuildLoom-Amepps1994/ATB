"""Deterministic synthetic markets for the micro-trading mode: tests and the research harness.

Every series here is generated from a seed and a formula, never downloaded.
They exist to exercise mechanics (a full buy, hold, sell and rebuy through the
real engine, an abstaining market, a falling market, partial depth) and to
give the research harness something to run while real exchange data cannot be
collected from this environment. A result on these series says the code does
what it claims; it says nothing about any real market.

``FixtureExchange`` is a public-data stand-in with the shape the engine reads
(CCXT method names, MEXC-style market metadata, ``precisionMode`` 4 =
TICK_SIZE). It has no order methods at all: an attempt to send an order to the
public client raises, so a test that reaches it fails loudly. Time comes from
a :class:`FixtureClock`, which the tests also install as ``time.time`` so the
engine, the market data cache and the fixture agree on "now" while the clock
jumps a minute per step.
"""
from __future__ import annotations

import math
import random
import time as _time
from dataclasses import dataclass, field

MINUTE = 60
# The real clocks, read at import: a clock installed later must not chain onto an earlier fixture clock.
_REAL_TIME, _REAL_MONOTONIC = _time.time, _time.monotonic


class FixtureClock:
    """Wall clock = the real start time plus an offset the test advances."""

    def __init__(self, start: float | None = None):
        real = _REAL_TIME()
        # Start a few seconds after a minute boundary so "the last completed bar" is unambiguous.
        base = start if start is not None else (math.floor(real / MINUTE) * MINUTE + 5.0)
        self._shift = self._shift0 = base - real
        self._real = _REAL_TIME
        self._mono = _REAL_MONOTONIC

    def time(self) -> float:
        return self._real() + self._shift

    def monotonic(self) -> float:
        """A monotonic reading that jumps with :meth:`advance`, so cache ages follow the fixture time."""
        return self._mono() + self._shift - self._shift0

    def advance(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError('a fixture clock only moves forward')
        self._shift += float(seconds)

    def minute(self) -> int:
        return int(self.time() // MINUTE)


CLOCKED_MODULES = ('storage', 'risk', 'trader', 'micro', 'micro_venue', 'observability')


def install_clock(clock: FixtureClock, setattr_) -> None:
    """Make every clock the engine reads follow ``clock``.

    ``setattr_`` is ``monkeypatch.setattr`` in tests (undone afterwards) or
    plain ``setattr`` in a throwaway research process. Wall time, monotonic
    time and the ``datetime.now`` of the engine modules all move together, so
    a one-minute rate limit or a cooldown measures fixture minutes, not the
    few real milliseconds a simulated minute takes. A second install replaces
    the first everywhere, including a module's earlier fixture ``datetime``.
    """
    import importlib
    from datetime import datetime as _datetime

    class FixtureDatetime(_datetime):
        fixture_clock = clock

        @classmethod
        def now(cls, tz=None):
            return _datetime.fromtimestamp(clock.time(), tz)

        @classmethod
        def utcnow(cls):
            raise RuntimeError('naive UTC time is not used by the engine')

    setattr_(_time, 'time', clock.time)
    setattr_(_time, 'monotonic', clock.monotonic)
    for name in CLOCKED_MODULES:
        module = importlib.import_module(name)
        current = getattr(module, 'datetime', None)
        if current is _datetime or getattr(current, 'fixture_clock', None) is not None:
            setattr_(module, 'datetime', FixtureDatetime)


def mexc_market(symbol: str, *, amount_step=0.0001, price_tick=0.01, min_amount=0.0001, market_min='1',
                limit_min=5.0, taker=0.0005, maker=0.0, status='1', **info) -> dict:
    """A MEXC-shaped CCXT spot market (TICK_SIZE precision, market-order minimum in info)."""
    base, quote = symbol.split('/')
    raw = {'status': status, 'orderTypes': ['LIMIT', 'MARKET', 'LIMIT_MAKER'], 'isSpotTradingAllowed': True,
           'quoteAmountPrecision': str(limit_min), 'baseSizePrecision': str(amount_step),
           'quoteOrderQtyMarketAllowed': True, 'maxQuoteAmountMarket': '100000'}
    if market_min is not None:
        raw['quoteAmountPrecisionMarket'] = str(market_min)
    raw.update(info)
    return {'id': base + quote, 'symbol': symbol, 'base': base, 'quote': quote, 'type': 'spot', 'spot': True,
            'active': status == '1', 'contract': False, 'swap': False, 'future': False, 'option': False,
            'taker': taker, 'maker': maker, 'precision': {'amount': amount_step, 'price': price_tick},
            'limits': {'amount': {'min': min_amount, 'max': None}, 'cost': {'min': limit_min, 'max': 2000000.0}},
            'info': raw}


@dataclass
class SeriesMarket:
    """One pair: a close per minute (index = epoch minute - origin) and a book model around it."""
    symbol: str
    origin_minute: int
    closes: list[float]
    spread_bps: float = 2.0
    level_quote: float = 400.0        # quote notional per book level
    levels: int = 20
    tick: float = 0.01
    volume: float = 50_000.0
    market: dict = field(default_factory=dict)
    rows: list | None = None          # recorded OHLCV rows (real data); closes are then their closes
    venue: str = 'mexc'               # whose rules and book format the market follows

    @classmethod
    def from_rows(cls, symbol: str, rows: list, **kwargs) -> 'SeriesMarket':
        rows = sorted(rows, key=lambda r: r[0])
        return cls(symbol, int(rows[0][0] // 60000), [float(r[4]) for r in rows], rows=[list(r) for r in rows], **kwargs)

    def index(self, minute: int) -> int:
        return max(0, min(len(self.closes) - 1, minute - self.origin_minute))

    def price(self, minute: int) -> float:
        """The executable mid while ``minute`` is forming: the previous bar's close."""
        return self.closes[self.index(minute - 1)]

    def bar(self, minute: int) -> list[float]:
        i = self.index(minute)
        if self.rows is not None:
            return list(self.rows[i])
        close = self.closes[i]
        opened = self.closes[i - 1] if i > 0 else close
        wiggle = abs(close - opened) * 0.25 + close * 0.0002
        high, low = max(opened, close) + wiggle, min(opened, close) - wiggle
        return [minute * MINUTE * 1000, opened, high, low, close, self.volume]

    def book(self, minute: int, *, now_ms: int, sequence: int, depth_scale: float = 1.0) -> dict:
        return self.book_at(self.price(minute), now_ms=now_ms, sequence=sequence, depth_scale=depth_scale)

    def book_at(self, mid: float, *, now_ms: int, sequence: int, depth_scale: float = 1.0,
                spread_scale: float = 1.0) -> dict:
        """A CCXT-shaped book around ``mid``: ``levels`` ticks per side, ``level_quote`` notional each."""
        half = max(self.tick, mid * self.spread_bps * spread_scale / 20000.0)
        # Count whole ticks first (rounded, so float error cannot drop one), then price them.
        bid_ticks = math.floor(round((mid - half) / self.tick, 6))
        ask_ticks = max(math.ceil(round((mid + half) / self.tick, 6)), bid_ticks + 1)
        if bid_ticks - self.levels < 0:
            raise ValueError(f'{self.symbol}: a {self.tick} tick cannot model a book around {mid}')
        places = max(10, 2 - math.floor(math.log10(self.tick)))
        bids, asks = [], []
        for level in range(self.levels):
            bid = round((bid_ticks - level) * self.tick, places)
            ask = round((ask_ticks + level) * self.tick, places)
            bids.append([bid, round(self.level_quote * depth_scale / bid, 6)])
            asks.append([ask, round(self.level_quote * depth_scale / ask, 6)])
        return {'symbol': self.symbol, 'bids': bids, 'asks': asks, 'timestamp': now_ms, 'datetime': None,
                'nonce': sequence}


class FixtureExchange:
    """Public-data stand-in. It has no order methods: the public client must never trade."""
    id = 'mexc'
    precisionMode = 4
    rateLimit = 50
    has = {'fetchTickers': False, 'fetchBidsAsks': True, 'fetchOrderBook': True, 'fetchOHLCV': True}

    def __init__(self, clock: FixtureClock, markets: list[SeriesMarket]):
        self.clock = clock
        self.series = {m.symbol: m for m in markets}
        self.markets = {m.symbol: (m.market or mexc_market(m.symbol)) for m in markets}
        self.options = {}
        self.sequence = 0
        self.calls: list[tuple] = []
        self.fail_books: set[str] = set()
        self.depth_scale: dict[str, float] = {}
        self.candle_delay_s = 0.0

    def load_markets(self, reload: bool = False):
        self.calls.append(('load_markets',))
        return self.markets

    def market(self, symbol):
        return self.markets[symbol]

    def fetch_order_book(self, symbol, limit=None, params=None):
        self.calls.append(('fetch_order_book', symbol))
        if symbol in self.fail_books:
            import ccxt
            raise ccxt.NetworkError('fixture: order book unavailable')
        self.sequence += 1
        now = self.clock.time()
        book = self.series[symbol].book(self.clock.minute(), now_ms=int(now * 1000), sequence=self.sequence,
                                        depth_scale=self.depth_scale.get(symbol, 1.0))
        if limit:
            book['bids'], book['asks'] = book['bids'][:limit], book['asks'][:limit]
        return book

    def fetch_ticker(self, symbol, params=None):
        self.calls.append(('fetch_ticker', symbol))
        book = self.series[symbol].book(self.clock.minute(), now_ms=int(self.clock.time() * 1000), sequence=0)
        bid, ask = book['bids'][0], book['asks'][0]
        return {'symbol': symbol, 'bid': bid[0], 'ask': ask[0], 'bidVolume': bid[1], 'askVolume': ask[1],
                'last': (bid[0] + ask[0]) / 2, 'timestamp': int(self.clock.time() * 1000), 'quoteVolume': 5e7}

    def fetch_bids_asks(self, symbols=None, params=None):
        """MEXC's book ticker: best bid/ask with their quantities (the lighter protective read)."""
        self.calls.append(('fetch_bids_asks', tuple(symbols or ())))
        return {symbol: self.fetch_ticker(symbol) for symbol in (symbols or self.series)}

    def fetch_ohlcv(self, symbol, timeframe='1m', since=None, limit=120, params=None):
        self.calls.append(('fetch_ohlcv', symbol, timeframe))
        if self.candle_delay_s:
            _time.sleep(self.candle_delay_s)
        series = self.series[symbol]
        current = self.clock.minute()
        if timeframe == '1m':
            return [series.bar(minute) for minute in range(current - limit + 1, current + 1)]
        if timeframe == '5m':
            current5 = current - current % 5
            rows = []
            for start in range(current5 - 5 * (limit - 1), current5 + 1, 5):
                bars = [series.bar(minute) for minute in range(start, start + 5) if minute <= current]
                rows.append([start * MINUTE * 1000, bars[0][1], max(b[2] for b in bars), min(b[3] for b in bars),
                             bars[-1][4], sum(b[5] for b in bars)])
            return rows
        raise ValueError('fixture supports 1m and 5m only')


# ---------------------------------------------------------------- series
def oscillating(minutes: int, *, base: float, amplitude_bps: float, period: int, noise_bps: float = 0.0,
                seed: int = 1) -> list[float]:
    """A range: a sine around ``base`` (no drift) plus small seeded noise."""
    rng = random.Random(seed)
    return [base * (1 + amplitude_bps / 1e4 * math.sin(2 * math.pi * i / period) + rng.gauss(0, noise_bps / 1e4))
            for i in range(minutes)]


def random_walk(minutes: int, *, base: float, vol_bps: float, drift_bps: float = 0.0, seed: int = 7) -> list[float]:
    """Geometric random walk with per-minute volatility ``vol_bps`` (no edge by construction)."""
    rng = random.Random(seed)
    price, out = base, []
    for _ in range(minutes):
        price *= math.exp(rng.gauss(drift_bps / 1e4, vol_bps / 1e4))
        out.append(price)
    return out


def trend(minutes: int, *, base: float, bps_per_minute: float, noise_bps: float = 1.0, seed: int = 3) -> list[float]:
    rng = random.Random(seed)
    return [base * math.exp(i * bps_per_minute / 1e4 + rng.gauss(0, noise_bps / 1e4)) for i in range(minutes)]


def flat(minutes: int, *, base: float) -> list[float]:
    return [base] * minutes


def swings(minutes: int, *, base: float, range_bps: float, rise_bars: int, drop_bars: int,
           zigzag_bps: float = 0.0, phase: int = 0) -> list[float]:
    """A mean-reverting saw: a slow rise over ``rise_bars`` and a quick drop over ``drop_bars``.

    ``zigzag_bps`` adds an alternating +/- wiggle (deterministic, no seed), so
    the 20-bar path is choppy and the series stays a range. It is the
    "mechanics fixture": a market shaped so the range-reversion candidate can
    fire and the following rise can reach a net target. It proves the cycle
    machinery works end to end; it is not evidence about any real market.
    """
    period = rise_bars + drop_bars
    out = []
    for i in range(minutes):
        k = (i + phase) % period
        if k < drop_bars:
            level = 1.0 - (k + 1) / drop_bars                  # 1 -> 0 over the drop
        else:
            level = (k - drop_bars + 1) / rise_bars            # 0 -> 1 over the rise
        wiggle = zigzag_bps if i % 2 else -zigzag_bps
        out.append(base * (1 + (range_bps * (level - 0.5) + wiggle) / 1e4))
    return out
