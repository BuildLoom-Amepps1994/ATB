"""Validated order-book snapshots for the PAPER-only micro-trading mode.

The micro mode sizes an entry by walking the asks, estimates an exit by
walking the bids, and fills paper orders against the same levels, so one bad
book (crossed, unsorted, stale, out of order, garbage quantities) would size,
gate and fill an order wrongly in three places at once. Every book therefore
enters through this module and leaves it as an immutable, exactly parsed
:class:`BookSnapshot` whose invariants hold by construction: both sides
non-empty, every price and known quantity a positive finite ``Decimal`` from
:func:`money.parse`, bids strictly descending, asks strictly ascending, best
bid strictly below best ask. A violation raises :class:`BookError` whose
``code`` names the rule; nothing is repaired except equal adjacent prices,
which are merged by summing their quantities (never dropped).

Sources and semantics (as relayed for this build, 2026-10-26; recheck when the
venue or CCXT changes):

* MEXC REST depth (``GET /api/v3/depth``: weight 3, 100 levels by default, at
  most 5000) returns ``lastUpdateId`` and no timestamp. CCXT 4.5.90 puts
  ``lastUpdateId`` in ``nonce``, leaves ``timestamp`` None, and sorts both
  sides itself. A REST response is a complete snapshot, so there is no gap to
  detect; ordering between snapshots uses the venue's ``lastUpdateId``.
* A ticker (REST or the stream) is top of book only: one level per side,
  with the quantity from ``bidVolume``/``askVolume`` when the venue sent it.
  CCXT maps MEXC's ``"0"`` and ``""`` quantities to None, so a zero ticker
  volume is unknown here too, never an empty level. ``depth_known`` is
  always False for a ticker, even with both quantities (the contract: entries
  need a real depth snapshot, an exit estimate on a ticker uses the flat
  fallback). The quantities are kept for display only. A REST 24h ticker's
  ``timestamp`` is its window's close time, not the quote's.
* The stream feed's ``_stream_sequence`` is a local counter, never venue
  evidence, and is ignored.

Clocks: ``received_wall`` (``time.time()``) places venue timestamps on this
PC's clock; ``received_mono`` (``time.monotonic()``) measures age, so a wall
clock step can neither make an old book look fresh nor a fresh one old.
``clock_offset_s`` is local-minus-venue seconds (CCXT ``timeDifference`` /
1000, as :meth:`market_data.MarketData._clock_offset` reads it): a venue
timestamp ``ts`` happened at ``ts / 1000 + clock_offset_s`` on this PC.

The two receipt stamps must be taken together, when the response arrives:
the integration stamps ``time.monotonic()`` next to the feed's or market
data's wall ``_received_at``, never later and never from another clock. The
tracker refuses (``ValueError``) a receipt whose stamps disagree with its
reference pair by more than ``MIXED_CLOCKS_S`` (a wall time passed as the
monotonic one, or the reverse). A smaller mismatch cannot be told apart from
a wall-clock step: a wall stamp that lags its monotonic stamp by more than
a second can read as a rollback and invalidate every book.

``generation`` is a local per-symbol counter assigned by
:class:`BookTracker` when it accepts a snapshot. It links a decision or a
paper fill to the snapshot it used. It is NOT venue evidence: it is never
compared with ``venue_sequence`` and never used to order snapshots.

Bounds: a side longer than ``MAX_RAW_LEVELS`` entries is refused (no venue
sends more), and parsing stops at the first distinct price beyond
``max_levels``, so entries past the kept depth are never parsed.

Validation functions here are pure: they take the receipt times and the
clock offset as arguments and never read the clock, network, disk or
settings.
"""
from __future__ import annotations

import dataclasses
import math
import threading
from dataclasses import dataclass
from decimal import Decimal

import money

# Reason codes (BookError.code), shared with the micro manager and the dashboard.
BOOK_EMPTY = 'BOOK_EMPTY'
BOOK_CROSSED = 'BOOK_CROSSED'
BOOK_INVALID_LEVEL = 'BOOK_INVALID_LEVEL'
BOOK_UNSORTED = 'BOOK_UNSORTED'
BOOK_STALE = 'BOOK_STALE'
BOOK_FUTURE = 'BOOK_FUTURE'
BOOK_OUT_OF_ORDER = 'BOOK_OUT_OF_ORDER'
CLOCK_ROLLBACK = 'CLOCK_ROLLBACK'
BOOK_MISSING = 'BOOK_MISSING'
CODES = (BOOK_EMPTY, BOOK_CROSSED, BOOK_INVALID_LEVEL, BOOK_UNSORTED, BOOK_STALE, BOOK_FUTURE,
         BOOK_OUT_OF_ORDER, CLOCK_ROLLBACK, BOOK_MISSING)

FUTURE_TOLERANCE_S = 5.0            # venue timestamp may lead receipt by this much (clock jitter, latency)
TIMESTAMP_REGRESSION_S = 1.0        # a venue timestamp older than the newest one by more than this is out of order
CLOCK_ROLLBACK_TOLERANCE_S = 1.0    # wall time may fall this far behind the previous receipt before it is a rollback
# time.time() and time.monotonic() differ by decades on a real machine, while a wall clock corrected during
# a run moves far less (a clock years off could not even validate the venue's TLS certificate): a receipt whose
# wall and monotonic stamps disagree with the tracker's reference pair by more than this came from another clock.
MIXED_CLOCKS_S = 10 * 365 * 86400.0
MAX_RAW_LEVELS = 5000               # MEXC's maximum depth; a longer side is not a venue response
MAX_LEVELS = MAX_RAW_LEVELS
MAX_VENUE_INT = 2 ** 63             # venue sequences and millisecond timestamps are 64-bit

DEPTH_SOURCES = ('rest_depth', 'fixture')
TICKER_SOURCES = ('ticker', 'stream_ticker', 'fixture')
SOURCES = ('rest_depth', 'ticker', 'stream_ticker', 'fixture')
_TWO = Decimal(2)
_SPREAD_STEP = Decimal('0.01')      # reported spread resolution in bps (rounded up, the conservative side)


class BookError(ValueError):
    """A book that cannot be used. ``code`` is one of :data:`CODES`; the message never echoes raw input."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


# --------------------------------------------------------------- parsing
def _as_float(value) -> float | None:
    """``value`` as a finite float, or None when it is not a number or does not fit one (a huge int)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) else None


def _finite(value, name: str) -> float:
    number = _as_float(value)
    if number is None:
        raise ValueError(f'{name} must be a finite number')
    return number


def _venue_int(value, name: str, *, minimum: int) -> int | None:
    """A venue integer (sequence or ms timestamp); None stays unknown, anything malformed is refused.

    A corrupt header field makes the whole response untrustworthy, so it is
    refused like a corrupt level rather than read as unknown.
    """
    if value is None:
        return None
    if isinstance(value, float) and math.isfinite(value) and value == int(value):
        value = int(value)
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value < MAX_VENUE_INT:
        raise BookError(BOOK_INVALID_LEVEL, f'venue {name} is malformed')
    return value


def _price(value, side: str, index: int) -> Decimal:
    try:
        return money.parse(value, field=f'{side} price', allow_zero=False)
    except money.MoneyError:
        raise BookError(BOOK_INVALID_LEVEL, f'{side} level {index} has an invalid price') from None


def _quantity(value, side: str, index: int) -> Decimal:
    try:
        return money.parse(value, field=f'{side} quantity', allow_zero=False)
    except money.MoneyError:
        raise BookError(BOOK_INVALID_LEVEL, f'{side} level {index} has an invalid quantity') from None


def _check_side(levels, side: str, *, descending: bool, unknown_quantity_ok: bool) -> tuple:
    """Validate one side of a snapshot as given; equal adjacent prices are a defect at this point."""
    if levels is None:
        raise BookError(BOOK_EMPTY, f'{side} side is missing')
    if not isinstance(levels, (list, tuple)):
        raise BookError(BOOK_INVALID_LEVEL, f'{side} is not a list of levels')
    if not levels:
        raise BookError(BOOK_EMPTY, f'{side} side is empty')
    if len(levels) > MAX_LEVELS:
        raise BookError(BOOK_INVALID_LEVEL, f'{side} has more than {MAX_LEVELS} levels')
    checked, previous = [], None
    for index, level in enumerate(levels):
        if not isinstance(level, (list, tuple)) or len(level) != 2:
            raise BookError(BOOK_INVALID_LEVEL, f'{side} level {index} is not a (price, quantity) pair')
        price = _price(level[0], side, index)
        if level[1] is None and unknown_quantity_ok:
            quantity = None
        elif level[1] is None:
            raise BookError(BOOK_INVALID_LEVEL, f'{side} level {index} has no quantity')
        else:
            quantity = _quantity(level[1], side, index)
        if previous is not None and (price >= previous if descending else price <= previous):
            raise BookError(BOOK_UNSORTED, f'{side} are not strictly {"descending" if descending else "ascending"} '
                                           f'at level {index}')
        checked.append((price, quantity))
        previous = price
    return tuple(checked)


def _merged_depth_side(raw, side: str, *, descending: bool, max_levels: int) -> tuple:
    """Parse a CCXT side, merging equal adjacent prices, keeping at most ``max_levels`` distinct prices."""
    if raw is None:
        raise BookError(BOOK_EMPTY, f'{side} side is missing')
    if not isinstance(raw, (list, tuple)):
        raise BookError(BOOK_INVALID_LEVEL, f'{side} is not a list of levels')
    if not raw:
        raise BookError(BOOK_EMPTY, f'{side} side is empty')
    if len(raw) > MAX_RAW_LEVELS:
        raise BookError(BOOK_INVALID_LEVEL, f'{side} has more than {MAX_RAW_LEVELS} levels')
    merged: list[list] = []
    for index, level in enumerate(raw):
        # CCXT levels are [price, amount] or [price, amount, count/id] (Kraken adds a timestamp).
        if not isinstance(level, (list, tuple)) or len(level) < 2:
            raise BookError(BOOK_INVALID_LEVEL, f'{side} level {index} is not a [price, amount] pair')
        price = _price(level[0], side, index)
        if level[1] is None:
            raise BookError(BOOK_INVALID_LEVEL, f'{side} level {index} has no quantity')
        quantity = _quantity(level[1], side, index)
        if merged:
            last = merged[-1][0]
            if price == last:
                merged[-1][1] = money.add(merged[-1][1], quantity)
                continue
            if price > last if descending else price < last:
                raise BookError(BOOK_UNSORTED, f'{side} are not sorted best first at level {index}')
            if len(merged) == max_levels:
                break       # a distinct price beyond the kept depth; nothing after it is read
        merged.append([price, quantity])
    return tuple((price, quantity) for price, quantity in merged)


# -------------------------------------------------------------- snapshot
@dataclass(frozen=True)
class BookSnapshot:
    exchange: str
    symbol: str
    bids: tuple[tuple[Decimal, Decimal | None], ...]   # best first (price descending)
    asks: tuple[tuple[Decimal, Decimal | None], ...]   # best first (price ascending)
    source: str                       # rest_depth | ticker | stream_ticker | fixture
    depth_known: bool                 # False for a ticker (top of book only) and whenever a quantity is unknown
    venue_timestamp_ms: int | None    # the venue's time for this book, when it sent one
    venue_sequence: int | None        # MEXC REST depth lastUpdateId (CCXT 'nonce'); a venue value, never local
    received_wall: float              # time.time() when the response arrived
    received_mono: float              # time.monotonic() when the response arrived
    generation: int = 0               # local per-symbol BookTracker counter (NOT venue evidence); 0 = not accepted
    clock_offset_s: float = 0.0       # local-minus-venue seconds used to place venue_timestamp_ms on this PC's clock

    def __post_init__(self):
        for name in ('exchange', 'symbol'):
            if not isinstance(getattr(self, name), str) or not getattr(self, name):
                raise ValueError(f'{name} must be a non-empty string')
        if self.source not in SOURCES:
            raise ValueError(f'book source must be one of {", ".join(SOURCES)}')
        if not isinstance(self.depth_known, bool):
            raise ValueError('depth_known must be a boolean')
        unknown_ok = self.source != 'rest_depth'
        bids = _check_side(self.bids, 'bids', descending=True, unknown_quantity_ok=unknown_ok)
        asks = _check_side(self.asks, 'asks', descending=False, unknown_quantity_ok=unknown_ok)
        if self.depth_known and self.source in ('ticker', 'stream_ticker'):
            raise ValueError('a ticker is top of book only and never claims known depth')
        if self.depth_known and any(quantity is None for _, quantity in bids + asks):
            raise BookError(BOOK_INVALID_LEVEL, 'depth is marked known but a level quantity is unknown')
        if bids[0][0] >= asks[0][0]:
            kind = 'locked' if bids[0][0] == asks[0][0] else 'crossed'
            raise BookError(BOOK_CROSSED, f'book is {kind}: best bid {money.text(bids[0][0])} '
                                          f'is not below best ask {money.text(asks[0][0])}')
        object.__setattr__(self, 'bids', bids)
        object.__setattr__(self, 'asks', asks)
        object.__setattr__(self, 'venue_timestamp_ms', _venue_int(self.venue_timestamp_ms, 'timestamp', minimum=1))
        object.__setattr__(self, 'venue_sequence', _venue_int(self.venue_sequence, 'sequence', minimum=0))
        object.__setattr__(self, 'received_wall', _finite(self.received_wall, 'received_wall'))
        object.__setattr__(self, 'received_mono', _finite(self.received_mono, 'received_mono'))
        object.__setattr__(self, 'clock_offset_s', _finite(self.clock_offset_s, 'clock_offset_s'))
        if isinstance(self.generation, bool) or not isinstance(self.generation, int) or self.generation < 0:
            raise ValueError('generation must be a non-negative integer')

    # ------------------------------------------------------------ figures
    @property
    def best_bid(self) -> Decimal:
        return self.bids[0][0]

    @property
    def best_ask(self) -> Decimal:
        return self.asks[0][0]

    @property
    def mid(self) -> Decimal:
        return money.div(money.add(self.best_bid, self.best_ask), _TWO)

    @property
    def spread_bps(self) -> Decimal:
        """(ask - bid) / mid in basis points, the same convention as the engine's quote spread."""
        return money.mul(money.div(money.sub(self.best_ask, self.best_bid), self.mid), money.BPS)

    @property
    def level_count(self) -> int:
        """Levels on the shallower side: the depth both an entry and an exit walk can rely on."""
        return min(len(self.bids), len(self.asks))

    @property
    def venue_wall(self) -> float | None:
        """The venue's timestamp on this PC's wall clock, or None when the venue sent none."""
        if self.venue_timestamp_ms is None:
            return None
        return self.venue_timestamp_ms / 1000 + self.clock_offset_s

    @property
    def venue_lag_s(self) -> float | None:
        """How old the venue said the book was when it arrived (diagnostic; ages use the monotonic clock)."""
        venue_wall = self.venue_wall
        return None if venue_wall is None else self.received_wall - venue_wall

    def age_seconds(self, now_mono: float) -> float:
        """Monotonic seconds since the response arrived.

        A ``now_mono`` slightly before the receipt (a reader racing the
        writer) is age 0; one more than a second before it means the caller
        mixed clocks, so the age is unknown and reported as infinite (stale).
        """
        now = _as_float(now_mono)
        if now is None:
            return math.inf
        age = now - self.received_mono
        if not age >= -CLOCK_ROLLBACK_TOLERANCE_S:
            return math.inf
        return max(0.0, age)

    def as_dict(self, levels: int = 5) -> dict:
        """JSON-safe summary; every Decimal is a decimal string. ``levels`` caps the levels listed per side."""
        if isinstance(levels, bool) or not isinstance(levels, int) or levels < 0:
            raise ValueError('levels must be a non-negative integer')

        def side(rows):
            return [[money.text(price), money.text(quantity)] for price, quantity in rows[:levels]]
        lag = self.venue_lag_s
        return {'exchange': self.exchange, 'symbol': self.symbol, 'source': self.source,
                'depth_known': self.depth_known, 'venue_timestamp_ms': self.venue_timestamp_ms,
                'venue_sequence': self.venue_sequence, 'generation': self.generation,
                'received_wall': self.received_wall, 'received_mono': self.received_mono,
                'clock_offset_s': self.clock_offset_s,
                'venue_lag_s': None if lag is None else round(lag, 3),
                'best_bid': money.text(self.best_bid), 'best_ask': money.text(self.best_ask),
                'mid': money.text(self.mid), 'spread_bps': money.text(money.q_up(self.spread_bps, _SPREAD_STEP)),
                'bid_levels': len(self.bids), 'ask_levels': len(self.asks), 'level_count': self.level_count,
                'bids': side(self.bids), 'asks': side(self.asks)}


def _check_future(snapshot: BookSnapshot, tolerance_s: float) -> None:
    venue_wall = snapshot.venue_wall
    if venue_wall is not None and venue_wall - snapshot.received_wall > tolerance_s:
        raise BookError(BOOK_FUTURE, f'{snapshot.symbol} venue timestamp is '
                                     f'{venue_wall - snapshot.received_wall:.1f} s ahead of its receipt '
                                     f'(tolerance {tolerance_s:.1f} s, clock offset applied)')


def _check_symbol(response: dict, symbol: str) -> None:
    named = response.get('symbol')
    if named is not None and named != symbol:
        raise BookError(BOOK_MISSING, f'response is for another symbol than {symbol}')


def from_ccxt_order_book(book: dict, *, exchange: str, symbol: str, received_wall: float, received_mono: float,
                         clock_offset_s: float = 0.0, max_levels: int = 50, source: str = 'rest_depth') -> BookSnapshot:
    """A validated depth snapshot from a CCXT order book (``fetch_order_book``)."""
    if isinstance(max_levels, bool) or not isinstance(max_levels, int) or not 1 <= max_levels <= MAX_LEVELS:
        raise ValueError(f'max_levels must be an integer from 1 to {MAX_LEVELS}')
    if source not in DEPTH_SOURCES:
        raise ValueError(f'an order book source must be one of {", ".join(DEPTH_SOURCES)}')
    if not isinstance(book, dict):
        raise BookError(BOOK_MISSING, f'no order book response for {symbol}')
    _check_symbol(book, symbol)
    bids = _merged_depth_side(book.get('bids'), 'bids', descending=True, max_levels=max_levels)
    asks = _merged_depth_side(book.get('asks'), 'asks', descending=False, max_levels=max_levels)
    snapshot = BookSnapshot(exchange=exchange, symbol=symbol, bids=bids, asks=asks, source=source, depth_known=True,
                            venue_timestamp_ms=_venue_int(book.get('timestamp'), 'timestamp', minimum=1),
                            venue_sequence=_venue_int(book.get('nonce'), 'sequence', minimum=0),
                            received_wall=received_wall, received_mono=received_mono, clock_offset_s=clock_offset_s)
    _check_future(snapshot, FUTURE_TOLERANCE_S)
    return snapshot


def _ticker_quantity(value, side: str) -> Decimal | None:
    # CCXT reads MEXC's "0" and "" as "not sent"; a missing quantity is unknown, never an empty level.
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    try:
        quantity = money.parse(value, field=f'{side} volume')
    except money.MoneyError:
        raise BookError(BOOK_INVALID_LEVEL, f'ticker {side} volume is invalid') from None
    return quantity if quantity > 0 else None


def from_ticker(ticker: dict, *, exchange: str, symbol: str, received_wall: float, received_mono: float,
                clock_offset_s: float = 0.0, source: str = 'ticker') -> BookSnapshot:
    """A one-level top-of-book snapshot from a CCXT ticker; depth is never known (the volumes are display only)."""
    if source not in TICKER_SOURCES:
        raise ValueError(f'a ticker source must be one of {", ".join(TICKER_SOURCES)}')
    if not isinstance(ticker, dict):
        raise BookError(BOOK_MISSING, f'no ticker response for {symbol}')
    _check_symbol(ticker, symbol)
    sides = {}
    for side, price_key, volume_key in (('bids', 'bid', 'bidVolume'), ('asks', 'ask', 'askVolume')):
        if ticker.get(price_key) is None:
            raise BookError(BOOK_EMPTY, f'ticker has no {price_key}')
        sides[side] = ((_price(ticker.get(price_key), side, 0), _ticker_quantity(ticker.get(volume_key), side)),)
    snapshot = BookSnapshot(exchange=exchange, symbol=symbol, bids=sides['bids'], asks=sides['asks'], source=source,
                            depth_known=False,
                            venue_timestamp_ms=_venue_int(ticker.get('timestamp'), 'timestamp', minimum=1),
                            venue_sequence=None, received_wall=received_wall, received_mono=received_mono,
                            clock_offset_s=clock_offset_s)
    _check_future(snapshot, FUTURE_TOLERANCE_S)
    return snapshot


# --------------------------------------------------------------- tracker
@dataclass
class _Track:
    """Per-symbol state, guarded by the tracker's lock."""
    snapshot: BookSnapshot | None = None
    generation: int = 0               # last generation assigned; never reset, so a number names one snapshot
    max_sequence: int | None = None   # newest venue evidence accepted since the last invalidation
    max_timestamp_ms: int | None = None
    fence_mono: float | None = None   # a book received no later than this was received before an invalidation
    invalidated: str | None = None
    last_error: str | None = None
    accepted: int = 0
    refused: int = 0
    # (first receipt, first id, highest id, last receipt) of the lower venue ids refused since the stored book
    lower_run: tuple[float, int, int, float] | None = None
    sequence_resets: int = 0          # venue counter restarts accepted (see BookTracker)


class BookTracker:
    """The newest valid snapshot per symbol, served only while it is fresh.

    ``accept`` refuses, and keeps the stored snapshot, when the new one is
    older by any evidence: a venue sequence lower than the newest accepted;
    otherwise a venue timestamp more than ``TIMESTAMP_REGRESSION_S`` older
    than the newest accepted; and, when it was received before the stored
    snapshot, unless venue evidence it shares with the stored one shows it is
    strictly newer than everything accepted. So an equal sequence, an equal
    timestamp or one inside the jitter tolerance never outranks the receipt
    order. Venue evidence is compared with the newest value accepted since
    the last invalidation, so a ticker accepted in between cannot let an
    older depth snapshot back in.

    A wall clock that stepped back (receipt wall time more than
    ``CLOCK_ROLLBACK_TOLERANCE_S`` before the previous accepted receipt while
    the monotonic clock advanced) invalidates every symbol's snapshot:
    anything derived from wall time must be rebuilt from new snapshots.
    Snapshots received before the rollback was detected are refused after
    it. The venue evidence is kept: a local clock step says nothing about the
    venue's update ids or the venue's own clock.

    A venue that restarted its update-id counter sends only ids lower than
    the newest accepted. When such lower ids keep arriving, none below the
    first of them and rising above it, for longer than ``max_age_s`` with no
    snapshot accepted in between (and each received after the stored book),
    the stored book has expired and nothing else is coming: that symbol's id
    evidence starts over from the current snapshot, which is accepted and
    counted in ``sequence_resets``. A lagging replica's id below the previous
    one keeps the run; an id below the run's first starts it over. One lower
    id, a replica stuck on one id, or a venue timestamp older than the newest
    accepted are still refused.

    ``invalidate`` (a stream reconnect) clears the served snapshot and the
    venue evidence, so a venue that restarted its counters cannot lock a
    symbol out, and fences the receipt clock at the invalidation time the
    caller gives (or the newest receipt seen, if later): a snapshot received
    no later than that was received before the invalidation and is refused,
    whatever its venue evidence says. A named symbol the tracker has not
    seen yet is fenced too, and invalidating every symbol also fences the
    symbols first seen after it. ``generation`` keeps counting.

    A receipt whose wall and monotonic stamps disagree with the tracker's
    reference pair by more than ``MIXED_CLOCKS_S`` raises ``ValueError`` and
    changes nothing, so one mis-stamped snapshot can neither switch rollback
    detection off nor lock its symbol out.
    """

    def __init__(self, *, max_age_s: float, future_tolerance_s: float = FUTURE_TOLERANCE_S):
        self.max_age_s = _finite(max_age_s, 'max_age_s')
        self.future_tolerance_s = _finite(future_tolerance_s, 'future_tolerance_s')
        if self.max_age_s <= 0 or self.future_tolerance_s < 0:
            raise ValueError('max_age_s must be positive and future_tolerance_s not negative')
        self.lock = threading.Lock()
        self.exchange: str | None = None
        self.rollbacks = 0
        self._tracks: dict[str, _Track] = {}
        # (wall, mono) of the latest receipt accepted, or of the one that revealed a rollback
        self._clock_ref: tuple[float, float] | None = None
        self._rollback_mono: float | None = None
        self._fence_mono: float | None = None     # the latest invalidation of every symbol
        self.sequence_resets = 0

    def accept(self, snapshot: BookSnapshot) -> BookSnapshot:
        """Store ``snapshot`` as the symbol's newest book; returns it with its generation assigned."""
        if not isinstance(snapshot, BookSnapshot):
            raise BookError(BOOK_MISSING, 'no book snapshot to accept')
        with self.lock:
            if self.exchange is not None and snapshot.exchange != self.exchange:
                raise ValueError(f'this tracker holds {self.exchange} books, not {snapshot.exchange}')
            self._check_clock_pair(snapshot)
            track = self._tracks.setdefault(snapshot.symbol, _Track())
            try:
                self._check_clock(snapshot)
                _check_future(snapshot, self.future_tolerance_s)
                restarted = self._check_order(track, snapshot)
            except BookError as exc:
                track.refused += 1
                track.last_error = f'{exc.code}: {exc}'
                raise
            if restarted:
                track.max_sequence = None
                track.sequence_resets += 1
                self.sequence_resets += 1
            track.generation += 1
            stored = dataclasses.replace(snapshot, generation=track.generation)
            track.snapshot, track.invalidated, track.lower_run = stored, None, None
            if stored.venue_sequence is not None:
                track.max_sequence = max(stored.venue_sequence, track.max_sequence or 0)
            if stored.venue_timestamp_ms is not None:
                track.max_timestamp_ms = max(stored.venue_timestamp_ms, track.max_timestamp_ms or 0)
            track.accepted += 1
            if self._clock_ref is None or stored.received_mono >= self._clock_ref[1]:
                self._clock_ref = (stored.received_wall, stored.received_mono)
            self.exchange = stored.exchange
            return stored

    def _check_clock_pair(self, snapshot: BookSnapshot) -> None:
        """Refuse a receipt stamped from another clock before it can become the reference or a stored book."""
        if self._clock_ref is None:
            return
        ref_wall, ref_mono = self._clock_ref
        drift = (snapshot.received_wall - ref_wall) - (snapshot.received_mono - ref_mono)
        if not abs(drift) <= MIXED_CLOCKS_S:
            raise ValueError(f'{snapshot.symbol} receipt stamps disagree with the previous receipts by '
                             f'{abs(drift) / 86400:.0f} days: received_wall must be time.time() and received_mono '
                             'time.monotonic(), taken together when the response arrived')

    def _check_clock(self, snapshot: BookSnapshot) -> None:
        if self._rollback_mono is not None and snapshot.received_mono < self._rollback_mono:
            raise BookError(CLOCK_ROLLBACK, f'{snapshot.symbol} book was received before the wall-clock rollback '
                                            'was detected')
        if self._clock_ref is None:
            return
        ref_wall, ref_mono = self._clock_ref
        if snapshot.received_mono >= ref_mono and snapshot.received_wall < ref_wall - CLOCK_ROLLBACK_TOLERANCE_S:
            stepped = ref_wall - snapshot.received_wall
            self._clock_ref = (snapshot.received_wall, snapshot.received_mono)
            self._rollback_mono = snapshot.received_mono
            self.rollbacks += 1
            self._invalidate(None, f'wall clock stepped back {stepped:.1f} s', keep_evidence=True)
            raise BookError(CLOCK_ROLLBACK, f'wall clock stepped back {stepped:.1f} s while the monotonic clock '
                                            'advanced; every book was invalidated')

    def _check_order(self, track: _Track, snapshot: BookSnapshot) -> bool:
        """Refuse an older snapshot; True when it shows the venue restarted its id counter."""
        fences = [fence for fence in (track.fence_mono, self._fence_mono) if fence is not None]
        if fences and snapshot.received_mono <= max(fences):
            raise BookError(BOOK_OUT_OF_ORDER, f'{snapshot.symbol} book was received before the book was '
                                               f'invalidated ({track.invalidated or "invalidated"})')
        sequence, timestamp = snapshot.venue_sequence, snapshot.venue_timestamp_ms
        restarted = False
        if sequence is not None and track.max_sequence is not None:
            if sequence < track.max_sequence:
                restarted = self._counter_restarted(track, snapshot)
                if restarted and timestamp is not None and track.max_timestamp_ms is not None:
                    # only the id counter restarted: the venue's clock still orders its books
                    restarted = (track.max_timestamp_ms - timestamp) / 1000 <= TIMESTAMP_REGRESSION_S
                if not restarted:
                    raise BookError(BOOK_OUT_OF_ORDER, f'{snapshot.symbol} venue sequence {sequence} is older '
                                                       f'than {track.max_sequence} already accepted')
        elif timestamp is not None and track.max_timestamp_ms is not None:
            behind = (track.max_timestamp_ms - timestamp) / 1000
            if behind > TIMESTAMP_REGRESSION_S:
                raise BookError(BOOK_OUT_OF_ORDER, f'{snapshot.symbol} venue timestamp is {behind:.1f} s older '
                                                   'than one already accepted')
        stored = track.snapshot
        if stored is None or snapshot.received_mono >= stored.received_mono:
            return restarted
        # Received before the stored book: only venue evidence shared with it, strictly newer than everything
        # accepted, outranks the receipt order (an equal value or a timestamp inside the jitter tolerance does not).
        if sequence is not None and stored.venue_sequence is not None:
            newer = sequence > max(stored.venue_sequence, track.max_sequence or 0)
        elif timestamp is not None and stored.venue_timestamp_ms is not None:
            newer = timestamp > max(stored.venue_timestamp_ms, track.max_timestamp_ms or 0)
        else:
            newer = False
        if not newer:
            raise BookError(BOOK_OUT_OF_ORDER, f'{snapshot.symbol} book was received before the stored one and '
                                               'carries no venue evidence that it is newer')
        return restarted

    def _counter_restarted(self, track: _Track, snapshot: BookSnapshot) -> bool:
        """Record a venue id lower than the newest accepted; True once the run of them shows a counter restart.

        A run starts at the first lower id received after the stored book and
        ends with the next accepted snapshot; its third field is its highest
        id. A lower id received before the run's last one (handed over late)
        adds nothing. One below the run's last but not below its first comes
        from a lagging replica and keeps the run; one below the run's first
        starts a new run, since it could be a second restart.
        """
        mono, sequence = snapshot.received_mono, snapshot.venue_sequence
        stored, run = track.snapshot, track.lower_run
        if (stored is not None and mono <= stored.received_mono) or (run is not None and mono < run[3]):
            return False
        if run is None or sequence < run[1]:
            track.lower_run = (mono, sequence, sequence, mono)
            return False
        highest = max(run[2], sequence)
        track.lower_run = (run[0], run[1], highest, mono)
        return highest > run[1] and mono - run[0] > self.max_age_s

    def latest(self, symbol: str, *, now_mono: float, max_age_s: float | None = None) -> BookSnapshot | None:
        """The stored snapshot when it is valid and no older than the limit, else None.

        ``max_age_s`` can only tighten the tracker's own limit: a caller
        cannot loosen the configured freshness requirement.
        """
        limit = self.max_age_s if max_age_s is None else min(self.max_age_s, _finite(max_age_s, 'max_age_s'))
        with self.lock:
            track = self._tracks.get(symbol)
            snapshot = track.snapshot if track is not None else None
        if snapshot is None or not snapshot.age_seconds(now_mono) <= limit:
            return None
        return snapshot

    def resets(self, symbol: str) -> int:
        """How many venue id-counter restarts have been accepted for ``symbol``."""
        with self.lock:
            track = self._tracks.get(symbol)
            return track.sequence_resets if track is not None else 0

    def invalidate(self, symbol: str | None = None, reason: str = '', *, now_mono: float) -> None:
        """Stop serving ``symbol``'s book (every symbol when None) until a new snapshot is accepted.

        ``now_mono`` is when the invalidation happened (a stream reconnect), on
        the ``time.monotonic()`` clock the receipts are stamped with.
        """
        now = _finite(now_mono, 'now_mono')
        with self.lock:
            if self._clock_ref is not None and not abs(now - self._clock_ref[1]) <= MIXED_CLOCKS_S:
                raise ValueError('now_mono must be time.monotonic(), the clock the book receipts are stamped with')
            self._invalidate(symbol, reason or 'invalidated', fence=now)

    def _invalidate(self, symbol: str | None, reason: str, *, keep_evidence: bool = False,
                    fence: float | None = None) -> None:
        """Stop serving the books.

        A rollback keeps the venue evidence (``_rollback_mono`` already refuses the receipts before it).
        An explicit invalidation clears the evidence and fences receipts at its own time or the newest one
        seen so far, whichever is later.
        """
        if keep_evidence:
            tracks = self._tracks.values() if symbol is None else [self._tracks[symbol]] if symbol in self._tracks else []
            for track in tracks:
                track.snapshot, track.invalidated = None, reason
            return
        # Every receipt the tracker has seen was handed to accept() before this call, so nothing received no
        # later than the newest of them, or than the invalidation itself, can be news from after it.
        if self._clock_ref is not None:
            fence = self._clock_ref[1] if fence is None else max(fence, self._clock_ref[1])
        if symbol is None:
            tracks = list(self._tracks.values())
            if fence is not None:
                self._fence_mono = fence if self._fence_mono is None else max(fence, self._fence_mono)
        else:
            tracks = [self._tracks.setdefault(symbol, _Track())]
        for track in tracks:
            track.snapshot, track.invalidated, track.lower_run = None, reason, None
            track.max_sequence, track.max_timestamp_ms = None, None
            if fence is not None:
                track.fence_mono = fence if track.fence_mono is None else max(fence, track.fence_mono)

    def status(self, now_mono: float) -> dict:
        """Per symbol: generation, age, source, venue sequence, last refusal and whether the book is served."""
        with self.lock:
            tracks = {symbol: dataclasses.replace(track) for symbol, track in self._tracks.items()}
        out = {}
        for symbol in sorted(tracks):
            track = tracks[symbol]
            snapshot = track.snapshot
            age = snapshot.age_seconds(now_mono) if snapshot is not None else None
            if snapshot is None:
                state, code = ('invalidated' if track.invalidated else 'missing'), BOOK_MISSING
            elif age <= self.max_age_s:
                state, code = 'fresh', None
            else:
                state, code = 'stale', BOOK_STALE
            out[symbol] = {
                'generation': track.generation,
                'age_s': round(age, 3) if age is not None and math.isfinite(age) else None,
                'source': snapshot.source if snapshot else None,
                'sequence': snapshot.venue_sequence if snapshot else None,
                'last_error': track.last_error, 'state': state, 'code': code, 'invalidated': track.invalidated,
                'depth_known': snapshot.depth_known if snapshot else None,
                'best_bid': money.text(snapshot.best_bid) if snapshot else None,
                'best_ask': money.text(snapshot.best_ask) if snapshot else None,
                'spread_bps': float(snapshot.spread_bps) if snapshot else None,
                'accepted': track.accepted, 'refused': track.refused, 'sequence_resets': track.sequence_resets}
        return out


__all__ = ['BookError', 'BookSnapshot', 'BookTracker', 'from_ccxt_order_book', 'from_ticker', 'CODES',
           'BOOK_EMPTY', 'BOOK_CROSSED', 'BOOK_INVALID_LEVEL', 'BOOK_UNSORTED', 'BOOK_STALE', 'BOOK_FUTURE',
           'BOOK_OUT_OF_ORDER', 'CLOCK_ROLLBACK', 'BOOK_MISSING', 'FUTURE_TOLERANCE_S', 'MAX_RAW_LEVELS',
           'MAX_LEVELS', 'SOURCES']
