"""Durable, depth-aware paper venue for the micro-trading mode.

:class:`paper_exchange.PaperExchange` fills a whole order at one reference
price. That is adequate for the legacy strategy's sizes and holding periods,
but it hides exactly what decides a trade of a few USDT: how far the order
walks into the book, the taker fee on each leg, and the venue's increments
and minimums. This module is the simulated counterparty of the micro mode. It
fills market orders against the order-book snapshot the engine captured at
decision time, refuses what a venue would refuse, and keeps its own durable
order records, so a crash between the venue accepting an order and the engine
booking it leaves the evidence a real venue would leave: an order that can be
found, read-only, by its client order id.

Semantics:

* PAPER only. Nothing here reads credentials, builds an exchange client or
  opens a socket. Rules come from a callable over the public catalogue
  (:class:`venue_rules.VenueRules`) and the book from the engine.
* Market orders only, always taker. A buy spends a quote amount (``cost``)
  across the asks; a sell sells a base quantity across the bids. Every level
  price is first moved against the order by ``adverse_bps``. A level is taken
  whole while the remaining budget covers it; the last, partly used level is
  bought in whole quantity steps (rounded down) because a venue fills in its
  quantity increment, and the unspent quote is never debited. When the book
  runs out first the remainder is canceled: status ``canceled``, the partial
  fill, and the exact remainder in ``info['exact']``. There is no maker
  simulation, so any other order type raises ``ccxt.NotSupported``.
* A level whose quantity is unknown (a ticker top of book) cannot be filled
  against: the order is refused as missing depth. The book must be the one of
  the order's symbol on this venue's exchange; any other book is refused, so a
  symbol mix-up can never become a fill at another pair's prices.
* Quantization never rounds up. :func:`money.q_down` divides at 34
  significant digits, so its result is checked against the input and lowered
  by one step when the division rounded onto the next boundary; the last level
  of a buy is lowered while it would spend more than the requested cost.
* The fee is a taker fee: the higher of the public taker schedule and the
  configured rate passed as ``params['taker_fee']``, so a paper fill is never
  charged less than either. A maker rate is never used. It is billed in the
  quote asset unless the fee currency is ``base`` (``params['fee_currency']``
  or the rules' side-specific fee currency); a base fee on a buy reduces the
  coins delivered, and ``unknown`` bills the quote asset.
* Definite rejections raise before anything is stored: ``ccxt.BadSymbol`` for
  a pair the catalogue does not list, ``ccxt.InsufficientFunds`` for an order
  the paper ledger's free balance cannot cover (a quote-billed buy's maximum
  fee and a base-billed sell's maximum fee included), ``ccxt.InvalidOrder``
  for every other venue rule (untradable side, an amount off its step, below
  a minimum or above a maximum, a missing, crossed, depth-less or foreign
  book, an unknown fee, a figure the store could not read back). A minimum,
  maximum or balance that is not published is not enforced, and the record
  lists it under ``info['unchecked']`` (missing is never zero).
* Unpublished steps follow the contract. A buy's quote amount is checked
  against the sizing fallback step ``SIZING_FALLBACK_COST_STEP`` (contract
  sizing step 3) and the record says so. A sell with no published quantity
  step fills the quantity as given and lists the step as unchecked, so a held
  position can always be exited. A buy with no quantity step is refused: its
  last level cannot be filled in whole steps.
* Idempotency: a client order id is unique per exchange. An identical repeat
  returns the stored order without filling twice; a repeat with different
  terms is ``ccxt.InvalidOrder``. The repeat check runs before the rules and
  the book are consulted, so a venue state that changed since acceptance can
  never make an accepted order look rejected.
* Durability: the record is committed in its own transaction before a create
  returns or raises a post-acceptance error.
* The book walk is written here independently of the micro economics
  estimator on purpose: the estimator predicts, this module plays the
  counterparty, and a defect in one cannot hide behind the same code in the
  other.

Failure injection (tests only). Each attribute applies to the next create
call and is reset by it: ``reject_next`` is raised before anything is stored;
``drop_response_next`` stores and fills the order, then raises
``ccxt.RequestTimeout``; ``partial_next`` fills only that fraction of what the
book allows and cancels the rest; ``late_fill_next`` stores a fill of that
fraction of what the book allows but answers the create with status
``canceled`` and a smaller partial fill (``LATE_FILL_FIRST_SHARE`` of it), as
when a cancel acknowledgement races a late fill; every later read shows the
larger cumulative fill. The first answer describes only its own fill (levels
used, depth) and does not name the late-fill injection; the stored record
keeps both. ``latency_s`` delays every create's response.
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Callable

import ccxt

import money
import storage
import venue_rules

SCHEMA = '''CREATE TABLE IF NOT EXISTS paper_venue_orders (
    order_id TEXT PRIMARY KEY, client_order_id TEXT NOT NULL, exchange TEXT NOT NULL,
    symbol TEXT NOT NULL, side TEXT NOT NULL, type TEXT NOT NULL, status TEXT NOT NULL,
    requested_cost TEXT, requested_amount TEXT, filled TEXT NOT NULL, cost TEXT NOT NULL,
    average TEXT, fee_cost TEXT NOT NULL, fee_currency TEXT NOT NULL, liquidity TEXT NOT NULL,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL, timestamp_ms INTEGER NOT NULL,
    book_generation INTEGER, book_sequence INTEGER, info TEXT NOT NULL,
    UNIQUE(exchange, client_order_id))'''
INDEX = 'CREATE INDEX IF NOT EXISTS paper_venue_orders_recent ON paper_venue_orders(exchange,symbol,timestamp_ms)'
COLUMNS = ('order_id', 'client_order_id', 'exchange', 'symbol', 'side', 'type', 'status', 'requested_cost',
           'requested_amount', 'filled', 'cost', 'average', 'fee_cost', 'fee_currency', 'liquidity', 'created_at',
           'updated_at', 'timestamp_ms', 'book_generation', 'book_sequence', 'info')
DECIMAL_COLUMNS = ('requested_cost', 'requested_amount', 'filled', 'cost', 'average', 'fee_cost')

# The share of the eventual fill a late-fill injection reports in its first answer.
LATE_FILL_FIRST_SHARE = Decimal('0.5')
# The quote-amount step the sizing contract assumes (and records) when the
# catalogue publishes none; the venue checks a buy against the same step.
SIZING_FALLBACK_COST_STEP = Decimal('0.00000001')
NEEDS_DEPTH = 'paper venue needs a depth book'
# A top-of-book quote has one level of known size and nothing known behind it.
TOP_OF_BOOK_SOURCES = ('ticker', 'stream_ticker')
# venue_rules.normalize records this issue when the catalogue has no such market.
_NOT_LISTED = 'market is not in the catalogue'
_FEE_KINDS = ('quote', 'base')


def ensure_schema(db) -> None:
    """Create the venue's order table and its lookup index when missing."""
    db.execute(SCHEMA)
    db.execute(INDEX)


@dataclass(frozen=True)
class _Fill:
    base: Decimal         # base quantity executed
    quote: Decimal        # quote amount exchanged (spent on a buy, received gross on a sell)
    levels_used: int
    exhausted: bool       # the book ran out before the request was satisfied


@dataclass(frozen=True)
class _Injected:
    reject: Exception | None
    drop_response: bool
    partial: Decimal | None
    late_fill: Decimal | None


def _step_down(value: Decimal, step: Decimal) -> Decimal:
    """``value - step``; refused when 34-digit rounding would not make it smaller."""
    lower = money.sub(value, step)
    if not lower < value:
        raise ccxt.InvalidOrder(f'paper venue cannot lower {money.text(value)} by the step {money.text(step)} '
                                f'exactly')
    return lower


def _floor_step(value: Decimal, step: Decimal) -> Decimal:
    """The largest multiple of ``step`` that is not above ``value``, without exception.

    ``money.q_down`` divides at 34 significant digits, so for an input with
    more digits the quotient can round up onto the next boundary and the
    result exceed ``value`` by one step. The result is compared with the input
    exactly and lowered until it is not above it. A step finer than the
    value's last 34-digit place is refused by ``money.q_down`` with
    ``MoneyError`` rather than looped on.
    """
    result = money.q_down(value, step)
    while result > value:
        result = _step_down(result, step)
    return result


def _walk_asks(asks, *, spend: Decimal, adverse: Decimal, amount_step: Decimal) -> _Fill:
    """Spend at most ``spend`` quote across ``asks`` (best first), each price raised by ``adverse``.

    Every amount added to the running total is compared with ``spend`` as the
    very sum that is kept, so the total can never exceed it, whatever the
    34-digit rounding of a quotient or product does.
    """
    base, quote, used = money.ZERO, money.ZERO, 0
    for price, quantity in asks:
        if quote >= spend:
            break
        paid = money.mul(price, money.add(money.ONE, adverse))
        level_quote = money.mul(paid, quantity)
        if money.add(quote, level_quote) <= spend:
            base, quote, used = money.add(base, quantity), money.add(quote, level_quote), used + 1
            continue
        # Whole quantity steps of this level. The rounded quotient can name one
        # step more than the rest pays for; lower it until the total fits.
        part = _floor_step(money.div(money.sub(spend, quote), paid), amount_step)
        while part > 0 and money.add(quote, money.mul(part, paid)) > spend:
            part = _step_down(part, amount_step)
        if part > 0:
            base, quote, used = money.add(base, part), money.add(quote, money.mul(part, paid)), used + 1
        # The rest is less than one quantity step at this price: precision, not missing depth.
        return _Fill(base, quote, used, exhausted=False)
    return _Fill(base, quote, used, exhausted=quote < spend)


def _walk_bids(bids, *, amount: Decimal, adverse: Decimal) -> _Fill:
    """Sell ``amount`` base across ``bids`` (best first), each price lowered by ``adverse``."""
    remaining, base, quote, used = amount, money.ZERO, money.ZERO, 0
    for price, quantity in bids:
        if remaining <= 0:
            break
        received = money.mul(price, money.sub(money.ONE, adverse))
        take = min(quantity, remaining)
        base, quote = money.add(base, take), money.add(quote, money.mul(take, received))
        remaining, used = money.sub(remaining, take), used + 1
    return _Fill(base, quote, used, exhausted=remaining > 0)


def _levels(book, name: str) -> tuple:
    levels = getattr(book, name, None)
    if not isinstance(levels, (tuple, list)) or not levels:
        raise ccxt.InvalidOrder(f'{NEEDS_DEPTH}: the book has no {name}')
    parsed = []
    for level in levels:
        try:
            price, quantity = level
            price = money.parse(price, field='book price', allow_zero=False)
            quantity = None if quantity is None else money.parse(quantity, field='book quantity', allow_zero=False)
        except (TypeError, ValueError):
            raise ccxt.InvalidOrder('paper venue book has an invalid level') from None
        parsed.append((price, quantity))
    return tuple(parsed)


def _read_book(book, side: str, *, symbol: str, exchange: str, amount: Decimal | None = None) -> tuple[tuple, tuple]:
    """Bids and asks of a usable book of ``symbol`` on ``exchange`` for ``side``.

    Anything else is a definite rejection. A book that does not name this
    symbol and exchange (a mix-up, or a stand-in without the fields) cannot be
    shown to price this order, so it is refused rather than filled against.

    A buy always needs a depth book. A sell (the risk-reducing side) may also
    fill against a top-of-book quote when its one level's stated size covers
    the whole ``amount``: that level is real, and nothing behind it is used.
    A quote too small for the sale is refused, never priced by a guess.
    """
    if book is None:
        raise ccxt.InvalidOrder(NEEDS_DEPTH)
    book_symbol, book_exchange = getattr(book, 'symbol', None), getattr(book, 'exchange', None)
    if book_symbol != symbol or book_exchange != exchange:
        raise ccxt.InvalidOrder(f'paper venue book is for {book_symbol} on {book_exchange}, '
                                f'not {symbol} on {exchange}')
    bids, asks = _levels(book, 'bids'), _levels(book, 'asks')
    if any(lower[0] >= higher[0] for higher, lower in zip(bids, bids[1:])) or any(
            higher[0] <= lower[0] for lower, higher in zip(asks, asks[1:])):
        raise ccxt.InvalidOrder('paper venue book levels are not sorted best first')
    if bids[0][0] >= asks[0][0]:
        raise ccxt.InvalidOrder('paper venue book is crossed or locked')
    walked = asks if side == 'buy' else bids
    if getattr(book, 'depth_known', None) is not True:
        top_of_book = (side == 'sell' and getattr(book, 'source', None) in TOP_OF_BOOK_SOURCES
                       and isinstance(amount, Decimal) and len(bids) == 1 and bids[0][1] is not None
                       and bids[0][1] >= amount)
        if not top_of_book:
            raise ccxt.InvalidOrder(NEEDS_DEPTH)
    elif any(quantity is None for _, quantity in walked):
        raise ccxt.InvalidOrder(NEEDS_DEPTH)
    return bids, asks


def _int_or_none(value) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _iso(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat(timespec='milliseconds')


def _fraction(value, name: str) -> Decimal | None:
    if value is None:
        return None
    try:
        fraction = money.parse(value, field=name)
    except money.MoneyError:
        raise ValueError(f'{name} must be a fraction between 0 and 1') from None
    if fraction > 1:
        raise ValueError(f'{name} must be a fraction between 0 and 1')
    return fraction


def _check_range(value: Decimal, low: Decimal | None, high: Decimal | None, what: str, unchecked: list[str]) -> None:
    if low is None:
        unchecked.append(f'minimum {what} not published')
    elif value < low:
        raise ccxt.InvalidOrder(f'{what} {money.text(value)} is below the venue minimum {money.text(low)}')
    if high is None:
        unchecked.append(f'maximum {what} not published')
    elif value > high:
        raise ccxt.InvalidOrder(f'{what} {money.text(value)} is above the venue maximum {money.text(high)}')


def _check_storable(row: dict) -> None:
    """Refuse a figure the read path (``money.from_text``) would refuse.

    A stored value outside the money bounds would be committed and then make
    every later listing of this exchange's orders raise, so the order is a
    definite rejection instead and nothing is stored.
    """
    for column in DECIMAL_COLUMNS:
        try:
            money.from_text(row[column], field=column, allow_negative=False)
        except money.MoneyError:
            raise ccxt.InvalidOrder(f'paper venue cannot record {column} {row[column]} within the money bounds; '
                                    f'the order is refused') from None


class MicroPaperVenue:
    id = 'paper-micro'
    has = {'createMarketBuyOrderWithCost': True, 'createMarketOrder': True, 'fetchOrder': True,
           'fetchOpenOrders': True, 'fetchClosedOrders': True, 'fetchOrderTrades': False,
           'fetchBalance': True, 'fetchTickers': False}
    rateLimit = 0

    def __init__(self, exchange_name: str, *, rules: Callable[[str], venue_rules.VenueRules],
                 balances: Callable[[], dict], connection: Callable = storage.connection,
                 clock: Callable[[], float] = time.time):
        """``rules`` and ``balances`` are callables so the venue always sees the
        current public catalogue and paper ledger; ``connection`` opens the
        durable store (the application database by default)."""
        if not isinstance(exchange_name, str) or not exchange_name:
            raise ValueError('The paper venue needs an exchange name.')
        self.exchange_name = exchange_name
        self._rules, self._balances = rules, balances
        self._connection, self._clock = connection, clock
        self._injection_lock = threading.Lock()
        self.reject_next: Exception | None = None
        self.drop_response_next = False
        self.partial_next: Decimal | None = None
        self.late_fill_next: Decimal | None = None
        self.latency_s = 0.0

    # --------------------------------------------------------------- markets
    def _rules_for(self, symbol) -> venue_rules.VenueRules:
        rules = None
        if isinstance(symbol, str) and symbol:
            try:
                rules = self._rules(symbol)
            except LookupError:
                rules = None
        if (not isinstance(rules, venue_rules.VenueRules) or rules.symbol != symbol
                or _NOT_LISTED in rules.issues):
            raise ccxt.BadSymbol(f'{self.id} does not have market symbol {symbol}')
        return rules

    def market(self, symbol) -> dict:
        """A CCXT-shaped market built from the venue rules (market-order limits)."""
        rules = self._rules_for(symbol)
        f = money.to_float
        info = {'simulated': True, 'rules': rules.as_dict()}
        if rules.market_orders is not None:
            info['orderTypes'] = ['MARKET'] if rules.market_orders else []
        if rules.market_buy_by_cost is not None:
            info['quoteOrderQtyMarketAllowed'] = rules.market_buy_by_cost
        return {'id': symbol.replace('/', ''), 'symbol': symbol, 'base': rules.base, 'quote': rules.quote,
                'type': 'spot' if rules.spot is True else 'unknown', 'spot': rules.spot, 'active': rules.active,
                'contract': False, 'swap': False, 'future': False, 'option': False,
                'taker': f(rules.taker_fee), 'maker': f(rules.maker_fee),
                'precision': {'amount': f(rules.amount_step), 'price': f(rules.price_tick),
                              'cost': f(rules.cost_step)},
                'limits': {'amount': {'min': f(rules.min_amount), 'max': f(rules.max_amount)},
                           'cost': {'min': f(rules.market_min_cost()), 'max': f(rules.market_max_cost())}},
                'info': info}

    def amount_to_precision(self, symbol, amount) -> str:
        """``amount`` truncated (never rounded up) to the quantity step; like CCXT, zero is InvalidOrder.

        With no published step there is nothing to truncate to: the amount is
        returned as given, as a sell then fills it and lists the step as unchecked.
        """
        step = self._rules_for(symbol).amount_step
        value = self._number(amount, 'amount', allow_zero=True)
        if step is not None:
            value = _floor_step(value, step)
        if value == 0:
            raise ccxt.InvalidOrder(f'{symbol} amount is below the quantity step '
                                    f'{money.text(step) if step is not None else "(unpublished)"}')
        return money.text(value)

    def cost_to_precision(self, symbol, cost) -> str:
        """``cost`` truncated (never rounded up) to the quote-amount step of a market buy.

        With no published step, the sizing fallback step is used, as by the buy itself.
        """
        step = self._rules_for(symbol).cost_step
        if step is None:
            step = SIZING_FALLBACK_COST_STEP
        return money.text(_floor_step(self._number(cost, 'cost', allow_zero=True), step))

    # --------------------------------------------------------------- account
    def fetch_balance(self, params=None) -> dict:
        free = dict(self._balances())
        return {'free': free, 'used': {asset: 0.0 for asset in free}, 'total': dict(free), 'info': {'simulated': True}}

    # ---------------------------------------------------------------- orders
    @staticmethod
    def _number(value, field: str, *, allow_zero: bool = False) -> Decimal:
        try:
            return money.parse(value, field=field, allow_zero=allow_zero)
        except money.MoneyError as exc:
            raise ccxt.InvalidOrder(f'paper venue {exc}') from None

    def _take_injections(self) -> _Injected:
        with self._injection_lock:
            reject, drop, partial, late = (self.reject_next, self.drop_response_next, self.partial_next,
                                           self.late_fill_next)
            self.reject_next, self.drop_response_next = None, False
            self.partial_next = self.late_fill_next = None
        taken = _Injected(reject, bool(drop), _fraction(partial, 'partial_next'), _fraction(late, 'late_fill_next'))
        if taken.partial is not None and taken.late_fill is not None:
            raise ValueError('partial_next and late_fill_next cannot both shape one fill')
        return taken

    def create_market_buy_order_with_cost(self, symbol, cost, params=None) -> dict:
        injected = self._take_injections()
        if injected.reject is not None:
            raise injected.reject
        return self._create(symbol, 'buy', self._number(cost, 'cost'), params, injected)

    def create_order(self, symbol, type_, side, amount, price=None, params=None) -> dict:
        """Market sells only: buys go by quote amount, and there is no maker simulation."""
        injected = self._take_injections()
        if injected.reject is not None:
            raise injected.reject
        if type_ != 'market':
            raise ccxt.NotSupported('the micro paper venue simulates market (taker) orders only')
        if side != 'sell':
            raise ccxt.NotSupported('the micro paper venue buys by quote amount only')
        return self._create(symbol, 'sell', self._number(amount, 'amount'), params, injected)

    def _create(self, symbol, side: str, requested: Decimal, params, injected: _Injected) -> dict:
        if not isinstance(params, dict):
            raise ccxt.InvalidOrder('paper venue orders need params with clientOrderId and book')
        client_id = params.get('clientOrderId')
        if not isinstance(client_id, str) or not client_id.strip() or len(client_id) > 64:
            raise ccxt.InvalidOrder('paper venue orders need a clientOrderId of 1 to 64 characters')
        terms = {'symbol': symbol, 'side': side, 'type': 'market',
                 'requested_cost': requested if side == 'buy' else None,
                 'requested_amount': requested if side == 'sell' else None}
        lookup = ('WHERE exchange=? AND client_order_id=?', (self.exchange_name, client_id))
        # A repeat is answered from the store before the rules or the book are
        # consulted: a venue state that changed since acceptance must never make
        # an accepted order look rejected.
        existing, first_report = self._read(*lookup), None
        if not existing:
            # Validation and the fill run before the write lock is taken, so the
            # rules callable never runs while this venue holds the database.
            row, first_report = self._accept(symbol, side, requested, client_id, params, injected)
            with self._connection() as db:
                ensure_schema(db)
                if not getattr(db, 'in_transaction', False):
                    db.execute('BEGIN IMMEDIATE')
                # Checked again under the write lock: two creates with one client
                # order id cannot both fill.
                existing = self._select(db, *lookup)
                if not existing:
                    db.execute(f'INSERT INTO paper_venue_orders ({",".join(COLUMNS)}) '
                               f'VALUES ({",".join("?" * len(COLUMNS))})', tuple(row[column] for column in COLUMNS))
            # Committed: the venue's record now exists whatever happens to the response.
        if existing:
            stored, first_report = existing[0], None
            if any(stored[key] != value for key, value in terms.items()):
                raise ccxt.InvalidOrder(f'clientOrderId {client_id} was already used for a different order')
        else:
            stored = self._record(row)
        if self.latency_s and self.latency_s > 0:
            time.sleep(self.latency_s)
        if injected.drop_response:
            raise ccxt.RequestTimeout(f'{self.id} response lost after the order was accepted')
        return self._view(stored, report=first_report)

    def _accept(self, symbol, side: str, requested: Decimal, client_id: str, params: dict,
                injected: _Injected) -> tuple[dict, dict | None]:
        """Validate like a venue and fill against the book; returns the row and any first report."""
        rules = self._rules_for(symbol)
        ok, code, detail = rules.tradable(side.upper())
        if not ok:
            raise ccxt.InvalidOrder(f'{code}: {detail}')
        book = params.get('book')
        bids, asks = _read_book(book, side, symbol=symbol, exchange=self.exchange_name,
                                amount=requested if side == 'sell' else None)
        adverse_bps = self._number(params.get('adverse_bps') if params.get('adverse_bps') is not None else 0,
                                   'adverse_bps', allow_zero=True)
        if adverse_bps >= money.BPS:
            raise ccxt.InvalidOrder('paper venue adverse_bps must be below 10000')
        adverse = money.bps(adverse_bps)
        amount_step, cost_step = rules.amount_step, rules.cost_step
        unchecked: list[str] = []
        if side == 'buy':
            if amount_step is None:
                raise ccxt.InvalidOrder(f'{symbol} quantity step is unknown; the paper venue cannot fill a buy '
                                        f'in whole steps')
            if cost_step is None:
                cost_step = SIZING_FALLBACK_COST_STEP
                unchecked.append(f'quote-amount step not published; checked against the sizing fallback '
                                 f'{money.text(cost_step)}')
            if _floor_step(requested, cost_step) != requested:
                raise ccxt.InvalidOrder(f'cost {money.text(requested)} is not a multiple of the quote-amount step '
                                        f'{money.text(cost_step)}')
            _check_range(requested, rules.market_min_cost(), rules.market_max_cost(), 'market order amount', unchecked)
        else:
            if amount_step is None:
                # An exit must stay possible: the quantity is filled as given and the record says so.
                unchecked.append('quantity step not published; the quantity is filled as given')
            elif _floor_step(requested, amount_step) != requested:
                raise ccxt.InvalidOrder(f'amount {money.text(requested)} is not a multiple of the quantity step '
                                        f'{money.text(amount_step)}')
            _check_range(requested, rules.min_amount, rules.max_amount, 'quantity', unchecked)
            _check_range(money.mul(requested, bids[0][0]), rules.market_min_cost(), rules.market_max_cost(),
                         'market order amount at the best bid', unchecked)
        fee_rate, fee_source = self._fee_rate(rules, params.get('taker_fee'))
        fee_kind = params.get('fee_currency')
        if fee_kind is None:
            fee_kind = rules.buy_fee_currency if side == 'buy' else rules.sell_fee_currency
            fee_kind = fee_kind if fee_kind in _FEE_KINDS else 'quote'
        elif fee_kind not in _FEE_KINDS:
            raise ccxt.InvalidOrder("paper venue fee_currency must be 'quote' or 'base'")
        self._check_funds(rules, side, requested, fee_rate, fee_kind, unchecked)

        def walk(limit: Decimal) -> _Fill:
            if side == 'buy':
                return _walk_asks(asks, spend=limit, adverse=adverse, amount_step=amount_step)
            return _walk_bids(bids, amount=limit, adverse=adverse)

        def share(fill: _Fill, fraction: Decimal) -> Decimal:
            if side == 'buy':
                return _floor_step(money.mul(fill.quote, fraction), cost_step)
            portion = money.mul(fill.base, fraction)
            return portion if amount_step is None else _floor_step(portion, amount_step)

        full = walk(requested)
        limit = injected.partial if injected.partial is not None else injected.late_fill
        if full.base == 0 and limit is None:
            raise ccxt.InvalidOrder(f'cost {money.text(requested)} buys less than one quantity step')
        if limit is not None and limit < 1:
            fill, canceled = walk(share(full, limit)), True
        else:
            fill, canceled = full, full.exhausted
        figures = self._figures(rules, side, requested, fill, 'canceled' if canceled else 'closed', fee_rate, fee_kind)
        first_report = None
        if injected.late_fill is not None:
            first_fill = walk(share(fill, LATE_FILL_FIRST_SHARE))
            first_report = self._figures(rules, side, requested, first_fill, 'canceled', fee_rate, fee_kind)
            # The first answer describes its own walk, not the larger fill stored for later reads.
            first_report.update(levels_used=first_fill.levels_used, depth_exhausted=first_fill.exhausted)
        now = float(self._clock())
        generation, sequence = _int_or_none(getattr(book, 'generation', None)), _int_or_none(
            getattr(book, 'venue_sequence', None))
        injected_names = [name for name, active in (('drop_response_next', injected.drop_response),
                                                    ('partial_next', injected.partial is not None),
                                                    ('late_fill_next', injected.late_fill is not None)) if active]
        info = {'simulated': True, 'liquidity': 'taker', 'book_generation': generation, 'book_sequence': sequence,
                'book_source': getattr(book, 'source', None),
                'depth_known': getattr(book, 'depth_known', None) is True,
                'fee_rate': money.text(fee_rate), 'fee_source': fee_source, 'fee_basis': fee_kind,
                'adverse_bps': money.text(adverse_bps), 'levels_used': fill.levels_used,
                'depth_exhausted': full.exhausted, 'best_bid': money.text(bids[0][0]),
                'best_ask': money.text(asks[0][0]), 'unchecked': unchecked, 'injected': injected_names,
                'late_fill': {'first_report': {key: (money.text(value) if isinstance(value, Decimal) else value)
                                               for key, value in first_report.items()}} if first_report else None}
        row = {'order_id': f'{self.id}-{uuid.uuid4().hex}', 'client_order_id': client_id,
               'exchange': self.exchange_name, 'symbol': symbol, 'side': side, 'type': 'market',
               'status': figures['status'], 'requested_cost': money.text(requested) if side == 'buy' else None,
               'requested_amount': money.text(requested) if side == 'sell' else None,
               'filled': money.text(figures['filled']), 'cost': money.text(figures['cost']),
               'average': money.text(figures['average']), 'fee_cost': money.text(figures['fee_cost']),
               'fee_currency': figures['fee_currency'], 'liquidity': 'taker',
               'created_at': _iso(now), 'updated_at': _iso(now), 'timestamp_ms': int(now * 1000),
               'book_generation': generation, 'book_sequence': sequence,
               'info': json.dumps(info, sort_keys=True, allow_nan=False)}
        _check_storable(row)
        return row, first_report

    def _check_funds(self, rules: venue_rules.VenueRules, side: str, requested: Decimal, fee_rate: Decimal,
                     fee_kind: str, unchecked: list[str]) -> None:
        """Refuse what the paper ledger's free balance cannot cover, as a venue would.

        A buy spends the quote asset and a sell the base asset; a fee billed in
        the spent asset is added at its maximum (the whole request at the
        charged rate). A balance the ledger does not report is unknown, not
        zero: the order is not refused for it and the record says so.
        """
        asset = rules.quote if side == 'buy' else rules.base
        needed = requested
        if (side == 'buy') == (fee_kind == 'quote'):
            needed = money.add(requested, money.mul(requested, fee_rate))
        balances = self._balances()
        raw = balances.get(asset) if isinstance(balances, dict) else None
        if raw is None:
            unchecked.append(f'free {asset} balance not known')
            return
        try:
            free = money.parse(raw, field=f'free {asset} balance', allow_negative=True)
        except money.MoneyError:
            unchecked.append(f'free {asset} balance is malformed and not checked')
            return
        if needed > free:
            raise ccxt.InsufficientFunds(f'{side} needs {money.text(needed)} {asset} including the maximum fee; '
                                         f'the paper balance has {money.text(free)} {asset} free')

    @staticmethod
    def _fee_rate(rules: venue_rules.VenueRules, configured) -> tuple[Decimal, str]:
        """The taker rate charged: never below the public taker schedule or the configured rate."""
        configured_rate = None
        if configured is not None:
            configured_rate = MicroPaperVenue._number(configured, 'taker_fee', allow_zero=True)
        known = [rate for rate in (rules.taker_fee, configured_rate) if rate is not None]
        if not known:
            raise ccxt.InvalidOrder(f'{rules.symbol} taker fee is unknown; pass the configured rate as taker_fee')
        rate = max(known)
        if rate >= 1:
            raise ccxt.InvalidOrder('paper venue taker fee must be a fraction below 1')
        if configured_rate is None:
            source = f'public taker {money.text(rules.taker_fee)}'
        elif rules.taker_fee is None:
            source = f'configured taker {money.text(configured_rate)} (public schedule unknown)'
        else:
            source = (f'max(configured taker {money.text(configured_rate)}, '
                      f'public taker {money.text(rules.taker_fee)})')
        return rate, source

    @staticmethod
    def _figures(rules, side: str, requested: Decimal, fill: _Fill, status: str, fee_rate: Decimal,
                 fee_kind: str) -> dict:
        fee_cost = money.mul(fill.base if fee_kind == 'base' else fill.quote, fee_rate)
        return {'status': status, 'filled': fill.base, 'cost': fill.quote,
                'average': money.div(fill.quote, fill.base) if fill.base > 0 else None,
                'fee_cost': fee_cost, 'fee_currency': rules.base if fee_kind == 'base' else rules.quote,
                'remaining': money.sub(requested, fill.base) if side == 'sell' else None,
                'remaining_cost': money.sub(requested, fill.quote) if side == 'buy' else None}

    # ------------------------------------------------------------- records
    @staticmethod
    def _select(db, where: str, args: tuple) -> list[dict]:
        cursor = db.execute(f'SELECT {",".join(COLUMNS)} FROM paper_venue_orders {where}', args)
        names = [column[0] for column in cursor.description]
        return [MicroPaperVenue._record(dict(zip(names, row))) for row in cursor.fetchall()]

    @staticmethod
    def _record(row: dict) -> dict:
        """A stored row with exact Decimal amounts and the decoded info."""
        record = dict(row)
        for column in DECIMAL_COLUMNS:
            record[column] = money.from_text(record[column], field=column, allow_negative=False)
        record['info'] = json.loads(record['info']) if isinstance(record['info'], str) else dict(record['info'])
        return record

    def _view(self, record: dict, *, report: dict | None = None) -> dict:
        """The CCXT-shaped order; ``report`` replaces the fill figures for a late-fill first answer."""
        side = record['side']
        first_answer = report is not None
        if report is None:
            requested = record['requested_cost'] if side == 'buy' else record['requested_amount']
            report = {'status': record['status'], 'filled': record['filled'], 'cost': record['cost'],
                      'average': record['average'], 'fee_cost': record['fee_cost'],
                      'fee_currency': record['fee_currency'],
                      'remaining': money.sub(requested, record['filled']) if side == 'sell' else None,
                      'remaining_cost': money.sub(requested, record['cost']) if side == 'buy' else None}
        status, filled = report['status'], report['filled']
        if side == 'sell':
            amount, remaining = record['requested_amount'], report['remaining']
        else:
            # A buy by quote amount has no base quantity until it is complete; the
            # unbought base of a canceled buy is unknown, not zero.
            amount = filled if status == 'closed' else None
            remaining = money.ZERO if status == 'closed' else None
        info = {key: value for key, value in record['info'].items() if key != 'late_fill'}
        if first_answer:
            # A late-fill first answer gives no hint of the later fill: its own
            # walk's figures, and no mention of the injection.
            info.update(levels_used=report['levels_used'], depth_exhausted=report['depth_exhausted'],
                        injected=[name for name in info.get('injected', ()) if name != 'late_fill_next'])
        info['exact'] = {key: money.text(report[key]) for key in ('filled', 'cost', 'average', 'fee_cost')}
        info['exact'].update(fee_currency=report['fee_currency'], remaining=money.text(report['remaining']),
                             remaining_cost=money.text(report['remaining_cost']),
                             requested_cost=money.text(record['requested_cost']),
                             requested_amount=money.text(record['requested_amount']))
        fee = {'currency': report['fee_currency'], 'cost': money.to_float(report['fee_cost']),
               'rate': float(info['fee_rate'])}
        timestamp = record['timestamp_ms']
        average = money.to_float(report['average'])
        return {'id': record['order_id'], 'clientOrderId': record['client_order_id'], 'symbol': record['symbol'],
                'type': record['type'], 'side': side, 'status': status, 'timestamp': timestamp,
                'datetime': record['created_at'], 'lastTradeTimestamp': timestamp if filled > 0 else None,
                'price': average, 'average': average, 'amount': money.to_float(amount),
                'filled': money.to_float(filled), 'remaining': money.to_float(remaining),
                'cost': money.to_float(report['cost']), 'fee': fee, 'fees': [dict(fee)], 'trades': [],
                'info': info}

    def _read(self, where: str, args: tuple) -> list[dict]:
        with self._connection() as db:
            ensure_schema(db)
            return self._select(db, where, args)

    def fetch_order(self, order_id, symbol=None, params=None) -> dict:
        """By venue id, or by ``params['clientOrderId']``; read-only."""
        found = []
        if order_id:
            found = self._read('WHERE exchange=? AND order_id=?', (self.exchange_name, str(order_id)))
        client_id = (params or {}).get('clientOrderId')
        if not found and isinstance(client_id, str) and client_id:
            found = self._read('WHERE exchange=? AND client_order_id=?', (self.exchange_name, client_id))
        if not found or (symbol is not None and found[0]['symbol'] != symbol):
            raise ccxt.OrderNotFound(f'{self.id} order {order_id or client_id} not found')
        return self._view(found[0])

    def fetch_open_orders(self, symbol=None, since=None, limit=None, params=None) -> list:
        return []   # every simulated order is final when its create returns

    def fetch_closed_orders(self, symbol=None, since=None, limit=None, params=None) -> list:
        """Final orders, oldest first; ``limit`` keeps the most recent ones."""
        where, args = 'WHERE exchange=?', [self.exchange_name]
        if symbol is not None:
            where, args = where + ' AND symbol=?', args + [symbol]
        if since is not None:
            where, args = where + ' AND timestamp_ms>=?', args + [int(since)]
        found = [self._view(record) for record in self._read(where + ' ORDER BY timestamp_ms, rowid', tuple(args))]
        if limit is not None:
            found = found[-int(limit):] if int(limit) > 0 else []
        return found

    def records(self, symbol: str | None = None, *, since_ms: int | None = None) -> list[dict]:
        """Every stored order of this exchange (optionally one pair, optionally from ``since_ms``), exact,
        oldest first, for reconciliation. The time filter runs in SQL on the (exchange, symbol, time) index,
        so a periodic check reads only its window however long the venue history grows."""
        where, args = 'WHERE exchange=?', [self.exchange_name]
        if symbol is not None:
            where, args = where + ' AND symbol=?', args + [symbol]
        if since_ms is not None:
            where, args = where + ' AND timestamp_ms>=?', args + [int(since_ms)]
        return self._read(where + ' ORDER BY timestamp_ms, rowid', tuple(args))
