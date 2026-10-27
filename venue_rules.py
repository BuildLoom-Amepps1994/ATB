"""Normalized venue trading rules for one spot symbol, from the public catalogue.

One object answers, for every component that sizes, gates, simulates or
displays a micro-trading order, the questions the venue's rules decide:
may this pair be traded now and on which side, which order types, which
quantity/price/quote-amount increments, which minimums and maximums (order
type specific), which fee schedule, and how old and uncertain the metadata is.

Sources and semantics (checked 2026-10-26; recheck when the venue changes):

* CCXT 4.5.90 normalizes MEXC ``exchangeInfo`` with ``precisionMode = TICK_SIZE``:
  ``precision.amount = 10**-baseAssetPrecision`` (a quantity step),
  ``precision.price = 10**-quoteAssetPrecision`` (a price tick),
  ``limits.amount.min = baseSizePrecision`` and ``limits.cost.min =
  quoteAmountPrecision``. Despite their names, MEXC documents
  ``baseSizePrecision`` as "min order quantity" and ``quoteAmountPrecision``
  as "min order amount", which is the limit-order minimum. The market-order
  minimum is ``quoteAmountPrecisionMarket`` ("min order amount in market
  order") and the market maximum ``maxQuoteAmountMarket``; CCXT leaves both in
  ``info`` only, so they are read from there. ``status``: 1 online, 2 pause,
  3 offline. ``tradeSideType``: 1 all, 2 buy only, 3 sell only, 4 close. A
  value outside these allows sells only: a holding can still be closed, but
  no entry opens on a side rule the code cannot read.
  ``makerCommission``/``takerCommission`` in ``exchangeInfo`` are the public
  schedule, not this account's rate. That needs the signed ``/api/v3/tradeFee``
  endpoint, which this code never calls; :func:`parse_mexc_symbol_commission`
  validates a response to it that a caller supplies.
* Kraken through CCXT: ``TICK_SIZE`` precision, ``limits.amount.min = ordermin``,
  ``limits.cost.min = costmin``; fees are CCXT's published tier defaults.
* A missing field is unknown, never zero. Unknown values are listed in
  ``issues`` so the dashboard and decision records can say what was assumed.
* The quote-amount increment of a market buy by cost follows CCXT's own
  ``cost_to_precision`` convention (the price precision). The venue may round
  differently; the sizing component rounds down to it and keeps a reserve.
* Whether a market buy's fee is billed in the quote asset (an extra debit) or
  the base asset (fewer coins received) is not published per account. It is
  ``unknown`` unless the caller states it, and sizing reserves for both.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from decimal import Decimal

import money

TICK_SIZE, DECIMAL_PLACES, SIGNIFICANT_DIGITS = 4, 2, 3
SIDE_PERMISSIONS = {'1': 'all', '2': 'buy_only', '3': 'sell_only', '4': 'close_only'}
MEXC_STATUS = {'1': 'online', '2': 'paused', '3': 'offline'}

# Reason codes shared with the sizing component, the risk gate and the dashboard.
MARKET_UNKNOWN = 'MARKET_METADATA_MISSING'
MARKET_INACTIVE = 'MARKET_INACTIVE'
MARKET_NOT_SPOT = 'MARKET_NOT_SPOT'
SIDE_NOT_ALLOWED = 'SIDE_NOT_ALLOWED'
MARKET_ORDERS_UNSUPPORTED = 'MARKET_ORDERS_UNSUPPORTED'
COST_BUY_UNSUPPORTED = 'MARKET_BUY_BY_COST_UNSUPPORTED'
METADATA_STALE = 'MARKET_METADATA_STALE'


def _decimal(value, issues: list[str], name: str) -> Decimal | None:
    if value is None:
        return None
    try:
        number = money.parse(value, field=name)
    except money.MoneyError:
        issues.append(f'{name} is malformed and treated as unknown')
        return None
    return number


def _step(value, mode: int | None, issues: list[str], name: str) -> Decimal | None:
    """A CCXT precision value as a positive step, under the client's precision mode."""
    if value is None:
        return None
    if mode in (None, TICK_SIZE):
        step = _decimal(value, issues, name)
        if step is not None and step <= 0:
            issues.append(f'{name} is not positive and treated as unknown')
            return None
        return step
    if mode == DECIMAL_PLACES:
        try:
            places = float(value)
        except (TypeError, ValueError):
            issues.append(f'{name} is malformed and treated as unknown')
            return None
        if not math.isfinite(places) or places != int(places) or not 0 <= places <= 30:
            issues.append(f'{name} decimal places are out of range')
            return None
        return Decimal(1).scaleb(-int(places))
    issues.append(f'{name} uses significant-digit precision, which this sizing does not support')
    return None


@dataclass(frozen=True)
class VenueRules:
    exchange: str
    symbol: str
    base: str
    quote: str
    active: bool | None
    spot: bool | None
    status: str                       # online | paused | offline | active | inactive | unknown
    side_permission: str              # all | buy_only | sell_only | close_only | unknown
    market_orders: bool | None        # MARKET listed in the venue's order types (None: list absent)
    market_buy_by_cost: bool | None   # quote-amount market buys accepted (None: not stated)
    amount_step: Decimal | None
    price_tick: Decimal | None
    cost_step: Decimal | None
    min_amount: Decimal | None
    max_amount: Decimal | None
    min_cost_market: Decimal | None
    min_cost_limit: Decimal | None
    max_cost_market: Decimal | None
    max_cost_limit: Decimal | None
    taker_fee: Decimal | None
    maker_fee: Decimal | None
    fee_source: str
    buy_fee_currency: str             # quote | base | unknown
    sell_fee_currency: str            # quote | base | unknown
    loaded_at: float | None           # wall-clock time the catalogue was loaded
    source: str
    issues: tuple[str, ...] = field(default_factory=tuple)

    # ------------------------------------------------------------ questions
    def market_min_cost(self) -> Decimal | None:
        """The minimum quote amount of a market order, with the limit minimum as the fallback."""
        return self.min_cost_market if self.min_cost_market is not None else self.min_cost_limit

    def market_max_cost(self) -> Decimal | None:
        return self.max_cost_market if self.max_cost_market is not None else self.max_cost_limit

    def age_seconds(self, now: float) -> float | None:
        """Seconds since the rules were loaded, or None when that cannot be known (a future or unreadable time)."""
        if self.loaded_at is None or not math.isfinite(now) or not math.isfinite(self.loaded_at) or now < self.loaded_at:
            return None
        return now - self.loaded_at

    def tradable(self, side: str, *, now: float | None = None, max_age_seconds: float | None = None) -> tuple[bool, str | None, str]:
        """``(ok, code, detail)`` for a market order on ``side`` ('BUY' or 'SELL')."""
        if self.spot is not True:
            return False, MARKET_NOT_SPOT, f'{self.symbol} is not a listed spot market'
        if self.active is not True:
            return False, MARKET_INACTIVE, f'{self.symbol} is not trading ({self.status})'
        allowed = {'all': ('BUY', 'SELL'), 'buy_only': ('BUY',), 'sell_only': ('SELL',),
                   'close_only': ('SELL',), 'unknown': ('SELL',)}[self.side_permission]   # unreadable: exit only
        if side not in allowed:
            return False, SIDE_NOT_ALLOWED, f'{self.symbol} accepts {self.side_permission.replace("_", " ")} orders'
        if self.market_orders is False:
            return False, MARKET_ORDERS_UNSUPPORTED, f'{self.symbol} does not accept market orders'
        if side == 'BUY' and self.market_buy_by_cost is False:
            return False, COST_BUY_UNSUPPORTED, f'{self.symbol} does not accept market buys by quote amount'
        if max_age_seconds is not None and now is not None:
            age = self.age_seconds(now)
            if age is None or not age <= max_age_seconds:         # NaN limits fail too
                return False, METADATA_STALE, f'{self.symbol} market rules are older than {max_age_seconds:.0f} s'
        return True, None, ''

    def as_dict(self) -> dict:
        out = {}
        for key, value in self.__dict__.items():
            out[key] = money.text(value) if isinstance(value, Decimal) else (list(value) if isinstance(value, tuple) else value)
        return out


def normalize(market: dict | None, *, exchange: str, symbol: str, loaded_at: float | None,
              precision_mode: int | None = TICK_SIZE, source: str = 'ccxt public catalogue',
              buy_fee_currency: str = 'unknown', sell_fee_currency: str = 'unknown',
              market_buy_by_cost: bool | None = None) -> VenueRules:
    """Rules for ``symbol`` from a CCXT market dict. A missing market yields an untradable record."""
    issues: list[str] = []
    if not isinstance(market, dict):
        base, _, quote = symbol.partition('/')
        return VenueRules(exchange=exchange, symbol=symbol, base=base, quote=quote, active=None, spot=None,
                          status='unknown', side_permission='unknown', market_orders=None,
                          market_buy_by_cost=None, amount_step=None, price_tick=None, cost_step=None,
                          min_amount=None, max_amount=None, min_cost_market=None, min_cost_limit=None,
                          max_cost_market=None, max_cost_limit=None, taker_fee=None, maker_fee=None,
                          fee_source='unknown', buy_fee_currency='unknown', sell_fee_currency='unknown',
                          loaded_at=loaded_at, source=source, issues=('market is not in the catalogue',))
    info = market.get('info') if isinstance(market.get('info'), dict) else {}
    base = str(market.get('base') or symbol.split('/')[0])
    quote = str(market.get('quote') or symbol.split('/')[-1])
    spot = market.get('spot') is True and market.get('type', 'spot') == 'spot' and not any(
        market.get(key) for key in ('contract', 'swap', 'future', 'option'))
    active = market.get('active') if isinstance(market.get('active'), bool) else None
    raw_status = info.get('status')
    if exchange == 'mexc' and raw_status is not None:
        status = MEXC_STATUS.get(str(raw_status), 'unknown')
        if status != 'online':
            active = False
    else:
        status = 'active' if active else 'inactive' if active is False else 'unknown'
    if active is None:
        issues.append('active flag missing; the pair is treated as not trading')
    side_raw = info.get('tradeSideType')
    side_permission = SIDE_PERMISSIONS.get(str(side_raw), 'unknown') if side_raw is not None else 'all'
    if side_raw is not None and side_permission == 'unknown':
        issues.append('tradeSideType has an unrecognised value')
    order_types = info.get('orderTypes')
    market_orders = ('MARKET' in order_types) if isinstance(order_types, list) else None
    if market_orders is None:
        issues.append('order types not listed; market orders assumed available')
    if market_buy_by_cost is None:
        allowed = info.get('quoteOrderQtyMarketAllowed')
        market_buy_by_cost = allowed if isinstance(allowed, bool) else None
    precision = market.get('precision') if isinstance(market.get('precision'), dict) else {}
    limits = market.get('limits') if isinstance(market.get('limits'), dict) else {}
    amount_limits = limits.get('amount') if isinstance(limits.get('amount'), dict) else {}
    cost_limits = limits.get('cost') if isinstance(limits.get('cost'), dict) else {}
    amount_step = _step(precision.get('amount'), precision_mode, issues, 'amount precision')
    price_tick = _step(precision.get('price'), precision_mode, issues, 'price precision')
    if amount_step is None:
        issues.append('quantity step unknown; sell sizes cannot be checked against it')
    min_amount = _decimal(amount_limits.get('min'), issues, 'minimum quantity')
    max_amount = _decimal(amount_limits.get('max'), issues, 'maximum quantity')
    min_cost_limit = _decimal(cost_limits.get('min'), issues, 'minimum order amount')
    max_cost_limit = _decimal(cost_limits.get('max'), issues, 'maximum order amount')
    min_cost_market = _decimal(info.get('quoteAmountPrecisionMarket'), issues, 'minimum market order amount')
    max_cost_market = _decimal(info.get('maxQuoteAmountMarket'), issues, 'maximum market order amount')
    if min_cost_market is None:
        issues.append('market-order minimum not published; the limit-order minimum is used'
                      if min_cost_limit is not None else 'no minimum order amount published')
    taker, maker = market.get('taker'), market.get('maker')
    taker_fee = _decimal(taker, issues, 'taker fee') if taker is not None else None
    maker_fee = _decimal(maker, issues, 'maker fee') if maker is not None else None
    fee_source = 'public catalogue schedule (not this account)' if taker_fee is not None else 'unknown'
    return VenueRules(exchange=exchange, symbol=symbol, base=base, quote=quote, active=active, spot=spot,
                      status=status, side_permission=side_permission, market_orders=market_orders,
                      market_buy_by_cost=market_buy_by_cost, amount_step=amount_step, price_tick=price_tick,
                      cost_step=price_tick, min_amount=min_amount, max_amount=max_amount,
                      min_cost_market=min_cost_market, min_cost_limit=min_cost_limit,
                      max_cost_market=max_cost_market, max_cost_limit=max_cost_limit,
                      taker_fee=taker_fee, maker_fee=maker_fee, fee_source=fee_source,
                      buy_fee_currency=buy_fee_currency, sell_fee_currency=sell_fee_currency,
                      loaded_at=loaded_at, source=source, issues=tuple(dict.fromkeys(issues)))


# ------------------------------------------------------------ account commission
MAX_COMMISSION = Decimal('0.01')     # 1 % per leg, far above any MEXC spot rate: a larger value is malformed


class CommissionError(ValueError):
    """A symbol-commission response that cannot be used; the message says why."""


@dataclass(frozen=True)
class AccountCommission:
    """This account's fee rates for one symbol, from a supplied MEXC ``GET /api/v3/tradeFee`` response.

    Rates are fractions per leg (0.0005 is 0.05 %). Nothing here signs or
    sends that request: the response comes from the caller.
    """
    exchange: str
    symbol: str
    maker: Decimal
    taker: Decimal
    rpi_taker: Decimal | None        # the RPI order rate, when the venue states one
    venue_ms: int                    # the response's own timestamp, on the venue clock
    received_at: float               # local wall-clock time the caller received the response
    source: str = 'MEXC signed tradeFee (supplied response)'

    def age_seconds(self, now) -> float | None:
        """Seconds since the response was received, or None when that cannot be known (a future or unreadable time)."""
        now, received = _wall_clock(now), _wall_clock(self.received_at)
        if now is None or received is None or now < received:
            return None
        age = now - received
        return age if math.isfinite(age) else None

    def as_dict(self) -> dict:
        return {key: money.text(value) if isinstance(value, Decimal) else value for key, value in self.__dict__.items()}


def _wall_clock(value) -> float | None:
    """A wall-clock reading as a finite float; None when missing, boolean, not a number or not finite."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) else None


def _no_constant(name):
    raise ValueError(f'{name} is not a number')


def _unique_keys(pairs):
    names = [name for name, _ in pairs]
    if len(set(names)) != len(names):
        raise ValueError('a key is repeated')      # json.loads keeps the last copy, so 'code' could be overridden
    return dict(pairs)


def _commission(value, name: str, *, allow_negative: bool) -> Decimal:
    if value is None:
        raise CommissionError(f'{name} is missing')
    try:
        rate = money.parse(value, field=name, allow_negative=allow_negative)
    except money.MoneyError as exc:
        raise CommissionError(str(exc)) from None
    except (ArithmeticError, TypeError, ValueError):          # a hostile number type: still fail closed
        raise CommissionError(f'{name} is not a valid number') from None
    if rate.copy_abs() > MAX_COMMISSION:                      # exact: abs() would round to the caller's context
        raise CommissionError(f'{name} is beyond 1 %, above any spot fee')
    try:
        # A JSON number arrives as a Decimal of any length; its stored form must read back, as a string's would.
        money.from_text(money.text(rate), field=name, allow_negative=allow_negative)
    except money.MoneyError:
        raise CommissionError(f'{name} has more digits than a stored rate may') from None
    return rate


def parse_mexc_symbol_commission(response, *, symbol: str, received_at: float) -> AccountCommission:
    """Validate a supplied response of MEXC's signed ``GET /api/v3/tradeFee`` for ``symbol``.

    The documented response (checked 2026-10-27) is ``{"data": {"makerCommission":
    0E-18, "takerCommission": 0.000500000000000000, "rpiTakerCommission": null},
    "code": 0, "msg": "success", "timestamp": 1781149594924}``. It does not name
    the symbol, so the caller passes the one it asked for. JSON text is parsed
    with Decimal numbers, so no rate passes through a float.

    Anything else fails closed with :class:`CommissionError`: JSON text that
    repeats a key or holds a number Decimal cannot represent, a ``code`` that
    is not the integer 0, no ``data`` object, a missing, malformed, negative or
    implausible (above 1 %) taker rate, a maker rate outside -1 % to 1 % (a
    rebate is negative), a rate whose stored form would not read back (more
    digits than :data:`money.MAX_TEXT` allows a string), an RPI rate that is
    stated but is not a valid taker rate, or a missing, non-integer or
    non-positive timestamp. A repeated key can only be
    seen in text: pass the response as received, not as a decoded dict.
    """
    if not isinstance(symbol, str) or not symbol.strip():
        raise CommissionError('the symbol the response answers is missing')
    received = _wall_clock(received_at)
    if received is None:
        raise CommissionError('the time the response was received is missing')
    if isinstance(response, (bytes, bytearray)):
        try:
            response = bytes(response).decode('utf-8')
        except UnicodeDecodeError:
            raise CommissionError('the response is not UTF-8 text') from None
    if isinstance(response, str):
        if len(response) > 4096:
            raise CommissionError('the response is too long for a symbol commission')
        try:
            response = json.loads(response, parse_float=Decimal, parse_constant=_no_constant,
                                  object_pairs_hook=_unique_keys)
        except (ValueError, ArithmeticError, RecursionError):     # Decimal's InvalidOperation is an ArithmeticError
            raise CommissionError('the response is not valid JSON') from None
    if not isinstance(response, dict):
        raise CommissionError('the response is not a JSON object')
    code = response.get('code')
    if type(code) is not int:                      # a bool is not a code
        raise CommissionError('the response has no integer code')
    if code != 0:
        shown = code if abs(code) < 10 ** 12 else 'out of range'      # a huge int cannot even be formatted
        raise CommissionError(f'the venue answered code {shown}, not 0')
    data = response.get('data')
    if not isinstance(data, dict):
        raise CommissionError('the response has no data object')
    taker = _commission(data.get('takerCommission'), 'takerCommission', allow_negative=False)
    maker = _commission(data.get('makerCommission'), 'makerCommission', allow_negative=True)
    rpi = data.get('rpiTakerCommission')
    rpi_taker = None if rpi is None else _commission(rpi, 'rpiTakerCommission', allow_negative=False)
    stamp = response.get('timestamp')
    if type(stamp) is not int or stamp <= 0:
        raise CommissionError('the response has no timestamp')
    return AccountCommission(exchange='mexc', symbol=symbol, maker=maker, taker=taker, rpi_taker=rpi_taker,
                             venue_ms=stamp, received_at=received)
