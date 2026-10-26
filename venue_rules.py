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
  3 offline. ``tradeSideType``: 1 all, 2 buy only, 3 sell only, 4 close.
  ``makerCommission``/``takerCommission`` in ``exchangeInfo`` are the public
  schedule, not this account's rate (that needs the signed ``/api/v3/tradeFee``
  endpoint, which this code never calls).
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
        if self.loaded_at is None:
            return None
        return max(0.0, now - self.loaded_at)

    def tradable(self, side: str, *, now: float | None = None, max_age_seconds: float | None = None) -> tuple[bool, str | None, str]:
        """``(ok, code, detail)`` for a market order on ``side`` ('BUY' or 'SELL')."""
        if self.spot is not True:
            return False, MARKET_NOT_SPOT, f'{self.symbol} is not a listed spot market'
        if self.active is not True:
            return False, MARKET_INACTIVE, f'{self.symbol} is not trading ({self.status})'
        allowed = {'all': ('BUY', 'SELL'), 'buy_only': ('BUY',), 'sell_only': ('SELL',),
                   'close_only': ('SELL',), 'unknown': ('BUY', 'SELL')}[self.side_permission]
        if side not in allowed:
            return False, SIDE_NOT_ALLOWED, f'{self.symbol} accepts {self.side_permission.replace("_", " ")} orders'
        if self.market_orders is False:
            return False, MARKET_ORDERS_UNSUPPORTED, f'{self.symbol} does not accept market orders'
        if side == 'BUY' and self.market_buy_by_cost is False:
            return False, COST_BUY_UNSUPPORTED, f'{self.symbol} does not accept market buys by quote amount'
        if max_age_seconds is not None and now is not None:
            age = self.age_seconds(now)
            if age is None or age > max_age_seconds:
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
