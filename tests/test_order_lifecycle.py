"""Order state machine, client order IDs, ambiguity lookup and paper/live parity.

All exchanges are fakes. No network, no credentials.
"""
import time
import uuid

import ccxt
import pytest

import orders
import storage
import trader
from test_engine import FakeExchange, arm_fake_live, engine  # noqa: F401  (fixture)


def journal():
    return storage.rows('SELECT * FROM order_journal ORDER BY created_at')


class LookupExchange(FakeExchange):
    """A fake whose create call times out after accepting, like a lost response."""
    has = {'fetchOrder': True, 'fetchOpenOrders': True, 'fetchClosedOrders': True}
    id = 'kraken'

    def __init__(self):
        super().__init__()
        self.accepted = []
        self.lookups = 0

    def create_market_buy_order_with_cost(self, symbol, cost, params=None):
        self.orders.append(('buy', (symbol, cost, params)))
        self.accepted.append({**self.fill, 'id': 'srv-' + str(len(self.accepted)), 'clientOrderId': params['clientOrderId'],
                             'symbol': symbol, 'timestamp': int(time.time() * 1000)})
        raise ccxt.RequestTimeout('response lost')

    def fetch_order(self, order_id, symbol=None, params=None):
        self.lookups += 1
        for order in self.accepted:
            if order['id'] == order_id:
                return dict(order)
        raise ccxt.OrderNotFound(order_id)

    def fetch_open_orders(self, symbol=None, since=None, limit=None, params=None):
        return []

    def fetch_closed_orders(self, symbol=None, since=None, limit=None, params=None):
        self.lookups += 1
        return [dict(o) for o in self.accepted]


# ----------------------------------------------------------------- parity

def test_paper_orders_use_the_same_journal_states_as_live(engine):
    assert engine._buy('BTC/USDT', 800, 100, .04)
    engine._observe_ticker('BTC/USDT', {'bid': 110, 'ask': 110.01, 'timestamp': int(time.time() * 1000), 'quoteVolume': 1})
    assert engine._sell(engine._active_positions('paper')[0], 110, 'exit')
    rows = journal()
    assert [row['mode'] for row in rows] == ['paper', 'paper']
    assert [row['status'] for row in rows] == ['accounted', 'accounted']
    assert all(row['client_order_id'] == row['intent_id'][:32] for row in rows)
    assert all(row['submitted_at'] and row['acked_at'] and row['final_at'] for row in rows)
    assert rows[0]['intended_price'] == 100 and rows[0]['filled_price'] == pytest.approx(100.2)
    assert rows[1]['intended_price'] == 110 and rows[1]['filled_price'] == pytest.approx(110 * .998)
    assert engine.public.orders == []                  # the public client never trades
    assert storage.unresolved_orders() == []           # paper rows never block live
    assert engine._cash('paper') == pytest.approx(9200 + 800 / (100.2 * 1.001) * 110 * .998 * .999)


def test_paper_fill_arithmetic_is_unchanged_by_the_shared_path(engine):
    assert engine._buy('BTC/USDT', 800, 100, .04)
    position = engine._active_positions('paper')[0]
    assert position['cost'] == 800
    assert position['qty'] == pytest.approx(800 / (100 * 1.002 * 1.001))
    trade = storage.rows('SELECT * FROM trades')[0]
    assert trade['fee'] == pytest.approx(800 / 1.001 * .001)
    assert trade['order_id'].startswith('paper-')


def test_paper_ambiguity_blocks_paper_only_and_never_halts_live(engine):
    engine.paper.fail_next = ccxt.RequestTimeout('simulated')
    engine.paper.ambiguous_next = True
    engine.paper.has = {**engine.paper.has, 'fetchOrder': False, 'fetchOpenOrders': False, 'fetchClosedOrders': False}
    assert not engine._buy('BTC/USDT', 200, 100, .04)
    assert not engine.live_halted
    assert engine.risk_block and 'RequestTimeout' in engine.risk_block
    assert journal()[0]['status'] == 'uncertain'
    assert storage.unresolved_orders() == []
    # A fresh engine abandons the simulated intent rather than carrying it forever.
    other = trader.TradingEngine()
    assert journal()[0]['status'] == 'rejected' and journal()[0]['note'] == 'abandoned at startup'
    assert not other.live_halted


def test_paper_partial_fill_keeps_the_remainder_tracked(engine):
    assert engine._buy('BTC/USDT', 800, 100, .04)
    original = engine._active_positions('paper')[0]
    engine.paper.partial_next = .25
    assert engine._sell(original, 100, 'partial')
    remainder = engine._active_positions('paper')[0]
    assert remainder['qty'] == pytest.approx(original['qty'] * .75)
    assert remainder['cost'] == pytest.approx(original['cost'] * .75)


def test_paper_definite_rejection_is_recorded_without_a_halt(engine):
    engine.paper.fail_next = ccxt.InsufficientFunds('simulated')
    assert not engine._buy('BTC/USDT', 200, 100, .04)
    assert journal()[0]['status'] == 'rejected' and journal()[0]['note'] == 'InsufficientFunds'
    assert not engine.live_halted and not engine.stop_event.is_set()


# ----------------------------------------------------- ambiguity and lookup

def test_ambiguous_live_submit_is_resolved_by_client_order_id_without_resending(engine):
    client = arm_fake_live(engine)
    engine.private = client = LookupExchange()
    assert engine._buy('BTC/USDT', 800, 100, .04)
    assert len(client.orders) == 1                      # exactly one request ever left
    assert client.lookups >= 1
    assert not engine.live_halted
    row = journal()[0]
    assert row['status'] == 'accounted' and row['order_id'] == 'srv-0'
    assert any('No order was resent' in r['message'] for r in storage.rows('SELECT message FROM audit'))
    assert engine.metrics.snapshot()['counters']['execution.located_after_ambiguity'] == 1


def test_ambiguous_live_submit_that_cannot_be_located_halts(engine):
    client = arm_fake_live(engine)
    client.order_error = ccxt.RequestTimeout('lost')
    assert not engine._buy('BTC/USDT', 800, 100, .04)
    assert len(client.orders) == 1
    assert engine.live_halted
    assert journal()[0]['status'] == 'uncertain' and journal()[0]['note'] == 'RequestTimeout'


def test_confirmed_zero_fill_is_a_rejection_not_a_halt(engine):
    client = arm_fake_live(engine)
    client.fill = {'id': 'empty', 'status': 'canceled', 'filled': 0.0, 'cost': 0.0}
    client.fetch_order = lambda order_id, symbol=None, params=None: dict(client.fill)
    assert not engine._buy('BTC/USDT', 800, 100, .04)
    assert not engine.live_halted
    assert journal()[0]['status'] == 'rejected' and 'without any fill' in journal()[0]['note']
    assert 'without a fill' in engine.risk_block


def test_missing_filled_field_on_a_final_order_stays_uncertain(engine):
    client = arm_fake_live(engine)
    client.fill = {'id': 'odd', 'status': 'closed'}
    client.fetch_order = lambda order_id, symbol=None, params=None: dict(client.fill)
    assert not engine._buy('BTC/USDT', 800, 100, .04)
    assert engine.live_halted
    assert journal()[0]['status'] == 'uncertain'


def test_classify_and_slippage_helpers():
    assert orders.classify({'status': 'closed', 'filled': 1}) == 'final_filled'
    assert orders.classify({'status': 'canceled', 'filled': 0}) == 'final_empty'
    assert orders.classify({'status': 'closed', 'filled': None}) == 'unknown'
    assert orders.classify({'status': 'closed'}) == 'unknown'
    assert orders.classify({'status': 'open', 'filled': .5}) == 'open'
    assert orders.classify('nonsense') == 'unknown'
    assert orders.slippage_bps(100, 101, 'BUY') == 100
    assert orders.slippage_bps(100, 99, 'SELL') == 100
    assert orders.slippage_bps(100, 101, 'SELL') == -100
    assert orders.slippage_bps(None, 1, 'BUY') is None
    assert orders.order_params('kraken', 'abc') == {'clientOrderId': 'abc', 'oflags': 'fciq'}
    assert orders.order_params('mexc', 'abc') == {'clientOrderId': 'abc'}


def test_locate_uses_mexc_client_id_lookup_and_falls_back_to_order_lists():
    class Mexc:
        id, has = 'mexc', {'fetchOrder': True, 'fetchOpenOrders': True, 'fetchClosedOrders': True}
        calls = []

        def fetch_order(self, order_id, symbol=None, params=None):
            self.calls.append(('fetch_order', order_id, params))
            if params.get('clientOrderId') == 'known':
                return {'id': 'x1', 'clientOrderId': 'known', 'status': 'closed', 'filled': 1}
            raise ccxt.OrderNotFound('nope')

        def fetch_open_orders(self, symbol=None, since=None, limit=None, params=None):
            return []

        def fetch_closed_orders(self, symbol=None, since=None, limit=None, params=None):
            return [{'id': 'x2', 'clientOrderId': 'listed', 'status': 'closed', 'filled': 2}]
    client = Mexc()
    assert orders.locate(client, 'BTC/USDT', 'known')['id'] == 'x1'
    assert client.calls[-1] == ('fetch_order', None, {'clientOrderId': 'known'})
    assert orders.locate(client, 'BTC/USDT', 'listed')['id'] == 'x2'
    assert orders.locate(client, 'BTC/USDT', 'absent') is None

    class Bare:
        has = {}
    with pytest.raises(ccxt.NotSupported):
        orders.locate(Bare(), 'BTC/USDT', 'anything')


def test_locate_errors_other_than_not_found_propagate_as_uncertain():
    class Flaky:
        id, has = 'mexc', {'fetchOrder': True}

        def fetch_order(self, *a, **k):
            raise ccxt.NetworkError('down')
    with pytest.raises(ccxt.NetworkError):
        orders.locate(Flaky(), 'BTC/USDT', 'cid')


# ---------------------------------------------------- restart reconciliation

def test_probe_reports_exchange_state_for_unresolved_intents_without_changing_them(engine):
    client = arm_fake_live(engine)
    engine.private = client = LookupExchange()
    client.create_market_buy_order_with_cost = FakeExchange.create_market_buy_order_with_cost.__get__(client)
    client.order_error = ccxt.RequestTimeout('lost')
    assert not engine._buy('BTC/USDT', 800, 100, .04)
    assert engine.live_halted
    intent = journal()[0]
    client.accepted.append({'id': 'srv-late', 'clientOrderId': intent['client_order_id'], 'status': 'closed',
                            'filled': 1.0, 'cost': 100.0, 'symbol': 'BTC/USDT', 'timestamp': int(time.time() * 1000)})
    findings = engine.probe_unresolved(client)
    assert findings == [{'intent_id': intent['intent_id'], 'symbol': 'BTC/USDT', 'side': 'BUY',
                         'journal_status': 'uncertain', 'exchange_status': 'closed', 'filled': 1.0, 'order_id': 'srv-late'}]
    assert journal()[0]['status'] == 'uncertain'       # still a manual reconciliation
    assert engine.live_halted
    assert engine.snapshot()['unresolved_probe'][0]['order_id'] == 'srv-late'
    assert any('Confirm against the exchange order history' in r['message'] for r in storage.rows('SELECT message FROM audit'))


def test_connect_runs_the_probe_but_a_probe_failure_never_blocks_connecting(engine, monkeypatch):
    import hashlib
    storage.create_order_intent('mexc', 'ETH/USDT', 'BUY', cost=50)
    engine.live_halted = True
    # The account guard only admits the identity that owns the unresolved intent.
    storage.set_meta(engine._key('live_account_fingerprint'), hashlib.sha256(('mexc\0' + 'k' * 10).encode('utf8')).hexdigest())
    monkeypatch.setattr(trader.TradingEngine, '_new_exchange', staticmethod(lambda *a, **k: FakeExchange()))
    engine.connect('k' * 10, 's' * 10)
    assert engine.private is not None
    assert engine.unresolved_probe and 'not found' in engine.unresolved_probe[0]['exchange_status'] \
        or 'lookup failed' in engine.unresolved_probe[0]['exchange_status']


def test_execution_latency_metrics_are_recorded_for_every_fill(engine):
    assert engine._buy('BTC/USDT', 200, 100, .04)
    latency = engine.metrics.snapshot()['latency']
    assert latency['execution.submit_to_ack_ms']['count'] >= 1
    assert latency['execution.submit_total_ms']['p50_ms'] is not None
    assert latency['risk.authorize_ms']['count'] >= 1


def test_client_order_id_fits_both_exchanges(engine):
    intent = storage.create_order_intent('mexc', 'BTC/USDT', 'BUY', cost=100, mode='paper')
    client_id = storage.client_order_id(intent)
    assert len(client_id) == 32 and client_id.isalnum()
