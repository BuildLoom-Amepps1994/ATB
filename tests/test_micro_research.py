"""The research harness: frozen protocol, honest accounting, baselines and the verdict rule."""
import copy
import json
import sys
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from research import micro_fixtures as fx      # noqa: E402
from research import micro_report             # noqa: E402
from research import micro_research as r      # noqa: E402

MINUTES = 600


def prepared(series):
    rows_1m = r.bars_1m(series)
    rows_5m = r.bars_5m(rows_1m)
    return rows_1m, r.feature_cache(series, rows_1m, rows_5m)


@pytest.fixture(scope='module')
def mechanics():
    series = r.fixture_suite(MINUTES)[0][0]
    rows_1m, cache = prepared(series)
    return series, rows_1m, cache


def test_the_protocol_is_frozen_and_hashed():
    assert r.PROTOCOL['frozen_before_results'] is True
    frozen = r.protocol_hash()
    assert frozen == r.protocol_hash() and len(frozen) == 64
    changed = copy.deepcopy(r.PROTOCOL)
    changed['settings']['micro_target_net_bps'] = 21           # any edit to the protocol changes its hash
    original = r.PROTOCOL
    try:
        r.PROTOCOL = changed
        assert r.protocol_hash() != frozen
    finally:
        r.PROTOCOL = original
    assert r.protocol_hash() == frozen
    assert r.PROTOCOL['budgets_sized'] == [10, 15, 25] and r.PROTOCOL['budgets_algebra_only'] == [50]


def test_cash_is_conserved_and_an_unsellable_remainder_is_carried_not_dropped(mechanics):
    series, rows_1m, cache = mechanics
    result = r.run(series, cache, rows_1m, start=120, end=len(rows_1m), scenario=r.SCENARIOS['baseline'],
                   budget=Decimal('25'))
    summary = r.summarize(result, Decimal('25'), len(rows_1m) - 120)
    assert summary['closed_cycles'] > 0
    assert any(c.carried_base > 0 for c in result['cycles'][1:])          # a remainder entered the next cycle
    residual = summary['residual_at_end']['basis'] if summary['residual_at_end'] else 0.0
    if result['open'] is None:
        assert summary['cash_change'] == pytest.approx(summary['net'] - residual, abs=1e-6)
    assert summary['gross_pnl_before_fees'] == pytest.approx(summary['net'] + summary['fees'], abs=1e-6)
    assert all(Decimal(str(c.entry_debit)) <= Decimal('25') for c in result['cycles'])


def test_costs_alone_make_churning_lose_and_the_strategy_abstains_on_a_flat_market():
    series = fx.SeriesMarket('XRP/USDT', 29_000_000, fx.flat(MINUTES, base=0.6),
                             market=fx.mexc_market('XRP/USDT', amount_step=0.000001, price_tick=0.0001), tick=0.0001)
    rows_1m, cache = prepared(series)
    common = dict(start=120, end=len(rows_1m), scenario=r.SCENARIOS['baseline'], budget=Decimal('25'))
    strategy = r.summarize(r.run(series, cache, rows_1m, **common), Decimal('25'), len(rows_1m) - 120)
    always = r.summarize(r.run(series, cache, rows_1m, policy='always', **common), Decimal('25'), len(rows_1m) - 120)
    assert strategy['closed_cycles'] == 0 and strategy['net'] == 0
    assert always['closed_cycles'] > 0 and always['net'] < 0 and always['wins'] == 0


def test_harsher_costs_never_improve_the_result(mechanics):
    series, rows_1m, cache = mechanics
    nets = [r.summarize(r.run(series, cache, rows_1m, start=120, end=len(rows_1m), scenario=r.SCENARIOS[name],
                              budget=Decimal('25')), Decimal('25'), len(rows_1m) - 120)['net']
            for name in ('baseline', 'doubled', 'severe')]
    assert nets[0] >= nets[1] >= nets[2]


def test_a_budget_above_the_micro_cap_cannot_be_sized(mechanics):
    series, rows_1m, cache = mechanics
    result = r.run(series, cache, rows_1m, start=120, end=len(rows_1m), scenario=r.SCENARIOS['baseline'],
                   budget=Decimal('50'), policy='always')
    assert result['cycles'] == [] and result['open'] is None


def _holdout(net, doubled, mean, sd, cycles=10):
    return {'by_budget': {'25': {'holdout': {
        'strategy': {'baseline': {'net': net, 'closed_cycles': cycles}, 'doubled': {'net': doubled}},
        'baselines': {'random_entries': {'mean': mean, 'sd': sd}}}}}}


@pytest.mark.parametrize('report, expected', [
    (_holdout(1.0, 0.2, 0.0, 0.3), 'EDGE DEMONSTRATED ON THIS HOLDOUT (prospective paper evidence still required)'),
    (_holdout(1.0, -0.1, 0.0, 0.3), 'NO DEMONSTRATED EDGE'),      # gone under doubled costs
    (_holdout(0.5, 0.2, 0.0, 0.3), 'NO DEMONSTRATED EDGE'),       # inside two random sd
    (_holdout(-0.1, -0.2, -1.0, 0.1), 'NO DEMONSTRATED EDGE'),    # a smaller loss than random is still a loss
    (_holdout(1.0, 0.2, 0.0, 0.3, cycles=0), 'NO DEMONSTRATED EDGE'),
    ({'by_budget': {}}, 'NO DEMONSTRATED EDGE (no holdout)'),
])
def test_the_verdict_rule_needs_every_condition_on_real_data(report, expected):
    assert r.verdict(report, synthetic=False) == expected


def test_synthetic_data_can_never_show_an_edge():
    assert r.verdict(_holdout(100.0, 100.0, -1.0, 0.01), synthetic=True).startswith('MECHANICS ONLY')


def test_the_report_renders_every_series_from_the_result_file(tmp_path):
    result = {'protocol': r.PROTOCOL, 'protocol_sha256': r.protocol_hash(), 'feature_version': 'f', 'decision_version': 'd',
              'runtime_seconds': 1.0, 'real_data': 'NOT RUN', 'cost_algebra': [], 'series': []}
    series = fx.SeriesMarket('XRP/USDT', 29_000_000, fx.flat(900, base=0.6),
                             market=fx.mexc_market('XRP/USDT', amount_step=0.000001, price_tick=0.0001), tick=0.0001)
    original = r.PROTOCOL['random_repetitions']
    try:
        r.PROTOCOL['random_repetitions'] = 2
        report = r.evaluate_series(series, synthetic=True, budgets=[Decimal('25')])
    finally:
        r.PROTOCOL['random_repetitions'] = original
    report['fixture'] = 'flat'
    result['series'].append(report)
    path = tmp_path / 'result.json'
    path.write_text(json.dumps(result, default=str))
    text = micro_report.render(json.loads(path.read_text()))
    assert '### XRP/USDT: flat' in text and 'MECHANICS ONLY' in text and 'Random entries mean' in text
