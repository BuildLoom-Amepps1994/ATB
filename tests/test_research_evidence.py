"""The walk-forward harness with --metrics and the evidence reader, on synthetic candles.

No market data is needed: a deterministic daily series with a long rise and a
long fall is written in the harness's file format, the harness runs the
production backtest on it, and the statistics it attaches are checked for
shape and for agreement with the backtest's own figures.
"""
import json
import math
from datetime import datetime, timezone

import pytest

from research import classic_walkforward as wf
from research import evidence


DAY = 86_400_000


def _ms(text):
    return int(datetime.fromisoformat(text).replace(tzinfo=timezone.utc).timestamp() * 1000)


def synthetic_daily(start='2021-06-01', days=1600, seed=1, drift=0.0006):
    """A rise, a fall and a rise again, with a small deterministic wobble."""
    rows, price, t0 = [], 100.0, _ms(start)
    for i in range(days):
        phase = math.sin(i / 160) * 0.004 + math.sin(i * seed / 7) * 0.003
        price *= 1 + drift * (1 if (i // 400) % 2 == 0 else -2) + phase
        high, low = price * 1.015, price * 0.985
        rows.append([t0 + i * DAY, round(price * 0.999, 6), round(high, 6), round(low, 6), round(price, 6), 1000.0])
    return rows


@pytest.fixture
def data_dir(tmp_path):
    for base, seed in (('BTC', 1), ('ALT', 2)):
        rows = synthetic_daily(seed=seed)
        (tmp_path / f'{base}_USDT_1d_long.json').write_text(json.dumps({'provenance': {'synthetic': True}, 'ohlcv': rows}))
    return tmp_path


def test_harness_attaches_metrics_and_the_selection_adjustment(data_dir, tmp_path):
    out = tmp_path / 'wf.json'
    assert wf.main(['--data', str(data_dir), '--pairs', 'BTC,ALT', '--profiles', 'trend', '--costs', 'realistic',
                    '--splits', 'development,validation', '--metrics', '--trials', '12', '--out', str(out)]) == 0
    result = json.loads(out.read_text())
    assert result['trials'] == 12
    block = result['results']['trend/realistic']
    ran = [(base, split, entry) for base, pair in block['pairs'].items()
           for split, entry in pair['splits'].items() if 'return_pct' in entry]
    assert ran, 'the synthetic series must produce at least one evaluated split'
    for base, split, entry in ran:
        m = entry['metrics']
        assert m['period_ms'] == DAY
        assert m['equity']['net_return'] == pytest.approx(entry['return_pct'] / 100, abs=1e-4)
        assert m['trades']['trades'] == len(entry['trades'])
        assert set(m) >= {'equity', 'trades', 'trade_mean_ci95', 'calendar'}
        assert m['equity']['periods'] == entry['evaluation_bars'] + 1
        json.dumps(m)
    summary = block['summary']
    for split, s in summary.items():
        if s.get('pairs'):
            assert set(s['metrics']) >= {'sharpe_mean', 'profit_factor', 'expectancy_pct', 'trials',
                                         'probabilistic_sharpe_mean', 'max_drawdown_periods_worst'}
            assert s['metrics']['trials'] == 12
    if result['trial_sharpe_variance'] is not None:
        assert any('deflated_sharpe_of_mean' in s.get('metrics', {}) for s in summary.values())


def test_harness_without_metrics_is_unchanged(data_dir, tmp_path):
    out = tmp_path / 'plain.json'
    wf.main(['--data', str(data_dir), '--pairs', 'BTC', '--profiles', 'trend', '--costs', 'realistic',
             '--splits', 'development', '--out', str(out)])
    result = json.loads(out.read_text())
    assert 'trials' not in result
    entry = result['results']['trend/realistic']['pairs']['BTC']['splits']['development']
    assert 'metrics' not in entry and 'metrics' not in result['results']['trend/realistic']['summary']['development']


def test_evidence_reads_every_result_format(data_dir, tmp_path):
    results = tmp_path / 'results'
    results.mkdir()
    wf.main(['--data', str(data_dir), '--pairs', 'BTC,ALT', '--profiles', 'trend', '--costs', 'realistic',
             '--splits', 'development,validation', '--out', str(results / 'wf.json')])
    (results / 'trend.json').write_text(json.dumps({
        'protocol': {'version': 'trend-research-v1'},
        'results': {'T100/realistic/validation': {'summary': {}, 'per_pair': {'BTC': {'trades': [0.1, -0.05, 0.2]}}}}}))
    (results / 'variants.json').write_text(json.dumps({
        'protocol': {'version': 'trend-variants-v1'},
        'results': {'band5/any': {'validation': {'summary': {}, 'per_pair': {'ALT': {'trades': [-0.1, 0.3]}}}}}}))
    (results / 'broken.json').write_text('{not json')
    report = evidence.collect(results)
    candidates = {(r['file'], r['candidate'], r['split']) for r in report['rows']}
    assert ('trend.json', 'T100/realistic', 'validation') in candidates
    assert ('variants.json', 'band5/any', 'validation') in candidates
    assert any(f == 'wf.json' for f, _, _ in candidates)
    trend_row = next(r for r in report['rows'] if r['file'] == 'trend.json')
    assert trend_row['trades'] == 3 and trend_row['profit_factor'] == pytest.approx(6.0)
    assert trend_row['expectancy_pct'] == pytest.approx(100 * (0.1 - 0.05 + 0.2) / 3, rel=1e-3)
    assert report['trial_count'] >= 3
    text = evidence.markdown(report)
    assert 'Trials recorded' in text and 'trend.json' in text


def test_evidence_reads_portfolio_level_sharpes_against_the_number_of_variants(tmp_path):
    results = tmp_path / 'results'
    results.mkdir()
    variants = {f'v{i}': {'development': {'return_pct': 10.0, 'sharpe': 1.0, 'max_drawdown_pct': 30.0, 'trades': 50},
                          'validation': {'return_pct': i - 5.0, 'sharpe': (i - 5) / 10, 'max_drawdown_pct': 40.0, 'trades': 20}}
                for i in range(12)}
    (results / 'portfolio.json').write_text(json.dumps({
        'protocol': {'version': 'trend-portfolio-v1',
                     'splits': {'development': [None, '2024-01-01'], 'validation': ['2024-01-01', '2025-05-01']}},
        'results': variants}))
    report = evidence.collect(results)
    adjusted = report['selection_adjusted']
    assert adjusted['trials'] == 12 and adjusted['validation_days'] == 486
    assert report['trial_count'] == 12 and report['rows'] == []
    assert adjusted['rows'][0]['variant'] == 'v11' and adjusted['rows'][0]['validation_sharpe'] == 0.6
    assert 0 < adjusted['expected_max_sharpe_of_noise'] < 1
    assert all(0 < r['deflated_sharpe'] < 1 for r in adjusted['rows'])
    assert adjusted['rows'][0]['deflated_sharpe'] > adjusted['rows'][-1]['deflated_sharpe']
    assert 'Deflated Sharpe' in evidence.markdown(report)
    assert evidence.collect(tmp_path / 'empty')['selection_adjusted'] is None


def test_evidence_on_the_committed_results_runs():
    report = evidence.collect()
    assert report['rows'], 'research/results/classic must hold walk-forward results'
    assert all(r['trades'] >= 0 for r in report['rows'])
    assert report['selection_adjusted']['trials'] >= 26          # trend-portfolio-v1 and trend-rotation-v1
