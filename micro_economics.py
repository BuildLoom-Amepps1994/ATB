"""Sizing, cost and accounting for the paper-only micro-trading mode.

A micro cycle spends at most ``MICRO_MAX_ALL_IN_USDT``. At that size a venue
minimum, a quantity step or one cent of quote rounding is a visible share of
the order, so one component owns every number that decides whether a small
order is worth sending and what it did: the cost model, the order-book walk,
the sizing of an entry, the exit estimate and the accounting of fills. Keeping
them together is what lets the break-even bid shown before entry, the
estimate used to exit and the realized result afterwards apply each cost
exactly once.

Semantics:

* Every amount is a Decimal from :mod:`money`. Missing is unknown (None), never
  zero. A fee that was not reported is never filled in from a configured
  rate in a realized result: the record is ``unconfirmed`` and says so.
* The cap is all-in. The quote spent plus the quote reserved for a fee never
  exceeds ``min(budget, cash, allocation)``, which never exceeds the cap.
  Nothing here sizes up to reach a venue minimum, to use spare cash or to
  compound gains. A minimum above the budget makes the pair ineligible; the
  smallest feasible budget is computed separately and is display only.
* Costs are counted once. Executable bid and ask prices already contain the
  spread, and walking the book already contains the depth impact, so a walk
  only moves each level price against us by the latency margin and never adds
  flat slippage. The flat fallback slippage applies only where depth is
  unknown, and only to exit estimates: an entry needs depth. Beyond the last
  visible bid depth is unknown too, so an exit the visible bids cannot absorb
  prices the rest with the fallback, from the worst visible bid, and is never
  labelled a book estimate (profit exits need one; safety exits need a figure).
* An unknown fee currency is sized both ways (a quote fee reserved and a base
  fee assumed), the conservative reading of each.
* Functions are pure: rules, books, cash and ``now`` are explicit inputs. A
  book is any object with ``bids`` and ``asks`` (``(price, quantity or None)``
  pairs, best first), ``depth_known``, ``best_bid`` and ``best_ask``, and
  optionally ``exchange`` and ``symbol``, which must then match the rules. Its
  levels are validated again here, because a corrupt or hostile level must not
  reach the arithmetic even if it got past the book module.
* ``now`` is a finite wall-clock reading. A missing or non-finite clock, a
  malformed age limit, or rules stamped later than ``now`` make the rules' age
  unknown, which fails the staleness gate instead of skipping it.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field, fields
from decimal import Decimal

import money
from money import BPS, ONE, ZERO, MoneyError
from venue_rules import METADATA_STALE, AccountCommission

MICRO_MAX_ALL_IN_USDT = Decimal('25')     # absolute cap of the micro profile; never raised by code
# Sizing obeys the smaller of this literal and the public name, so rebinding the
# public constant (a research script, a test) can lower the cap but never raise it.
_CAP_LITERAL = Decimal('25')

# Reason codes. Venue codes from venue_rules.tradable() pass through unchanged.
BUDGET_BELOW_VENUE_MINIMUM = 'BUDGET_BELOW_VENUE_MINIMUM'
EXIT_BELOW_VENUE_MINIMUM = 'EXIT_BELOW_VENUE_MINIMUM'
INSUFFICIENT_ASK_DEPTH = 'INSUFFICIENT_ASK_DEPTH'
INSUFFICIENT_BID_DEPTH = 'INSUFFICIENT_BID_DEPTH'
DEPTH_UNKNOWN = 'DEPTH_UNKNOWN'
CASH_INSUFFICIENT = 'CASH_INSUFFICIENT'
ALLOCATION_EXHAUSTED = 'ALLOCATION_EXHAUSTED'
MAX_COST_EXCEEDED = 'MAX_COST_EXCEEDED'
PRICE_UNKNOWN = 'PRICE_UNKNOWN'
FEE_UNKNOWN = 'FEE_UNKNOWN'
BUDGET_INVALID = 'BUDGET_INVALID'

FEE_CURRENCIES = ('quote', 'base', 'unknown')
FEE_STATUSES = ('observed', 'simulated', 'modeled', 'estimated', 'unknown', 'reconciled')
CONFIRMING_FEE_STATUSES = ('observed', 'simulated', 'reconciled')
DEPTH_MODELS = ('book', 'flat_fallback', 'unavailable')

FALLBACK_STEP = Decimal('0.00000001')     # used when the venue publishes no quote or quantity step
ALLOCATION_STEP = Decimal('1E-18')        # basis allocations are cut here so residual = basis - allocated is exact
SEARCH_STEP = Decimal('0.01')
SEARCH_FLOOR_USDT = Decimal('100')
SEARCH_MIN_COST_MULTIPLE = Decimal('20')
MAX_BOOK_LEVELS = 1000
HUNDRED = Decimal(100)

# Failures that a larger budget can cure; every other sizing failure gets no
# better (depth, maximums) or never changes (unknown depth, a refused book).
_SMALL_BUDGET_CODES = (BUDGET_BELOW_VENUE_MINIMUM, EXIT_BELOW_VENUE_MINIMUM)


# --------------------------------------------------------------------- helpers
def _cap() -> Decimal:
    return min(MICRO_MAX_ALL_IN_USDT, _CAP_LITERAL)


def _fraction(value, name: str) -> Decimal:
    """A rate as a fraction in [0, 1), so ``1 - rate`` stays positive."""
    number = money.parse(value, field=name)
    if number >= ONE:
        raise MoneyError(f'{name} must be below 100%')
    return number


def _bps_rate(value, name: str) -> Decimal:
    number = money.parse(value, field=name)
    if number >= BPS:
        raise MoneyError(f'{name} must be below 10000 bps')
    return number


def _show(value) -> str:
    """A compact human number for details; exact when it has at most 8 places."""
    if value is None:
        return 'unknown'
    shown = money.display(value, places=8)
    if '.' in shown and '<' not in shown:
        shown = shown.rstrip('0').rstrip('.')
    return shown


def _value(value):
    if isinstance(value, Decimal):
        return money.text(value)
    if value is None or isinstance(value, (bool, str)):
        return value
    return str(value)


def _record(obj) -> dict:
    """A frozen record as a dict with Decimals as strings (also inside tuples and dicts)."""
    def convert(value):
        if isinstance(value, Decimal):
            return money.text(value)
        if isinstance(value, tuple):
            return [convert(item) for item in value]
        if isinstance(value, dict):
            return {key: convert(item) for key, item in value.items()}
        return value
    return {item.name: convert(getattr(obj, item.name)) for item in fields(obj)}


def _clock(value) -> float | None:
    """A wall-clock reading or duration as a finite float; None when missing, boolean, text or not finite."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) else None


def _rules_age(rules, now_wall, max_age) -> tuple[bool, str, float | None, float | None]:
    """``(ok, detail, age_s, limit_s)`` for the step-2 staleness gate.

    The clock is required even without a limit (the contract makes it an
    input). With a limit, an age that cannot be established (no load time,
    rules stamped after ``now``) is stale, never zero.
    """
    now, loaded = _clock(now_wall), _clock(rules.loaded_at)
    age = now - loaded if now is not None and loaded is not None and now >= loaded else None
    if now is None:
        return False, ('The current time is missing or not a finite number, so the age of the market rules '
                       'cannot be checked'), None, None
    if max_age is None:
        return True, '', age, None
    limit = _clock(max_age)
    if limit is None or limit < 0:
        return False, 'The market-rules age limit is not a finite, non-negative number of seconds', age, None
    if loaded is None:
        return False, f'{rules.symbol} market rules have no valid load time, so their age is unknown', None, limit
    if now < loaded:
        return False, (f'{rules.symbol} market rules are stamped {loaded - now:.3f} s after the current time '
                       '(clock or units mismatch), so their age is unknown'), None, limit
    if age > limit:
        return False, f'{rules.symbol} market rules are {age:.0f} s old, older than the {limit:.0f} s limit', age, limit
    return True, '', age, limit


def _tick_up(value: Decimal, tick: Decimal | None) -> Decimal:
    """The first venue price at or above ``value``: the bid that is actually needed."""
    return value if tick is None else money.q_up(value, tick)


def _allocate(basis: Decimal, part: Decimal, whole: Decimal) -> Decimal:
    """Basis carried by ``part`` of ``whole`` units, pro rata; all of it when the whole closes."""
    if part > whole:
        raise MoneyError('cannot allocate basis to more units than are owned')
    if part == 0:
        return ZERO
    if part == whole:
        return basis
    return money.q_down(money.div(money.mul(basis, part), whole), ALLOCATION_STEP)


def _conserved(basis: Decimal, allocated: Decimal) -> Decimal:
    residual = money.sub(basis, allocated)
    if money.add(allocated, residual) != basis:
        raise MoneyError('basis allocation is not exact at this precision')
    return residual


# ------------------------------------------------------------------ cost model
@dataclass(frozen=True)
class CostModel:
    taker_fee: Decimal              # fraction per leg used for estimates (modeled)
    fee_source: str                 # e.g. 'modeled: max(configured 0.1%, public taker 0.05%)'
    latency_bps: Decimal            # adverse latency / selection margin per leg, in bps
    fallback_slippage_bps: Decimal  # flat impact used ONLY when depth is unknown
    buy_fee_currency: str           # quote | base | unknown
    sell_fee_currency: str          # quote | base | unknown

    def __post_init__(self):
        # Bound every rate once here, so every consumer can rely on 1 - rate > 0.
        object.__setattr__(self, 'taker_fee', _fraction(self.taker_fee, 'taker fee'))
        latency = _bps_rate(self.latency_bps, 'latency margin')
        slippage = _bps_rate(self.fallback_slippage_bps, 'fallback slippage')
        if money.add(latency, slippage) >= BPS:
            raise MoneyError('latency margin plus fallback slippage must be below 10000 bps')
        object.__setattr__(self, 'latency_bps', latency)
        object.__setattr__(self, 'fallback_slippage_bps', slippage)
        for name in ('buy_fee_currency', 'sell_fee_currency'):
            if getattr(self, name) not in FEE_CURRENCIES:
                raise MoneyError(f'{name} must be quote, base or unknown')

    def as_dict(self) -> dict:
        return _record(self)


def cost_model(*, configured_fee_pct, configured_slippage_pct, latency_bps, rules, account=None) -> CostModel:
    """The per-leg cost assumptions for one symbol.

    The modeled fee is the larger of the configured rate and the venue's public
    taker schedule. An unknown public fee leaves the configured rate, and the
    source says so. The configured slippage percent becomes the flat fallback,
    used only where depth is unknown.

    ``account`` is this account's own rate for the symbol, a
    :class:`venue_rules.AccountCommission` parsed from a supplied response of
    the signed endpoint (no code here obtains one, and no caller passes one
    yet). It must name the same exchange and symbol as ``rules``, and its taker
    rate joins the maximum: a known account rate can raise the modeled fee,
    never lower it below the configured or public rate.
    """
    configured_pct = money.parse(configured_fee_pct, field='configured fee percent')
    configured = money.div(configured_pct, HUNDRED)
    slippage_pct = money.parse(configured_slippage_pct, field='configured slippage percent')
    public = money.parse_optional(rules.taker_fee, field='public taker fee')
    if public is None:
        fee = configured
        source = f'modeled: configured {_show(configured_pct)}% (public taker fee unknown)'
    else:
        fee = max(configured, public)
        source = f'modeled: max(configured {_show(configured_pct)}%, public taker {_show(money.mul(public, HUNDRED))}%)'
    if account is not None:
        if not isinstance(account, AccountCommission):
            raise MoneyError('account fee must be a parsed AccountCommission')
        if (account.exchange, account.symbol) != (rules.exchange, rules.symbol):
            raise MoneyError(f'account fee is for {account.exchange} {account.symbol}, not {rules.exchange} {rules.symbol}')
        own = money.parse(account.taker, field='account taker fee')
        fee = max(fee, own)
        source = f'{source}; account taker {_show(money.mul(own, HUNDRED))}% joins the maximum'

    def currency(value):
        return value if value in FEE_CURRENCIES else 'unknown'
    return CostModel(taker_fee=fee, fee_source=source, latency_bps=latency_bps,
                     fallback_slippage_bps=money.mul(slippage_pct, HUNDRED),
                     buy_fee_currency=currency(rules.buy_fee_currency),
                     sell_fee_currency=currency(rules.sell_fee_currency))


# ------------------------------------------------------------------ book walks
@dataclass(frozen=True)
class _Book:
    bids: tuple
    asks: tuple
    depth_known: bool
    best_bid: Decimal
    best_ask: Decimal
    exchange: str | None
    symbol: str | None


def _levels(raw, side: str) -> tuple:
    if raw is None or isinstance(raw, (str, bytes, dict)) or not hasattr(raw, '__iter__'):
        raise MoneyError(f'{side} levels are not a sequence')
    levels: list[tuple[Decimal, Decimal | None]] = []
    for level in raw:
        if len(levels) >= MAX_BOOK_LEVELS:
            raise MoneyError(f'{side} side has more than {MAX_BOOK_LEVELS} levels')
        if not isinstance(level, (tuple, list)) or len(level) != 2:
            raise MoneyError(f'{side} level is not a (price, quantity) pair')
        price = money.parse(level[0], field=f'{side} price', allow_zero=False)
        quantity = None if level[1] is None else money.parse(level[1], field=f'{side} quantity', allow_zero=False)
        if levels and not (price < levels[-1][0] if side == 'bid' else price > levels[-1][0]):
            raise MoneyError(f'{side} levels are not strictly ordered best first')
        levels.append((price, quantity))
    if not levels:
        raise MoneyError(f'{side} side is empty')
    return tuple(levels)


def _book(book) -> _Book:
    """Validate a duck-typed book snapshot or raise :class:`MoneyError`."""
    if book is None:
        raise MoneyError('order book is missing')
    bids = _levels(getattr(book, 'bids', None), 'bid')
    asks = _levels(getattr(book, 'asks', None), 'ask')
    for name, first in (('best_bid', bids[0][0]), ('best_ask', asks[0][0])):
        stated = getattr(book, name, None)
        if stated is not None and money.parse(stated, field=name, allow_zero=False) != first:
            raise MoneyError(f'{name} does not match the first level')
    if bids[0][0] >= asks[0][0]:
        raise MoneyError('order book is crossed or locked')
    market = {}
    for name in ('exchange', 'symbol'):
        value = getattr(book, name, None)
        if value is not None and not isinstance(value, str):
            raise MoneyError(f'order book {name} is not text')
        market[name] = value
    return _Book(bids=bids, asks=asks, depth_known=getattr(book, 'depth_known', False) is True,
                 best_bid=bids[0][0], best_ask=asks[0][0], **market)


def _try_book(book):
    try:
        return _book(book)
    except MoneyError as exc:
        return exc


def _foreign(book: _Book, rules) -> str | None:
    """Why ``book`` is not a book of the market ``rules`` describe, or None. An unlabelled book is not foreign."""
    if book.exchange is not None and book.exchange != rules.exchange:
        return f'Order book is from {book.exchange}, not {rules.exchange}'
    if book.symbol is not None and book.symbol != rules.symbol:
        return f'Order book is for {book.symbol}, not {rules.symbol}'
    return None


@dataclass(frozen=True)
class Walk:
    base: Decimal
    quote: Decimal
    vwap: Decimal | None          # quote / base at the latency-adjusted level prices
    levels_used: int
    exhausted: bool               # the amount was not fully placed
    depth_known: bool
    worst_price: Decimal | None   # latency-adjusted price of the last level used


def _walk(levels: tuple, amount: Decimal, latency: Decimal, *, buy: bool, depth_known: bool) -> Walk:
    """Spend quote on asks (``buy``) or sell base into bids, each level moved against us once."""
    factor = money.add(ONE, latency) if buy else money.sub(ONE, latency)
    remaining, base, quote, used, worst, known = amount, ZERO, ZERO, 0, None, depth_known
    for price, quantity in levels:
        if remaining <= 0:
            break
        if quantity is None:
            known = False
            break
        adjusted = money.mul(price, factor)
        used, worst = used + 1, adjusted
        if buy:
            level_quote = money.mul(adjusted, quantity)
            if level_quote >= remaining:
                base, quote, remaining = money.add(base, money.div(remaining, adjusted)), money.add(quote, remaining), ZERO
            else:
                base, quote = money.add(base, quantity), money.add(quote, level_quote)
                remaining = money.sub(remaining, level_quote)
        else:
            take = min(quantity, remaining)
            base, quote = money.add(base, take), money.add(quote, money.mul(take, adjusted))
            remaining = money.sub(remaining, take)
    return Walk(base=base, quote=quote, vwap=money.div(quote, base) if base > 0 else None, levels_used=used,
                exhausted=remaining > 0, depth_known=known, worst_price=worst)


def walk_asks(book, *, quote_amount: Decimal, latency_bps: Decimal) -> Walk:
    """Spend ``quote_amount`` on the asks (each price raised by the latency margin) and receive base."""
    validated = _book(book)
    return _walk(validated.asks, money.positive(quote_amount, field='quote amount'),
                 money.bps(_bps_rate(latency_bps, 'latency margin')), buy=True, depth_known=validated.depth_known)


def walk_bids(book, *, base_amount: Decimal, latency_bps: Decimal) -> Walk:
    """Sell ``base_amount`` into the bids (each price lowered by the latency margin) and receive quote."""
    validated = _book(book)
    return _walk(validated.bids, money.positive(base_amount, field='base amount'),
                 money.bps(_bps_rate(latency_bps, 'latency margin')), buy=False, depth_known=validated.depth_known)


# ---------------------------------------------------------------------- sizing
@dataclass(frozen=True)
class SizingResult:
    symbol: str
    eligible: bool
    code: str | None                  # dominant reason when not eligible
    detail: str                       # human sentence naming the rule and numbers
    checks: tuple[dict, ...]          # every check: {'check', 'step', 'passed', 'code', 'detail', ...values as strings}
    budget_cap: Decimal               # min(configured micro budget, MICRO_MAX_ALL_IN_USDT)
    target_budget: Decimal            # cap reduced by cash and allocation, never increased
    gross_quote: Decimal | None       # market-buy quote amount, rounded DOWN to cost_step
    fee_reserve: Decimal | None       # quote reserved for a quote-billed fee, rounded UP
    max_debit: Decimal | None         # gross_quote + fee_reserve; <= target_budget <= budget_cap
    expected_base: Decimal | None     # ask walk result for gross_quote (after latency)
    expected_fee_base: Decimal | None
    sellable_base: Decimal | None     # owned after a base fee, rounded DOWN to amount_step
    predicted_residual_base: Decimal | None
    entry_vwap: Decimal | None
    break_even_bid: Decimal | None    # exit bid where estimated net = 0 (basis = max_debit), up to the tick
    target_bid: Decimal | None        # exit bid where estimated net = target_net, up to the tick
    required_move_bps: Decimal | None  # (target_bid / best_ask - 1) * 10000
    minimum_feasible_budget: Decimal | None   # separately calculated, display only, never used to size
    minimum_feasible_detail: str
    cost: dict                        # CostModel.as_dict()

    def as_dict(self) -> dict:
        return _record(self)


@dataclass
class _Plan:
    """Scratch values of one pass through sizing steps 3-9."""
    gross: Decimal | None = None
    fee_reserve: Decimal | None = None
    ask: Walk | None = None
    fee_base: Decimal | None = None
    owned: Decimal | None = None
    sellable: Decimal | None = None
    residual: Decimal | None = None
    bid: Walk | None = None
    failure: tuple[str, str] | None = None


def _ignore(*args, **values) -> None:
    return None


def _market_minimum(rules) -> tuple[Decimal | None, str]:
    if rules.min_cost_market is not None:
        return rules.min_cost_market, 'min_cost_market, the venue market-order minimum'
    if rules.min_cost_limit is not None:
        return rules.min_cost_limit, 'min_cost_limit, the limit-order minimum used because no market-order minimum is published'
    return None, 'no minimum order amount published'


def _plan(target: Decimal, *, rules, book, cost: CostModel, stress: Decimal, record) -> _Plan:
    """Steps 3-9 for one target budget, stopping at the first failure.

    ``record(name, step, passed, code, detail, **values)`` receives every
    check; the minimum-budget search passes a no-op.
    """
    plan = _Plan()
    fee, latency = cost.taker_fee, money.bps(cost.latency_bps)
    base_ccy, quote_ccy = rules.base or 'base', rules.quote or 'quote'

    def gate(name, step, passed, code, detail, **values) -> bool:
        record(name, step, passed, code, '' if passed else detail, **values)
        if not passed:
            plan.failure = (code, detail)
        return passed

    # 3. Quote and fee reserve, both on the venue's quote step, inside the target.
    cost_step, step_issue = rules.cost_step, None
    if cost_step is None:
        cost_step, step_issue = FALLBACK_STEP, f'quote-amount step unknown; {money.text(FALLBACK_STEP)} used'
    if cost.buy_fee_currency == 'base':
        gross, reserve = money.q_down(target, cost_step), ZERO
    else:
        gross = money.q_down(money.div(target, money.add(ONE, fee)), cost_step)
        reserve = money.q_up(money.mul(gross, fee), cost_step)
        while gross > 0 and money.add(gross, reserve) > target:
            gross = max(ZERO, money.sub(gross, cost_step))
            reserve = money.q_up(money.mul(gross, fee), cost_step)
    plan.gross, plan.fee_reserve = gross, reserve
    if not gate('fee_reserve', 3, gross > 0, BUDGET_BELOW_VENUE_MINIMUM,
                f'Target budget {_show(target)} {quote_ccy} rounds to no order at the {_show(cost_step)} {quote_ccy} step',
                target_budget=target, gross_quote=gross, fee_reserve=reserve, cost_step=cost_step, fee_rate=fee,
                buy_fee_currency=cost.buy_fee_currency, issue=step_issue):
        return plan

    # 4. Order-type-specific venue minimum and maximum for a market buy.
    minimum, source = _market_minimum(rules)
    if minimum is None:
        record('venue_minimum', 4, True, None, 'No minimum order amount is published; the venue may still reject',
               gross_quote=gross, minimum=None, minimum_source=source, issue='minimum order amount unknown')
    elif not gate('venue_minimum', 4, gross >= minimum, BUDGET_BELOW_VENUE_MINIMUM,
                  f'Market buy of {_show(gross)} {quote_ccy} is below the {_show(minimum)} {quote_ccy} minimum ({source})',
                  gross_quote=gross, minimum=minimum, minimum_source=source):
        return plan
    maximum = rules.market_max_cost()
    if not gate('venue_maximum', 4, maximum is None or gross <= maximum, MAX_COST_EXCEEDED,
                f'Market buy of {_show(gross)} {quote_ccy} is above the {_show(maximum)} {quote_ccy} market-order maximum',
                gross_quote=gross, maximum=maximum):
        return plan

    # 5. Ask walk: an entry needs a book with known depth.
    if isinstance(book, MoneyError):
        gate('ask_depth', 5, False, PRICE_UNKNOWN, f'Order book refused: {book}')
        return plan
    foreign = _foreign(book, rules)
    if foreign is not None:
        gate('ask_depth', 5, False, PRICE_UNKNOWN, foreign)
        return plan
    if not book.depth_known:
        gate('ask_depth', 5, False, DEPTH_UNKNOWN,
             f'Order-book depth for {rules.symbol} is unknown (top of book only); an entry needs depth',
             depth_known=False)
        return plan
    ask = _walk(book.asks, gross, latency, buy=True, depth_known=True)
    plan.ask = ask
    if not ask.depth_known:
        code, detail = DEPTH_UNKNOWN, f'An ask level of {rules.symbol} has no quantity before {_show(gross)} {quote_ccy} is filled'
    else:
        code = INSUFFICIENT_ASK_DEPTH
        detail = (f'Ask depth of {rules.symbol} ({ask.levels_used} levels, {_show(ask.quote)} {quote_ccy}) '
                  f'cannot fill {_show(gross)} {quote_ccy}')
    if not gate('ask_depth', 5, ask.depth_known and not ask.exhausted, code, detail, expected_base=ask.base,
                vwap=ask.vwap, levels_used=ask.levels_used, worst_price=ask.worst_price, latency_bps=cost.latency_bps):
        return plan

    # 6. A base-billed (or possibly base-billed) buy fee reduces what is owned.
    expected = ask.base
    fee_base = money.mul(expected, fee) if cost.buy_fee_currency in ('base', 'unknown') else ZERO
    owned = money.sub(expected, fee_base)
    plan.fee_base, plan.owned = fee_base, owned
    record('base_fee', 6, True, None, '', expected_base=expected, expected_fee_base=fee_base, owned_base=owned,
           buy_fee_currency=cost.buy_fee_currency)

    # 7. Quantity limits of the buy.
    min_amount, max_amount = rules.min_amount, rules.max_amount
    if min_amount is None:
        record('minimum_quantity', 7, True, None, 'No minimum quantity is published; the venue may still reject',
               expected_base=expected, min_amount=None, issue='minimum quantity unknown')
    elif not gate('minimum_quantity', 7, expected >= min_amount, BUDGET_BELOW_VENUE_MINIMUM,
                  f'Expected {_show(expected)} {base_ccy} is below the {_show(min_amount)} {base_ccy} minimum quantity (min_amount)',
                  expected_base=expected, min_amount=min_amount):
        return plan
    if not gate('maximum_quantity', 7, max_amount is None or expected <= max_amount, MAX_COST_EXCEEDED,
                f'Expected {_show(expected)} {base_ccy} is above the {_show(max_amount)} {base_ccy} maximum quantity (max_amount)',
                expected_base=expected, max_amount=max_amount):
        return plan

    # 8. What can actually be sold at the venue's quantity step; the rest is kept as residual.
    amount_step, amount_issue = rules.amount_step, None
    if amount_step is None:
        amount_step, amount_issue = FALLBACK_STEP, f'quantity step unknown; {money.text(FALLBACK_STEP)} used'
    sellable = money.q_down(owned, amount_step)
    residual = money.sub(owned, sellable)
    plan.sellable, plan.residual = sellable, residual
    record('sellable_quantity', 8, True, None, '', owned_base=owned, sellable_base=sellable, residual_base=residual,
           amount_step=amount_step, issue=amount_issue)

    # 9. The exit must be possible before any exposure is taken.
    if not gate('exit_minimum_quantity', 9, sellable > 0 and (min_amount is None or sellable >= min_amount),
                EXIT_BELOW_VENUE_MINIMUM,
                f'Sellable {_show(sellable)} {base_ccy} (owned {_show(owned)} after fees, step {_show(amount_step)}) '
                f'is below the {_show(min_amount if min_amount is not None else amount_step)} {base_ccy} minimum sell quantity',
                sellable_base=sellable, min_amount=min_amount):
        return plan
    bid_at_stress = money.mul(book.best_bid, money.sub(ONE, money.bps(stress)))
    exit_value = money.mul(sellable, bid_at_stress)
    if not gate('exit_minimum_amount', 9, minimum is None or exit_value >= minimum, EXIT_BELOW_VENUE_MINIMUM,
                f'Selling {_show(sellable)} {base_ccy} at the stressed bid {_show(bid_at_stress)} yields '
                f'{_show(exit_value)} {quote_ccy}, below the {_show(minimum)} {quote_ccy} minimum ({source})',
                sellable_base=sellable, bid_at_stress=bid_at_stress, exit_value=exit_value, minimum=minimum,
                exit_stress_bps=stress):
        return plan
    bid = _walk(book.bids, sellable, latency, buy=False, depth_known=True)
    plan.bid = bid
    if not bid.depth_known:
        code, detail = DEPTH_UNKNOWN, f'A bid level of {rules.symbol} has no quantity before {_show(sellable)} {base_ccy} is sold'
    else:
        code = INSUFFICIENT_BID_DEPTH
        detail = (f'Bid depth of {rules.symbol} ({bid.levels_used} levels, {_show(bid.base)} {base_ccy}) '
                  f'cannot absorb {_show(sellable)} {base_ccy}')
    gate('exit_depth', 9, bid.depth_known and not bid.exhausted, code, detail, sellable_base=sellable,
         exit_vwap=bid.vwap, levels_used=bid.levels_used, worst_price=bid.worst_price)
    return plan


def _minimum_feasible(*, rules, book, cost: CostModel, stress: Decimal) -> tuple[Decimal | None, str]:
    """Step 12: the smallest budget, to the cent, for which steps 3-9 pass on this book.

    Every failure a larger budget can cure (venue minimums, dust) gets no worse
    as the budget grows, and every other failure (depth, maximums) gets no
    better, so the feasible budgets form one interval and a bisection over the
    sizing function itself finds its lower end.
    """
    if isinstance(book, MoneyError):
        return None, f'Not calculated: the order book was refused ({book})'
    foreign = _foreign(book, rules)
    if foreign is not None:
        return None, f'Not calculated: {foreign}'
    minimum, _ = _market_minimum(rules)
    upper = SEARCH_FLOOR_USDT if minimum is None else max(SEARCH_FLOOR_USDT, money.mul(SEARCH_MIN_COST_MULTIPLE, minimum))
    top = int(money.div(money.q_down(upper, SEARCH_STEP), SEARCH_STEP))

    def plan_at(cents: int) -> _Plan:
        return _plan(money.mul(Decimal(cents), SEARCH_STEP), rules=rules, book=book, cost=cost, stress=stress,
                     record=_ignore)

    def large_enough(plan: _Plan) -> bool:
        return plan.failure is None or plan.failure[0] not in _SMALL_BUDGET_CODES

    searched = f'searched {_show(SEARCH_STEP)} to {_show(upper)} USDT'
    highest = plan_at(top)
    if not large_enough(highest):
        return None, f'No budget is feasible on this book ({searched}): {highest.failure[1]}'
    low, high = 0, top          # plan_at(low) is too small (0 trivially), plan_at(high) is large enough
    while high - low > 1:
        middle = (low + high) // 2
        if large_enough(plan_at(middle)):
            high = middle
        else:
            low = middle
    found, budget = plan_at(high), money.mul(Decimal(high), SEARCH_STEP)
    if found.failure is None:
        return budget, (f'Smallest feasible budget on this book: {_show(budget)} USDT ({searched}); '
                        'display only, never used to size an order')
    if high == 1:
        return None, f'No budget is feasible on this book ({searched}): {found.failure[1]}'
    return None, (f'No budget is feasible on this book ({searched}): below {_show(budget)} USDT the venue minimum '
                  f'or dust rule fails, and from {_show(budget)} USDT: {found.failure[1]}')


def size_entry(*, rules, book, cost: CostModel, budget_cap: Decimal, cash_available: Decimal,
               allocation_remaining: Decimal, target_net: Decimal, exit_stress_bps: Decimal, now_wall: float,
               max_rules_age_s: float | None = None) -> SizingResult:
    """Size one market buy by quote amount inside the all-in micro cap, or say why not.

    Every step is recorded as a named check. An ineligible result proposes no
    order (every order field is None) and is never resized upward; its checks
    keep the numbers that failed.
    """
    target_net = money.parse(target_net, field='target net')
    stress = _bps_rate(exit_stress_bps, 'exit stress')
    if not isinstance(cost, CostModel):
        raise MoneyError('cost must be a CostModel')
    checks: list[dict] = []
    failure: tuple[str, str] | None = None

    def record(name, step, passed, code=None, detail='', **values) -> None:
        checks.append({'check': name, 'step': step, 'passed': bool(passed), 'code': None if passed else code,
                       'detail': detail, **{key: _value(value) for key, value in values.items()}})

    def gate(name, step, passed, code, detail, **values) -> bool:
        nonlocal failure
        record(name, step, passed, code, '' if passed else detail, **values)
        if not passed and failure is None:
            failure = (code, detail)
        return passed

    validated = _try_book(book)
    cap_limit = _cap()

    # 1. The cap, then the cap reduced (never raised) by cash and allocation.
    try:
        requested, reason = money.parse(budget_cap, field='budget cap'), ''
    except MoneyError as exc:
        requested, reason = None, str(exc)
    cap = min(requested, cap_limit) if requested is not None else ZERO
    gate('budget_cap', 1, requested is not None and ZERO < requested <= cap_limit, BUDGET_INVALID,
         f'Budget cap {_show(requested)} USDT must be above 0 and at most the {_show(cap_limit)} USDT micro maximum'
         if requested is not None else f'Budget cap is invalid: {reason}',
         budget_cap=requested, micro_max=cap_limit)
    target, limited_by = ZERO, None
    if failure is None:
        amounts = {}
        for name, value in (('cash available', cash_available), ('allocation remaining', allocation_remaining)):
            try:
                amounts[name] = money.parse(value, field=name, allow_negative=True)
            except MoneyError:
                amounts[name] = None
        cash, allocation = amounts['cash available'], amounts['allocation remaining']
        if cash is None or cash <= 0:
            code = CASH_INSUFFICIENT
            detail = ('Available cash is unknown' if cash is None
                      else f'Available cash {_show(cash)} USDT leaves nothing to spend')
        elif allocation is None or allocation <= 0:
            code = ALLOCATION_EXHAUSTED
            detail = ('Remaining micro allocation is unknown' if allocation is None
                      else f'Remaining micro allocation {_show(allocation)} USDT leaves nothing to spend')
        else:
            code, detail = None, ''
            target = min(cap, cash, allocation)
        limited_by = None if code else ('budget cap' if target == cap else 'cash' if target == cash else 'allocation')
        gate('target_budget', 1, code is None, code, detail, budget_cap=cap, cash_available=cash,
             allocation_remaining=allocation, target_budget=target, limited_by=limited_by)

    # 2. Both legs must be tradable now: an entry without a possible exit is not an entry.
    if failure is None:
        for side, name in (('BUY', 'venue_buy'), ('SELL', 'venue_sell')):
            ok, code, detail = rules.tradable(side)
            if not gate(name, 2, ok, code, detail, issues='; '.join(rules.issues) or None):
                break
    if failure is None:
        ok, detail, age, limit = _rules_age(rules, now_wall, max_rules_age_s)
        gate('rules_age', 2, ok, METADATA_STALE, detail, rules_age_s=age, max_rules_age_s=limit,
             now_wall=_clock(now_wall), loaded_at=_clock(rules.loaded_at))

    # 3-9. Quote, fee reserve, minimums, depth, fees, quantity step and exit feasibility.
    plan = None
    if failure is None:
        plan = _plan(target, rules=rules, book=validated, cost=cost, stress=stress, record=record)
        failure = plan.failure
        if failure is not None and failure[0] in _SMALL_BUDGET_CODES and limited_by in ('cash', 'allocation'):
            # Only when the cap-sized order would be eligible is the binding limit (not the venue) the
            # reason. A cap-sized order that fails for any other cause (a refused book, unknown depth, a
            # maximum) is not cured by cash, so the venue minimum stays the reason. The order stays at
            # the reduced target either way.
            at_cap = _plan(cap, rules=rules, book=validated, cost=cost, stress=stress, record=_ignore)
            if at_cap.failure is None:
                code = CASH_INSUFFICIENT if limited_by == 'cash' else ALLOCATION_EXHAUSTED
                detail = (f'{failure[1]}; the {_show(cap)} USDT budget cap would meet it, but {limited_by} '
                          f'limits the target budget to {_show(target)} USDT')
                record('binding_limit', 1, False, code, detail, budget_cap=cap, target_budget=target,
                       limited_by=limited_by)
                failure = (code, detail)

    # 10-11. The all-in invariant, then the bids that make the round trip break even or reach the target.
    values = {}
    if failure is None:
        max_debit = money.add(plan.gross, plan.fee_reserve)
        if gate('max_debit', 10, max_debit <= target <= cap <= cap_limit, BUDGET_INVALID,
                f'Sizing defect: max debit {_show(max_debit)} exceeds target {_show(target)} or cap {_show(cap)}',
                max_debit=max_debit, target_budget=target, budget_cap=cap):
            keep = money.mul(plan.sellable, money.sub(ONE, money.bps(cost.latency_bps)), money.sub(ONE, cost.taker_fee))
            break_even = _tick_up(money.div(max_debit, keep), rules.price_tick)
            target_bid = _tick_up(money.div(money.add(max_debit, target_net), keep), rules.price_tick)
            required = money.mul(money.sub(money.div(target_bid, validated.best_ask), ONE), BPS)
            record('break_even', 11, True, None, '', break_even_bid=break_even, target_bid=target_bid,
                   target_net=target_net, required_move_bps=required, best_bid=validated.best_bid,
                   best_ask=validated.best_ask, price_tick=rules.price_tick,
                   sell_fee_currency=cost.sell_fee_currency)
            values = dict(gross_quote=plan.gross, fee_reserve=plan.fee_reserve, max_debit=max_debit,
                          expected_base=plan.ask.base, expected_fee_base=plan.fee_base,
                          sellable_base=plan.sellable, predicted_residual_base=plan.residual,
                          entry_vwap=plan.ask.vwap, break_even_bid=break_even, target_bid=target_bid,
                          required_move_bps=required)

    # 12. Shown next to the result, never used to size it.
    minimum, minimum_detail = _minimum_feasible(rules=rules, book=validated, cost=cost, stress=stress)
    record('minimum_feasible_budget', 12, True, None, minimum_detail, minimum_feasible_budget=minimum,
           display_only=True)

    eligible = failure is None
    if eligible:
        quote_ccy, base_ccy = rules.quote or 'quote', rules.base or 'base'
        detail = (f'Buy {_show(values["gross_quote"])} {quote_ccy} of {rules.symbol} at market '
                  f'(+{_show(values["fee_reserve"])} fee reserve; max debit {_show(values["max_debit"])} of '
                  f'{_show(target)} target), about {_show(values["sellable_base"])} {base_ccy} sellable; '
                  f'break-even bid {_show(values["break_even_bid"])}, target bid {_show(values["target_bid"])} '
                  f'({_show(values["required_move_bps"])} bps above the ask)')
    else:
        detail = failure[1]
    empty = dict.fromkeys(('gross_quote', 'fee_reserve', 'max_debit', 'expected_base', 'expected_fee_base',
                           'sellable_base', 'predicted_residual_base', 'entry_vwap', 'break_even_bid',
                           'target_bid', 'required_move_bps'))
    return SizingResult(symbol=rules.symbol, eligible=eligible, code=None if eligible else failure[0], detail=detail,
                        checks=tuple(checks), budget_cap=cap, target_budget=target, **(values or empty),
                        minimum_feasible_budget=minimum, minimum_feasible_detail=minimum_detail,
                        cost=cost.as_dict())


# ----------------------------------------------------------------------- exits
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
    break_even_bid: Decimal | None
    target_bid: Decimal | None
    residual_base: Decimal            # owned - sellable (kept, never deleted)
    labels: dict                      # {'proceeds': 'estimated', 'fees': 'modeled', ...}

    def as_dict(self) -> dict:
        return _record(self)


def estimate_exit(*, owned_base: Decimal, basis_quote: Decimal, rules, book, cost: CostModel,
                  target_net: Decimal, uncertainty_bps: Decimal) -> ExitEstimate:
    """What selling the sellable part of a holding into ``book`` would return now.

    With known depth the bid walk prices the sale (latency once per level, no
    flat slippage). With unknown depth the best bid is reduced by the fallback
    slippage and the latency margin. A refused book, or one of another market,
    is ``unavailable``. When the visible bids cannot absorb the sale, depth
    beyond them is unknown: the visible levels price what they absorb and the
    rest is priced by the flat fallback from the worst visible bid. That is a
    conservative estimate a safety exit can act on, not a book walk, so it is
    ``flat_fallback`` with ``depth_exhausted`` set (profit exits need ``book``);
    the labels also carry the floor where nothing beyond the visible bids sells.
    """
    owned = money.parse(owned_base, field='owned base')
    basis = money.parse(basis_quote, field='basis')
    target = money.parse(target_net, field='target net')
    uncertainty_rate = money.bps(_bps_rate(uncertainty_bps, 'uncertainty'))
    fee, latency = cost.taker_fee, money.bps(cost.latency_bps)
    base_ccy, quote_ccy = rules.base or 'base', rules.quote or 'quote'
    amount_step = rules.amount_step if rules.amount_step is not None else FALLBACK_STEP
    sellable = money.q_down(owned, amount_step)
    residual = money.sub(owned, sellable)
    allocated = _allocate(basis, sellable, owned)
    labels = {'proceeds': 'estimated', 'fees': 'modeled', 'fee_source': cost.fee_source,
              'basis': 'allocated pro rata to the sellable quantity',
              'residual': f'{_show(residual)} {base_ccy} below the quantity step is kept, not sold'}
    if rules.amount_step is None:
        labels['quantity_step'] = f'unknown; {money.text(FALLBACK_STEP)} used'
    if cost.sell_fee_currency == 'base':
        labels['fees'] = 'modeled; billed in base, valued at the exit price'

    validated = _try_book(book)
    reason = str(validated) if isinstance(validated, MoneyError) else _foreign(validated, rules)
    if reason is not None:
        labels.update(proceeds='unknown', depth=f'unavailable: {reason}')
        return ExitEstimate(sellable_base=sellable, gross_proceeds=None, sell_fee=None, net_proceeds=None,
                            allocated_basis=allocated, est_net_pnl=None,
                            uncertainty=money.mul(allocated, uncertainty_rate), depth_model='unavailable',
                            depth_exhausted=False, break_even_bid=None, target_bid=None, residual_base=residual,
                            labels=labels)

    fallback = money.sub(ONE, money.bps(money.add(cost.fallback_slippage_bps, cost.latency_bps)))
    model, walk = ('book' if validated.depth_known else 'flat_fallback'), None
    if model == 'book' and sellable > 0:
        walk = _walk(validated.bids, sellable, latency, buy=False, depth_known=True)
        if not walk.depth_known:
            model, walk = 'flat_fallback', None
            labels['depth'] = 'a bid level has no quantity; flat fallback used'
    exhausted = walk is not None and walk.exhausted
    if exhausted:
        model = 'flat_fallback'     # partly priced beyond the visible book: never a profit-exit estimate
    if model == 'book':
        factor = money.sub(ONE, latency)
        labels.setdefault('depth', 'bid walk')
        labels['slippage'] = 'none added: the bid walk already contains the spread and depth impact'
    else:
        factor = fallback
        labels.setdefault('depth', 'unknown (top of book only)')
        labels['slippage'] = f'flat fallback {_show(cost.fallback_slippage_bps)} bps because depth is unknown'
    labels['latency'] = f'{_show(cost.latency_bps)} bps against the bid, once'

    gross = None
    if sellable <= 0:
        labels['proceeds'] = 'unknown: nothing is sellable at the quantity step'
    elif exhausted:
        worst_bid = validated.bids[walk.levels_used - 1][0]
        unabsorbed = money.sub(sellable, walk.base)
        gross = money.add(walk.quote, money.mul(unabsorbed, worst_bid, fallback))
        labels['depth'] = (f'bid depth exhausted after {walk.levels_used} levels ({_show(walk.base)} of '
                           f'{_show(sellable)} {base_ccy}); depth beyond them is unknown')
        labels['slippage'] = (f'none added to the visible levels; flat fallback {_show(cost.fallback_slippage_bps)} bps '
                              f'on the {_show(unabsorbed)} {base_ccy} beyond them, from the worst visible bid '
                              f'{_show(worst_bid)}')
        labels['proceeds'] = ('estimated, conservatively: visible bids for what they absorb, the flat fallback '
                              'from the worst visible bid for the rest; not a book estimate')
        labels['visible_gross_proceeds'] = money.text(walk.quote)
        labels['floor_net_pnl'] = money.text(money.sub(money.sub(walk.quote, money.mul(walk.quote, fee)), allocated))
    elif walk is not None:
        gross = walk.quote
    else:
        gross = money.mul(sellable, validated.best_bid, factor)
    sell_fee = money.mul(gross, fee) if gross is not None else None
    net = money.sub(gross, sell_fee) if gross is not None else None
    estimate = money.sub(net, allocated) if net is not None else None

    break_even = target_bid = None
    if sellable > 0:
        keep = money.mul(sellable, factor, money.sub(ONE, fee))
        break_even = _tick_up(money.div(allocated, keep), rules.price_tick)
        target_bid = _tick_up(money.div(money.add(allocated, target), keep), rules.price_tick)
    minimum, source = _market_minimum(rules)
    if sellable <= 0:
        labels['exit_minimum'] = 'nothing is sellable at the quantity step'
    elif rules.min_amount is not None and sellable < rules.min_amount:
        labels['exit_minimum'] = f'below the {_show(rules.min_amount)} {base_ccy} minimum quantity'
    elif minimum is not None and money.mul(sellable, validated.best_bid) < minimum:
        labels['exit_minimum'] = f'below the {_show(minimum)} {quote_ccy} minimum ({source})'
    else:
        labels['exit_minimum'] = 'ok' if minimum is not None and rules.min_amount is not None else 'partly unknown'
    return ExitEstimate(sellable_base=sellable, gross_proceeds=gross, sell_fee=sell_fee, net_proceeds=net,
                        allocated_basis=allocated, est_net_pnl=estimate,
                        uncertainty=money.mul(sellable, validated.best_bid, uncertainty_rate), depth_model=model,
                        depth_exhausted=exhausted, break_even_bid=break_even, target_bid=target_bid,
                        residual_base=residual, labels=labels)


# ------------------------------------------------------------------ accounting
def _other_fees(other_fees) -> dict[str, Decimal]:
    """Third-asset fee legs by currency; a zero leg costs nothing and is dropped."""
    if other_fees is None:
        return {}
    if not isinstance(other_fees, dict):
        raise MoneyError('other fees must be a mapping of currency to amount')
    legs = {}
    for currency, amount in other_fees.items():
        if not isinstance(currency, str) or not currency:
            raise MoneyError('a third-asset fee needs a currency')
        value = money.parse(amount, field=f'{currency} fee')
        if value > 0:
            legs[currency] = value
    return legs


def _settle_fee_status(declared, *, quote_fee, legs, legs_value, notes: list[str]) -> str:
    """The fee status a record can honestly claim, given which legs are known."""
    if declared not in FEE_STATUSES:
        raise MoneyError('fee status is not recognised')
    if declared == 'modeled':
        raise MoneyError('a booked fee cannot be modeled from a configured rate')
    if legs_value is not None and legs_value > 0 and not legs:
        raise MoneyError('a third-asset fee value needs the fee amounts')
    status = declared
    if quote_fee is None:
        notes.append(f'{FEE_UNKNOWN}: the quote fee was not reported; amounts exclude it and it is not '
                     'filled in from a configured rate')
        status = 'unknown'
    if legs and legs_value is None:
        notes.append(f'{FEE_UNKNOWN}: third-asset fee ' + ', '.join(f'{_show(v)} {c}' for c, v in legs.items())
                     + ' has no quote value; amounts exclude it')
        status = 'unknown'
    elif legs and status not in ('unknown', 'reconciled'):
        notes.append('third-asset fee ' + ', '.join(f'{_show(v)} {c}' for c, v in legs.items())
                     + f' valued by estimate at {_show(legs_value)}')
        status = 'estimated'
    return status


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
    notes: tuple[str, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict:
        return _record(self)


def realize_exit(*, owned_base: Decimal, basis_quote: Decimal, sold_base: Decimal, gross_quote: Decimal,
                 quote_fee: Decimal | None, base_fee: Decimal | None, other_fees: dict[str, Decimal],
                 other_fee_value: Decimal | None, fee_status: str) -> Realized:
    """Book a (possibly partial) sale against a holding's basis.

    ``quote_fee`` is the quote fee the venue reported (0 when it reported none
    charged in quote; None when it reported nothing). An unknown leg is left
    out of the amounts and makes the result ``unconfirmed``; it is never
    replaced by a configured rate. Basis moves pro rata to the base that
    leaves ownership, and ``allocated + residual`` equals the basis exactly.
    """
    owned = money.parse(owned_base, field='owned base')
    basis = money.parse(basis_quote, field='basis')
    sold = money.positive(sold_base, field='sold base')
    gross = money.parse(gross_quote, field='gross proceeds')
    quote_fee = money.parse_optional(quote_fee, field='quote fee')
    base_fee = money.parse_optional(base_fee, field='base fee')
    legs = _other_fees(other_fees)
    legs_value = money.parse_optional(other_fee_value, field='third-asset fee value')
    notes: list[str] = []
    settled = _settle_fee_status(fee_status, quote_fee=quote_fee, legs=legs, legs_value=legs_value, notes=notes)
    closed = money.add(sold, base_fee if base_fee is not None else ZERO)
    if closed > owned:
        raise MoneyError('the sale and its base fee close more base than is owned')
    allocated = _allocate(basis, closed, owned)
    residual_basis = _conserved(basis, allocated)
    net = money.sub(gross, quote_fee) if quote_fee is not None else gross
    realized = money.sub(net, allocated)
    if legs and legs_value is not None:
        realized = money.sub(realized, legs_value)
    return Realized(closed_base=closed, allocated_basis=allocated, net_proceeds=net, realized_net=realized,
                    residual_base=money.sub(owned, closed), residual_basis=residual_basis,
                    status='confirmed' if settled in CONFIRMING_FEE_STATUSES else 'unconfirmed',
                    fee_status=settled, notes=tuple(notes))


@dataclass(frozen=True)
class Acquisition:
    filled_base: Decimal
    owned_base: Decimal           # filled - base fee
    gross_quote: Decimal
    quote_fee: Decimal | None
    base_fee: Decimal | None
    debit: Decimal                # cash leaving: gross + quote fee
    basis: Decimal                # debit + valued third-asset fee
    fee_status: str
    notes: tuple[str, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict:
        return _record(self)


def account_entry(*, filled_base, gross_quote, quote_fee, base_fee, other_fees, other_fee_value,
                  fee_status, reserved: Decimal) -> Acquisition:
    """Book a filled entry. A debit above the reservation is a defect, not a cost overrun to absorb."""
    filled = money.positive(filled_base, field='filled base')
    gross = money.positive(gross_quote, field='gross quote')
    quote_fee = money.parse_optional(quote_fee, field='quote fee')
    base_fee = money.parse_optional(base_fee, field='base fee')
    legs = _other_fees(other_fees)
    legs_value = money.parse_optional(other_fee_value, field='third-asset fee value')
    reserved = money.parse(reserved, field='reserved')
    if reserved > _cap():
        raise MoneyError('the reservation is above the micro all-in cap')
    notes: list[str] = []
    settled = _settle_fee_status(fee_status, quote_fee=quote_fee, legs=legs, legs_value=legs_value, notes=notes)
    debit = money.add(gross, quote_fee if quote_fee is not None else ZERO)
    if debit > reserved:
        raise MoneyError('the entry debit exceeds the reserved amount')
    owned = money.sub(filled, base_fee if base_fee is not None else ZERO)
    if owned < 0:
        raise MoneyError('the base fee exceeds the filled quantity')
    basis = money.add(debit, legs_value) if legs and legs_value is not None else debit
    return Acquisition(filled_base=filled, owned_base=owned, gross_quote=gross, quote_fee=quote_fee,
                       base_fee=base_fee, debit=debit, basis=basis, fee_status=settled, notes=tuple(notes))


# ------------------------------------------------- simplified algebra (docs only)
def simple_break_even_ratio(*, buy_slip, buy_fee, sell_slip, sell_fee) -> Decimal:
    """Exit/entry price ratio at which a quote-fee round trip nets zero.

    ``(1 + buy_slip)(1 + buy_fee) / ((1 - sell_slip)(1 - sell_fee))``, all
    rates as fractions. Budget-independent: every cost here is proportional.
    For docs and the sensitivity table; sizing uses the book and the venue steps.
    """
    buy_slip, buy_fee = _fraction(buy_slip, 'buy slippage'), _fraction(buy_fee, 'buy fee')
    sell_slip, sell_fee = _fraction(sell_slip, 'sell slippage'), _fraction(sell_fee, 'sell fee')
    return money.div(money.mul(money.add(ONE, buy_slip), money.add(ONE, buy_fee)),
                     money.mul(money.sub(ONE, sell_slip), money.sub(ONE, sell_fee)))


def simple_target_ratio(*, budget, target_net, buy_slip, buy_fee, sell_slip, sell_fee) -> Decimal:
    """Exit/entry price ratio that nets ``target_net`` on an all-in ``budget``: ``(1 + g/B)`` times break-even."""
    budget = money.positive(budget, field='budget')
    target_net = money.parse(target_net, field='target net')
    return money.mul(money.add(ONE, money.div(target_net, budget)),
                     simple_break_even_ratio(buy_slip=buy_slip, buy_fee=buy_fee, sell_slip=sell_slip,
                                             sell_fee=sell_fee))


def move_units(ratio: Decimal) -> dict:
    """A price ratio as the move it requires, in every unit the dashboard and docs use."""
    move = money.sub(money.parse(ratio, field='ratio', allow_zero=False), ONE)
    return {'ratio': ratio, 'fraction': move, 'percent': money.mul(move, HUNDRED), 'bps': money.mul(move, BPS)}


def _min_fee_entry_value(budget: Decimal, fee: Decimal, slip: Decimal, min_fee: Decimal) -> Decimal | None:
    """Quantity times the unchanged price bought by an all-in ``budget``; the buy pays ``max(rate * notional, min_fee)``."""
    proportional_gross = money.div(budget, money.add(ONE, fee))
    buy_gross = proportional_gross if money.mul(proportional_gross, fee) >= min_fee else money.sub(budget, min_fee)
    if buy_gross <= 0:
        return None
    return money.div(buy_gross, money.add(ONE, slip))


def _min_fee_flat_net(budget: Decimal, fee: Decimal, slip: Decimal, min_fee: Decimal) -> Decimal | None:
    """The round-trip net at an unchanged price when each leg pays ``max(rate * notional, min_fee)``.

    Computed leg by leg: with a minimum fee the loss is not ``budget / break-even``,
    because the minimum may bind at the unchanged price and not at break-even.
    """
    entry_value = _min_fee_entry_value(budget, fee, slip, min_fee)
    if entry_value is None:
        return None
    proceeds = money.mul(entry_value, money.sub(ONE, slip))
    return money.sub(money.sub(proceeds, max(money.mul(proceeds, fee), min_fee)), budget)


def _min_fee_ratio(budget: Decimal, target: Decimal, fee: Decimal, slip: Decimal, min_fee: Decimal) -> Decimal | None:
    """The target ratio when each leg pays ``max(rate * notional, min_fee)``; None if the fee eats the budget."""
    entry_value = _min_fee_entry_value(budget, fee, slip, min_fee)      # quantity times entry price
    if entry_value is None:
        return None
    needed = money.add(budget, target)
    sell_gross = money.div(needed, money.sub(ONE, fee))
    if money.mul(sell_gross, fee) < min_fee:
        sell_gross = money.add(needed, min_fee)
    return money.div(sell_gross, money.mul(entry_value, money.sub(ONE, slip)))


def sensitivity(budgets, fee_rates, slips, target_bps, *, min_fee_quote: Decimal | None = None) -> list[dict]:
    """Round-trip economics for each budget, fee rate and slippage (fractions, both legs alike).

    ``target_bps`` (one value or several) is the net target as bps of the
    budget. With proportional costs the required move is the same for every
    budget and the loss at flat prices scales with the budget; ``min_fee_quote``
    adds a per-leg minimum fee, the kind of fixed cost that makes a small
    budget need a larger move. Rows above the micro cap are marked; nothing
    here changes ``MICRO_MAX_ALL_IN_USDT``. Values are decimal strings.
    """
    targets = tuple(target_bps) if isinstance(target_bps, (list, tuple)) else (target_bps,)
    min_fee = money.parse_optional(min_fee_quote, field='minimum fee')
    rows = []
    for budget in budgets:
        budget = money.positive(budget, field='budget')
        for fee in fee_rates:
            fee = _fraction(fee, 'fee rate')
            for slip in slips:
                slip = _fraction(slip, 'slippage')
                for bps_target in targets:
                    bps_target = money.parse(bps_target, field='target bps')
                    target = money.mul(budget, money.bps(bps_target))
                    if min_fee is None:
                        kind = 'proportional'
                        even = simple_break_even_ratio(buy_slip=slip, buy_fee=fee, sell_slip=slip, sell_fee=fee)
                        goal = simple_target_ratio(budget=budget, target_net=target, buy_slip=slip, buy_fee=fee,
                                                   sell_slip=slip, sell_fee=fee)
                    else:
                        kind = 'minimum_fee'
                        even = _min_fee_ratio(budget, ZERO, fee, slip, min_fee)
                        goal = _min_fee_ratio(budget, target, fee, slip, min_fee)
                    row = {'budget': budget, 'fee_rate': fee, 'slip': slip, 'target_bps': bps_target,
                           'target_net': target, 'cost_kind': kind, 'within_micro_cap': budget <= _cap(),
                           'min_fee_quote': min_fee}
                    if even is None or goal is None:
                        row.update(feasible=False)
                    else:
                        if min_fee is None:
                            # Proportional costs only: at an unchanged price the round trip returns
                            # budget / break-even ratio, so the loss is exactly proportional to the budget.
                            flat_net = money.mul(budget, money.sub(money.div(ONE, even), ONE))
                        else:
                            flat_net = _min_fee_flat_net(budget, fee, slip, min_fee)
                        even_units, goal_units = move_units(even), move_units(goal)
                        row.update(feasible=True, break_even_ratio=even, break_even_pct=even_units['percent'],
                                   break_even_bps=even_units['bps'], target_ratio=goal,
                                   target_pct=goal_units['percent'], target_move_bps=goal_units['bps'],
                                   flat_net=flat_net)
                    rows.append({key: _value(value) if isinstance(value, Decimal) else value
                                 for key, value in row.items()})
    return rows


__all__ = ['MICRO_MAX_ALL_IN_USDT', 'BUDGET_BELOW_VENUE_MINIMUM', 'EXIT_BELOW_VENUE_MINIMUM', 'INSUFFICIENT_ASK_DEPTH',
           'INSUFFICIENT_BID_DEPTH', 'DEPTH_UNKNOWN', 'CASH_INSUFFICIENT', 'ALLOCATION_EXHAUSTED', 'MAX_COST_EXCEEDED',
           'PRICE_UNKNOWN', 'FEE_UNKNOWN', 'BUDGET_INVALID', 'FEE_CURRENCIES', 'FEE_STATUSES',
           'CONFIRMING_FEE_STATUSES', 'DEPTH_MODELS', 'CostModel', 'cost_model', 'Walk', 'walk_asks', 'walk_bids',
           'SizingResult', 'size_entry', 'ExitEstimate', 'estimate_exit', 'Realized', 'realize_exit', 'Acquisition',
           'account_entry', 'simple_break_even_ratio', 'simple_target_ratio', 'move_units', 'sensitivity']
