"""In-process simulated spot exchange with the CCXT method shapes the engine uses.

Paper orders flow through exactly the same submit, confirm, fee and ledger
code as live orders; only the client differs. Fills are immediate. When the
engine supplies a fresh public order book the fill walks it, level by level,
with each level moved ``BOOK_LATENCY_BPS`` against the order (the spread and
depth are then the market's own, as in the micro paper venue). Otherwise, or
when the book cannot absorb the order or its fill is more than
``BOOK_MAX_DEVIATION`` from the caller's reference quote, the fill is the
reference quote (the executable bid/ask the caller just observed) adjusted by
the configured slippage. The configured fee is charged in USDT either way.
Nothing here touches a network: the book comes from a callable the engine owns. Failure injection hooks let tests exercise the
uncertain, rejected and partially filled paths without a real exchange.
"""
from __future__ import annotations

import math
import threading
import time
import uuid

import ccxt


BOOK_LATENCY_BPS = 3.0        # each walked level moves this much against the order (the micro venue's margin)
BOOK_MAX_DEVIATION = 0.01     # a book fill more than 1 % from the reference quote is not used


def walk_levels(levels, *, quote: float | None = None, base: float | None = None) -> tuple[float, float, int] | None:
    """(base filled, quote paid or received, levels used) to spend ``quote`` or sell ``base``.

    ``levels`` are (price, quantity) pairs, best first. None when the visible
    levels cannot absorb the whole order or a level is unusable.
    """
    if (quote is None) == (base is None):
        raise ValueError('walk a quote amount or a base amount, not both')
    remaining = quote if quote is not None else base
    got_base = got_quote = 0.0
    used = 0
    for level in levels or ():
        try:
            price, quantity = float(level[0]), float(level[1])
        except (TypeError, ValueError, IndexError):
            return None
        if not (math.isfinite(price) and price > 0 and math.isfinite(quantity) and quantity > 0):
            return None
        used += 1
        if quote is not None:
            take = min(remaining, price * quantity)
            got_quote += take
            got_base += take / price
        else:
            take = min(remaining, quantity)
            got_base += take
            got_quote += take * price
        remaining -= take
        if remaining <= (quote if quote is not None else base) * 1e-12:
            return got_base, got_quote, used
    return None


class PaperExchange:
    id = 'paper'
    has = {'createMarketBuyOrderWithCost': True, 'createMarketOrder': True, 'fetchOrder': True,
           'fetchBalance': True, 'fetchOrderTrades': False, 'fetchOpenOrders': True, 'fetchClosedOrders': True,
           'fetchTickers': False}
    rateLimit = 0

    def __init__(self, exchange_name: str, *, catalogue, settings, balances, precision_client=None, book=None):
        """``catalogue``, ``settings`` and ``balances`` are callables so the
        simulation always sees the engine's current market list, fee/slippage
        assumptions and paper ledger. ``book(symbol)`` (optional) returns a
        fresh public order book ``{'bids': [[price, qty], ...], 'asks': [...]}``
        or None; any failure there falls back to the flat slippage."""
        self.exchange_name = exchange_name
        self._catalogue, self._settings, self._balances = catalogue, settings, balances
        self._book = book
        self._precision = precision_client
        self.orders: dict[str, dict] = {}
        self.lock = threading.Lock()
        # Failure injection (tests only): an exception raised by the next create;
        # ``ambiguous`` raises it *after* the order was accepted and filled, like
        # a timeout on the response leg; ``partial`` scales the next fill.
        self.fail_next: Exception | None = None
        self.fee_currency: str | None = None     # bill the fee in another asset (tests the MX-style fee path)
        self.ambiguous_next = False
        self.partial_next: float | None = None
        self.latency_s = 0.0

    # --------------------------------------------------------------- markets
    @property
    def markets(self):
        return self._catalogue()

    def load_markets(self, reload=False):
        return self._catalogue()

    def market(self, symbol: str) -> dict:
        market = self._catalogue().get(symbol)
        if not isinstance(market, dict):
            raise ccxt.BadSymbol(f'paper does not have market symbol {symbol}')
        return market

    def cost_to_precision(self, symbol, cost):
        if self._precision is not None and hasattr(self._precision, 'cost_to_precision'):
            try:
                return self._precision.cost_to_precision(symbol, cost)
            except Exception:
                pass
        return repr(float(cost))

    def amount_to_precision(self, symbol, amount):
        if self._precision is not None and hasattr(self._precision, 'amount_to_precision'):
            try:
                return self._precision.amount_to_precision(symbol, amount)
            except Exception:
                pass
        return repr(float(amount))

    # --------------------------------------------------------------- account
    def fetch_balance(self, params=None) -> dict:
        free = dict(self._balances())
        return {'free': free, 'used': {k: 0.0 for k in free}, 'total': dict(free)}

    # ---------------------------------------------------------------- orders
    def _reference_price(self, params: dict | None) -> float:
        price = (params or {}).get('reference_price')
        try:
            value = float(price)
        except (TypeError, ValueError):
            raise ccxt.InvalidOrder('paper orders need a reference_price parameter') from None
        if not math.isfinite(value) or value <= 0:
            raise ccxt.InvalidOrder('paper reference_price must be a finite positive number')
        return value

    def _book_fill(self, symbol: str, side: str, reference: float, *, quote: float | None = None,
                   base: float | None = None) -> tuple[float, float, int] | None:
        """(base, quote, levels used) from walking the engine's public book, or None for the flat slippage."""
        if self._book is None:
            return None
        try:
            book = self._book(symbol)
        except Exception:
            return None
        if not isinstance(book, dict):
            return None
        levels = book.get('asks' if side == 'buy' else 'bids')
        if not isinstance(levels, list) or not levels:
            return None
        shift = BOOK_LATENCY_BPS / 10_000
        moved = []
        for level in levels:
            if not isinstance(level, (list, tuple)) or len(level) < 2:
                return None
            try:
                price = float(level[0])
            except (TypeError, ValueError):
                return None
            moved.append((price * (1 + shift) if side == 'buy' else price * (1 - shift), level[1]))
        walked = walk_levels(moved, quote=quote, base=base)
        if walked is None or walked[0] <= 0:
            return None
        if abs(walked[1] / walked[0] / reference - 1) > BOOK_MAX_DEVIATION:
            return None
        return walked

    def _accept(self, symbol: str, side: str, filled: float, gross: float, average: float, params: dict | None,
                fill_model: str = 'flat slippage') -> dict:
        settings = self._settings()
        fee = gross * float(settings['fee_pct']) / 100
        fee_currency = self.fee_currency or symbol.split('/')[1]
        order_id = 'paper-' + uuid.uuid4().hex[:12]
        order = {'id': order_id, 'clientOrderId': (params or {}).get('clientOrderId'), 'symbol': symbol,
                 'type': 'market', 'side': side, 'status': 'closed', 'filled': filled, 'remaining': 0.0,
                 'amount': filled, 'cost': gross, 'average': average, 'price': average,
                 'fee': {'currency': fee_currency, 'cost': fee}, 'fees': [],
                 'timestamp': int(time.time() * 1000), 'info': {'simulated': True, 'fill_model': fill_model}}
        if self.partial_next is not None:
            ratio = max(0.0, min(1.0, float(self.partial_next)))
            self.partial_next = None
            order.update({'status': 'canceled', 'filled': filled * ratio, 'remaining': filled * (1 - ratio),
                          'cost': gross * ratio, 'fee': {'currency': fee_currency, 'cost': fee * ratio}})
        with self.lock:
            self.orders[order_id] = order
        if self.latency_s:
            time.sleep(self.latency_s)
        if self.fail_next is not None:
            error, self.fail_next = self.fail_next, None
            if not self.ambiguous_next:
                with self.lock:
                    self.orders.pop(order_id, None)   # a definite rejection accepted nothing
            self.ambiguous_next = False
            raise error
        return dict(order)

    def create_market_buy_order_with_cost(self, symbol: str, cost, params=None) -> dict:
        self.market(symbol)
        reference = self._reference_price(params)
        settings = self._settings()
        gross = float(cost)
        if not math.isfinite(gross) or gross <= 0:
            raise ccxt.InvalidOrder('paper buy cost must be positive')
        walked = self._book_fill(symbol, 'buy', reference, quote=gross)
        if walked is not None:
            filled, paid, levels = walked
            return self._accept(symbol, 'buy', filled, paid, paid / filled, params, f'order book, {levels} levels')
        fill_price = reference * (1 + float(settings['slippage_pct']) / 100)
        return self._accept(symbol, 'buy', gross / fill_price, gross, fill_price, params)

    def create_order(self, symbol: str, type_: str, side: str, amount, price=None, params=None) -> dict:
        self.market(symbol)
        if type_ != 'market' or side != 'sell':
            raise ccxt.NotSupported('paper exchange simulates market sells and cost-bounded market buys only')
        reference = self._reference_price(params)
        settings = self._settings()
        quantity = float(amount)
        if not math.isfinite(quantity) or quantity <= 0:
            raise ccxt.InvalidOrder('paper sell amount must be positive')
        walked = self._book_fill(symbol, 'sell', reference, base=quantity)
        if walked is not None:
            sold, received, levels = walked
            return self._accept(symbol, 'sell', sold, received, received / sold, params, f'order book, {levels} levels')
        fill_price = reference * (1 - float(settings['slippage_pct']) / 100)
        return self._accept(symbol, 'sell', quantity, quantity * fill_price, fill_price, params)

    def fetch_order(self, order_id, symbol=None, params=None) -> dict:
        client_id = (params or {}).get('clientOrderId')
        with self.lock:
            if order_id in self.orders:
                return dict(self.orders[order_id])
            for order in self.orders.values():
                if client_id and order.get('clientOrderId') == client_id:
                    return dict(order)
        raise ccxt.OrderNotFound(f'paper order {order_id or client_id} not found')

    def fetch_open_orders(self, symbol=None, since=None, limit=None, params=None) -> list:
        return []

    def fetch_closed_orders(self, symbol=None, since=None, limit=None, params=None) -> list:
        with self.lock:
            return [dict(o) for o in self.orders.values() if symbol in (None, o['symbol'])
                    and (since is None or o['timestamp'] >= since)]
