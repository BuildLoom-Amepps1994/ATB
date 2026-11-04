"""Regression tests for the venue and micro lenses of the 2026-11-04 audit.

* A venue error response that CCXT maps to a bare ``ExchangeError`` (MEXC
  code 30000, "suspended transaction for the symbol") was journaled
  uncertain and halted live trading durably, switching off every protective
  exit, although the venue had refused the request and held no order for
  its client id.
* Leveraged tokens with a 2x or 4x suffix passed the spot filter.
* A remainder below one amount step was not dust, so it blocked entries as
  a pending protective exit that could never be sent.
* The micro late-fill check compared the venue's exact quantity with the
  journal's float without the tolerance the orphan check applies.
* The micro owned-symbols fallback silently claimed every paper position.

All exchanges are fakes; no network, no credentials.
"""

from decimal import Decimal

import ccxt
import pytest

import orders
import storage
import trader
from test_engine import FakeExchange, engine  # noqa: F401  (fixture)


class LookupExchange(FakeExchange):
    """A venue whose open and recent order lists can be read."""
    has = {'fetchOpenOrders': True, 'fetchClosedOrders': True}

    def __init__(self):
        super().__init__()
        self.lookup_error = None

    def fetch_open_orders(self, symbol):
        if self.lookup_error:
            raise self.lookup_error
        return []

    def fetch_closed_orders(self, symbol, since=None):
        if self.lookup_error:
            raise self.lookup_error
        return []


def _arm(engine, monkeypatch):
    client = LookupExchange()
    engine.private = client
    engine.mode, engine.running = 'live', True
    engine.stop_event.clear()
    monkeypatch.setattr(trader.time, 'sleep', lambda seconds: None)   # the second lookup pass waits a second
    return client


def _venue_refusal():
    return ccxt.ExchangeError('mexc {"code":30000,"msg":"suspended transaction for the symbol"}')


# ------------------------------------------------------------ venue refusal
def test_a_venue_refusal_with_no_order_at_the_venue_is_a_rejection_not_a_halt(engine, monkeypatch):
    client = _arm(engine, monkeypatch)
    client.order_error = _venue_refusal()
    assert not engine._buy('BTC/USDT', 200, 100, .04)
    assert len(client.orders) == 1 and not engine.live_halted and not engine._durable_live_block()
    row = storage.rows('SELECT status,note FROM order_journal')[0]
    assert row['status'] == 'rejected' and row['note'] == 'ExchangeError (refused; no order at the venue)'
    assert 'rejected by the exchange: ExchangeError (refused; no order at the venue)' in engine.risk_block
    assert storage.unresolved_orders() == []


def test_the_same_refusal_with_a_failed_lookup_stays_uncertain(engine, monkeypatch):
    client = _arm(engine, monkeypatch)
    client.order_error = _venue_refusal()
    client.lookup_error = ccxt.NetworkError('lookup unavailable')
    assert not engine._buy('BTC/USDT', 200, 100, .04)
    assert engine.live_halted
    assert storage.rows('SELECT status FROM order_journal') == [{'status': 'uncertain'}]


def test_a_transport_error_with_no_order_found_stays_uncertain(engine, monkeypatch):
    client = _arm(engine, monkeypatch)
    client.order_error = ccxt.RequestTimeout('synthetic transport timeout')
    assert not engine._buy('BTC/USDT', 200, 100, .04)
    assert engine.live_halted
    assert storage.rows('SELECT status FROM order_journal') == [{'status': 'uncertain'}]


def test_a_located_order_after_a_venue_error_is_booked(engine, monkeypatch):
    client = _arm(engine, monkeypatch)
    client.order_error = _venue_refusal()

    def listed(symbol):
        # The venue holds the order it answered with an error: continue with its record.
        intent = storage.rows('SELECT intent_id FROM order_journal')[0]['intent_id']
        return [{**client.fill, 'clientOrderId': storage.client_order_id(intent)}]
    client.fetch_open_orders = listed
    assert engine._buy('BTC/USDT', 200, 100, .04)
    assert not engine.live_halted and storage.rows('SELECT status FROM order_journal') == [{'status': 'accounted'}]


def test_venue_error_responses_exclude_transport_and_unreadable_answers():
    assert orders.venue_error_response(ccxt.ExchangeError('x')) and orders.venue_error_response(ccxt.OperationRejected('x'))
    for exc in (ccxt.NetworkError('x'), ccxt.RequestTimeout('x'), ccxt.RateLimitExceeded('x'), ccxt.InvalidNonce('x'),
                ccxt.BadResponse('x'), ccxt.ExchangeNotAvailable('x'), RuntimeError('x')):
        assert not orders.venue_error_response(exc)
    assert orders.ConfirmedRefusal in orders.definite_rejections()
    assert 'ValueError' in str(orders.ConfirmedRefusal(ValueError('x')))


# ---------------------------------------------------------------- dust step
def test_a_remainder_below_one_amount_step_is_dust():
    market = {'limits': {'amount': {'min': .00001}, 'cost': {'min': 1}}, 'precision': {'amount': .0001}}
    assert trader.TradingEngine._is_dust({'qty': .00005}, market, 100000.0)      # above both minimums, under the step
    assert not trader.TradingEngine._is_dust({'qty': .0002}, market, 100000.0)
    decimals = {'limits': {}, 'precision': {'amount': 8}}                          # a count of decimals is not a step
    assert not trader.TradingEngine._is_dust({'qty': .5}, decimals, 100.0)


# ------------------------------------------------------------------- micro
def _accounted_micro_intent(engine, quantity):
    intent = storage.create_order_intent(engine.exchange_name, 'ETH/USDT', 'BUY', cost=10, mode='paper', decision_id='cyc_test')
    storage.update_order_intent(intent, 'acked', order_id='venue-1')
    storage.update_order_intent(intent, 'filled', filled_quantity=quantity, filled_price=10.0)
    storage.update_order_intent(intent, 'accounted')
    return intent


def test_a_rounding_residue_is_not_a_micro_late_fill(engine, monkeypatch):
    exact = Decimal('0.123456789012345678901234')
    _accounted_micro_intent(engine, float(exact))
    monkeypatch.setattr(engine.micro, '_venue_record', lambda intent, strict=False: {'id': 'venue-1'})
    monkeypatch.setattr(engine.micro, '_fill', lambda view: {'filled': exact})
    assert engine.micro._late_fills({'cycle_id': 'cyc_test'}) == []
    monkeypatch.setattr(engine.micro, '_fill', lambda view: {'filled': exact * 2})
    late = engine.micro._late_fills({'cycle_id': 'cyc_test'})
    assert len(late) == 1 and late[0][2] == pytest.approx(exact)


def test_the_micro_ownership_fallback_is_audited(engine, monkeypatch):
    assert engine._buy('ETH/USDT', 200, 100, .04)
    original = storage.rows

    def failing(sql, params=()):
        if 'micro_cycles' in sql:
            raise storage.sqlite3.OperationalError('no such table: micro_cycles')
        return original(sql, params)
    monkeypatch.setattr(storage, 'rows', failing)
    assert engine.micro.owned_symbols() == {'ETH/USDT'}
    assert 'Micro cycle table unreadable (OperationalError)' in storage.rows('SELECT message FROM audit ORDER BY id DESC LIMIT 1')[0]['message']
