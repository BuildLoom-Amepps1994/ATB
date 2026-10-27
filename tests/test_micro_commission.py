"""MEXC's symbol commission (signed ``GET /api/v3/tradeFee``): supplied responses only.

The brief allows this endpoint to be implemented and tested against supplied
responses, never called. Every response here is a literal: the documented
example, the same values as a CCXT client would decode them, and malformed
or hostile variants that must fail closed. No request is signed or sent.
"""
import ast
import json
import sys
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import micro_economics as me
import money
import venue_rules
from venue_rules import AccountCommission, CommissionError, parse_mexc_symbol_commission

D = Decimal

# MEXC's documented example response, verbatim (spot v3, Query Symbol Commission, checked 2026-10-27).
DOCUMENTED = '''{
    "data": {
        "makerCommission": 0E-18,
        "takerCommission": 0.000500000000000000,
        "rpiTakerCommission": null
    },
    "code": 0,
    "msg": "success",
    "timestamp": 1781149594924
}'''

RULES = venue_rules.VenueRules(
    exchange='mexc', symbol='ETH/USDT', base='ETH', quote='USDT', active=True, spot=True, status='online',
    side_permission='all', market_orders=True, market_buy_by_cost=True, amount_step=D('0.0001'),
    price_tick=D('0.01'), cost_step=D('0.01'), min_amount=D('0.0001'), max_amount=None, min_cost_market=D('1'),
    min_cost_limit=D('5'), max_cost_market=D('100000'), max_cost_limit=D('2000000'), taker_fee=D('0.0005'),
    maker_fee=D('0'), fee_source='public catalogue schedule (not this account)', buy_fee_currency='quote',
    sell_fee_currency='quote', loaded_at=1000.0, source='fixture')


def parse(response, symbol='ETH/USDT', received_at=1781149595.0):
    return parse_mexc_symbol_commission(response, symbol=symbol, received_at=received_at)


def documented(**data):
    response = json.loads(DOCUMENTED, parse_float=Decimal)
    response['data'].update(data)
    return response


def test_the_documented_response_parses_exactly():
    rates = parse(DOCUMENTED)
    assert isinstance(rates, AccountCommission)
    assert (rates.exchange, rates.symbol) == ('mexc', 'ETH/USDT')
    assert rates.maker == 0 and rates.taker == D('0.0005') and rates.rpi_taker is None
    assert rates.venue_ms == 1781149594924 and rates.received_at == 1781149595.0
    assert 'supplied response' in rates.source
    assert parse(DOCUMENTED.encode('utf-8')) == rates             # bytes as received
    record = rates.as_dict()
    assert D(record['taker']) == D('0.0005') and record['maker'] == '0' and record['rpi_taker'] is None
    json.dumps(record)                                            # a decision record can store it


def test_the_same_values_decoded_as_floats_parse_to_the_same_rates():
    """A CCXT client decodes JSON numbers as floats; each goes through its shortest repr."""
    decoded = json.loads(DOCUMENTED)
    assert isinstance(decoded['data']['takerCommission'], float)
    assert parse(decoded) == parse(DOCUMENTED)
    as_text = documented(makerCommission='0', takerCommission='0.001', rpiTakerCommission='0.0007')
    rates = parse(as_text)
    assert (rates.maker, rates.taker, rates.rpi_taker) == (0, D('0.001'), D('0.0007'))


def test_json_text_is_read_without_passing_a_rate_through_a_float():
    text = DOCUMENTED.replace('0.000500000000000000', '0.000500000000000000001')
    assert parse(text).taker == D('0.000500000000000000001')        # a float would give 0.0005


def test_a_long_json_number_gets_the_same_verdict_as_the_same_digits_in_a_string():
    digits = '0.0015' + '0' * 60 + '1'
    as_number = DOCUMENTED.replace('0.000500000000000000', digits)
    as_string = DOCUMENTED.replace('0.000500000000000000', f'"{digits}"')
    for response in (as_number, as_string):
        with pytest.raises(CommissionError, match='takerCommission'):
            parse(response)
    longest = '0.0005' + '0' * 57 + '1'                              # 64 characters, the stored limit
    rates = parse(DOCUMENTED.replace('0.000500000000000000', longest))
    assert money.from_text(rates.as_dict()['taker']) == D(longest)


def test_the_1_percent_bound_does_not_depend_on_the_callers_decimal_context():
    from decimal import localcontext
    for prec in (1, 3, 28):
        with localcontext() as context:
            context.prec = prec
            for field, value in (('takerCommission', '0.014'), ('makerCommission', '-0.0149')):
                with pytest.raises(CommissionError, match='beyond 1 %'):
                    parse(DOCUMENTED.replace({'takerCommission': '0.000500000000000000',
                                              'makerCommission': '0E-18'}[field], value))
    with localcontext() as context:
        context.prec = 1
        assert parse(DOCUMENTED).taker == D('0.0005')


def test_a_maker_rebate_is_kept_as_a_negative_rate():
    assert parse(documented(makerCommission=D('-0.0001'))).maker == D('-0.0001')


@pytest.mark.parametrize('response, reason', [
    ({'code': 700002, 'msg': 'Signature for this request is not valid.'}, 'code 700002'),
    ({**json.loads(DOCUMENTED), 'code': 200}, 'code 200'),
    ({**json.loads(DOCUMENTED), 'code': '0'}, 'no integer code'),
    ({**json.loads(DOCUMENTED), 'code': False}, 'no integer code'),
    ({k: v for k, v in json.loads(DOCUMENTED).items() if k != 'code'}, 'no integer code'),
    ({**json.loads(DOCUMENTED), 'data': None}, 'no data object'),
    ({**json.loads(DOCUMENTED), 'data': [0.0005]}, 'no data object'),
    ({**json.loads(DOCUMENTED), 'timestamp': None}, 'no timestamp'),
    ({**json.loads(DOCUMENTED), 'timestamp': True}, 'no timestamp'),
    ({**json.loads(DOCUMENTED), 'timestamp': '1781149594924'}, 'no timestamp'),
    ({**json.loads(DOCUMENTED), 'timestamp': 1781149594924.5}, 'no timestamp'),
    ({**json.loads(DOCUMENTED), 'timestamp': 0}, 'no timestamp'),
    ({**json.loads(DOCUMENTED), 'timestamp': -1}, 'no timestamp'),
    ({'code': 10 ** 5000, 'msg': 'x'}, 'code out of range'),          # too many digits to format
    ({'code': -(10 ** 12), 'msg': 'x'}, 'code out of range'),
    ({'code': 10 ** 12 - 1, 'msg': 'x'}, 'code 999999999999,'),
    ([], 'not a JSON object'),
    (None, 'not a JSON object'),
    ('not json', 'not valid JSON'),
    ('{"code": 0, "data": {"takerCommission": NaN, "makerCommission": 0}, "timestamp": 1}', 'not valid JSON'),
    ('{"code": 0, "data": {"takerCommission": Infinity, "makerCommission": 0}, "timestamp": 1}', 'not valid JSON'),
    ('[' * 100000 + ']' * 100000, 'too long'),
    (b'\xff\xfe', 'not UTF-8'),
    # Decimal raises InvalidOperation (an ArithmeticError) on an exponent it cannot hold.
    (DOCUMENTED.replace('0.000500000000000000', '1e1000000000000000000'), 'not valid JSON'),
    (DOCUMENTED.replace('0E-18', '0e99999999999999999999'), 'not valid JSON'),
    (DOCUMENTED.replace('0.000500000000000000', '1e1000000000000000000').encode('utf-8'), 'not valid JSON'),
    # json.loads keeps a repeated key's last copy, so a refusal could be overridden by a later 'code'.
    ('{"code": 700002, "code": 0, "data": {"takerCommission": 0.0005, "makerCommission": 0}, "timestamp": 1}',
     'not valid JSON'),
    ('{"code": 0, "data": {"takerCommission": 0.5, "takerCommission": 0.0005, "makerCommission": 0}, "timestamp": 1}',
     'not valid JSON'),
    ('{"code": 0, "data": {"takerCommission": 0.5, "makerCommission": 0}, '
     '"data": {"takerCommission": 0.0005, "makerCommission": 0}, "timestamp": 1}', 'not valid JSON'),
])
def test_a_malformed_or_refused_response_fails_closed(response, reason):
    with pytest.raises(CommissionError, match=reason):
        parse(response)


class HostileInt(int):
    """A caller-supplied number type whose arithmetic raises something other than a money error."""
    def __abs__(self):
        raise TypeError('hostile')


@pytest.mark.parametrize('field, value, reason', [
    ('takerCommission', None, 'takerCommission is missing'),
    ('takerCommission', D('-0.0001'), 'takerCommission'),
    ('takerCommission', D('0.0101'), 'beyond 1 %'),
    ('takerCommission', True, 'takerCommission'),
    ('takerCommission', 'abc', 'takerCommission'),
    ('takerCommission', '1e999', 'takerCommission'),
    ('takerCommission', float('nan'), 'takerCommission'),
    ('takerCommission', [0.0005], 'takerCommission'),
    ('takerCommission', HostileInt(0), 'takerCommission is not a valid number'),
    ('makerCommission', None, 'makerCommission is missing'),
    ('makerCommission', D('-0.0101'), 'beyond 1 %'),
    ('makerCommission', D('0.02'), 'beyond 1 %'),
    ('rpiTakerCommission', D('-0.0001'), 'rpiTakerCommission'),
    ('rpiTakerCommission', D('0.5'), 'beyond 1 %'),
    ('rpiTakerCommission', 'n/a', 'rpiTakerCommission'),
    ('takerCommission', D('0.0015' + '0' * 60 + '1'), 'more digits than a stored rate may'),
    ('makerCommission', D('-0.0001' + '0' * 60 + '1'), 'more digits than a stored rate may'),
    ('takerCommission', D('0.01000000000000000000000000000001'), 'beyond 1 %'),      # above 1 % past 28 digits
    ('makerCommission', D('-0.01000000000000000000000000000001'), 'beyond 1 %'),
])
def test_a_missing_malformed_or_implausible_rate_fails_closed(field, value, reason):
    with pytest.raises(CommissionError, match=reason):
        parse(documented(**{field: value}))


def test_a_numpy_float_rate_parses_through_its_float_value():
    """A pandas or NumPy caller may hand over np.float64, whose repr is 'np.float64(0.0005)'."""
    numpy = pytest.importorskip('numpy')
    rates = parse(documented(takerCommission=numpy.float64(0.0005), makerCommission=numpy.float64(0.0)))
    assert (rates.taker, rates.maker) == (D('0.0005'), 0)
    assert money.parse(numpy.float64(0.0005), field='fee') == D('0.0005')


def test_the_boundary_rates_are_accepted():
    rates = parse(documented(takerCommission=D('0.01'), makerCommission=D('-0.01'), rpiTakerCommission=D('0')))
    assert (rates.taker, rates.maker, rates.rpi_taker) == (D('0.01'), D('-0.01'), D('0'))


@pytest.mark.parametrize('symbol, received_at', [('', 1.0), ('  ', 1.0), (None, 1.0), ('ETH/USDT', None),
                                                 ('ETH/USDT', float('nan')), ('ETH/USDT', True),
                                                 ('ETH/USDT', 10 ** 400), ('ETH/USDT', '1781149595'),
                                                 ('ETH/USDT', float('inf')), ('ETH/USDT', float('-inf'))])
def test_the_caller_must_say_which_symbol_and_when(symbol, received_at):
    with pytest.raises(CommissionError):
        parse(DOCUMENTED, symbol=symbol, received_at=received_at)


def test_age_is_unknown_for_a_future_or_unreadable_time():
    rates = parse(DOCUMENTED, received_at=1000.0)
    assert rates.age_seconds(1060.0) == 60.0
    assert rates.age_seconds(999.0) is None and rates.age_seconds(float('nan')) is None
    assert rates.age_seconds(None) is None and rates.age_seconds('1060') is None and rates.age_seconds(True) is None
    assert rates.age_seconds(10 ** 400) is None                    # too large for a float: unknown, not a crash
    assert rates.age_seconds(1060) == 60.0
    assert rates.age_seconds(float('inf')) is None
    extreme = parse(DOCUMENTED, received_at=-1e308)
    assert extreme.age_seconds(1e308) is None                       # the difference overflows to inf: unknown


def model(account=None, rules=RULES, configured='0.1'):
    return me.cost_model(configured_fee_pct=configured, configured_slippage_pct=0.2, latency_bps=3, rules=rules,
                         account=account)


def test_an_account_rate_joins_the_maximum_and_never_lowers_the_modeled_fee():
    without = model()
    assert without.taker_fee == D('0.001') and 'account' not in without.fee_source
    lower = model(parse(documented(takerCommission=D('0.0002'))))
    assert lower.taker_fee == D('0.001') and 'account taker 0.02% joins the maximum' in lower.fee_source
    higher = model(parse(documented(takerCommission=D('0.002'))))
    assert higher.taker_fee == D('0.002') and 'account taker 0.2%' in higher.fee_source
    public_binds = model(parse(documented(takerCommission=D('0.0002'))), configured='0.01')
    assert public_binds.taker_fee == D('0.0005') and 'public taker 0.05%' in public_binds.fee_source
    unknown_public = model(parse(documented(takerCommission=D('0.0015'))), rules=replace(RULES, taker_fee=None))
    assert unknown_public.taker_fee == D('0.0015') and 'public taker fee unknown' in unknown_public.fee_source


def test_an_account_rate_for_another_symbol_or_venue_is_refused():
    with pytest.raises(money.MoneyError, match='BTC/USDT'):
        model(parse(DOCUMENTED, symbol='BTC/USDT'))
    with pytest.raises(money.MoneyError, match='kraken'):
        model(parse(DOCUMENTED), rules=replace(RULES, exchange='kraken'))
    with pytest.raises(money.MoneyError, match='AccountCommission'):
        model({'taker': '0.0005'})


SKIPPED_DIRS = {'tests', '.claude', '.git', '__pycache__', 'build', 'dist', 'node_modules'}


def application_sources():
    root = Path(__file__).resolve().parents[1]
    for path in sorted(root.rglob('*.py')):
        if not SKIPPED_DIRS.intersection(path.relative_to(root).parts[:-1]):
            yield path, path.read_text(encoding='utf-8')


def test_no_application_module_obtains_a_signed_fee():
    """The parser is the only code that touches this endpoint, and nothing reads a signed fee.

    CCXT reaches the endpoint as ``fetch_trading_fee(s)``, ``fetchTradingFee(s)``,
    ``spotPrivateGetTradeFee`` or ``spot_private_get_tradefee``; the lower-cased
    names below match every spelling of each.
    """
    forbidden = ('gettradefee', 'get_tradefee', 'fetch_trading_fee', 'fetchtradingfee')
    sources = list(application_sources())
    assert any(path.name == 'micro.py' for path, _ in sources)      # the walk does see the application
    for path, text in sources:
        lowered = text.lower()
        assert not any(name in lowered for name in forbidden), path
    # Any other spelling of the endpoint (CCXT's generic request('tradeFee', ...), a raw URL) names it.
    naming = [path.name for path, text in sources if 'tradefee' in text.lower() and path.name != 'venue_rules.py']
    assert naming == []
    parser_module = next(text for path, text in sources if path.name == 'venue_rules.py')
    imported = {alias.name.split('.')[0] for node in ast.walk(ast.parse(parser_module))
                if isinstance(node, ast.Import) for alias in node.names}
    imported |= {node.module.split('.')[0] for node in ast.walk(ast.parse(parser_module))
                 if isinstance(node, ast.ImportFrom) and node.module}
    assert imported <= {'__future__', 'json', 'math', 'dataclasses', 'decimal', 'money'}, imported
    callers = [path.name for path, text in sources
               if 'parse_mexc_symbol_commission' in text and path.name != 'venue_rules.py']
    assert callers == []
    passes_account = [f'{path.name}:{node.lineno}' for path, text in sources for node in ast.walk(ast.parse(text))
                      if isinstance(node, ast.Call) and getattr(node.func, 'attr', getattr(node.func, 'id', None))
                      == 'cost_model' and any(keyword.arg in ('account', None) for keyword in node.keywords)]
    assert passes_account == []                                     # no code path feeds an account rate yet
