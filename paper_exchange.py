"""In-process simulated spot exchange with the CCXT method shapes the engine uses.

Paper orders flow through exactly the same submit, confirm, fee and ledger
code as live orders; only the client differs. Fills are immediate at the
caller's reference quote (the executable bid/ask it just observed) adjusted
by the configured slippage, with the configured fee charged in USDT. Nothing
here touches a network. Failure injection hooks let tests exercise the
uncertain, rejected and partially filled paths without a real exchange.
"""
from __future__ import annotations

import math
import threading
import time
import uuid

import ccxt


class PaperExchange:
    id = 'paper'
    has = {'createMarketBuyOrderWithCost': True, 'createMarketOrder': True, 'fetchOrder': True,
           'fetchBalance': True, 'fetchOrderTrades': False, 'fetchOpenOrders': True, 'fetchClosedOrders': True,
           'fetchTickers': False}
    rateLimit = 0

    def __init__(self, exchange_name: str, *, catalogue, settings, balances, precision_client=None):
        """``catalogue``, ``settings`` and ``balances`` are callables so the
        simulation always sees the engine's current market list, fee/slippage
        assumptions and paper ledger."""
        self.exchange_name = exchange_name
        self._catalogue, self._settings, self._balances = catalogue, settings, balances
        self._precision = precision_client
        self.orders: dict[str, dict] = {}
        self.lock = threading.Lock()
        # Failure injection (tests only): an exception raised by the next create;
        # ``ambiguous`` raises it *after* the order was accepted and filled, like
        # a timeout on the response leg; ``partial`` scales the next fill.
        self.fail_next: Exception | None = None
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

    def _accept(self, symbol: str, side: str, filled: float, gross: float, average: float, params: dict | None) -> dict:
        settings = self._settings()
        fee = gross * float(settings['fee_pct']) / 100
        order_id = 'paper-' + uuid.uuid4().hex[:12]
        order = {'id': order_id, 'clientOrderId': (params or {}).get('clientOrderId'), 'symbol': symbol,
                 'type': 'market', 'side': side, 'status': 'closed', 'filled': filled, 'remaining': 0.0,
                 'amount': filled, 'cost': gross, 'average': average, 'price': average,
                 'fee': {'currency': symbol.split('/')[1], 'cost': fee}, 'fees': [],
                 'timestamp': int(time.time() * 1000), 'info': {'simulated': True}}
        if self.partial_next is not None:
            ratio = max(0.0, min(1.0, float(self.partial_next)))
            self.partial_next = None
            order.update({'status': 'canceled', 'filled': filled * ratio, 'remaining': filled * (1 - ratio),
                          'cost': gross * ratio, 'fee': {'currency': symbol.split('/')[1], 'cost': fee * ratio}})
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
