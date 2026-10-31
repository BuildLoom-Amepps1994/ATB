"""Classic paper fills walk the public order book when the engine supplies one."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import paper_exchange  # noqa: E402

MARKETS = {'BTC/USDT': {'symbol': 'BTC/USDT', 'spot': True, 'active': True}}
SETTINGS = {'fee_pct': 0.1, 'slippage_pct': 0.2}
BOOK = {'bids': [[99.99, 2.0], [99.90, 5.0]], 'asks': [[100.01, 2.0], [100.10, 5.0]]}


def exchange(book=None):
    return paper_exchange.PaperExchange('mexc', catalogue=lambda: MARKETS, settings=lambda: SETTINGS,
                                        balances=lambda: {'USDT': 10_000.0, 'BTC': 10.0}, book=book)


def test_walk_levels_spends_quote_or_sells_base_across_levels_and_refuses_a_thin_book():
    assert paper_exchange.walk_levels([(100., 1.), (101., 1.)], quote=150.) == pytest.approx((1 + 50 / 101, 150., 2))
    assert paper_exchange.walk_levels([(100., 1.), (99., 1.)], base=1.5) == pytest.approx((1.5, 100 + 49.5, 2))
    assert paper_exchange.walk_levels([(100., 1.)], quote=150.) is None
    assert paper_exchange.walk_levels([(100., float('nan'))], base=.5) is None


def test_a_buy_walks_the_asks_with_the_latency_margin_and_says_so():
    order = exchange(lambda symbol: BOOK).create_market_buy_order_with_cost(
        'BTC/USDT', 150.0, {'reference_price': 100.01})
    first = 100.01 * (1 + paper_exchange.BOOK_LATENCY_BPS / 10_000)
    assert order['average'] == pytest.approx(first)                 # 150 USDT fits inside the first level
    assert order['cost'] == pytest.approx(150.0) and order['fee']['cost'] == pytest.approx(.15)
    assert order['info']['fill_model'] == 'order book, 1 levels'
    flat = exchange().create_market_buy_order_with_cost('BTC/USDT', 150.0, {'reference_price': 100.01})
    assert flat['average'] == pytest.approx(100.01 * 1.002) and flat['info']['fill_model'] == 'flat slippage'


def test_a_sell_walks_the_bids_into_the_second_level():
    order = exchange(lambda symbol: BOOK).create_order('BTC/USDT', 'market', 'sell', 3.0,
                                                       params={'reference_price': 99.99})
    shift = 1 - paper_exchange.BOOK_LATENCY_BPS / 10_000
    assert order['cost'] == pytest.approx((2 * 99.99 + 1 * 99.90) * shift)
    assert order['info']['fill_model'] == 'order book, 2 levels'


@pytest.mark.parametrize('book', [
    lambda symbol: {'bids': [[99.99, .01]], 'asks': [[100.01, .01]]},            # too thin for the order
    lambda symbol: {'bids': [[90.0, 50.0]], 'asks': [[110.0, 50.0]]},            # 10 % from the reference
    lambda symbol: None,                                                          # no fresh book
    lambda symbol: {'bids': 'garbage', 'asks': [['x', 1]]},                       # malformed
    lambda symbol: (_ for _ in ()).throw(RuntimeError('network down')),           # the read failed
])
def test_an_unusable_book_falls_back_to_the_configured_slippage(book):
    order = exchange(book).create_market_buy_order_with_cost('BTC/USDT', 150.0, {'reference_price': 100.01})
    assert order['average'] == pytest.approx(100.01 * 1.002) and order['info']['fill_model'] == 'flat slippage'


@pytest.mark.parametrize('taker, expected_fee, source', [
    (0.0005, 150.0 * 0.0005, 'published taker'),      # MEXC's public spot taker rate
    (0.0026, 150.0 * 0.0026, 'published taker'),      # Kraken's, above the configured 0.1 %
    (None, 0.15, 'configured'),                        # not published
    ('x', 0.15, 'configured'),                         # malformed
    (0.5, 0.15, 'configured'),                         # 50 %: a catalogue error
    (True, 0.15, 'configured'),
])
def test_the_paper_fee_is_the_published_taker_rate_when_the_catalogue_has_one(taker, expected_fee, source):
    markets = {'BTC/USDT': {'symbol': 'BTC/USDT', 'spot': True, 'active': True, 'taker': taker}}
    venue = paper_exchange.PaperExchange('mexc', catalogue=lambda: markets, settings=lambda: SETTINGS,
                                         balances=lambda: {'USDT': 10_000.0})
    order = venue.create_market_buy_order_with_cost('BTC/USDT', 150.0, {'reference_price': 100.0})
    assert order['fee']['cost'] == pytest.approx(expected_fee) and order['info']['fee_source'] == source
