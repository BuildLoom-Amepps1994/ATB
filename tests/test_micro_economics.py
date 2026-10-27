"""Micro-trading sizing, costs and accounting: brief tests 1-11 and the budget-revision regressions.

Each test states an invariant the economics must keep (the all-in cap, exact
conservation, costs counted once, unknown never treated as zero) and checks
it against fixtures built here. No network, no settings, no clock.
"""
import json
import sys
from dataclasses import dataclass, replace
from decimal import Decimal, localcontext
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import micro_economics as me
import money
import venue_rules
from venue_rules import VenueRules

D = Decimal


@dataclass(frozen=True)
class Book:
    """Stand-in for micro_book.BookSnapshot with only the attributes the economics read."""
    bids: tuple
    asks: tuple
    depth_known: bool = True
    symbol: object = 'ETH/USDT'
    exchange: object = 'mexc'
    stated_bid: object = None     # a best_bid / best_ask the snapshot states, when it differs from its levels
    stated_ask: object = None

    @property
    def best_bid(self):
        return self.bids[0][0] if self.stated_bid is None else self.stated_bid

    @property
    def best_ask(self):
        return self.asks[0][0] if self.stated_ask is None else self.stated_ask


RULES = VenueRules(
    exchange='mexc', symbol='ETH/USDT', base='ETH', quote='USDT', active=True, spot=True, status='online',
    side_permission='all', market_orders=True, market_buy_by_cost=True, amount_step=D('0.0001'),
    price_tick=D('0.01'), cost_step=D('0.01'), min_amount=D('0.0001'), max_amount=None, min_cost_market=D('1'),
    min_cost_limit=D('5'), max_cost_market=D('100000'), max_cost_limit=D('2000000'), taker_fee=D('0.0005'),
    maker_fee=D('0'), fee_source='public catalogue schedule (not this account)', buy_fee_currency='quote',
    sell_fee_currency='quote', loaded_at=1000.0, source='fixture')


def rules(**changes):
    return replace(RULES, **changes)


def cost(fee='0.001', latency='3', fallback='20', buy='quote', sell='quote'):
    return me.CostModel(taker_fee=D(fee), fee_source='test', latency_bps=D(latency),
                        fallback_slippage_bps=D(fallback), buy_fee_currency=buy, sell_fee_currency=sell)


def deep_book(bid='99.99', ask='100.01', qty='1000', **kw):
    return Book(bids=((D(bid), D(qty)),), asks=((D(ask), D(qty)),), **kw)


def size(*, rules_=RULES, book_=None, cost_=None, budget='25', cash='1000', allocation='1000', target='0.05',
         stress='10', **kw):
    return me.size_entry(rules=rules_, book=book_ if book_ is not None else deep_book(),
                         cost=cost_ if cost_ is not None else cost(), budget_cap=D(budget), cash_available=D(cash),
                         allocation_remaining=D(allocation), target_net=D(target), exit_stress_bps=D(stress),
                         now_wall=2000.0, **kw)


def check(result, name):
    return next(item for item in result.checks if item['check'] == name)


def fill(result, fee='0.001', currency='quote'):
    """Book the fill the sizing predicted, with the fee billed as the venue would bill it."""
    if currency == 'base':
        base_fee = money.mul(result.expected_base, D(fee))
        return me.account_entry(filled_base=result.expected_base, gross_quote=result.gross_quote, quote_fee=D('0'),
                                base_fee=base_fee, other_fees={}, other_fee_value=None, fee_status='simulated',
                                reserved=result.max_debit)
    return me.account_entry(filled_base=result.expected_base, gross_quote=result.gross_quote,
                            quote_fee=money.mul(result.gross_quote, D(fee)), base_fee=None, other_fees={},
                            other_fee_value=None, fee_status='simulated', reserved=result.max_debit)


def exit_estimate(owned, basis, book, *, rules_=RULES, cost_=None, target='0.05', uncertainty='5'):
    return me.estimate_exit(owned_base=owned, basis_quote=basis, rules=rules_, book=book,
                            cost=cost_ if cost_ is not None else cost(), target_net=D(target),
                            uncertainty_bps=D(uncertainty))


def realize(**kw):
    values = dict(owned_base=D('0.25'), basis_quote=D('25.02'), sold_base=D('0.25'), gross_quote=D('25.10'),
                  quote_fee=D('0'), base_fee=None, other_fees={}, other_fee_value=None, fee_status='simulated')
    values.update(kw)
    return me.realize_exit(**values)


# ----------------------------------------------------------- 1. the all-in cap
@pytest.mark.parametrize('currency', ['quote', 'unknown', 'base'])
def test_all_in_cap_holds_with_quote_fees_and_rounding_across_prices_steps_and_fees(currency):
    eligible = 0
    for price in (D('0.00001234'), D('0.5'), D('99.99'), D('65000.5')):
        book = Book(bids=((price, D('1E9')),), asks=((money.add(price, D('0.00000001')), D('1E9')),))
        for step in (D('0.00000001'), D('0.0001'), D('0.01'), D('1')):
            pair = rules(cost_step=step, price_tick=D('0.00000001'), amount_step=D('0.00000001'), min_amount=None,
                         min_cost_market=D('0.01'))
            for fee in ('0', '0.0005', '0.002', '0.01'):
                for budget in ('0.5', '3.33', '10', '24.99', '25'):
                    result = size(rules_=pair, book_=book, cost_=cost(fee=fee, buy=currency), budget=budget)
                    assert result.target_budget <= result.budget_cap <= me.MICRO_MAX_ALL_IN_USDT
                    reserve_check = check(result, 'fee_reserve')
                    gross, reserve = D(reserve_check['gross_quote']), D(reserve_check['fee_reserve'])
                    target = result.target_budget
                    assert money.add(gross, reserve) <= target
                    assert money.q_down(gross, step) == gross and money.q_up(reserve, step) == reserve
                    bigger = money.add(gross, step)
                    if currency == 'base':
                        assert reserve == 0 and bigger > target
                    else:
                        # the reserve covers the fee the venue will charge, and one more step would not fit
                        assert reserve >= money.mul(gross, D(fee))
                        assert money.add(bigger, money.q_up(money.mul(bigger, D(fee)), step)) > target
                    if result.eligible:
                        eligible += 1
                        assert result.max_debit == money.add(result.gross_quote, result.fee_reserve)
                        assert result.max_debit <= result.target_budget
                    else:
                        assert result.gross_quote is None and result.max_debit is None
    assert eligible > 200


# ------------------------------------------------------------ 2. base-asset fees
def test_base_fee_reduces_sellable_ownership_without_oversell_and_conserves_basis():
    result = size(cost_=cost(buy='base'))
    assert result.eligible and result.fee_reserve == 0 and result.max_debit == result.gross_quote
    owned = money.sub(result.expected_base, result.expected_fee_base)
    assert result.expected_fee_base > 0 and owned < result.expected_base
    assert money.add(result.sellable_base, result.predicted_residual_base) == owned
    assert result.sellable_base <= owned

    # An unknown fee currency is sized both ways: quote reserved and base assumed.
    unknown = size(cost_=cost(buy='unknown'))
    assert unknown.fee_reserve > 0 and unknown.expected_fee_base > 0 and unknown.max_debit <= D('25')
    assert unknown.sellable_base <= result.sellable_base

    entry = fill(result, currency='base')
    assert entry.debit == entry.basis == result.gross_quote
    assert entry.owned_base == money.sub(entry.filled_base, entry.base_fee)
    with pytest.raises(money.MoneyError):
        realize(owned_base=entry.owned_base, basis_quote=entry.basis, sold_base=entry.filled_base,
                gross_quote=D('25'))
    with pytest.raises(money.MoneyError):     # the base fee on the sale counts against ownership too
        realize(owned_base=entry.owned_base, basis_quote=entry.basis, sold_base=entry.owned_base,
                gross_quote=D('25'), base_fee=D('0.00000001'))
    sale_fee = D('0.0002')
    closed = realize(owned_base=entry.owned_base, basis_quote=entry.basis,
                     sold_base=money.sub(entry.owned_base, sale_fee), gross_quote=D('24.9'), base_fee=sale_fee)
    assert closed.closed_base == entry.owned_base and closed.residual_base == 0
    assert closed.allocated_basis == entry.basis and closed.residual_basis == 0


# ------------------------------------------------------------ 3. fee provenance
def test_fee_statuses_stay_distinct_and_a_missing_fee_is_never_filled_from_a_configured_rate():
    zero = realize(quote_fee=D('0'), fee_status='simulated')
    observed = realize(quote_fee=D('0.0251'), fee_status='observed')
    missing = realize(quote_fee=None, fee_status='simulated')
    third_valued = realize(other_fees={'MX': D('0.004')}, other_fee_value=D('0.0251'), fee_status='observed')
    third_unvalued = realize(other_fees={'MX': D('0.004')}, other_fee_value=None, fee_status='observed')
    confirmed_later = realize(other_fees={'MX': D('0.004')}, other_fee_value=D('0.0251'), fee_status='reconciled')

    assert (zero.fee_status, zero.status, zero.net_proceeds) == ('simulated', 'confirmed', D('25.10'))
    assert (observed.fee_status, observed.status, observed.net_proceeds) == ('observed', 'confirmed', D('25.0749'))
    assert (missing.fee_status, missing.status) == ('unknown', 'unconfirmed')
    assert (third_valued.fee_status, third_valued.status) == ('estimated', 'unconfirmed')
    assert (third_unvalued.fee_status, third_unvalued.status) == ('unknown', 'unconfirmed')
    assert (confirmed_later.fee_status, confirmed_later.status) == ('reconciled', 'confirmed')

    # The missing fee is not replaced by any rate: proceeds are the gross exactly, and the record says so.
    assert missing.net_proceeds == D('25.10') and missing.realized_net == D('0.08')
    assert any(note.startswith(me.FEE_UNKNOWN) and 'quote fee' in note for note in missing.notes)
    assert any('MX' in note for note in third_unvalued.notes) and not any('MX' in n for n in missing.notes)
    assert third_valued.realized_net == D('0.0549') and third_unvalued.realized_net == D('0.08')
    with pytest.raises(money.MoneyError):
        realize(quote_fee=D('0.0251'), fee_status='modeled')
    with pytest.raises(money.MoneyError):
        realize(fee_status='guessed')

    entry = me.account_entry(filled_base=D('0.25'), gross_quote=D('24.97'), quote_fee=None, base_fee=None,
                             other_fees={}, other_fee_value=None, fee_status='simulated', reserved=D('25'))
    assert entry.fee_status == 'unknown' and entry.debit == D('24.97') and entry.notes
    third = me.account_entry(filled_base=D('0.25'), gross_quote=D('24.97'), quote_fee=D('0'), base_fee=None,
                             other_fees={'MX': D('0.003')}, other_fee_value=D('0.02'), fee_status='observed',
                             reserved=D('25'))
    assert third.fee_status == 'estimated' and third.debit == D('24.97') and third.basis == D('24.99')


# ------------------------------------------------- 4. venue minimum above budget
def test_venue_minimum_above_the_budget_is_rejected_with_a_separate_minimum_and_no_upsizing():
    result = size(rules_=rules(min_cost_market=D('30')))
    assert not result.eligible and result.code == me.BUDGET_BELOW_VENUE_MINIMUM
    assert 'min_cost_market' in result.detail and '30' in result.detail
    assert result.gross_quote is None and result.fee_reserve is None and result.max_debit is None
    assert result.budget_cap == D('25') and result.target_budget == D('25')
    failed = [item for item in result.checks if not item['passed']]
    assert [item['check'] for item in failed] == ['venue_minimum'] and D(failed[0]['gross_quote']) <= D('25')
    assert result.minimum_feasible_budget is not None and result.minimum_feasible_budget > D('30')
    assert 'display only' in result.minimum_feasible_detail

    # The separately calculated minimum is exact to the cent: it sizes, one cent less does not.
    pair = rules(min_cost_market=D('12'))
    refused = size(rules_=pair, budget='10')
    assert refused.code == me.BUDGET_BELOW_VENUE_MINIMUM and refused.gross_quote is None
    minimum = refused.minimum_feasible_budget
    assert D('12') < minimum <= D('25')
    assert size(rules_=pair, budget=str(minimum)).eligible
    below = size(rules_=pair, budget=str(money.sub(minimum, D('0.01'))))
    assert below.code in (me.BUDGET_BELOW_VENUE_MINIMUM, me.EXIT_BELOW_VENUE_MINIMUM) and below.gross_quote is None


# ---------------------------------------- 5. precision and order-type minimums
def mexc_market(**info):
    base = {'status': '1', 'orderTypes': ['LIMIT', 'MARKET', 'LIMIT_MAKER'], 'quoteAmountPrecision': '5',
            'baseSizePrecision': '0.0001'}
    base.update(info)
    return {'id': 'ETHUSDT', 'symbol': 'ETH/USDT', 'base': 'ETH', 'quote': 'USDT', 'type': 'spot', 'spot': True,
            'active': True, 'contract': False, 'swap': False, 'future': False, 'option': False,
            'taker': 0.0005, 'maker': 0.0, 'precision': {'amount': 0.0001, 'price': 0.01},
            'limits': {'amount': {'min': 0.0001, 'max': None}, 'cost': {'min': 5.0, 'max': 2000000.0}}, 'info': base}


def test_market_order_minimum_not_the_limit_minimum_governs_and_quantities_sit_on_venue_steps():
    with_market = venue_rules.normalize(mexc_market(quoteAmountPrecisionMarket='1'), exchange='mexc',
                                        symbol='ETH/USDT', loaded_at=1000.0)
    limit_only = venue_rules.normalize(mexc_market(), exchange='mexc', symbol='ETH/USDT', loaded_at=1000.0)
    model = me.cost_model(configured_fee_pct=0.1, configured_slippage_pct=0.2, latency_bps=3, rules=with_market)
    small = size(rules_=with_market, cost_=model, budget='3')
    assert small.eligible and D(check(small, 'venue_minimum')['minimum']) == D('1')
    assert 'min_cost_market' in check(small, 'venue_minimum')['minimum_source']
    refused = size(rules_=limit_only, cost_=model, budget='3')
    assert refused.code == me.BUDGET_BELOW_VENUE_MINIMUM
    assert 'min_cost_limit' in refused.detail and '5' in refused.detail

    for result, pair in ((small, with_market), (size(rules_=with_market, cost_=model), with_market)):
        assert money.q_down(result.gross_quote, pair.cost_step) == result.gross_quote
        assert money.q_up(result.fee_reserve, pair.cost_step) == result.fee_reserve
        assert money.q_down(result.sellable_base, pair.amount_step) == result.sellable_base
        assert money.q_up(result.break_even_bid, pair.price_tick) == result.break_even_bid
        assert money.q_up(result.target_bid, pair.price_tick) == result.target_bid
        assert result.sellable_base >= pair.min_amount

    coarse = size(rules_=rules(amount_step=D('0.01')), budget='10')
    assert coarse.sellable_base == D('0.09') and D('0') < coarse.predicted_residual_base < D('0.01')
    assert size(rules_=rules(max_cost_market=D('10'))).code == me.MAX_COST_EXCEEDED
    assert size(rules_=rules(max_amount=D('0.1'))).code == me.MAX_COST_EXCEEDED
    unknown_step = size(rules_=rules(cost_step=None))
    assert unknown_step.eligible and check(unknown_step, 'fee_reserve')['issue'].startswith('quote-amount step unknown')


@pytest.mark.parametrize('info,code,name', [
    ({'orderTypes': ['LIMIT', 'LIMIT_MAKER']}, venue_rules.MARKET_ORDERS_UNSUPPORTED, 'venue_buy'),
    ({'tradeSideType': '2'}, venue_rules.SIDE_NOT_ALLOWED, 'venue_sell'),
    ({'status': '2'}, venue_rules.MARKET_INACTIVE, 'venue_buy'),
])
def test_venue_rule_codes_pass_through_and_an_entry_needs_a_possible_exit(info, code, name):
    pair = venue_rules.normalize(mexc_market(quoteAmountPrecisionMarket='1', **info), exchange='mexc',
                                 symbol='ETH/USDT', loaded_at=1000.0)
    result = size(rules_=pair)
    assert result.code == code and not check(result, name)['passed'] and result.gross_quote is None


def test_stale_rules_block_sizing_when_an_age_limit_is_given():
    assert size(max_rules_age_s=3600).eligible
    assert size(max_rules_age_s=1000).eligible          # exactly at the limit
    assert size(max_rules_age_s=60).code == venue_rules.METADATA_STALE
    assert not check(size(max_rules_age_s=60), 'rules_age')['passed']


def sized_at(now, limit, rules_=RULES):
    return me.size_entry(rules=rules_, book=deep_book(), cost=cost(), budget_cap=D('25'), cash_available=D('1000'),
                         allocation_remaining=D('1000'), target_net=D('0.05'), exit_stress_bps=D('10'),
                         now_wall=now, max_rules_age_s=limit)


@pytest.mark.parametrize('now,limit,rules_', [
    (None, 60, RULES), (float('nan'), 60, RULES), (float('inf'), 60, RULES), (True, 60, RULES), ('2000', 60, RULES),
    (None, None, RULES), (float('nan'), None, RULES),                      # now_wall is required even without a limit
    (2000.0, float('nan'), RULES), (2000.0, float('inf'), RULES), (2000.0, -1, RULES), (2000.0, True, RULES),
    (2000.0, '60', RULES),
    (0.0, 60, RULES),                                                       # earlier than loaded_at: age unknown, not 0
    (2000.0, 60, rules(loaded_at=float('nan'))), (2000.0, 60, rules(loaded_at=None)),
])
def test_an_unusable_clock_or_age_limit_fails_closed_instead_of_skipping_the_staleness_gate(now, limit, rules_):
    result = sized_at(now, limit, rules_)
    assert not result.eligible and result.code == venue_rules.METADATA_STALE
    assert result.gross_quote is None and result.max_debit is None
    assert not check(result, 'rules_age')['passed']


def test_a_usable_clock_still_sizes_without_an_age_limit():
    assert sized_at(2000, None).eligible and sized_at(2000.0, None).eligible
    assert sized_at(2000.0, None, rules(loaded_at=None)).eligible     # no limit given: the age is not judged
    assert sized_at(1000.0, 0).eligible                              # loaded this instant


# ------------------------------------------------- 6. exit below the venue rules
def test_entry_allowed_but_exit_below_the_minimum_amount_is_refused_before_exposure():
    pair = rules(min_cost_market=D('5'), min_cost_limit=D('5'))
    result = size(rules_=pair, budget='5.01')
    assert check(result, 'venue_minimum')['passed'] and D(check(result, 'fee_reserve')['gross_quote']) == D('5.00')
    assert result.code == me.EXIT_BELOW_VENUE_MINIMUM and not check(result, 'exit_minimum_amount')['passed']
    assert result.gross_quote is None
    assert result.minimum_feasible_budget > D('5.01') and size(rules_=pair, budget=str(result.minimum_feasible_budget)).eligible


def test_a_base_fee_that_leaves_dust_below_the_minimum_quantity_is_refused_before_exposure():
    pair = rules(min_amount=D('0.01'), min_cost_market=D('0.5'))
    book = deep_book(bid='98.99', ask='99.01')
    result = size(rules_=pair, book_=book, cost_=cost(fee='0.01', buy='base'), budget='1')
    assert check(result, 'minimum_quantity')['passed']        # the buy itself meets the minimum quantity
    assert result.code == me.EXIT_BELOW_VENUE_MINIMUM and not check(result, 'exit_minimum_quantity')['passed']
    assert D(check(result, 'sellable_quantity')['sellable_base']) < D('0.01')


# --------------------------------------------- 7-8. costs are never a profit
@pytest.mark.parametrize('fee,latency,ask,currency', [
    ('0', '0', '100.00', 'quote'), ('0.0005', '3', '100.01', 'quote'), ('0.001', '10', '100.50', 'quote'),
    ('0.002', '3', '100.01', 'base'), ('0.001', '0', '100.00', 'base'),
])
def test_flat_executable_prices_lose_with_positive_costs(fee, latency, ask, currency):
    book = deep_book(bid='99.99', ask=ask)
    model = cost(fee=fee, latency=latency, buy=currency)
    result = size(book_=book, cost_=model)
    assert result.eligible and result.break_even_bid > book.best_bid
    entry = fill(result, fee=fee, currency=currency)
    estimate = exit_estimate(entry.owned_base, entry.basis, book, cost_=model)
    assert estimate.depth_model == 'book' and estimate.est_net_pnl < 0


def test_a_gross_gain_below_costs_is_not_a_net_profit():
    result = size()
    entry = fill(result)
    later_bid = money.q_up(money.mul(result.entry_vwap, D('1.001')), D('0.01'))
    later = deep_book(bid=str(later_bid), ask=str(money.add(later_bid, D('0.02'))))
    assert later_bid > result.entry_vwap and later_bid < result.break_even_bid
    gross_gain = money.mul(entry.owned_base, money.sub(later_bid, result.entry_vwap))
    estimate = exit_estimate(entry.owned_base, entry.basis, later)
    assert gross_gain > 0 and estimate.est_net_pnl < 0


# ----------------------------------------------- 9. break-even and target math
def test_simple_break_even_and_target_ratios_match_the_brief_in_every_unit():
    rates = dict(buy_slip=D('0.002'), buy_fee=D('0.001'), sell_slip=D('0.002'), sell_fee=D('0.001'))
    ratio = me.simple_break_even_ratio(**rates)
    with localcontext() as context:
        context.prec = 60
        expected = (D('1.002') * D('1.001')) / (D('0.998') * D('0.999'))
    assert abs(ratio - expected) < D('1E-30')
    units = me.move_units(ratio)
    assert ratio.quantize(D('0.000001')) == D('1.006018')
    assert units['fraction'].quantize(D('0.000001')) == D('0.006018')
    assert units['percent'].quantize(D('0.0001')) == D('0.6018')
    assert units['bps'].quantize(D('0.01')) == D('60.18')
    target = me.simple_target_ratio(budget=D('25'), target_net=D('0.05'), **rates)
    assert target.quantize(D('0.000001')) == D('1.008030')
    assert me.move_units(target)['percent'].quantize(D('0.0001')) == D('0.8030')
    assert me.simple_target_ratio(budget=D('10'), target_net=D('0.02'), **rates) == target
    with pytest.raises(money.MoneyError):
        me.simple_break_even_ratio(buy_slip=D('1'), buy_fee=D('0'), sell_slip=D('0'), sell_fee=D('0'))


def test_sensitivity_rows_are_exact_strings_in_percent_bps_and_ratio():
    rows = me.sensitivity([D('10'), D('25')], [D('0.001')], [D('0.002')], D('20'))
    assert [row['budget'] for row in rows] == ['10', '25']
    assert D(rows[0]['target_net']) == D('0.02')
    assert D(rows[1]['target_net']) == D('0.05')
    for row in rows:
        assert D(row['break_even_pct']).quantize(D('0.0001')) == D('0.6018')
        assert D(row['target_move_bps']).quantize(D('0.01')) == D('80.30')
        assert row['cost_kind'] == 'proportional' and row['within_micro_cap'] is True
    json.dumps(rows)


def test_a_depth_walk_adds_no_flat_slippage_and_bids_are_not_charged_the_spread_again():
    book = Book(bids=((D('99.99'), D('0.1')), (D('99.95'), D('5'))), asks=((D('100.01'), D('5')),))
    wide_ask = replace(book, asks=((D('110'), D('5')),))
    owned, basis = D('0.25'), D('25.03')
    plain = exit_estimate(owned, basis, book, cost_=cost(fallback='0'))
    slipped = exit_estimate(owned, basis, book, cost_=cost(fallback='500'))
    wide = exit_estimate(owned, basis, wide_ask)
    assert plain.depth_model == 'book' and plain.gross_proceeds == slipped.gross_proceeds == wide.gross_proceeds
    with localcontext() as context:
        context.prec = 60
        expected = (D('0.1') * D('99.99') + D('0.15') * D('99.95')) * (1 - D('3') / 10000)
    assert plain.gross_proceeds == expected
    assert plain.sell_fee == money.mul(plain.gross_proceeds, D('0.001'))
    assert money.add(plain.net_proceeds, plain.sell_fee) == plain.gross_proceeds
    assert plain.est_net_pnl == money.sub(plain.net_proceeds, basis)
    assert 'none added' in plain.labels['slippage']


def test_latency_margin_is_applied_once_per_leg():
    book = Book(bids=((D('99.99'), D('1')), (D('99.98'), D('1'))), asks=((D('100.01'), D('1')), (D('100.02'), D('1'))))
    up, down = D('1.0005'), D('0.9995')
    spend = money.add(money.mul(D('100.01'), up), money.mul(D('100.02'), up, D('0.5')))
    asks = me.walk_asks(book, quote_amount=spend, latency_bps=D('5'))
    assert asks.base == D('1.5') and asks.quote == spend and not asks.exhausted
    assert asks.levels_used == 2 and asks.worst_price == money.mul(D('100.02'), up)
    bids = me.walk_bids(book, base_amount=D('1.5'), latency_bps=D('5'))
    assert bids.quote == money.add(money.mul(D('99.99'), down), money.mul(D('99.98'), down, D('0.5')))

    # Sizing and the exit estimate apply it the same single time: at the break-even bid the
    # estimate is not negative, and one tick lower it is.
    model = cost(latency='10')
    result = size(cost_=model, rules_=rules(amount_step=D('0.00000001')))
    owned = money.add(result.sellable_base, result.predicted_residual_base)
    at_even = deep_book(bid=str(result.break_even_bid), ask=str(money.add(result.break_even_bid, D('0.01'))))
    below = deep_book(bid=str(money.sub(result.break_even_bid, D('0.01'))), ask=str(result.break_even_bid))
    assert exit_estimate(owned, result.max_debit, at_even, cost_=model).est_net_pnl >= 0
    assert exit_estimate(owned, result.max_debit, below, cost_=model).est_net_pnl < 0
    at_target = deep_book(bid=str(result.target_bid), ask=str(money.add(result.target_bid, D('0.01'))))
    assert exit_estimate(owned, result.max_debit, at_target, cost_=model).est_net_pnl >= D('0.05')


# A small top bid, then levels 40 and 70 bps lower, then the real depth about 160 bps lower: the walk is not
# exhausted, so the pair is eligible, but selling the position into these bids costs far more than the best bid says.
THIN_TOP_BIDS = ((D('99.99'), D('0.02')), (D('99.59'), D('0.03')), (D('99.29'), D('0.05')), (D('98.39'), D('4')),
                 (D('98.29'), D('4')))


def test_break_even_and_target_bids_walk_the_bids_so_a_thin_top_cannot_hide_the_exit_cost():
    thin = Book(bids=THIN_TOP_BIDS, asks=((D('100.01'), D('1000')),))
    result = size(book_=thin)
    assert result.eligible                                    # the bid walk absorbs the sale: not a depth refusal
    owned = money.add(result.sellable_base, result.predicted_residual_base)
    # What the exit estimate says the moment the position is bought, on the same book.
    now = exit_estimate(owned, result.max_debit, thin)
    assert now.depth_model == 'book' and now.est_net_pnl < 0
    carried = money.mul(result.max_debit, money.sub(D('1'), money.div(thin.best_bid, result.break_even_bid)))
    assert money.add(now.est_net_pnl, carried) >= 0, (now.est_net_pnl, carried)   # the loss carried is not understated

    def shifted(best):
        """The same book with every level moved by the same factor, so its best bid is ``best``."""
        factor = money.div(best, thin.best_bid)
        return Book(bids=tuple((money.mul(price, factor), quantity) for price, quantity in thin.bids),
                    asks=((money.mul(D('100.01'), factor, D('1.01')), D('1000')),))
    assert exit_estimate(owned, result.max_debit, shifted(result.break_even_bid)).est_net_pnl >= 0
    assert exit_estimate(owned, result.max_debit, shifted(result.target_bid)).est_net_pnl >= D('0.05')
    assert result.required_move_bps == money.mul(money.sub(money.div(result.target_bid, D('100.01')), D('1')),
                                                 D('10000'))
    # On a deep book the walk sells everything at the best bid: the figures are the single-price ones, unchanged.
    deep = size()
    keep = money.mul(deep.sellable_base, D('0.9997'), D('0.999'))
    assert deep.break_even_bid == money.q_up(money.div(deep.max_debit, keep), D('0.01'))
    assert deep.target_bid == money.q_up(money.div(money.add(deep.max_debit, D('0.05')), keep), D('0.01'))
    assert result.sellable_base == deep.sellable_base and result.break_even_bid > deep.break_even_bid


# ------------------------------------------------ 10. partial-close conservation
def test_partial_closes_conserve_ownership_basis_and_cash_exactly():
    entry = me.account_entry(filled_base=D('0.24958519'), gross_quote=D('24.97'), quote_fee=D('0.02497'),
                             base_fee=None, other_fees={}, other_fee_value=None, fee_status='simulated',
                             reserved=D('25.00'))
    owned, basis = entry.owned_base, entry.basis
    third = money.q_down(money.div(owned, D('3')), D('0.0001'))
    sales = [(third, D('8.3301'), D('0.0083301'), None),
             (third, D('8.4112'), D('0'), D('0.0000832')),
             (None, D('8.1999'), D('0.0081999'), None)]
    closed_total = allocated_total = net_total = realized_total = D('0')
    for sold, gross, quote_fee, base_fee in sales:
        if sold is None:
            sold = owned
        result = me.realize_exit(owned_base=owned, basis_quote=basis, sold_base=sold, gross_quote=gross,
                                 quote_fee=quote_fee, base_fee=base_fee, other_fees={}, other_fee_value=None,
                                 fee_status='simulated')
        assert money.add(result.allocated_basis, result.residual_basis) == basis
        assert money.add(result.closed_base, result.residual_base) == owned
        assert result.status == 'confirmed'
        closed_total = money.add(closed_total, result.closed_base)
        allocated_total = money.add(allocated_total, result.allocated_basis)
        net_total = money.add(net_total, result.net_proceeds)
        realized_total = money.add(realized_total, result.realized_net)
        owned, basis = result.residual_base, result.residual_basis
    assert owned == 0 and basis == 0
    assert closed_total == entry.owned_base and allocated_total == entry.basis
    # cash: every net proceed in minus the one debit out is exactly the realized total
    assert money.sub(net_total, entry.debit) == realized_total


def test_an_exit_estimate_keeps_the_residual_and_its_share_of_the_basis():
    rules_ = rules(amount_step=D('0.01'))
    estimate = exit_estimate(D('0.2496'), D('25'), deep_book(), rules_=rules_)
    assert estimate.sellable_base == D('0.24') and estimate.residual_base == D('0.0096')
    assert estimate.allocated_basis == money.q_down(money.div(D('6'), D('0.2496')), me.ALLOCATION_STEP)
    assert estimate.allocated_basis < D('25')
    assert estimate.uncertainty == money.mul(D('0.24'), D('99.99'), D('0.0005'))


# ------------------------------------------------- 11. adversarial book levels
BAD_LEVELS = [
    (D('-1'), D('1')), ('nan', D('1')), ('NaN', D('1')), (True, D('1')), (False, D('1')), (float('nan'), D('1')),
    (float('inf'), D('1')), ('1e400', D('1')), (D('99.98'), True), (D('99.98'), False), (D('99.98'), 'NaN'),
    (D('99.98'), float('nan')), (D('99.98'), D('-1')), (D('99.98'), D('0')), (D('99.98'),),
    (D('99.98'), D('1'), D('2')), 'garbage', None,
]


@pytest.mark.parametrize('level', BAD_LEVELS)
def test_adversarial_book_levels_are_refused(level):
    # Behind a valid best level, so only the level validation itself can refuse it.
    book = Book(bids=((D('99.99'), D('0.01')), level), asks=((D('100.01'), D('5')),))
    with pytest.raises(money.MoneyError):
        me.walk_bids(book, base_amount=D('0.1'), latency_bps=D('0'))
    with pytest.raises(money.MoneyError):
        me.walk_asks(book, quote_amount=D('10'), latency_bps=D('0'))
    result = size(book_=book)
    assert not result.eligible and result.code == me.PRICE_UNKNOWN and result.gross_quote is None
    assert result.minimum_feasible_budget is None
    estimate = exit_estimate(D('0.25'), D('25'), book)
    assert estimate.depth_model == 'unavailable' and estimate.gross_proceeds is None and estimate.est_net_pnl is None


@pytest.mark.parametrize('level', BAD_LEVELS)
def test_adversarial_best_levels_are_refused_on_either_side(level):
    with pytest.raises(money.MoneyError):
        me.walk_bids(Book(bids=(level,), asks=((D('100.01'), D('5')),)), base_amount=D('0.1'), latency_bps=D('0'))
    with pytest.raises(money.MoneyError):
        me.walk_asks(Book(bids=((D('99.99'), D('5')),), asks=(level,)), quote_amount=D('10'), latency_bps=D('0'))


MALFORMED_BOOKS = {
    'crossed': Book(bids=((D('100.02'), D('1')),), asks=((D('100.01'), D('1')),)),
    'locked': Book(bids=((D('100.01'), D('1')),), asks=((D('100.01'), D('1')),)),
    'bids ascending': Book(bids=((D('99.98'), D('1')), (D('99.99'), D('1'))), asks=((D('100.01'), D('1')),)),
    'duplicate ask': Book(bids=((D('99.99'), D('1')),), asks=((D('100.02'), D('1')), (D('100.02'), D('1')))),
    'empty bids': Book(bids=(), asks=((D('100.01'), D('1')),)),
    'empty asks': Book(bids=((D('99.99'), D('1')),), asks=()),
    'stated best bid differs': deep_book(stated_bid=D('99.98')),
    'stated best ask differs': deep_book(stated_ask=D('100.02')),
    'too many levels': Book(bids=tuple((money.sub(D('99.99'), money.mul(D('0.001'), D(i))), D('1'))
                                       for i in range(me.MAX_BOOK_LEVELS + 1)), asks=((D('100.01'), D('1000')),)),
    'symbol not text': deep_book(symbol=42),
    'exchange not text': deep_book(exchange=b'mexc'),
}
FOREIGN_BOOKS = {
    'other symbol': deep_book(symbol='BTC/USDT'),
    'other exchange': deep_book(exchange='kraken'),
}


@pytest.mark.parametrize('name', sorted(MALFORMED_BOOKS))
def test_crossed_unsorted_empty_or_mislabelled_books_are_refused_everywhere(name):
    book = MALFORMED_BOOKS[name]
    with pytest.raises(money.MoneyError):
        me.walk_asks(book, quote_amount=D('1'), latency_bps=D('0'))
    with pytest.raises(money.MoneyError):
        me.walk_bids(book, base_amount=D('0.1'), latency_bps=D('0'))
    assert_refused(book)


@pytest.mark.parametrize('name', sorted(FOREIGN_BOOKS))
def test_a_book_of_another_market_is_refused_for_this_market(name):
    book = FOREIGN_BOOKS[name]
    assert not me.walk_asks(book, quote_amount=D('10'), latency_bps=D('0')).exhausted   # a valid book in itself
    assert_refused(book)
    assert size(book_=deep_book()).eligible                                              # the same levels, this market


def assert_refused(book):
    result = size(book_=book)
    assert not result.eligible and result.code == me.PRICE_UNKNOWN
    assert result.gross_quote is None and result.max_debit is None and result.minimum_feasible_budget is None
    estimate = exit_estimate(D('0.25'), D('25'), book)
    assert estimate.depth_model == 'unavailable' and estimate.gross_proceeds is None
    assert estimate.net_proceeds is None and estimate.est_net_pnl is None


# ------------------------------------------------- budget-revision regressions
def test_the_cap_is_not_a_venue_floor_and_a_smaller_budget_stays_bounded():
    pair = rules(min_cost_market=D('1'))
    ten = size(rules_=pair, budget='10')
    assert ten.eligible and ten.max_debit <= D('10') and ten.target_budget == D('10')
    two = size(rules_=pair, budget='2', cash='1000', allocation='1000')
    assert two.eligible and two.max_debit <= D('2') and two.budget_cap == D('2')


def test_metadata_can_show_a_minimum_below_or_above_the_cap():
    low = size(rules_=rules(min_cost_market=D('1')))
    high = size(rules_=rules(min_cost_market=D('30')))
    assert low.eligible and D('1') < low.minimum_feasible_budget < D('2')
    assert high.code == me.BUDGET_BELOW_VENUE_MINIMUM and high.minimum_feasible_budget > me.MICRO_MAX_ALL_IN_USDT


def test_proportional_break_even_is_budget_independent_and_absolute_losses_scale():
    rows = me.sensitivity([D('10'), D('25')], [D('0.001')], [D('0.002')], [D('20')])
    ten, twenty_five = rows
    assert ten['break_even_ratio'] == twenty_five['break_even_ratio']
    assert ten['target_ratio'] == twenty_five['target_ratio']
    assert D(ten['flat_net']) < 0 and D(twenty_five['flat_net']) == money.mul(D('2.5'), D(ten['flat_net']))

    # The same holds for the book-based sizing when nothing budget-dependent is in play.
    fine = rules(cost_step=D('0.00000001'), amount_step=D('0.00000001'), price_tick=D('0.00000001'))
    ratios = [money.div(size(rules_=fine, budget=b).break_even_bid, D('100.01')) for b in ('10', '25')]
    assert abs(money.sub(*ratios)) < D('0.0000001')


def test_minimum_fee_rounding_and_depth_impact_make_small_and_large_budgets_differ():
    proportional = me.sensitivity([D('10'), D('25')], [D('0.001')], [D('0.002')], D('20'))
    minimum_fee = me.sensitivity([D('10'), D('25')], [D('0.001')], [D('0.002')], D('20'), min_fee_quote=D('0.02'))
    assert proportional[0]['break_even_ratio'] == proportional[1]['break_even_ratio']
    assert [row['cost_kind'] for row in minimum_fee] == ['minimum_fee', 'minimum_fee']
    # the minimum binds only on the small budget's legs; at 25 the proportional fee is already above it
    assert D(minimum_fee[0]['break_even_ratio']) > D(minimum_fee[1]['break_even_ratio'])
    assert minimum_fee[1]['break_even_ratio'] == proportional[1]['break_even_ratio']
    assert D(minimum_fee[0]['flat_net']) < money.mul(D('0.4'), D(minimum_fee[1]['flat_net']))


@pytest.mark.parametrize('budget,min_fee', [
    ('10', '0.02'), ('1', '0.1'), ('2', '0.5'), ('25', '0.02'), ('25', None), ('3.33', None), ('0.5', '0.3'),
])
def test_flat_net_matches_an_independent_round_trip_at_an_unchanged_price(budget, min_fee):
    B, f, s = D(budget), D('0.001'), D('0.002')
    m = None if min_fee is None else D(min_fee)
    row = me.sensitivity([B], [f], [s], D('20'), min_fee_quote=m)[0]
    with localcontext() as context:
        context.prec = 60
        floor = D(0) if m is None else m
        bought = B / (1 + f)                         # buy leg: gross + max(gross * f, m) = B
        if bought * f < floor:
            bought = B - floor
        value = bought / (1 + s)                     # quantity times the unchanged price
        proceeds = value * (1 - s)                   # sell leg at that price
        expected = proceeds - max(proceeds * f, floor) - B
    assert row['feasible'] and abs(D(row['flat_net']) - expected) < D('1E-25')

    # Rounding: a coarse quantity step leaves a residual whose basis the sellable part must carry.
    coarse = rules(amount_step=D('0.01'), cost_step=D('0.00000001'), price_tick=D('0.00000001'))
    small, large = size(rules_=coarse, budget='10'), size(rules_=coarse, budget='25')
    assert small.predicted_residual_base > 0 and large.predicted_residual_base > 0
    assert money.div(small.break_even_bid, D('100.01')) > money.add(money.div(large.break_even_bid, D('100.01')), D('0.01'))

    # Depth impact: the larger budget walks further up a thin book.
    thin = Book(bids=((D('99.99'), D('1000')),),
                asks=((D('100.01'), D('0.1')), (D('100.10'), D('0.1')), (D('100.50'), D('1000'))))
    fine = rules(cost_step=D('0.00000001'), amount_step=D('0.00000001'), price_tick=D('0.00000001'))
    near, far = size(rules_=fine, book_=thin, budget='10'), size(rules_=fine, book_=thin, budget='25')
    assert near.entry_vwap < far.entry_vwap
    assert money.div(near.break_even_bid, D('100.01')) < money.div(far.break_even_bid, D('100.01'))


def test_no_research_comparison_can_raise_the_cap(monkeypatch):
    assert size(budget='50').code == me.BUDGET_INVALID
    assert size(budget='25.01').code == me.BUDGET_INVALID
    assert size(budget='0').code == me.BUDGET_INVALID
    rows = me.sensitivity([D('10'), D('25'), D('100')], [D('0.001')], [D('0.002')], D('20'))
    assert [row['within_micro_cap'] for row in rows] == [True, True, False]
    assert me.MICRO_MAX_ALL_IN_USDT == D('25')
    monkeypatch.setattr(me, 'MICRO_MAX_ALL_IN_USDT', D('100'))
    assert size(budget='50').code == me.BUDGET_INVALID         # the public name cannot lift the cap
    with pytest.raises(money.MoneyError):
        me.account_entry(filled_base=D('1'), gross_quote=D('40'), quote_fee=D('0'), base_fee=None, other_fees={},
                         other_fee_value=None, fee_status='simulated', reserved=D('40'))


def test_no_compounding_and_no_upsizing_from_cash_or_allocation():
    assert size(cash='1000000').target_budget == D('25')
    assert size(cash='7.5').target_budget == D('7.5') and size(cash='7.5').max_debit <= D('7.5')
    assert size(allocation='4').target_budget == D('4') and check(size(allocation='4'), 'target_budget')['limited_by'] == 'allocation'
    assert size(cash='0').code == me.CASH_INSUFFICIENT
    assert size(cash='-3').code == me.CASH_INSUFFICIENT
    assert size(allocation='0').code == me.ALLOCATION_EXHAUSTED
    unknown = me.size_entry(rules=RULES, book=deep_book(), cost=cost(), budget_cap=D('25'), cash_available=None,
                            allocation_remaining=D('25'), target_net=D('0.05'), exit_stress_bps=D('10'),
                            now_wall=2000.0)
    assert unknown.code == me.CASH_INSUFFICIENT and 'unknown' in unknown.detail
    for cash in ('0.5', '1.5', '3', '24.99', '25', '26', '500'):
        result = size(cash=cash, budget='20')
        assert result.target_budget == min(D(cash), D('20'))
        assert result.max_debit is None or result.max_debit <= result.target_budget

    # Cash below a minimum the cap would meet: the reason is the cash, and the order is not resized.
    short = size(rules_=rules(min_cost_market=D('5')), cash='3')
    assert short.code == me.CASH_INSUFFICIENT and short.gross_quote is None and short.target_budget == D('3')
    assert '3 USDT' in short.detail and not check(short, 'venue_minimum')['passed']
    capped = size(rules_=rules(min_cost_market=D('30')), cash='3')
    assert capped.code == me.BUDGET_BELOW_VENUE_MINIMUM     # the cap could not meet this minimum either
    assert size(rules_=rules(min_cost_market=D('5')), allocation='3').code == me.ALLOCATION_EXHAUSTED


@pytest.mark.parametrize('book_,own_code', [
    (MALFORMED_BOOKS['crossed'], me.PRICE_UNKNOWN),
    (Book(bids=((D('99.99'), None),), asks=((D('100.01'), None),), depth_known=False), me.DEPTH_UNKNOWN),
    (Book(bids=((D('99.99'), D('1000')),), asks=((D('100.01'), D('0.1')), (D('100.02'), None))), me.DEPTH_UNKNOWN),
])
@pytest.mark.parametrize('shortfall', [dict(cash='3'), dict(allocation='3')])
def test_a_shortfall_is_blamed_only_when_the_cap_sized_order_would_be_eligible(book_, own_code, shortfall):
    pair = rules(min_cost_market=D('5'))
    assert size(rules_=pair, book_=book_).code == own_code       # more cash can never cure this book
    result = size(rules_=pair, book_=book_, **shortfall)
    assert result.code == me.BUDGET_BELOW_VENUE_MINIMUM and result.gross_quote is None
    assert 'would meet it' not in result.detail
    assert not any(item['check'] == 'binding_limit' for item in result.checks)


# --------------------------------------------------------------- depth handling
def test_a_larger_amount_crossing_more_levels_gets_a_worse_vwap():
    book = Book(bids=((D('99.99'), D('0.05')), (D('99.90'), D('0.05')), (D('99.50'), D('10'))),
                asks=((D('100.01'), D('0.05')), (D('100.05'), D('0.05')), (D('100.10'), D('0.1')), (D('100.20'), D('10'))))
    walks = [me.walk_asks(book, quote_amount=D(amount), latency_bps=D('0')) for amount in ('4', '10', '25')]
    assert walks[0].vwap == D('100.01') and walks[0].levels_used == 1
    assert walks[0].vwap < walks[1].vwap < walks[2].vwap
    assert walks[0].levels_used < walks[1].levels_used < walks[2].levels_used
    sells = [me.walk_bids(book, base_amount=D(amount), latency_bps=D('0')) for amount in ('0.04', '0.08', '0.2')]
    assert sells[0].vwap > sells[1].vwap > sells[2].vwap


def test_exhausted_depth_has_its_own_codes():
    shallow_asks = Book(bids=((D('99.99'), D('1000')),), asks=((D('100.01'), D('0.1')),))
    walk = me.walk_asks(shallow_asks, quote_amount=D('25'), latency_bps=D('0'))
    assert walk.exhausted and walk.depth_known and walk.quote == money.mul(D('100.01'), D('0.1'))
    assert size(book_=shallow_asks).code == me.INSUFFICIENT_ASK_DEPTH
    shallow_bids = Book(bids=((D('99.99'), D('0.1')),), asks=((D('100.01'), D('1000')),))
    assert size(book_=shallow_bids).code == me.INSUFFICIENT_BID_DEPTH
    estimate = exit_estimate(D('0.25'), D('25'), shallow_bids)
    visible = money.mul(D('99.99'), D('0.1'), D('0.9997'))
    assert D(estimate.labels['visible_gross_proceeds']) == visible
    # the 0.15 the bids cannot absorb is priced like unknown depth, from the worst visible bid
    assert estimate.depth_exhausted and estimate.depth_model == 'flat_fallback'
    assert estimate.gross_proceeds == money.add(visible, money.mul(D('0.15'), D('99.99'), D('0.9977')))
    assert estimate.est_net_pnl == money.sub(estimate.net_proceeds, D('25'))


def test_exhausted_bid_depth_in_a_crash_still_gives_safety_exits_a_conservative_loss():
    crash = Book(bids=((D('60'), D('0.1')), (D('59'), D('0.1'))), asks=((D('61'), D('1')),))
    estimate = exit_estimate(D('0.25'), D('25'), crash)
    assert estimate.depth_exhausted and estimate.est_net_pnl is not None
    # no profit exit can rest on it: profit exits need depth_model 'book'
    assert estimate.depth_model == 'flat_fallback' and estimate.depth_model != 'book'
    visible = money.mul(money.add(money.mul(D('60'), D('0.1')), money.mul(D('59'), D('0.1'))), D('0.9997'))
    remainder = money.mul(D('0.05'), D('59'), D('0.9977'))           # worst visible bid, 20 bps fallback + 3 latency
    gross = money.add(visible, remainder)
    assert estimate.gross_proceeds == gross and estimate.sell_fee == money.mul(gross, D('0.001'))
    assert estimate.est_net_pnl == money.sub(money.sub(gross, estimate.sell_fee), D('25'))
    assert estimate.est_net_pnl < D('-10')
    # never better than if the unseen remainder sold at the worst visible level with latency only
    optimistic = money.sub(money.mul(money.add(visible, money.mul(D('0.05'), D('59'), D('0.9997'))), D('0.999')), D('25'))
    assert estimate.est_net_pnl < optimistic
    # and no better than the same crash seen as a top-of-book ticker, which carries less information
    ticker = Book(bids=((D('60'), None),), asks=((D('61'), None),), depth_known=False)
    assert estimate.est_net_pnl <= exit_estimate(D('0.25'), D('25'), ticker).est_net_pnl
    # the floor (nothing beyond the visible bids sells) is reported too
    floor = D(estimate.labels['floor_net_pnl'])
    assert floor == money.sub(money.mul(visible, D('0.999')), D('25')) and floor < estimate.est_net_pnl
    assert 'flat fallback' in estimate.labels['slippage'] and 'estimated' in estimate.labels['proceeds']


def test_unknown_depth_blocks_entries_but_exits_use_the_flat_fallback():
    ticker = Book(bids=((D('99.99'), None),), asks=((D('100.01'), None),), depth_known=False)
    walk = me.walk_asks(ticker, quote_amount=D('10'), latency_bps=D('0'))
    assert walk.exhausted and not walk.depth_known and walk.base == 0 and walk.vwap is None
    assert size(book_=ticker).code == me.DEPTH_UNKNOWN
    sized_ticker = Book(bids=((D('99.99'), D('5')),), asks=((D('100.01'), D('5')),), depth_known=False)
    assert size(book_=sized_ticker).code == me.DEPTH_UNKNOWN
    missing_level = Book(bids=((D('99.99'), D('1000')),), asks=((D('100.01'), D('0.1')), (D('100.02'), None)))
    assert size(book_=missing_level).code == me.DEPTH_UNKNOWN

    estimate = exit_estimate(D('0.25'), D('25'), ticker)
    assert estimate.depth_model == 'flat_fallback' and not estimate.depth_exhausted
    assert estimate.gross_proceeds == money.mul(D('0.25'), D('99.99'), D('0.9977'))   # 20 bps fallback + 3 latency
    assert 'flat fallback' in estimate.labels['slippage']


# ----------------------------------------------------- cost model and records
def test_cost_model_takes_the_larger_fee_and_converts_units():
    model = me.cost_model(configured_fee_pct=0.1, configured_slippage_pct=0.2, latency_bps=3, rules=RULES)
    assert model.taker_fee == D('0.001') and model.fallback_slippage_bps == D('20')
    assert model.latency_bps == D('3') and 'max(configured 0.1%, public taker 0.05%)' in model.fee_source
    higher = me.cost_model(configured_fee_pct=0.02, configured_slippage_pct=0, latency_bps=0,
                           rules=rules(taker_fee=D('0.002')))
    assert higher.taker_fee == D('0.002')
    unknown = me.cost_model(configured_fee_pct=0.1, configured_slippage_pct=0.2, latency_bps=3,
                            rules=rules(taker_fee=None, buy_fee_currency='unknown'))
    assert unknown.taker_fee == D('0.001') and 'public taker fee unknown' in unknown.fee_source
    assert unknown.buy_fee_currency == 'unknown'
    for bad in (dict(fee='1'), dict(latency='10000'), dict(fallback='-1'), dict(buy='QUOTE')):
        with pytest.raises(money.MoneyError):
            cost(**bad)
    with pytest.raises(money.MoneyError):
        cost(latency='5000', fallback='5000')


def test_an_eligible_result_records_all_twelve_steps_and_serializes():
    result = size()
    assert result.eligible and result.code is None
    assert {item['step'] for item in result.checks} == set(range(1, 13))
    assert all(item['passed'] and item['code'] is None for item in result.checks)
    record = result.as_dict()
    json.dumps(record)
    assert record['max_debit'] == money.text(result.max_debit) and record['cost']['taker_fee'] == '0.001'
    assert isinstance(record['checks'], list) and record['checks'][0]['check'] == 'budget_cap'
    assert size().as_dict() == record        # pure: the same inputs give the same record
    assert result.required_move_bps == money.mul(money.sub(money.div(result.target_bid, D('100.01')), D('1')),
                                                 D('10000'))


def test_entry_accounting_refuses_a_debit_above_the_reservation():
    with pytest.raises(money.MoneyError):
        me.account_entry(filled_base=D('0.25'), gross_quote=D('24.97'), quote_fee=D('0.04'), base_fee=None,
                         other_fees={}, other_fee_value=None, fee_status='simulated', reserved=D('25.00'))
    with pytest.raises(money.MoneyError):
        me.account_entry(filled_base=D('0.25'), gross_quote=D('24.97'), quote_fee=D('0'), base_fee=D('0.3'),
                         other_fees={}, other_fee_value=None, fee_status='simulated', reserved=D('25.00'))
    entry = me.account_entry(filled_base=D('0.25'), gross_quote=D('24.97'), quote_fee=D('0.03'), base_fee=None,
                             other_fees={}, other_fee_value=None, fee_status='observed', reserved=D('25.00'))
    assert entry.debit == D('25.00') and entry.basis == D('25.00') and entry.owned_base == D('0.25')
    json.dumps(entry.as_dict())
