"""Durable, depth-aware micro paper venue (brief tests 15, 16 and 26).

Every test builds its own venue rules (a MEXC-shaped CCXT market through
``venue_rules.normalize``) and its own order book, and points the store at a
temporary database. Nothing here needs the network.
"""
import itertools
import socket
import sqlite3
import sys
import threading
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import ccxt
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import money
import orders
import storage
import venue_rules
import micro_venue

D = Decimal
SYMBOL = 'ETH/USDT'
STEP = D('0.0001')                 # quantity step of the fixture market
ASKS = ((D('2500.00'), D('0.0040')), (D('2501.00'), D('0.0040')), (D('2510.00'), D('0.0100')))
BIDS = ((D('2499.00'), D('0.0100')), (D('2498.00'), D('0.0200')), (D('2495.00'), D('0.0500')))
ASK_DEPTH_QUOTE = sum(price * qty for price, qty in ASKS)     # 45.104 USDT
BID_DEPTH_BASE = sum(qty for _, qty in BIDS)                   # 0.08 ETH
_ids = itertools.count(1)


@dataclass(frozen=True)
class Book:
    """Duck-typed stand-in for micro_book.BookSnapshot."""
    bids: tuple
    asks: tuple
    symbol: str | None = SYMBOL
    exchange: str | None = 'mexc'
    depth_known: bool = True
    generation: int = 7
    venue_sequence: int | None = 900001

    @property
    def best_bid(self):
        return self.bids[0][0] if self.bids else None

    @property
    def best_ask(self):
        return self.asks[0][0] if self.asks else None


def book(**overrides):
    return Book(**{'bids': BIDS, 'asks': ASKS, **overrides})


def market(symbol=SYMBOL, **info):
    base_asset, quote_asset = symbol.split('/')
    raw = {'status': '1', 'orderTypes': ['LIMIT', 'MARKET', 'LIMIT_MAKER'], 'isSpotTradingAllowed': True,
           'quoteAmountPrecision': '5', 'baseSizePrecision': '0.0001',
           'quoteAmountPrecisionMarket': '1', 'maxQuoteAmountMarket': '1000'}
    raw.update(info)
    return {'id': symbol.replace('/', ''), 'symbol': symbol, 'base': base_asset, 'quote': quote_asset,
            'type': 'spot', 'spot': True, 'active': True, 'contract': False, 'swap': False, 'future': False,
            'option': False, 'taker': 0.0005, 'maker': 0.0, 'precision': {'amount': 0.0001, 'price': 0.01},
            'limits': {'amount': {'min': 0.0001, 'max': None}, 'cost': {'min': 5.0, 'max': 2000000.0}}, 'info': raw}


def rules(raw=None, symbol=SYMBOL, **kwargs):
    return venue_rules.normalize(raw or market(symbol), exchange='mexc', symbol=symbol, loaded_at=100.0, **kwargs)


def venue(catalogue=None, exchange='mexc', **kwargs):
    catalogue = catalogue if catalogue is not None else {SYMBOL: rules()}
    return micro_venue.MicroPaperVenue(exchange, rules=lambda symbol: catalogue[symbol],
                                       balances=lambda: {'USDT': 100.0, 'ETH': 0.5}, **kwargs)


def params(**extra):
    out = {'clientOrderId': f'cid-{next(_ids)}', 'book': book()}
    out.update(extra)
    return out


def exact(order, key):
    return money.from_text(order['info']['exact'][key])


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, 'DATA_DIR', tmp_path)
    monkeypatch.setattr(storage, 'DB_PATH', tmp_path / 'portfolio.sqlite3')
    monkeypatch.setattr(storage, 'CONFIG_PATH', tmp_path / 'settings.json')
    return tmp_path


def raw_rows():
    """Rows read through an independent SQLite connection (what survives a crash)."""
    con = sqlite3.connect(str(storage.DB_PATH))
    try:
        return con.execute('SELECT client_order_id, status, filled FROM paper_venue_orders').fetchall()
    finally:
        con.close()


# --------------------------------------------------------------------- fills
def test_market_buy_walks_ask_levels_and_a_larger_cost_pays_a_worse_vwap():
    v = venue()
    small = v.create_market_buy_order_with_cost(SYMBOL, 5.00, params())
    large = v.create_market_buy_order_with_cost(SYMBOL, 25.00, params())
    # 5 USDT stays inside the best level; 25 USDT takes two whole levels and
    # whole quantity steps of the third: 0.004 + 0.004 + floor(4.996 / 2510).
    assert exact(small, 'filled') == D('0.002') and exact(small, 'cost') == D('5.00')
    assert exact(small, 'average') == ASKS[0][0]
    assert exact(large, 'filled') == D('0.0099')
    assert exact(large, 'cost') == D('10.00') + D('10.004') + D('0.0019') * D('2510.00')
    assert exact(large, 'average') > exact(small, 'average')
    assert ASKS[0][0] < exact(large, 'average') < ASKS[2][0]
    assert large['info']['levels_used'] == 3
    for order in (small, large):
        assert order['status'] == 'closed' and order['side'] == 'buy' and order['type'] == 'market'
        # The engine reads floats; they must be the exact figures, not a second computation.
        assert order['filled'] == float(exact(order, 'filled')) and order['cost'] == float(exact(order, 'cost'))
        assert order['average'] == float(exact(order, 'cost') / exact(order, 'filled'))
        assert orders.classify(order) == 'final_filled'
        assert order['info']['simulated'] is True and order['info']['book_generation'] == 7
        assert order['info']['book_sequence'] == 900001


def test_fill_invariants_hold_across_a_sweep_of_sizes():
    v = venue()
    previous = None
    for cents in range(100, 6001, 137):
        cost = D(cents) / 100
        order = v.create_market_buy_order_with_cost(SYMBOL, cost, params())
        spent, filled = exact(order, 'cost'), exact(order, 'filled')
        assert spent <= cost and exact(order, 'remaining_cost') == cost - spent
        assert filled == money.q_down(filled, STEP)
        if order['status'] == 'closed':
            # What is left unspent is less than one quantity step at the worst price.
            assert cost - spent < STEP * ASKS[-1][0]
        else:
            assert order['status'] == 'canceled' and spent == ASK_DEPTH_QUOTE and cost > ASK_DEPTH_QUOTE
        average = exact(order, 'average')
        assert ASKS[0][0] <= average <= ASKS[-1][0]
        if previous is not None:
            assert average >= previous
        previous = average
    previous = None
    for units in range(5, 1001, 37):          # 5 steps: the first size above the 1 USDT minimum
        amount = STEP * units
        order = v.create_order(SYMBOL, 'market', 'sell', amount, None, params())
        sold = exact(order, 'filled')
        assert sold + exact(order, 'remaining') == amount
        assert (order['status'] == 'closed') == (sold == amount)
        average = exact(order, 'average')
        assert BIDS[-1][0] <= average <= BIDS[0][0]
        if previous is not None:
            assert average <= previous
        previous = average


def test_adverse_bps_moves_every_level_against_the_order():
    v = venue()
    buy = v.create_market_buy_order_with_cost(SYMBOL, D('5.00'), params(adverse_bps=D('10')))
    assert exact(buy, 'average') == ASKS[0][0] * D('1.001')
    assert exact(buy, 'filled') == D('0.0019')              # floor(5 / 2502.5) in whole steps
    assert exact(buy, 'cost') == D('0.0019') * D('2502.5')
    plain = v.create_order(SYMBOL, 'market', 'sell', D('0.005'), None, params())
    adverse = v.create_order(SYMBOL, 'market', 'sell', D('0.005'), None, params(adverse_bps=D('10')))
    assert exact(plain, 'average') == BIDS[0][0]
    assert exact(adverse, 'average') == BIDS[0][0] * D('0.999')
    assert exact(adverse, 'cost') < exact(plain, 'cost')
    assert adverse['info']['adverse_bps'] == '10'


def test_adverse_bps_also_moves_the_deeper_levels_an_order_walks_into():
    v = venue()
    up, down = D('1.001'), D('0.999')
    buy = v.create_market_buy_order_with_cost(SYMBOL, D('25.00'), params(adverse_bps=D('10')))
    # Two whole levels at their adverse prices, then whole steps of the third:
    # floor((25 - 10.01 - 10.014004) / 2512.51) = 0.0019.
    assert buy['info']['levels_used'] == 3
    assert exact(buy, 'filled') == D('0.004') + D('0.004') + D('0.0019')
    assert exact(buy, 'cost') == (D('0.004') * ASKS[0][0] * up + D('0.004') * ASKS[1][0] * up
                                  + D('0.0019') * ASKS[2][0] * up)
    sell = v.create_order(SYMBOL, 'market', 'sell', D('0.03'), None, params(adverse_bps=D('10')))
    assert sell['info']['levels_used'] == 2
    assert exact(sell, 'cost') == D('0.01') * BIDS[0][0] * down + D('0.02') * BIDS[1][0] * down


def test_base_fee_buy_delivers_fewer_coins_and_quote_fee_bills_usdt():
    base_billed = venue({SYMBOL: rules(buy_fee_currency='base')})
    order = base_billed.create_market_buy_order_with_cost(SYMBOL, D('5.00'), params())
    fee = exact(order, 'fee_cost')
    assert order['fee']['currency'] == 'ETH' and order['info']['exact']['fee_currency'] == 'ETH'
    assert fee == exact(order, 'filled') * D('0.0005')
    assert exact(order, 'filled') - fee == D('0.001999')        # coins actually delivered
    assert exact(order, 'cost') == D('5.00')                     # no USDT on top

    quote_billed = venue()                                       # fee currency unknown: billed in quote
    order = quote_billed.create_market_buy_order_with_cost(SYMBOL, D('5.00'), params())
    assert order['fee']['currency'] == 'USDT' and exact(order, 'fee_cost') == D('0.0025')
    assert exact(order, 'filled') == D('0.002')                  # every coin delivered
    assert order['fees'] == [order['fee']]

    sale = quote_billed.create_order(SYMBOL, 'market', 'sell', D('0.03'), None, params())
    assert sale['fee']['currency'] == 'USDT'
    assert exact(sale, 'fee_cost') == (D('24.99') + D('49.96')) * D('0.0005')
    override = quote_billed.create_market_buy_order_with_cost(SYMBOL, D('5.00'), params(fee_currency='base'))
    assert override['fee']['currency'] == 'ETH'


def test_sells_walk_the_bids_and_a_larger_sale_receives_a_worse_vwap():
    v = venue()
    small = v.create_order(SYMBOL, 'market', 'sell', D('0.005'), None, params())
    large = v.create_order(SYMBOL, 'market', 'sell', D('0.03'), None, params())
    assert exact(small, 'cost') == D('0.005') * D('2499.00')
    assert exact(large, 'filled') == D('0.03') and exact(large, 'cost') == D('24.99') + D('49.96')
    assert exact(large, 'average') < exact(small, 'average')
    assert large['status'] == 'closed' and large['amount'] == 0.03 and large['remaining'] == 0.0
    assert large['info']['levels_used'] == 2


def test_a_partial_level_never_spends_more_than_the_requested_cost():
    # A 34-digit price makes money.div round the step count up onto a boundary.
    price = D('1245.454545454545454545454545454546')
    asks = ((price, D('1')),)
    overspent = []
    for cents in range(100, 3000):
        spend = D(cents) / 100
        fill = micro_venue._walk_asks(asks, spend=spend, adverse=money.ZERO, amount_step=STEP)
        assert fill.base == money.q_down(fill.base, STEP)
        if fill.quote > spend:
            overspent.append(spend)
        # Never more than the cost, and never a whole step less than it could buy
        # (in the 34-digit money context: the default 28 digits would round these).
        assert money.sub(spend, fill.quote) < money.mul(STEP, price)
    assert overspent == []
    v = venue()
    order = v.create_market_buy_order_with_cost(
        SYMBOL, D('1.37'), params(book=book(asks=asks, bids=((D('1245'), D('1')),))))
    assert exact(order, 'cost') <= D('1.37') and exact(order, 'remaining_cost') >= 0
    assert exact(order, 'filled') == D('0.001') and exact(order, 'cost') == money.mul(D('0.001'), price)
    assert v.records()[0]['cost'] == exact(order, 'cost')


def test_exhausted_depth_cancels_the_remainder_with_an_exact_partial_fill():
    v = venue()
    buy = v.create_market_buy_order_with_cost(SYMBOL, D('60.00'), params())
    assert buy['status'] == 'canceled' and buy['info']['depth_exhausted'] is True
    assert exact(buy, 'filled') == sum(qty for _, qty in ASKS)
    assert exact(buy, 'cost') == ASK_DEPTH_QUOTE
    assert exact(buy, 'remaining_cost') == D('60.00') - ASK_DEPTH_QUOTE == D('14.896')
    assert buy['remaining'] is None and buy['amount'] is None    # unbought base is unknown, not zero
    assert orders.classify(buy) == 'final_filled'

    sell = v.create_order(SYMBOL, 'market', 'sell', D('0.1'), None, params())
    assert sell['status'] == 'canceled'
    assert exact(sell, 'filled') == BID_DEPTH_BASE and exact(sell, 'remaining') == D('0.02')
    assert exact(sell, 'cost') == D('24.99') + D('49.96') + D('124.75')
    assert sell['amount'] == 0.1 and sell['remaining'] == float(D('0.02'))


def test_partial_injection_fills_a_fraction_of_what_the_book_allows():
    v = venue()
    v.partial_next = D('0.5')
    order = v.create_order(SYMBOL, 'market', 'sell', D('0.03'), None, params())
    assert order['status'] == 'canceled'
    assert exact(order, 'filled') == D('0.015') and exact(order, 'remaining') == D('0.015')
    assert v.partial_next is None
    assert v.create_order(SYMBOL, 'market', 'sell', D('0.03'), None, params())['status'] == 'closed'


# ----------------------------------------------------------------- fees (26)
def test_liquidity_is_always_taker_and_a_lower_maker_rate_is_never_charged():
    r = rules()
    assert r.maker_fee == 0 < r.taker_fee == D('0.0005')
    v = venue({SYMBOL: r})
    buy = v.create_market_buy_order_with_cost(SYMBOL, D('20.00'), params())
    sell = v.create_order(SYMBOL, 'market', 'sell', D('0.005'), None, params())
    for order in (buy, sell):
        assert order['info']['liquidity'] == 'taker'
        assert exact(order, 'fee_cost') == exact(order, 'cost') * r.taker_fee > 0
    assert all(record['liquidity'] == 'taker' for record in v.records())
    # A configured rate above the public taker schedule is charged; one below never lowers it.
    higher = v.create_market_buy_order_with_cost(SYMBOL, D('20.00'), params(taker_fee=D('0.001')))
    lower = v.create_market_buy_order_with_cost(SYMBOL, D('20.00'), params(taker_fee=D('0.0001')))
    assert exact(higher, 'fee_cost') == exact(higher, 'cost') * D('0.001')
    assert exact(lower, 'fee_cost') == exact(lower, 'cost') * D('0.0005')
    stored = len(v.records())
    # There is no maker simulation: any resting order type is refused, and nothing is stored.
    for type_, side in (('limit', 'buy'), ('limit', 'sell'), ('market', 'buy')):
        with pytest.raises(ccxt.NotSupported):
            v.create_order(SYMBOL, type_, side, D('0.01'), D('2400'), params())
    assert len(v.records()) == stored


# --------------------------------------------------------------- rejections
def _set(path, value):
    def apply(raw):
        target = raw
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value
        return raw
    return apply


def _buy(cost, **extra):
    return lambda v: v.create_market_buy_order_with_cost(SYMBOL, cost, params(**extra))


def _sell(amount, **extra):
    return lambda v: v.create_order(SYMBOL, 'market', 'sell', amount, None, params(**extra))


REJECTIONS = [
    ('cost-off-step', None, _buy(D('5.005')), 'multiple of the quote-amount step'),
    ('float-noise-off-step', None, _buy(0.1 + 0.2), 'multiple of the quote-amount step'),
    ('cost-below-market-minimum', None, _buy(D('0.99')), 'below the venue minimum'),
    ('cost-above-market-maximum', None, _buy(D('1000.01')), 'above the venue maximum'),
    ('sell-off-step', None, _sell(D('0.00015')), 'multiple of the quantity step'),
    ('sell-below-minimum-quantity', _set(('limits', 'amount', 'min'), 0.001), _sell(D('0.0009')),
     'quantity 0.0009 is below the venue minimum'),
    ('sell-notional-below-minimum', None, _sell(D('0.0003')), 'at the best bid'),
    ('buy-only-pair', _set(('info', 'tradeSideType'), '2'), _sell(D('0.001')), venue_rules.SIDE_NOT_ALLOWED),
    ('sell-only-pair', _set(('info', 'tradeSideType'), '3'), _buy(D('5')), venue_rules.SIDE_NOT_ALLOWED),
    ('paused-pair', _set(('info', 'status'), '2'), _buy(D('5')), venue_rules.MARKET_INACTIVE),
    ('no-cost-buys', _set(('info', 'quoteOrderQtyMarketAllowed'), False), _buy(D('5')),
     venue_rules.COST_BUY_UNSUPPORTED),
    ('unknown-taker-fee', _set(('taker',), None), _buy(D('5')), 'taker fee is unknown'),
    ('off-the-fallback-quote-step', _set(('precision', 'price'), None), _buy(D('5.000000005')),
     'multiple of the quote-amount step 0.00000001'),
    ('unknown-quantity-step-buy', _set(('precision', 'amount'), None), _buy(D('5')), 'quantity step is unknown'),
    ('missing-book', None, _buy(D('5'), book=None), 'needs a depth book'),
    ('one-sided-book', None, _buy(D('5'), book=book(bids=())), 'needs a depth book'),
    ('crossed-book', None, _buy(D('5'), book=book(bids=((D('2501'), D('1')),))), 'crossed'),
    ('ticker-top-of-book', None,
     _buy(D('5'), book=book(bids=((D('2499'), None),), asks=((D('2500'), None),), depth_known=False)),
     'needs a depth book'),
    ('unknown-level-quantity', None, _buy(D('5'), book=book(asks=((D('2500'), D('0.004')), (D('2501'), None)))),
     'needs a depth book'),
    ('unsorted-book', None, _sell(D('0.001'), book=book(bids=tuple(reversed(BIDS)))), 'not sorted'),
    ('other-symbol-book', None,
     _buy(D('25.00'), book=book(symbol='BTC/USDT', asks=((D('60000'), D('1')),), bids=((D('59999'), D('1')),))),
     'book is for BTC/USDT on mexc'),
    ('other-exchange-book', None, _sell(D('0.001'), book=book(exchange='kraken')), 'book is for ETH/USDT on kraken'),
    ('book-without-symbol', None, _buy(D('5'), book=book(symbol=None)), 'book is for None'),
    ('book-without-exchange', None, _buy(D('5'), book=book(exchange=None)), 'on None'),
    ('no-client-order-id', None, lambda v: v.create_market_buy_order_with_cost(SYMBOL, D('5'), {'book': book()}),
     'clientOrderId'),
    ('negative-cost', None, _buy(D('-5')), 'cost'),
]


@pytest.mark.parametrize('mutate,call,message', [case[1:] for case in REJECTIONS], ids=[c[0] for c in REJECTIONS])
def test_every_venue_rule_is_a_definite_rejection_that_stores_nothing(mutate, call, message):
    raw = market()
    v = venue({SYMBOL: rules(mutate(raw) if mutate else raw)})
    with pytest.raises(ccxt.InvalidOrder, match=message) as raised:
        call(v)
    # The engine records a definite rejection only for these classes.
    assert isinstance(raised.value, orders.definite_rejections())
    assert v.records() == [] and v.fetch_closed_orders() == []


def test_an_unknown_pair_is_bad_symbol_and_stores_nothing():
    v = venue()
    for call in (lambda: v.create_market_buy_order_with_cost('NOPE/USDT', D('5'), params()),
                 lambda: v.market('NOPE/USDT'), lambda: v.amount_to_precision('NOPE/USDT', 1.0)):
        with pytest.raises(ccxt.BadSymbol):
            call()
    listed_missing = venue({'NEW/USDT': venue_rules.normalize(None, exchange='mexc', symbol='NEW/USDT',
                                                               loaded_at=1.0)})
    with pytest.raises(ccxt.BadSymbol) as raised:
        listed_missing.create_market_buy_order_with_cost('NEW/USDT', D('5'), params())
    assert isinstance(raised.value, orders.definite_rejections())
    assert v.records() == [] and listed_missing.records() == []


def test_unpublished_limits_are_listed_as_unchecked_not_treated_as_zero():
    v = venue()
    order = v.create_order(SYMBOL, 'market', 'sell', D('0.001'), None, params())
    assert 'maximum quantity not published' in order['info']['unchecked']
    assert not any('minimum' in item for item in order['info']['unchecked'])


def test_unpublished_steps_follow_the_sizing_fallback_and_never_block_an_exit():
    raw = market()
    raw['precision']['price'] = None
    v = venue({SYMBOL: rules(raw)})
    # Sizing (contract step 3) proceeds on a 0.00000001 quote step when none is published.
    assert v.cost_to_precision(SYMBOL, '24.975024975') == '24.97502497'
    buy = v.create_market_buy_order_with_cost(SYMBOL, D('24.97502497'), params())
    assert buy['status'] == 'closed' and D(0) < exact(buy, 'cost') <= D('24.97502497')
    assert any(item.startswith('quote-amount step not published') for item in buy['info']['unchecked'])
    raw = market()
    raw['precision']['amount'] = None
    v = venue({SYMBOL: rules(raw)})
    # A held position stays exitable: the sell fills as asked and says the step was not checked.
    assert v.amount_to_precision(SYMBOL, D('0.0099')) == '0.0099'
    with pytest.raises(ccxt.InvalidOrder):
        v.amount_to_precision(SYMBOL, 0)
    sell = v.create_order(SYMBOL, 'market', 'sell', D('0.0099'), None, params())
    assert sell['status'] == 'closed' and exact(sell, 'filled') == D('0.0099')
    assert any(item.startswith('quantity step not published') for item in sell['info']['unchecked'])
    v.partial_next = D('0.5')
    half = v.create_order(SYMBOL, 'market', 'sell', D('0.0099'), None, params())
    assert half['status'] == 'canceled' and exact(half, 'filled') == D('0.00495')
    # A buy still needs the quantity step to fill in whole steps (see REJECTIONS).
    with pytest.raises(ccxt.InvalidOrder, match='quantity step is unknown'):
        v.create_market_buy_order_with_cost(SYMBOL, D('5.00'), params())


def test_orders_beyond_the_paper_balance_are_insufficient_funds_and_store_nothing():
    wallet = {'USDT': 25.0, 'ETH': 0.01}
    v = micro_venue.MicroPaperVenue('mexc', rules=lambda symbol: rules(), balances=lambda: dict(wallet))
    refused = (
        lambda: v.create_market_buy_order_with_cost(SYMBOL, D('25.00'), params()),       # + 0.0125 USDT fee
        lambda: v.create_market_buy_order_with_cost(SYMBOL, D('900.00'), params()),
        lambda: v.create_order(SYMBOL, 'market', 'sell', D('0.0101'), None, params()),
        lambda: v.create_order(SYMBOL, 'market', 'sell', D('0.01'), None, params(fee_currency='base')),  # + base fee
    )
    for call in refused:
        with pytest.raises(ccxt.InsufficientFunds) as raised:
            call()
        assert isinstance(raised.value, orders.definite_rejections())
    assert v.records() == []
    # Within the balance, the maximum fee included, the same orders fill.
    bought = v.create_market_buy_order_with_cost(SYMBOL, D('24.98'), params(clientOrderId='funded-1'))
    assert bought['status'] == 'closed'
    assert v.create_market_buy_order_with_cost(SYMBOL, D('25.00'), params(fee_currency='base'))['status'] == 'closed'
    assert v.create_order(SYMBOL, 'market', 'sell', D('0.01'), None, params())['status'] == 'closed'
    # A balance that changed after acceptance never turns the accepted order into a rejection.
    wallet['USDT'] = 0.0
    repeat = v.create_market_buy_order_with_cost(SYMBOL, D('24.98'), {'clientOrderId': 'funded-1', 'book': book()})
    assert repeat['id'] == bought['id']
    # A balance the ledger does not report is unknown, not zero: the order fills and says so.
    wallet.clear()
    order = v.create_market_buy_order_with_cost(SYMBOL, D('5.00'), params())
    assert order['status'] == 'closed' and 'free USDT balance not known' in order['info']['unchecked']
    assert len(v.records()) == 4


def test_a_value_the_store_could_not_read_back_is_refused_before_it_is_stored():
    raw = market()
    raw['taker'] = None
    v = venue({SYMBOL: rules(raw)})
    # A fee of 0.0019 ETH * 1e-30 has an exponent the read path refuses.
    with pytest.raises(ccxt.InvalidOrder, match='fee_cost') as raised:
        v.create_market_buy_order_with_cost(SYMBOL, D('5.00'), params(clientOrderId='tiny-fee', taker_fee=D('1e-30'),
                                                                      fee_currency='base'))
    assert isinstance(raised.value, orders.definite_rejections())
    assert v.records() == [] and v.fetch_closed_orders() == []
    assert orders.locate(v, SYMBOL, 'tiny-fee') is None
    accepted = v.create_market_buy_order_with_cost(SYMBOL, D('5.00'), params(taker_fee=D('0.001')))
    assert [record['client_order_id'] for record in v.records()] == [accepted['clientOrderId']]


# ------------------------------------------------------------- idempotency
def test_duplicate_client_order_id_same_terms_returns_the_stored_order_without_a_second_fill():
    catalogue = {SYMBOL: rules()}
    v = venue(catalogue)
    first = v.create_market_buy_order_with_cost(SYMBOL, D('25.00'), {'clientOrderId': 'dup-1', 'book': book()})
    # Same terms, a float amount and a different (better) book: still the same order.
    cheaper = book(asks=((D('2000.00'), D('1')),), bids=((D('1999.00'), D('1')),))
    again = v.create_market_buy_order_with_cost(SYMBOL, 25.0, {'clientOrderId': 'dup-1', 'book': cheaper})
    assert again['id'] == first['id'] and again['info']['exact'] == first['info']['exact']
    # Even a pair that has since been paused never turns an accepted order into a rejection.
    catalogue[SYMBOL] = rules(market(status='2'))
    assert v.create_market_buy_order_with_cost(SYMBOL, D('25'), {'clientOrderId': 'dup-1'})['id'] == first['id']
    catalogue[SYMBOL] = rules()
    for different in (lambda: v.create_market_buy_order_with_cost(SYMBOL, D('24.99'),
                                                                  {'clientOrderId': 'dup-1', 'book': book()}),
                      lambda: v.create_order(SYMBOL, 'market', 'sell', D('0.0099'), None,
                                             {'clientOrderId': 'dup-1', 'book': book()})):
        with pytest.raises(ccxt.InvalidOrder, match='already used'):
            different()
    records = v.records()
    assert len(records) == 1 and records[0]['filled'] == exact(first, 'filled')


def test_concurrent_creates_with_one_client_order_id_fill_once(monkeypatch):
    v = venue()
    barrier = threading.Barrier(2)
    real_accept = micro_venue.MicroPaperVenue._accept

    def accept_then_wait(self, *args, **kwargs):
        result = real_accept(self, *args, **kwargs)
        barrier.wait(timeout=5)          # both creates have validated and filled before either writes
        return result

    monkeypatch.setattr(micro_venue.MicroPaperVenue, '_accept', accept_then_wait)
    answers, errors = [], []

    def create():
        try:
            answers.append(v.create_market_buy_order_with_cost(SYMBOL, D('5.00'),
                                                               {'clientOrderId': 'race-cid', 'book': book()}))
        except Exception as exc:          # pragma: no cover - reported below
            errors.append(exc)

    threads = [threading.Thread(target=create) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert errors == [] and len(answers) == 2
    assert answers[0]['id'] == answers[1]['id'] and len(v.records()) == 1


def test_the_rules_callable_never_runs_inside_the_venue_write_lock():
    catalogue = {SYMBOL: rules()}

    def rules_that_write(symbol):
        # A catalogue refresh that records something must not wait on the venue's own lock.
        with _short_timeout_connection() as db:
            db.execute('CREATE TABLE IF NOT EXISTS catalogue_reads (symbol TEXT)')
            db.execute('INSERT INTO catalogue_reads VALUES (?)', (symbol,))
        return catalogue[symbol]

    v = micro_venue.MicroPaperVenue('mexc', rules=rules_that_write, balances=dict)
    assert v.create_market_buy_order_with_cost(SYMBOL, D('5.00'), params())['status'] == 'closed'
    assert len(v.records()) == 1


def _short_timeout_connection():
    """storage.connection with a 0.2 s busy timeout, so a self-deadlock fails fast instead of in 15 s."""
    import contextlib

    @contextlib.contextmanager
    def opened():
        with storage.LOCK:
            con = sqlite3.connect(str(storage.DB_PATH), timeout=0.2)
            con.row_factory = sqlite3.Row
            try:
                yield con
                con.commit()
            except BaseException:
                con.rollback()
                raise
            finally:
                con.close()
    return opened()


# ------------------------------------------------- durability and recovery
def test_the_record_is_committed_before_the_response_leaves(monkeypatch):
    seen = []
    monkeypatch.setattr(micro_venue.time, 'sleep', lambda _seconds: seen.append(raw_rows()))
    v = venue()
    v.latency_s = 0.5
    v.create_market_buy_order_with_cost(SYMBOL, D('5.00'), params(clientOrderId='durable-1'))
    assert len(seen) == 1 and [row[:2] for row in seen[0]] == [('durable-1', 'closed')]


def test_dropped_response_is_resolved_by_a_read_only_lookup_without_a_resend():
    v = venue()
    v.drop_response_next = True
    with pytest.raises(ccxt.RequestTimeout) as raised:
        v.create_market_buy_order_with_cost(SYMBOL, D('25.00'), params(clientOrderId='lost-1'))
    assert not isinstance(raised.value, orders.definite_rejections())   # the engine must treat it as uncertain
    assert [row[:2] for row in raw_rows()] == [('lost-1', 'closed')]    # committed before the error
    found = v.fetch_order(None, SYMBOL, {'clientOrderId': 'lost-1'})
    assert found['clientOrderId'] == 'lost-1' and found['status'] == 'closed'
    assert exact(found, 'filled') == D('0.0099')
    located = orders.locate(v, SYMBOL, 'lost-1')                         # the engine's own lookup
    assert located is not None and located['id'] == found['id']
    assert v.fetch_order(found['id'], SYMBOL)['id'] == found['id']
    assert len(v.records()) == 1                                         # reads never create an order
    assert v.drop_response_next is False
    assert v.create_market_buy_order_with_cost(SYMBOL, D('5.00'), params())['status'] == 'closed'
    with pytest.raises(ccxt.OrderNotFound):
        v.fetch_order(None, SYMBOL, {'clientOrderId': 'never-sent'})
    with pytest.raises(ccxt.OrderNotFound):
        v.fetch_order(found['id'], 'BTC/USDT')


@pytest.mark.parametrize('side,size,fraction,first_levels', [('buy', D('25.00'), D('0.8'), 1),
                                                             ('buy', D('25.00'), D('1'), 2),
                                                             ('sell', D('0.03'), D('1'), 2)])
def test_late_fill_reports_a_cancel_first_and_a_larger_cumulative_fill_later(side, size, fraction, first_levels):
    v = venue()
    v.late_fill_next = fraction
    if side == 'buy':
        first = v.create_market_buy_order_with_cost(SYMBOL, size, params(clientOrderId='race-1'))
    else:
        first = v.create_order(SYMBOL, 'market', 'sell', size, None, params(clientOrderId='race-1'))
    assert first['status'] == 'canceled' and orders.classify(first) == 'final_filled'
    assert 'late_fill' not in first['info']                 # the first answer does not know the future
    # Nor does any other field of it: it describes its own fill and names no late-fill injection.
    assert 'late_fill_next' not in first['info']['injected']
    assert first['info']['levels_used'] == first_levels and first['info']['depth_exhausted'] is False
    later = [v.fetch_order(first['id'], SYMBOL), v.fetch_order(None, SYMBOL, {'clientOrderId': 'race-1'}),
             v.fetch_closed_orders(SYMBOL)[0]]
    for order in later:
        assert order['id'] == first['id']
        assert exact(order, 'filled') > exact(first, 'filled') > 0
        assert exact(order, 'cost') > exact(first, 'cost')
        assert order['info']['exact'] == later[0]['info']['exact']
    (record,) = v.records()
    assert record['filled'] == exact(later[0], 'filled')    # the venue's record is the cumulative fill
    assert record['info']['late_fill']['first_report']['filled'] == first['info']['exact']['filled']
    assert record['info']['late_fill']['first_report']['levels_used'] == first_levels
    assert later[0]['info']['levels_used'] == record['info']['levels_used']   # later reads: the stored fill
    assert record['info']['injected'] == ['late_fill_next']   # the store keeps what was simulated
    if fraction == 1:
        assert later[0]['status'] == 'closed'
        assert (exact(later[0], 'filled') == size) if side == 'sell' else (exact(later[0], 'cost') <= size)
    else:
        assert later[0]['status'] == 'canceled' and exact(later[0], 'cost') <= size * fraction
    assert v.late_fill_next is None


def test_records_survive_a_restart_on_the_same_database():
    before = venue()
    buy = before.create_market_buy_order_with_cost(SYMBOL, D('25.00'), params(clientOrderId='keep-1'))
    sell = before.create_order(SYMBOL, 'market', 'sell', D('0.03'), None, params(clientOrderId='keep-2'))
    expected = before.records()
    after = venue()                                          # a new process: new venue, same file
    assert after.records() == expected
    assert [r['client_order_id'] for r in expected] == ['keep-1', 'keep-2']
    assert expected[0]['filled'] == D('0.0099') and expected[1]['cost'] == D('74.95')
    assert after.fetch_order(buy['id'], SYMBOL)['info']['exact'] == buy['info']['exact']
    repeat = after.create_order(SYMBOL, 'market', 'sell', D('0.03'), None, params(clientOrderId='keep-2'))
    assert repeat['id'] == sell['id'] and len(after.records()) == 2
    assert venue(exchange='kraken').records() == []          # records are scoped by exchange


def test_fetch_closed_orders_filters_symbol_and_since():
    btc = 'BTC/USDT'
    times = iter([1000.0, 2000.0, 3000.0])
    v = venue({SYMBOL: rules(), btc: rules(market(btc), symbol=btc)}, clock=lambda: next(times))
    first = v.create_market_buy_order_with_cost(SYMBOL, D('5.00'), params())
    second = v.create_order(SYMBOL, 'market', 'sell', D('0.001'), None, params())
    third = v.create_market_buy_order_with_cost(btc, D('5.00'), params(book=book(symbol=btc)))
    assert [o['timestamp'] for o in (first, second, third)] == [1_000_000, 2_000_000, 3_000_000]
    ids = lambda found: [o['id'] for o in found]
    assert ids(v.fetch_closed_orders(SYMBOL)) == [first['id'], second['id']]
    assert ids(v.fetch_closed_orders(SYMBOL, 2_000_000)) == [second['id']]
    assert ids(v.fetch_closed_orders(None, 2_000_000)) == [second['id'], third['id']]
    assert ids(v.fetch_closed_orders(btc, 3_000_001)) == []
    assert ids(v.fetch_closed_orders(None, None, 1)) == [third['id']]
    assert [r['symbol'] for r in v.records(btc)] == [btc]
    assert v.fetch_open_orders(SYMBOL) == []


# --------------------------------------------------- engine-facing helpers
def test_precision_helpers_truncate_and_their_output_is_accepted():
    v = venue()
    assert v.amount_to_precision(SYMBOL, 0.123456) == '0.1234'
    assert v.cost_to_precision(SYMBOL, 25 / 1.001) == '24.97'
    with pytest.raises(ccxt.InvalidOrder):
        v.amount_to_precision(SYMBOL, 0.00009)
    # More than 34 significant digits still truncate; they never round up past the input.
    assert v.amount_to_precision(SYMBOL, '0.00019999999999999999999999999999999999') == '0.0001'
    assert v.cost_to_precision(SYMBOL, '4.999999999999999999999999999999999999') == '4.99'
    assert v.cost_to_precision(SYMBOL, D('4.999999999999999999999999999999999999')) == '4.99'
    spend = float(v.cost_to_precision(SYMBOL, 25 / 1.001))   # the engine's own pattern
    assert v.create_market_buy_order_with_cost(SYMBOL, spend, params())['status'] == 'closed'
    quantity = float(v.amount_to_precision(SYMBOL, 0.0123456))
    assert v.create_order(SYMBOL, 'market', 'sell', quantity, None, params())['filled'] == quantity
    described = v.market(SYMBOL)
    assert described['spot'] is True and described['active'] is True
    assert described['limits']['cost']['min'] == 1.0 and described['limits']['cost']['max'] == 1000.0
    assert described['precision']['amount'] == 0.0001 and described['info']['orderTypes'] == ['MARKET']
    refused = venue({SYMBOL: rules(market(quoteOrderQtyMarketAllowed=False))}).market(SYMBOL)
    assert refused['info']['quoteOrderQtyMarketAllowed'] is False
    assert v.fetch_balance()['free'] == {'USDT': 100.0, 'ETH': 0.5}


def test_no_network_and_no_exchange_client_are_used(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError('the paper venue opened a connection')
    monkeypatch.setattr(socket.socket, 'connect', refuse)
    monkeypatch.setattr(socket.socket, 'connect_ex', refuse)
    monkeypatch.setattr(socket, 'getaddrinfo', refuse)
    monkeypatch.setattr(socket, 'create_connection', refuse)
    monkeypatch.setattr(ccxt.Exchange, '__init__', refuse)
    v = venue()
    v.drop_response_next = True
    with pytest.raises(ccxt.RequestTimeout):
        v.create_market_buy_order_with_cost(SYMBOL, D('25.00'), params(clientOrderId='offline-1'))
    assert orders.locate(v, SYMBOL, 'offline-1') is not None
    v.create_order(SYMBOL, 'market', 'sell', D('0.01'), None, params())
    v.market(SYMBOL), v.fetch_balance(), v.fetch_closed_orders(), v.records()
    assert not any(hasattr(v, name) for name in ('apiKey', 'secret', 'password', 'private'))
    assert 'credentials' not in vars(micro_venue)


def test_ensure_schema_is_idempotent_and_creates_the_lookup_index():
    with storage.connection() as db:
        micro_venue.ensure_schema(db)
        micro_venue.ensure_schema(db)
        indexes = {row['name'] for row in db.execute("PRAGMA index_list('paper_venue_orders')")}
    assert 'paper_venue_orders_recent' in indexes
