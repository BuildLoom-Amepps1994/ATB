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
import micro_economics as econ                 # noqa: E402
import money                                   # noqa: E402

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


def _holdout(net, doubled, mean, sd, cycles=10, open_mark=0.0):
    return {'by_budget': {'25': {'holdout': {
        'strategy': {'baseline': {'net': net, 'net_marked': net + open_mark, 'closed_cycles': cycles},
                     'doubled': {'net': doubled, 'net_marked': doubled + open_mark}},
        'baselines': {'random_entries': {'mean': mean, 'sd': sd}}}}}}


@pytest.mark.parametrize('report, expected', [
    (_holdout(1.0, 0.2, 0.0, 0.3), 'EDGE DEMONSTRATED ON THIS HOLDOUT (prospective paper evidence still required)'),
    (_holdout(1.0, -0.1, 0.0, 0.3), 'NO DEMONSTRATED EDGE'),      # gone under doubled costs
    (_holdout(0.5, 0.2, 0.0, 0.3), 'NO DEMONSTRATED EDGE'),       # inside two random sd
    (_holdout(-0.1, -0.2, -1.0, 0.1), 'NO DEMONSTRATED EDGE'),    # a smaller loss than random is still a loss
    (_holdout(1.0, 0.2, 0.0, 0.3, cycles=0), 'NO DEMONSTRATED EDGE'),
    (_holdout(1.0, 0.2, 0.0, 0.3, open_mark=-0.5), 'NO DEMONSTRATED EDGE'),   # a loss still open counts
    (_holdout(0.05, 0.02, -1.0, 0.01, open_mark=-0.07), 'NO DEMONSTRATED EDGE'),
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


def test_every_fixture_market_has_a_tick_and_step_too_fine_to_decide_its_result():
    # A 0.01 tick on a 0.50 price rounds every exit bid by 200 bps: the fixture would test rounding, not the strategy.
    for series, _ in r.fixture_suite(120):
        rules = r.rules_for(series, 1.0)
        low, high = Decimal(str(min(series.closes))), Decimal(str(max(series.closes)))
        assert rules.price_tick / low <= Decimal('0.0005'), (series.symbol, rules.price_tick)       # 5 bps or less
        assert rules.amount_step * high <= Decimal('0.125'), (series.symbol, rules.amount_step)     # 0.5 % of 25 USDT


def _spiked_walk(spike_bar=None, spike=0.02):
    closes = fx.random_walk(400, base=2500.0, vol_bps=3, seed=5)
    rows = []
    for i, close in enumerate(closes):
        opened = closes[i - 1] if i else close
        rows.append([60000 * (29_000_000 + i), opened, max(opened, close) * 1.0002, min(opened, close) * 0.9998, close, 1e3])
    if spike_bar is not None:
        rows[spike_bar][2] = rows[spike_bar][1] * (1 + spike)
    return fx.SeriesMarket.from_rows('ETH/USDT', rows, market=fx.mexc_market('ETH/USDT', amount_step=0.000001))


def test_a_profit_exit_fills_where_it_first_fires_not_at_the_bar_high():
    def first_cycle(series):
        rows_1m, cache = prepared(series)
        return r.run(series, cache, rows_1m, start=120, end=len(rows_1m), scenario=r.SCENARIOS['baseline'],
                     budget=Decimal('25'), policy='always')['cycles'][0]
    plain = first_cycle(_spiked_walk())
    spiked = first_cycle(_spiked_walk(spike_bar=plain.entry_bar))
    assert spiked.entry_bar == plain.entry_bar and spiked.exit_bar == plain.entry_bar
    assert spiked.trigger == 'take_profit'
    # A sale at the 2 % high would book about 0.43 USDT; the first price where the target clears gives about 0.06.
    assert spiked.target_net <= spiked.realized < Decimal('0.08'), spiked.realized


def test_a_protection_band_crossed_below_a_high_in_the_hold_gap_fills_at_the_band_not_the_close():
    # Protection fires for a net after uncertainty in (0, 0.25 target] once the peak reached 0.75 target, and the
    # target fires from 1 x target: between the two is a gap where the engine holds. A bar that rises through the
    # band into the gap crossed the band on the way up, so a ticking engine sold near its bottom.
    base = 2500.0

    def series_for(changes):
        rows = [[60000 * (29_000_000 + i), base, base * 1.00002, base * 0.99998, base, 1e3] for i in range(400)]
        for i, bar in changes.items():
            rows[i][1:5] = bar
        return fx.SeriesMarket.from_rows('ETH/USDT', rows, market=fx.mexc_market('ETH/USDT', amount_step=0.000001))

    def run(series):
        rows_1m, cache = prepared(series)
        out = r.run(series, cache, rows_1m, start=120, end=len(rows_1m), scenario=r.SCENARIOS['baseline'],
                    budget=Decimal('25'), policy='always')
        return out['cycles'][0]

    flat = series_for({})
    held = run(flat)
    rules = r.rules_for(flat, 0)
    cost = r.scenario_cost(rules, r.SCENARIOS['baseline'])
    uncertainty_bps = money.parse(max(r.params().uncertainty_floor_bps, r.SCENARIOS['baseline'].latency_bps))

    def estimate(price):
        snapshot = r.book(flat, price, 60000, r.SCENARIOS['baseline'], 1)
        return econ.estimate_exit(owned_base=held.owned, basis_quote=held.basis, rules=rules, book=snapshot,
                                  cost=cost, target_net=held.target_net, uncertainty_bps=uncertainty_bps)

    def price_for(net):
        low, high = base * 0.9, base * 1.1
        for _ in range(80):
            middle = (low + high) / 2
            low, high = (middle, high) if estimate(middle).est_net_pnl < net else (low, middle)
        return high
    target, uncertainty = held.target_net, estimate(base).uncertainty
    band_top = price_for(uncertainty + target / 4)
    gap_high = (band_top + price_for(uncertainty + target)) / 2
    peak_bar = (base, price_for(target * Decimal('0.9')), base * 0.99999, base)    # a hold that arms protection
    e = held.entry_bar
    for close in (band_top - 0.01, base):              # closes inside the band, or back where it started
        cycle = run(series_for({e: peak_bar, e + 1: (base, gap_high, base * 0.99999, close)}))
        assert (cycle.exit_bar, cycle.trigger) == (e + 1, 'profit_protection')
        assert uncertainty < cycle.realized < uncertainty + target / 16, cycle.realized    # the band's bottom


def test_first_profit_exit_finds_the_lowest_firing_price_inside_a_narrow_band():
    seen = []

    def fires(price):                       # a protection band of 3 bps far below a take-profit level
        seen.append(price)
        return ('band', price) if 100.40 <= price <= 100.43 or price >= 101.5 else None
    trigger, price = r.first_profit_exit(fires, 100.0, 102.0)
    assert trigger == 'band' and 100.40 <= price < 100.401
    assert r.first_profit_exit(lambda at: ('at low', at), 100.0, 102.0) == ('at low', 100.0)
    assert r.first_profit_exit(lambda at: None, 100.0, 102.0) is None


def test_stress_fees_multiply_the_fee_actually_modeled_in_decimals():
    series = fx.SeriesMarket('ETH/USDT', 29_000_000, fx.flat(10, base=2500.0),
                             market=fx.mexc_market('ETH/USDT', taker=0.0026))          # public rate above 0.1 %
    rules = r.rules_for(series, 1.0)
    fees = {name: r.scenario_cost(rules, scenario).taker_fee for name, scenario in r.SCENARIOS.items()}
    assert fees == {'baseline': Decimal('0.0026'), 'doubled': Decimal('0.0052'), 'severe': Decimal('0.0078')}
    cheap = r.rules_for(fx.SeriesMarket('ETH/USDT', 29_000_000, [2500.0], market=fx.mexc_market('ETH/USDT')), 1.0)
    assert r.scenario_cost(cheap, r.SCENARIOS['doubled']).taker_fee == Decimal('0.002')   # 2 x the configured 0.1 %


def _real_file(folder, symbol, closes, *, venue='mexc', market=None, embed=True):
    rows = [[60000 * (29_000_000 + i), c, c * 1.0003, c * 0.9997, c, 1e3] for i, c in enumerate(closes)]
    payload = {'provenance': {'venue': venue}, 'ohlcv': rows}
    stem = symbol.replace('/', '_')
    if market is not None and embed:
        payload['market'] = market
    elif market is not None:
        (folder / f'{stem}_market.json').write_text(json.dumps(market))
    (folder / f'{stem}_1m.json').write_text(json.dumps(payload))


def kraken_market(symbol):
    base, quote = symbol.split('/')
    return {'id': base + quote, 'symbol': symbol, 'base': base, 'quote': quote, 'type': 'spot', 'spot': True,
            'active': True, 'taker': 0.0026, 'maker': 0.0016, 'precision': {'amount': 1e-08, 'price': 0.01},
            'limits': {'amount': {'min': 0.002}, 'cost': {'min': 0.5}},
            'info': {'status': 'online', 'ordermin': '0.002', 'costmin': '0.5', 'pair_decimals': 2}}


def test_real_data_books_use_the_market_tick_and_rules_use_the_series_venue(tmp_path):
    _real_file(tmp_path, 'XRP/USDT', [0.6] * 200, market=fx.mexc_market('XRP/USDT', price_tick=0.0001))
    _real_file(tmp_path, 'PEPE/USDT', [0.00002] * 200, market=fx.mexc_market('PEPE/USDT', price_tick=1e-10), embed=False)
    _real_file(tmp_path, 'ETH/USDT', [2500.0] * 200, venue='kraken', market=kraken_market('ETH/USDT'))
    suite = {series.symbol: series for series, _label, _provenance in r.load_real(tmp_path)}
    assert suite['XRP/USDT'].tick == 0.0001 and suite['PEPE/USDT'].tick == 1e-10      # the sidecar market was read
    for symbol in ('XRP/USDT', 'PEPE/USDT'):
        snapshot = r.book(suite[symbol], suite[symbol].closes[-1], 60000, r.SCENARIOS['baseline'], 1)
        assert 0 < float(snapshot.spread_bps) < 20, (symbol, snapshot.spread_bps)
    kraken = suite['ETH/USDT']
    assert kraken.venue == 'kraken'
    assert r.rules_for(kraken, 1.0).tradable('BUY') == (True, None, '')


@pytest.mark.parametrize('payload, message', [
    ({'provenance': {'venue': 'mexc'}}, 'no market metadata'),
    ({'provenance': {}, 'market': fx.mexc_market('XRP/USDT')}, 'no supported venue'),
    ({'provenance': {'venue': 'mexc'}, 'market': {**fx.mexc_market('XRP/USDT'), 'precision': {}}}, 'price tick'),
])
def test_a_real_data_file_without_its_venue_rules_is_refused(tmp_path, payload, message):
    rows = [[60000 * (29_000_000 + i), 0.6, 0.6, 0.6, 0.6, 1e3] for i in range(10)]
    (tmp_path / 'XRP_USDT_1m.json').write_text(json.dumps({**payload, 'ohlcv': rows}))
    with pytest.raises(SystemExit, match=message):
        r.load_real(tmp_path)


def test_splits_too_short_for_a_cycle_are_reported_not_run_and_never_indexed():
    assert r.split_bounds(720)['holdout'] is None and r.split_bounds(720)['development'] == (120, 480)
    assert r.split_bounds(600)['validation'] is None
    assert all(r.split_bounds(2880).values())
    series = fx.SeriesMarket('XRP/USDT', 29_000_000, fx.flat(719, base=0.6),
                             market=fx.mexc_market('XRP/USDT', amount_step=0.000001, price_tick=0.0001), tick=0.0001)
    report = r.evaluate_series(series, synthetic=False, budgets=[Decimal('25')])
    assert report['splits']['holdout'].startswith('not run') and set(report['by_budget']['25']) == {'development'}
    assert report['verdict'] == 'NO DEMONSTRATED EDGE (no holdout)'
    assert report['book_model'] == {'spread_bps': 2.0, 'level_quote': 400.0, 'levels': 20, 'tick': 0.0001,
                                    'from': 'each bar (no recorded depth)'}


def test_the_protocol_hash_covers_the_decision_parameters_and_results_name_their_code(monkeypatch):
    import micro
    frozen = r.protocol_hash()
    monkeypatch.setattr(micro, 'EDGE_RATIO', micro.EDGE_RATIO / 2)
    assert r.protocol_hash() != frozen
    identity = r.code_identity()
    assert len(identity['files_sha256']) == 64 and 'micro_strategy.py' in identity['files']
    assert identity['commit'] is None or len(identity['commit']) == 40


def test_a_second_fixture_clock_replaces_the_first_everywhere(monkeypatch):
    import time
    import micro
    first, second = fx.FixtureClock(), fx.FixtureClock()
    fx.install_clock(first, monkeypatch.setattr)
    fx.install_clock(second, monkeypatch.setattr)
    second.advance(4 * 3600)
    assert abs(time.time() - micro.datetime.now().timestamp()) < 1.0
    assert second._real is fx._REAL_TIME and first._real is fx._REAL_TIME          # never chained
