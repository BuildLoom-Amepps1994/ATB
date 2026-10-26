"""Exact money parsing (brief test 11) and venue-rule normalization."""
import sys
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import money
import venue_rules

D = Decimal


@pytest.mark.parametrize('bad', [True, False, None, float('nan'), float('inf'), float('-inf'), 'nan', 'inf',
                                 'Infinity', '1e400', '1e-400', '1' * 70, '0x10', '1,5', ' ', '', '--1',
                                 '1e20', 10 ** 20, [], {}, object()])
def test_parse_refuses_bool_missing_nonfinite_huge_and_malformed(bad):
    with pytest.raises(money.MoneyError):
        money.parse(bad)


def test_parse_keeps_decimal_strings_exact_and_floats_through_their_shortest_repr():
    assert money.parse('0.1') + money.parse('0.2') == D('0.3')
    assert money.parse(0.1) == D('0.1')          # shortest repr, no binary noise added
    assert money.parse(1 / 3) == D(repr(1 / 3))  # but lost digits are not restored
    assert money.parse('25') == D('25')
    assert money.parse('  12.50 ') == D('12.50')
    assert money.parse('1e-8') == D('1E-8')
    assert money.parse(-2, allow_negative=True) == D(-2)


def test_parse_flags_for_sign_and_zero():
    with pytest.raises(money.MoneyError):
        money.parse('-1')
    with pytest.raises(money.MoneyError):
        money.parse('0', allow_zero=False)
    assert money.parse('-0') == 0 and money.text(money.parse('-0')) == '0'
    assert money.parse_optional(None) is None


def test_error_messages_never_echo_the_value():
    secret = 'sk_live_' + 'x' * 30
    with pytest.raises(money.MoneyError) as raised:
        money.parse(secret, field='fee')
    assert secret not in str(raised.value) and 'fee' in str(raised.value)


def test_quantize_down_and_up_at_explicit_steps():
    assert money.q_down(D('24.9999'), D('0.01')) == D('24.99')
    assert money.q_up(D('0.024975'), D('0.01')) == D('0.03')
    assert money.q_down(D('123.456'), D('1')) == D('123')
    assert money.q_down(D('-0.015'), D('0.01')) == D('-0.02')   # floor, not toward zero
    with pytest.raises(money.MoneyError):
        money.q_down(D('1'), D('0'))


def test_display_never_hides_a_sign_or_a_tiny_amount():
    assert money.display(D('-0.00003')) == '-0.00003'
    assert money.display(D('0.05'), signed=True) == '+0.0500'
    assert money.display(D('-0.05'), signed=True) == '-0.0500'
    assert money.display(D('0')) == '0.0000'
    assert money.display(D('-1E-12')) == '-<0.00000001'
    assert money.display(None) == 'unknown'


def test_text_round_trips_without_exponent_notation():
    for value in ('25', '0.00000001', '-0.15', '1234567.891011'):
        assert money.from_text(money.text(D(value))) == D(value)
    assert 'E' not in money.text(D('1E-8'))


def mexc_market(**info):
    base = {'status': '1', 'orderTypes': ['LIMIT', 'MARKET', 'LIMIT_MAKER'], 'isSpotTradingAllowed': True,
            'quoteAmountPrecision': '5', 'baseSizePrecision': '0.0001'}
    base.update(info)
    return {'id': 'ETHUSDT', 'symbol': 'ETH/USDT', 'base': 'ETH', 'quote': 'USDT', 'type': 'spot', 'spot': True,
            'active': True, 'contract': False, 'swap': False, 'future': False, 'option': False,
            'taker': 0.0005, 'maker': 0.0, 'precision': {'amount': 0.0001, 'price': 0.01},
            'limits': {'amount': {'min': 0.0001, 'max': None}, 'cost': {'min': 5.0, 'max': 2000000.0}}, 'info': base}


def test_mexc_market_minimum_comes_from_the_market_order_field_when_published():
    rules = venue_rules.normalize(mexc_market(quoteAmountPrecisionMarket='1', maxQuoteAmountMarket='100000'),
                                  exchange='mexc', symbol='ETH/USDT', loaded_at=100.0)
    assert rules.min_cost_market == D('1') and rules.min_cost_limit == D('5.0')
    assert rules.market_min_cost() == D('1') and rules.market_max_cost() == D('100000')
    assert rules.amount_step == D('0.0001') and rules.price_tick == D('0.01')
    assert rules.taker_fee == D('0.0005') and 'not this account' in rules.fee_source
    assert rules.tradable('BUY') == (True, None, '') and rules.tradable('SELL')[0]


def test_missing_market_minimum_falls_back_to_the_limit_minimum_and_says_so():
    rules = venue_rules.normalize(mexc_market(), exchange='mexc', symbol='ETH/USDT', loaded_at=100.0)
    assert rules.min_cost_market is None and rules.market_min_cost() == D('5.0')
    assert any('limit-order minimum is used' in issue for issue in rules.issues)


@pytest.mark.parametrize('info,side,code', [
    ({'status': '2'}, 'BUY', venue_rules.MARKET_INACTIVE),
    ({'status': '3'}, 'SELL', venue_rules.MARKET_INACTIVE),
    ({'tradeSideType': '3'}, 'BUY', venue_rules.SIDE_NOT_ALLOWED),
    ({'tradeSideType': '2'}, 'SELL', venue_rules.SIDE_NOT_ALLOWED),
    ({'orderTypes': ['LIMIT', 'LIMIT_MAKER']}, 'BUY', venue_rules.MARKET_ORDERS_UNSUPPORTED),
    ({'quoteOrderQtyMarketAllowed': False}, 'BUY', venue_rules.COST_BUY_UNSUPPORTED),
])
def test_untradable_markets_name_the_rule(info, side, code):
    rules = venue_rules.normalize(mexc_market(**info), exchange='mexc', symbol='ETH/USDT', loaded_at=1.0)
    ok, reason, detail = rules.tradable(side)
    assert not ok and reason == code and 'ETH/USDT' in detail


def test_an_unreadable_side_rule_allows_an_exit_but_no_entry():
    rules = venue_rules.normalize(mexc_market(tradeSideType='5'), exchange='mexc', symbol='ETH/USDT', loaded_at=1.0)
    assert rules.side_permission == 'unknown'
    assert any('tradeSideType' in issue for issue in rules.issues)
    ok, reason, _ = rules.tradable('BUY')
    assert not ok and reason == venue_rules.SIDE_NOT_ALLOWED
    assert rules.tradable('SELL') == (True, None, '')
    missing = venue_rules.normalize(mexc_market(), exchange='mexc', symbol='ETH/USDT', loaded_at=1.0)
    assert missing.side_permission == 'all' and missing.tradable('BUY')[0]       # no field: MEXC's default


def test_unknown_or_malformed_metadata_is_unknown_not_zero():
    market = mexc_market()
    market['limits']['amount']['min'] = 'garbage'
    market['precision']['amount'] = None
    rules = venue_rules.normalize(market, exchange='mexc', symbol='ETH/USDT', loaded_at=1.0)
    assert rules.min_amount is None and rules.amount_step is None
    assert any('malformed' in issue for issue in rules.issues)
    missing = venue_rules.normalize(None, exchange='mexc', symbol='NEW/USDT', loaded_at=None)
    assert missing.tradable('BUY')[1] == venue_rules.MARKET_NOT_SPOT


def test_stale_catalogue_blocks_trading_when_an_age_limit_is_given():
    rules = venue_rules.normalize(mexc_market(), exchange='mexc', symbol='ETH/USDT', loaded_at=100.0)
    assert rules.tradable('BUY', now=200.0, max_age_seconds=3600)[0]
    assert rules.tradable('BUY', now=100.0 + 7200, max_age_seconds=3600)[1] == venue_rules.METADATA_STALE


def test_decimal_places_precision_mode_becomes_a_step():
    market = mexc_market()
    market['precision'] = {'amount': 4, 'price': 2}
    rules = venue_rules.normalize(market, exchange='kraken', symbol='ETH/USDT', loaded_at=1.0,
                                  precision_mode=venue_rules.DECIMAL_PLACES)
    assert rules.amount_step == D('0.0001') and rules.price_tick == D('0.01')


@pytest.mark.parametrize('value, step, down, up', [
    ('5.499999999999999999999999999999999', '0.5', '5.0', '5.5'),
    ('5.500000000000000000000000000000001', '0.5', '5.5', '6.0'),
    ('2.99999999999999999999999999999999999999', '1', '2', '3'),
    ('0.010000000000000000000000000000000001', '0.01', '0.01', '0.02'),
])
def test_step_rounding_never_crosses_its_input(value, step, down, up):
    value, step = money.parse(value), money.parse(step)
    assert money.q_down(value, step) == D(down) and money.q_down(value, step) <= value
    assert money.q_up(value, step) == D(up) and money.q_up(value, step) >= value


def test_rules_with_an_unknowable_age_are_stale():
    rules = venue_rules.normalize(mexc_market(), exchange='mexc', symbol='ETH/USDT', loaded_at=10_000.0)
    assert rules.tradable('BUY', now=10_030.0, max_age_seconds=60) == (True, None, '')
    for now, limit in ((100.0, 60), (float('nan'), 60), (10_030.0, float('nan'))):     # future stamp, bad clock, bad limit
        ok, reason, _ = rules.tradable('BUY', now=now, max_age_seconds=limit)
        assert not ok and reason == venue_rules.METADATA_STALE, (now, limit)
    assert rules.age_seconds(100.0) is None and rules.age_seconds(10_030.0) == 30.0
