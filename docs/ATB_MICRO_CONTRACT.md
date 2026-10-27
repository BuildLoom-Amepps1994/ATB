# Micro-trading mode: module contract (working document)

This is the interface contract the micro-trading modules are built against.
`docs/ATB_MICRO_ARCHITECTURE.md` is the reader-facing description; this file
pins names, types and semantics so modules written in parallel fit together.
Every amount in these modules is a `decimal.Decimal` produced by `money.py`
unless a field is explicitly a legacy float.

## Ground rules (binding on every module)

* PAPER only. No module may create an authenticated client, read credentials,
  call a private endpoint, or offer any flag, setting, environment variable or
  hidden argument that lets the micro mode submit to a real venue.
* No network in tests. `tests/conftest.py` refuses non-loopback connections.
  Tests build their own fixtures. Never import `app` in a unit test that does
  not need it.
* Missing is unknown, never zero. A field that is absent stays `None` and the
  record says what was assumed.
* Exact money. Parse with `money.parse` (bounded, refuses bool/NaN/inf/huge
  exponents), compute with `money.add/sub/mul/div`, quantize only with
  `money.q_down`/`money.q_up` at a venue or reporting boundary, store with
  `money.text`. Display with `money.display` (never hides a sign).
* Pure where stated. Strategy, economics and book validation functions take
  explicit inputs (including `now`) and never read the clock, network, disk
  or settings themselves.
* No new dependencies.
* Style: match the existing code (module docstrings that explain the why,
  short functions, explicit names, no type-annotation-only abstractions).

## Shared modules that already exist

* `money.py`: `parse(value, *, field, allow_zero=True, allow_negative=False)`,
  `parse_optional`, `positive`, `q_down(value, step)`, `q_up(value, step)`,
  `add`, `sub`, `mul`, `div`, `bps(x)` (bps to fraction), `text(d)`,
  `from_text(s)`, `to_float(d)`, `display(d, places=4, signed=False)`,
  `MoneyError`, `ZERO`, `ONE`.
* `venue_rules.py`: `VenueRules` (frozen dataclass) and
  `normalize(market, *, exchange, symbol, loaded_at, precision_mode=TICK_SIZE,
  source, buy_fee_currency='unknown', sell_fee_currency='unknown',
  market_buy_by_cost=None)`. Fields: `amount_step`, `price_tick`, `cost_step`,
  `min_amount`, `max_amount`, `min_cost_market`, `min_cost_limit`,
  `max_cost_market`, `max_cost_limit`, `taker_fee`, `maker_fee`, `fee_source`,
  `buy_fee_currency`, `sell_fee_currency` (`quote|base|unknown`), `active`,
  `spot`, `status`, `side_permission`, `market_orders`, `market_buy_by_cost`,
  `loaded_at`, `source`, `issues`. Methods: `market_min_cost()`,
  `market_max_cost()`, `age_seconds(now)`, `tradable(side, *, now=None,
  max_age_seconds=None) -> (ok, code, detail)`, `as_dict()`.
  Reason-code constants: `MARKET_UNKNOWN`, `MARKET_INACTIVE`, `MARKET_NOT_SPOT`,
  `SIDE_NOT_ALLOWED`, `MARKET_ORDERS_UNSUPPORTED`, `COST_BUY_UNSUPPORTED`,
  `METADATA_STALE`.
  Added with the micro mode: `parse_mexc_symbol_commission(response, *,
  symbol, received_at) -> AccountCommission` validates a supplied response of
  MEXC's signed `GET /api/v3/tradeFee` (text, bytes or a decoded dict). It
  raises `CommissionError` unless `code` is the integer 0, `data` is an
  object with a valid taker rate (0 to 1 %), a valid maker rate (-1 % to
  1 %) and, if one is stated, a valid RPI taker rate, and `timestamp` is a
  positive integer; also on text that is not a JSON object, is over 4096
  characters or repeats a key, and when the symbol or receive time is
  missing. `msg` and extra keys are not checked; string or number rates are
  accepted. Nothing calls the endpoint. `AccountCommission` (frozen): `exchange`, `symbol`,
  `maker`, `taker`, `rpi_taker`, `venue_ms`, `received_at`, `source`;
  `age_seconds(now)`, `as_dict()`.

## `micro_book.py`: validated order-book snapshots

```python
class BookError(ValueError):
    code: str   # BOOK_EMPTY | BOOK_CROSSED | BOOK_INVALID_LEVEL | BOOK_UNSORTED |
                # BOOK_STALE | BOOK_FUTURE | BOOK_OUT_OF_ORDER | CLOCK_ROLLBACK | BOOK_MISSING

@dataclass(frozen=True)
class BookSnapshot:
    exchange: str
    symbol: str
    bids: tuple[tuple[Decimal, Decimal | None], ...]   # best first (price descending)
    asks: tuple[tuple[Decimal, Decimal | None], ...]   # best first (price ascending)
    source: str                 # 'rest_depth' | 'ticker' | 'stream_ticker' | 'fixture'
    depth_known: bool           # False when level quantities are unknown (ticker top of book)
    venue_timestamp_ms: int | None
    venue_sequence: int | None  # MEXC REST depth lastUpdateId (CCXT 'nonce'); a venue value, never a local counter
    received_wall: float        # time.time() when the response arrived
    received_mono: float        # time.monotonic() when the response arrived
    generation: int             # local per-symbol counter assigned by BookTracker (NOT venue evidence)
    # properties: best_bid, best_ask, mid, spread_bps (Decimal), level_count
    def age_seconds(self, now_mono: float) -> float
    def as_dict(self, levels: int = 5) -> dict   # strings for Decimals

def from_ccxt_order_book(book: dict, *, exchange, symbol, received_wall, received_mono,
                         clock_offset_s: float = 0.0, max_levels: int = 50, source='rest_depth') -> BookSnapshot
def from_ticker(ticker: dict, *, exchange, symbol, received_wall, received_mono,
                clock_offset_s: float = 0.0, source='ticker') -> BookSnapshot   # one level; qty from bidVolume/askVolume or None
```

Validation (raise `BookError` with the code): both sides non-empty; every
price and quantity parses positive and finite (quantity may be `None` only for
ticker sources); bids strictly descending, asks strictly ascending (equal
adjacent prices are merged by summing, never silently dropped); best bid <
best ask (a locked or crossed book is `BOOK_CROSSED`); a venue timestamp more
than `FUTURE_TOLERANCE_S = 5` seconds ahead of `received_wall` after the clock
offset is `BOOK_FUTURE`; at most `max_levels` per side are kept.

```python
class BookTracker:
    def __init__(self, *, max_age_s: float, future_tolerance_s: float = 5.0)
    def accept(self, snapshot: BookSnapshot) -> BookSnapshot
        # returns the stored snapshot with its generation assigned; raises BookError:
        #   BOOK_OUT_OF_ORDER when venue_sequence (if both known) is lower than the stored one,
        #     or (no sequences) venue_timestamp_ms is older than the stored one by > 1 s;
        #     the stored snapshot is kept. Lower ids, none below the first of them, that keep
        #     arriving for longer than max_age_s with none accepted in between are a venue
        #     counter restart: a read whose id rises above the run's highest for the second time
        #     since the count last started over is accepted, the id evidence starts over from it,
        #     and it is counted in sequence_resets. An id below the run's highest (a lagging
        #     replica) or stamped > 1 s older than the run's newest never counts, and the highest
        #     id received again right after a lower one (a stuck replica) starts the count over;
        #     an id the run received before it started over becomes its highest and starts the
        #     count over, so it is never the read accepted.
        #     An id that stays the run's highest (a replica frozen before the restart) keeps the
        #     restart refused while it is received again within max_age_s; the first lower read
        #     more than max_age_s after its last receipt becomes the run's highest, so the restart
        #     is accepted on the second rise after it, about max_age_s plus two to three of the
        #     symbol's reads after the old id's last answer. Ids the run set aside so, or received
        #     before the read accepted and above it, are refused even above the new counter's ids
        #     until an accepted id passes them (the highest 8 are kept; invalidate clears them).
        #     Not detected, since ids alone cannot tell: once the run is older than max_age_s,
        #     any read the run has not received that is such a second rise is accepted as the
        #     restart whatever its source (a stuck or frozen replica answering for the first
        #     time, with any number of lower reads between the two rises); a frozen replica whose
        #     id was not set aside is served when it answers after the restart was accepted; and a
        #     book served so, or a replica stuck on the newest accepted id, is served as fresh
        #     while it keeps answering (an equal id is accepted), the live depth being refused
        #     while a frozen one answers within max_age_s.
        #   CLOCK_ROLLBACK when received_wall is more than 1 s earlier than the previous
        #     accepted received_wall while received_mono advanced; every symbol's snapshot
        #     is invalidated (derived state must be rebuilt from new snapshots).
    def latest(self, symbol, *, now_mono: float, max_age_s: float | None = None) -> BookSnapshot | None
        # None when missing, invalidated or older than max_age (monotonic age)
    def invalidate(self, symbol: str | None = None, reason: str = '', *, now_mono: float) -> None
        # refuses every receipt no later than now_mono (or the newest receipt seen), unseen symbols included
    def status(self, now_mono: float) -> dict   # per symbol: generation, age_s, source, sequence, last_error,
                                                #   sequence_resets
```

A REST depth response is a complete snapshot, so REST has no gap concept;
ordering uses the venue's `lastUpdateId`. A ticker stream is top of book only
(`depth_known=False`). The tracker holds REST depth only (`MicroManager._fetch_book`); a ticker snapshot is built for each read and never accepted, so nothing calls `invalidate()` on a stream reconnect.

`market_data.MarketData.order_book(symbol, limit=20)` (added by the
integration step, not by the book module): `read('fetch_order_book', symbol,
limit)`, stamps `_received_at`, `fetch_order_book` is a priority method.

## `micro_economics.py`: one sizing, cost and accounting component

```python
MICRO_MAX_ALL_IN_USDT = Decimal('25')     # absolute cap of the micro profile; never raised by code

# Reason codes (strings):
BUDGET_BELOW_VENUE_MINIMUM, EXIT_BELOW_VENUE_MINIMUM, INSUFFICIENT_ASK_DEPTH,
INSUFFICIENT_BID_DEPTH, DEPTH_UNKNOWN, CASH_INSUFFICIENT, ALLOCATION_EXHAUSTED,
MAX_COST_EXCEEDED, PRICE_UNKNOWN, FEE_UNKNOWN, BUDGET_INVALID
# plus venue_rules codes passed through.

@dataclass(frozen=True)
class CostModel:
    taker_fee: Decimal          # fraction per leg used for estimates (modeled)
    fee_source: str             # e.g. 'modeled: max(configured 0.1%, public taker 0.05%)'
    latency_bps: Decimal        # adverse latency / selection margin per leg, in bps
    fallback_slippage_bps: Decimal  # flat impact used ONLY when depth is unknown
    buy_fee_currency: str       # quote | base | unknown
    sell_fee_currency: str      # quote | base | unknown
    def as_dict(self) -> dict

def cost_model(*, configured_fee_pct, configured_slippage_pct, latency_bps, rules: VenueRules,
               account: AccountCommission | None = None) -> CostModel
    # taker_fee = max(configured_fee_pct/100, rules.taker_fee or 0); unknown public fee -> configured only, noted.
    # account (same exchange and symbol as rules, else MoneyError) joins the max: it can raise the fee, never lower it.
    # No engine path passes one.

@dataclass(frozen=True)
class Walk:
    base: Decimal; quote: Decimal; vwap: Decimal | None; levels_used: int
    exhausted: bool; depth_known: bool; worst_price: Decimal | None

def walk_asks(book, *, quote_amount: Decimal, latency_bps: Decimal) -> Walk   # spend quote, receive base
def walk_bids(book, *, base_amount: Decimal, latency_bps: Decimal) -> Walk    # sell base, receive quote
    # every level price is moved against us by latency_bps (asks up, bids down) before use.
    # A level with unknown quantity makes depth_known False and the walk stops there (exhausted=True).

@dataclass(frozen=True)
class SizingResult:
    symbol: str
    eligible: bool
    code: str | None            # dominant reason when not eligible
    detail: str                 # human sentence naming the rule and numbers
    checks: tuple[dict, ...]    # every check: {'check', 'passed', 'code', 'detail', ...values as strings}
    budget_cap: Decimal         # min(configured micro budget, MICRO_MAX_ALL_IN_USDT)
    target_budget: Decimal      # cap reduced by cash and allocation, never increased
    gross_quote: Decimal | None   # market-buy quote amount, rounded DOWN to cost_step
    fee_reserve: Decimal | None   # quote reserved for a quote-billed fee, rounded UP
    max_debit: Decimal | None     # gross_quote + fee_reserve; <= target_budget <= budget_cap
    expected_base: Decimal | None # ask walk result for gross_quote (after latency)
    expected_fee_base: Decimal | None
    sellable_base: Decimal | None # owned after a base fee, rounded DOWN to amount_step
    predicted_residual_base: Decimal | None
    entry_vwap: Decimal | None
    break_even_bid: Decimal | None   # best bid where selling into the bids nets 0 (basis = max_debit)
    target_bid: Decimal | None       # best bid where selling into the bids nets target_net
    required_move_bps: Decimal | None  # (target_bid / best_ask - 1) * 10000
    minimum_feasible_budget: Decimal | None   # separately calculated, display only, never used to size
    minimum_feasible_detail: str
    cost: dict                      # CostModel.as_dict()
    def as_dict(self) -> dict       # Decimals as strings

def size_entry(*, rules: VenueRules, book: BookSnapshot, cost: CostModel, budget_cap: Decimal,
               cash_available: Decimal, allocation_remaining: Decimal, target_net: Decimal,
               exit_stress_bps: Decimal, now_wall: float, max_rules_age_s: float | None = None) -> SizingResult
```

Sizing algorithm (each step is a named check):
1. `budget_cap` must be > 0 and <= `MICRO_MAX_ALL_IN_USDT` (else `BUDGET_INVALID`).
   `target_budget = min(budget_cap, cash_available, allocation_remaining)`.
2. Venue: `rules.tradable('BUY')` and `rules.tradable('SELL')` (an exit must
   be possible) pass; rules age within `max_rules_age_s` when given.
3. Fee reserve: when the buy fee currency is `quote` or `unknown`,
   `gross = q_down(target_budget / (1 + fee), cost_step)` and
   `fee_reserve = q_up(gross * fee, cost_step)`; step down one `cost_step`
   while `gross + fee_reserve > target_budget`. For `base`, `fee_reserve = 0`
   and `gross = q_down(target_budget, cost_step)`. `cost_step` None: use
   `Decimal('0.00000001')` and record the issue.
4. `gross >= rules.market_min_cost()` else `BUDGET_BELOW_VENUE_MINIMUM`
   (detail names the minimum and the source field); `gross <= market_max_cost()`.
   A missing minimum is an issue, not a pass-by-zero: record it and continue.
5. Ask walk for `gross` with `cost.latency_bps`; exhausted is
   `INSUFFICIENT_ASK_DEPTH`; unknown depth is `DEPTH_UNKNOWN` (entries need depth).
6. Base fee: when the buy fee currency is `base` or `unknown`,
   `expected_fee_base = expected_base * fee`; owned = expected_base - fee_base.
   (`unknown` therefore reserves the quote fee AND assumes a base fee: both
   conservative.)
7. `expected_base >= min_amount` when published, else `BUDGET_BELOW_VENUE_MINIMUM`.
8. `sellable = q_down(owned, amount_step)`; residual = owned - sellable.
9. Exit feasibility: `sellable >= min_amount`; `sellable * bid_at_stress >=
   market_min_cost()` with `bid_at_stress = best_bid * (1 - exit_stress_bps/1e4)`;
   bid walk of `sellable` not exhausted (`INSUFFICIENT_BID_DEPTH`). Failure is
   `EXIT_BELOW_VENUE_MINIMUM` (or the depth code): a buy that predictably
   creates unsellable dust is ineligible.
10. `max_debit = gross + fee_reserve`. Invariant: `max_debit <= target_budget <= budget_cap`.
11. Break-even and target bids from the sell leg, with `X` the best bid:
    `net(X) = keep * X - max_debit`, where `keep` is the smaller of
    `sellable * (1 - latency) * (1 - sell_fee)` (everything sold at the best
    bid) and `walk_quote * (1 - sell_fee) / best_bid` (the step-9 bid walk,
    with the whole book moved by `X / best_bid`), so a small best bid with the
    depth far below it is priced as the exit estimate will price it (sell fee
    in quote; a base-billed sell fee reduces the quantity instead).
12. `minimum_feasible_budget`: the smallest budget (to 0.01 USDT) for which
    steps 3-9 pass on this book, found by search over the sizing function
    itself with an upper search bound of `max(100, 20 * market_min_cost)`.
    `None` when no budget in range is feasible. Never used to size an order.

```python
@dataclass(frozen=True)
class ExitEstimate:
    sellable_base: Decimal
    gross_proceeds: Decimal | None
    sell_fee: Decimal | None
    net_proceeds: Decimal | None
    allocated_basis: Decimal          # basis * sellable / owned
    est_net_pnl: Decimal | None       # net_proceeds - allocated_basis
    uncertainty: Decimal              # quote amount: notional * uncertainty_bps / 1e4
    depth_model: str                  # 'book' | 'flat_fallback' | 'unavailable'
    depth_exhausted: bool
    break_even_bid: Decimal | None    # best bid where this estimate's sale nets 0, up to the tick
    target_bid: Decimal | None        # best bid where this estimate's sale nets target_net, up to the tick
    residual_base: Decimal            # owned - sellable (kept, never deleted)
    labels: dict                      # {'proceeds': 'estimated', 'fees': 'modeled', ...}
    def as_dict(self) -> dict

def estimate_exit(*, owned_base: Decimal, basis_quote: Decimal, rules: VenueRules, book: BookSnapshot,
                  cost: CostModel, target_net: Decimal, uncertainty_bps: Decimal) -> ExitEstimate
    # sellable = q_down(owned, amount_step). Book with known depth: bid walk.
    # Unknown depth (ticker top of book): best_bid * (1 - fallback_slippage - latency), depth_model 'flat_fallback'.
    # Never charge the spread twice (bid prices already contain it) and never add flat slippage to a depth walk.
    # break_even_bid / target_bid: keep = net_proceeds / best_bid, so the bids shown are where est_net reaches 0
    # and target_net, up to the tick, in every depth model; labels['bids_priced_by'] is 'bid walk' for a book walk,
    # 'estimated proceeds' for exhausted bids and 'best bid' for a flat (ticker) book.

@dataclass(frozen=True)
class Realized:
    closed_base: Decimal          # base units leaving ownership (sold + base fee on the sale)
    allocated_basis: Decimal
    net_proceeds: Decimal         # gross - quote fee
    realized_net: Decimal         # net_proceeds - allocated_basis - valued third-asset fees
    residual_base: Decimal
    residual_basis: Decimal       # basis - allocated_basis (exact conservation)
    status: str                   # 'confirmed' | 'unconfirmed'
    fee_status: str               # 'observed' | 'simulated' | 'estimated' | 'unknown' | 'reconciled'
    notes: tuple[str, ...]
    def as_dict(self) -> dict

def realize_exit(*, owned_base: Decimal, basis_quote: Decimal, sold_base: Decimal, gross_quote: Decimal,
                 quote_fee: Decimal | None, base_fee: Decimal | None, other_fees: dict[str, Decimal],
                 other_fee_value: Decimal | None, fee_status: str) -> Realized
    # quote_fee None => fee unknown => status 'unconfirmed' (never filled in from a configured rate).
    # other_fees non-empty and other_fee_value None => 'unconfirmed'.
    # closed_base = sold_base + (base_fee or 0) must not exceed owned_base (raise MoneyError).

@dataclass(frozen=True)
class Acquisition:
    filled_base: Decimal; owned_base: Decimal; gross_quote: Decimal; quote_fee: Decimal | None
    base_fee: Decimal | None; debit: Decimal; basis: Decimal; fee_status: str; notes: tuple[str, ...]

def account_entry(*, filled_base, gross_quote, quote_fee, base_fee, other_fees, other_fee_value,
                  fee_status, reserved: Decimal) -> Acquisition
    # debit = gross + quote fee (cash leaving); basis = debit + valued third-asset fee;
    # owned = filled - base fee. debit > reserved raises MoneyError (the cap held or it is a defect).

def simple_break_even_ratio(*, buy_slip, buy_fee, sell_slip, sell_fee) -> Decimal
def simple_target_ratio(*, budget, target_net, buy_slip, buy_fee, sell_slip, sell_fee) -> Decimal
    # the brief's simplified quote-fee algebra, for docs and the sensitivity table only.
def sensitivity(budgets, fee_rates, slips, target_bps) -> list[dict]
```

Fee status vocabulary: `observed` (a real venue reported it), `simulated`
(the paper venue reported it), `modeled` (a configured rate used for an
estimate before execution), `estimated` (a third-asset fee valued by
estimate), `unknown` (missing), `reconciled` (operator-confirmed). Realized
P/L is `confirmed` only when every fee leg is observed, simulated or
reconciled.

## `micro_venue.py`: durable, depth-aware paper venue

```python
SCHEMA = '''CREATE TABLE IF NOT EXISTS paper_venue_orders (
    order_id TEXT PRIMARY KEY, client_order_id TEXT NOT NULL, exchange TEXT NOT NULL,
    symbol TEXT NOT NULL, side TEXT NOT NULL, type TEXT NOT NULL, status TEXT NOT NULL,
    requested_cost TEXT, requested_amount TEXT, filled TEXT NOT NULL, cost TEXT NOT NULL,
    average TEXT, fee_cost TEXT NOT NULL, fee_currency TEXT NOT NULL, liquidity TEXT NOT NULL,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL, timestamp_ms INTEGER NOT NULL,
    book_generation INTEGER, book_sequence INTEGER, info TEXT NOT NULL,
    UNIQUE(exchange, client_order_id))'''
def ensure_schema(db) -> None      # executes SCHEMA (+ index on exchange,symbol,timestamp_ms)

class MicroPaperVenue:
    id = 'paper-micro'
    has = {'createMarketBuyOrderWithCost': True, 'createMarketOrder': True, 'fetchOrder': True,
           'fetchOpenOrders': True, 'fetchClosedOrders': True, 'fetchOrderTrades': False,
           'fetchBalance': True, 'fetchTickers': False}
    rateLimit = 0
    def __init__(self, exchange_name: str, *, rules: Callable[[str], VenueRules],
                 balances: Callable[[], dict], connection: Callable = storage.connection,
                 clock: Callable[[], float] = time.time)
    # ccxt-shaped methods the engine's _submit/_confirm_filled/orders.locate use:
    def market(self, symbol) -> dict
    def create_market_buy_order_with_cost(self, symbol, cost, params) -> dict
    def create_order(self, symbol, type_, side, amount, price=None, params=None) -> dict   # market sells only
    def fetch_order(self, order_id, symbol=None, params=None) -> dict    # by id, or params['clientOrderId']
    def fetch_open_orders(self, symbol=None, since=None, limit=None, params=None) -> list   # always []
    def fetch_closed_orders(self, symbol=None, since=None, limit=None, params=None) -> list
    def fetch_balance(self, params=None) -> dict
    def amount_to_precision(self, symbol, amount) -> str   # truncates to amount_step
    def cost_to_precision(self, symbol, cost) -> str       # truncates to cost_step
    def records(self, symbol: str | None = None) -> list[dict]   # every stored order, for reconciliation
    # failure injection, tests only (each applies to the next create call and then resets):
    reject_next: Exception | None          # raised before anything is stored (definite rejection)
    drop_response_next: bool               # order stored and filled, then ccxt.RequestTimeout raised
    partial_next: Decimal | None           # fill only this fraction of what the book allows; remainder canceled
    late_fill_next: Decimal | None         # report status 'canceled' with a partial fill now, and a larger
                                           # cumulative fill (this fraction) on every later fetch
    latency_s: float
```

`params` for creates must contain `clientOrderId` and `book` (a
`micro_book.BookSnapshot` captured by the engine at decision time) and may
contain `adverse_bps` (Decimal, default 0) and `fee_currency` override for
tests. Rules enforced exactly like a venue (definite rejections raise
`ccxt.InvalidOrder`, nothing stored): unknown symbol (`ccxt.BadSymbol`),
untradable side, buy cost not a multiple of `cost_step` or below
`market_min_cost()` or above `market_max_cost()`, sell amount not a multiple
of `amount_step`, below `min_amount`, or `amount * best_bid <
market_min_cost()`, missing or crossed book, duplicate `clientOrderId` with
different terms (`ccxt.InvalidOrder`; an identical repeat returns the stored
order without filling twice).

Fills: a market buy walks the asks spending `cost` (each level price raised by
`adverse_bps`), a market sell walks the bids (lowered by `adverse_bps`); when
depth runs out the remainder is canceled (`status 'canceled'`, `filled` <
requested). Liquidity is always `taker`; the fee is the higher of `rules.taker_fee` and the (optional
configured fee passed in params as `taker_fee`) and is charged in the quote
asset unless the fee currency is `base` (buys: fee in base, reduces the coins
delivered). Returned dicts are CCXT-shaped with float `filled`, `cost`,
`average`, `remaining`, `amount`, `fee: {'currency','cost'}` and `fees: [...]`,
`status`, `id`, `clientOrderId`, `timestamp`, plus `info: {'simulated': True,
'exact': {'filled','cost','average','fee_cost','fee_currency'} (decimal
strings), 'book_generation', 'book_sequence', 'liquidity': 'taker'}`. The
store is written in its own transaction before the call returns (the venue's
record exists even if the engine crashes before booking). No maker or limit
orders are simulated: `create_order` with any other type raises
`ccxt.NotSupported`.

## `micro_strategy.py`: features, regime, entry candidates and exit policy

All functions are pure. Candles are completed bars only (the caller uses
`strategy.frame_from_ohlcv`, which drops the open candle).

```python
FEATURE_VERSION = 'micro-features-v1'
DECISION_VERSION = 'micro-entry-v1'
WARMUP_BARS_1M = 60
WARMUP_BARS_5M = 50

@dataclass(frozen=True)
class Features:
    symbol: str; bar_ts_ms: int; bars_1m: int; bars_5m: int
    close: float; ema_fast: float; ema_slow: float          # 9 / 21 on 1m closes
    atr_bps: float          # 14-bar ATR on 1m as bps of close
    vol_bps: float          # std of 1m log returns over 30 bars, bps
    efficiency: float       # 20-bar Kaufman efficiency on 1m
    slope_bps: float        # ema_slow change over the last 5 bars, bps of close
    zscore: float           # (close - SMA20) / std20 on 1m
    volume_ratio: float     # last bar volume / 20-bar average
    drop_bps: float         # close vs max close of last 15 bars, bps (<= 0)
    down_bars: int          # consecutive lower closes at the end
    higher_trend_up: bool   # 5m ema20 > ema50 and close > ema50
    higher_slope_bps: float # 5m ema50 change over 6 bars, bps
    def as_dict(self) -> dict

def compute_features(symbol: str, frame_1m, frame_5m) -> tuple[Features | None, str | None]
    # (None, 'WARMUP: ...') when either frame is short; uses only the rows passed (no look-ahead).

REGIMES = ('stale_uncertain', 'illiquid', 'excessively_volatile', 'trending_up', 'trending_down',
           'ranging', 'no_reliable_edge')

@dataclass(frozen=True)
class MicroParams:          # built from settings by the manager; all bounded by storage.BOUNDS
    target_net_bps: float   # minimum meaningful net target, bps of the budget cap
    max_loss_bps: float     # hard cap on estimated net loss per cycle, bps of the budget cap
    max_hold_s: float
    uncertainty_floor_bps: float   # minimum uncertainty margin per exit estimate
    max_spread_bps: float          # entry spread limit
    max_vol_bps: float             # above this 1m volatility the regime is excessively volatile
    min_efficiency_trend: float    # trend regimes need at least this efficiency
    max_efficiency_range: float    # range regime needs at most this efficiency
    edge_ratio: float              # required move must be <= edge_ratio * expected range

def classify_regime(features: Features | None, *, spread_bps, depth_ok: bool, data_fresh: bool,
                    params: MicroParams) -> tuple[str, tuple[str, ...]]

def stop_net_bps(features: Features, params: MicroParams, entry_cost_bps: float = 0.0) -> float | None
    # entry_cost_bps + max(2.0 * atr_bps, 25); None when that sum exceeds max_loss_bps.
    # Revised after review: the original clamp(2 ATR, 25, max_loss) ignored the round-trip cost
    # the position carries from the moment it is bought, so a stop inside that cost fired on the
    # first exit check with the book unchanged. entry_cost_bps = max_debit * (1 - best_bid /
    # break_even_bid) as bps of the budget cap (entry_cost_bps()), from the walk-priced bid of step 11.

@dataclass(frozen=True)
class MicroDecision:
    decision_id: str; symbol: str; decided_at: float; action: str      # 'enter' | 'abstain'
    code: str | None        # dominant reason code
    status: str             # one dashboard-safe sentence
    reasons: tuple[str, ...]; codes: tuple[str, ...]; checks: tuple[dict, ...]
    candidate: str | None   # 'momentum_pullback' | 'range_reversion'
    regime: str
    inputs: dict            # features, book summary, sizing summary, params (replayable)
    proposal: dict | None   # enter only: opportunity_key, target_net, stop_net, max_hold_s, expected_range_bps,
                            #   required_move_bps, heuristic_note
    version: str = DECISION_VERSION
    def as_dict(self) -> dict

def evaluate_entry(*, symbol, features, regime, regime_reasons, sizing: dict, book_summary: dict,
                   params: MicroParams, gates: dict, now: float) -> MicroDecision
```

`sizing` is `SizingResult.as_dict()`; `book_summary` has `best_bid`,
`best_ask`, `spread_bps`, `generation`, `sequence`, `age_s`, `source`;
`gates` has booleans/values decided by the manager: `selected`, `capacity`,
`cooldown_remaining_s`, `fresh_after_exit` (a completed bar newer than the
last exit), `review_block`, `risk_block` (str or None), `latched` (str or None).

Reason codes (used for dashboard counters): `WARMUP`, `STALE_DATA`, `SPREAD`,
`INSUFFICIENT_DEPTH`, `BUDGET_MINIMUM`, `VOLATILITY`, `WEAK_TREND`,
`NO_ECONOMIC_EDGE`, `FEES`, `COOLDOWN`, `CAPACITY`, `NOT_SELECTED`, `REVIEW`,
`RISK`, `ERROR`.

Candidates (hypotheses, not proven strategies):

* `momentum_pullback`: regime `trending_up`; close above `ema_slow`; a
  pullback (`zscore <= 0.5` and close within `1.0 * atr` of `ema_fast`); the
  last bar closed up; no collapse (`drop_bps > -3 * atr_bps`).
* `range_reversion`: regime `ranging`; `zscore <= -1.5`; `down_bars <= 4`;
  `drop_bps > -4 * atr_bps`; `higher_slope_bps >= -5` (not a sustained
  collapse on the 5m frame).

Economic gate (a heuristic feasibility test, never called an expected value):
`expected_range_bps = atr_bps * sqrt(max_hold_s / 60)`; require
`required_move_bps <= edge_ratio * expected_range_bps` (else `NO_ECONOMIC_EDGE`)
and `spread_bps <= 0.35 * required_move_bps` (else `FEES`), and
`max(2 ATR, 25) <= max_loss_bps` (else `VOLATILITY`, or `NO_ECONOMIC_EDGE` when
2 ATR fits and only the 25 bps floor exceeds the cap) and
`stop_net_bps(features, params, entry_cost_bps)` not None (else `NO_ECONOMIC_EDGE`). The opportunity key is
`f'{symbol}|{candidate}|{bar_ts_ms}'`.

```python
@dataclass(frozen=True)
class ExitDecision:
    action: str             # 'exit' | 'hold' | 'cannot_evaluate'
    trigger: str | None     # 'stop_loss' | 'max_hold' | 'regime_shift' | 'take_profit' | 'profit_protection'
    reason: str
    new_peak: Decimal | None
    needs_depth: bool       # True for profit exits
    checks: tuple[dict, ...]
    def as_dict(self) -> dict

def evaluate_exit(*, estimate: dict | None, depth_model: str, target_net: Decimal, stop_net: Decimal,
                  peak_est_net: Decimal | None, opened_wall: float, max_hold_s: float,
                  regime: str | None, now: float, realized_net: Decimal | None = None) -> ExitDecision
```

`estimate` is `ExitEstimate.as_dict()` or None (no fresh data). Order:
no estimate -> `cannot_evaluate`; `est_net + min(realized_net, 0) <= -stop_net`
-> `stop_loss` (`realized_net` is what the cycle's partial exits already
realized: a loss counts against the stop, a gain never loosens it);
`now - opened_wall >= max_hold_s` -> `max_hold`; regime `excessively_volatile`
or `trending_down` with `est_net < 0` -> `regime_shift`; with
`depth_model == 'book'` only: `est_net - uncertainty >= target_net` ->
`take_profit`; `peak >= 0.75 * target_net` and `peak - uncertainty > 0.25 *
target_net` (the band is entered by falling into it, never on the way up) and
`est_net - uncertainty <= 0.25 * target_net` and `est_net - uncertainty > 0` ->
`profit_protection`;
else hold. Safety exits (stop, max hold, regime) never need a profit, an entry
signal or depth; profit exits need a depth-based estimate.

## Cycle state machine (`micro.py`, integration step)

States: `ENTRY_RESERVED`, `ENTRY_SUBMITTED`, `ENTRY_UNKNOWN`, `HOLDING`,
`EXIT_SUBMITTED`, `EXIT_UNKNOWN`, `REVIEW`, `CLOSED`, `ABANDONED`. Terminal:
`CLOSED`, `ABANDONED`. One non-terminal cycle per exchange/mode/symbol (unique
partial index); one cycle per opportunity key (unique index). Order intents
carry `decision_id = cycle_id` so the journal links to the cycle. Booking a
fill, the journal `accounted` transition, the ledger rows, the cycle state and
the reservation release commit in one SQLite transaction. After a partial sale
the rest is judged at the sale's best bid; a holding no sale could be sent for
at the current best bid (below the minimum amount or the minimum order value)
closes at the next exit check (`HOLDING` -> `CLOSED`, no order sent) and is
kept as residual.

## Settings (storage.py, integration step)

`time_horizon` gains `'micro'`. New fields (all in `LATER_FIELDS`):
`micro_budget_usdt` 25 (0.5..25), `micro_max_cycles` 2 (1..4),
`micro_target_net_bps` 20 (5..200), `micro_max_loss_bps` 120 (20..400; 80 before the stop revision, see
docs/ATB_MICRO_ECONOMICS.md),
`micro_max_hold_minutes` 30 (2..240), `micro_cooldown_seconds` 60 (0..3600),
`micro_max_consecutive_losses` 4 (2..20), `micro_daily_loss_limit_usdt` 1.0
(0.1..25), `micro_latency_bps` 3 (0..50), `micro_book_max_age_seconds` 5 (1..30).
