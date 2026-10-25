"""Order lifecycle helpers: explicit states, client order IDs and read-only lookup.

The durable journal in :mod:`storage` is the state machine's record::

    pending ──(request sent, submitted_at)──▶ acked ──▶ filled ──▶ accounted
       │                                        │          │
       ├──▶ rejected   (documented exchange refusal before any acceptance)
       └──▶ uncertain  (ambiguous outcome; live trading halts until reconciled)

``submitted`` is reserved for an acceptance that carried no exchange id.
Nothing here ever creates, cancels or retries an order: ``locate`` and
``classify`` only read.
"""
from __future__ import annotations

import math
import time
from typing import Any

import ccxt

FINAL_STATUSES = ('closed', 'canceled', 'cancelled', 'expired', 'rejected')


class Unfilled(RuntimeError):
    """A fresh exchange read confirmed a final order state with zero fill."""
OPEN_STATUSES = ('open',)
LOOKUP_WINDOW_MS = 10 * 60 * 1000


def _positive(value) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    return number if math.isfinite(number) and number > 0 and not isinstance(value, bool) else 0.0


def definite_rejections() -> tuple[type, ...]:
    """CCXT errors that document the exchange refused the request outright.

    Transport errors, timeouts and unrecognized responses are never in this
    list: the exchange may have accepted the order anyway.
    """
    return tuple(cls for name in ('InsufficientFunds', 'InvalidOrder', 'AuthenticationError', 'PermissionDenied',
                                  'BadSymbol', 'BadRequest', 'NotSupported')
                 if isinstance(cls := getattr(ccxt, name, None), type))


def classify(order: Any) -> str:
    """Return ``final_filled``, ``final_empty``, ``open`` or ``unknown`` for a CCXT order."""
    if not isinstance(order, dict):
        return 'unknown'
    status, raw_filled = order.get('status'), order.get('filled')
    filled = _positive(raw_filled)
    if status in FINAL_STATUSES:
        if filled:
            return 'final_filled'
        # Only an explicit zero is a confirmed empty fill; a missing field is unknown.
        return 'final_empty' if isinstance(raw_filled, (int, float)) and not isinstance(raw_filled, bool) and raw_filled == 0 else 'unknown'
    if status in OPEN_STATUSES:
        return 'open'
    return 'unknown'


def order_params(exchange_name: str, client_order_id: str) -> dict:
    """Exchange-specific create parameters: idempotency key plus fee flags."""
    params = {'clientOrderId': client_order_id}
    if exchange_name == 'kraken':
        # Kraken charges fees in the base asset by default, which the ledger
        # cannot value; fciq requests the quote (USDT) for both sides.
        params['oflags'] = 'fciq'
    return params


def supports_client_id_lookup(client) -> bool:
    """MEXC resolves fetchOrder by origClientOrderId; Kraken maps clientOrderId to userref."""
    return getattr(client, 'id', None) == 'mexc' and bool(getattr(client, 'has', {}).get('fetchOrder'))


def locate(client, symbol: str, client_order_id: str, *, since_ms: int | None = None) -> dict | None:
    """Read-only search for an order by its client order id.

    Returns the CCXT order when the exchange knows it, ``None`` when it does
    not appear in the exchange's open or recent order lists. Raises when the
    exchange could not be asked, so the caller treats the outcome as
    uncertain rather than absent.
    """
    has = getattr(client, 'has', {}) or {}
    if supports_client_id_lookup(client):
        try:
            found = client.fetch_order(None, symbol, {'clientOrderId': client_order_id})
            if isinstance(found, dict) and found.get('id'):
                return found
        except ccxt.OrderNotFound:
            pass
    candidates: list[dict] = []
    if has.get('fetchOpenOrders'):
        candidates.extend(client.fetch_open_orders(symbol) or [])
    if has.get('fetchClosedOrders'):
        since = since_ms if since_ms is not None else int(time.time() * 1000) - LOOKUP_WINDOW_MS
        candidates.extend(client.fetch_closed_orders(symbol, since) or [])
    for order in candidates:
        if isinstance(order, dict) and str(order.get('clientOrderId') or '') == client_order_id:
            return order
    if not (has.get('fetchOpenOrders') or has.get('fetchClosedOrders') or supports_client_id_lookup(client)):
        raise ccxt.NotSupported('This exchange adapter cannot look an order up by client id')
    return None


def fill_summary(order: dict) -> dict:
    """Filled quantity, cost and average price as finite numbers (or None)."""
    filled, cost = _positive(order.get('filled')), _positive(order.get('cost'))
    average = _positive(order.get('average')) or (cost / filled if filled and cost else 0.0)
    return {'filled': filled or None, 'cost': cost or None, 'average': average or None}


def slippage_bps(intended: float | None, filled: float | None, side: str) -> float | None:
    """Adverse execution in basis points relative to the intended price (positive = worse)."""
    if not _positive(intended) or not _positive(filled):
        return None
    move = (float(filled) - float(intended)) / float(intended) * 10000
    return round(move if side == 'BUY' else -move, 3)
