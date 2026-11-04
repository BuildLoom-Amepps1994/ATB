"""Known-outcome tests for research.metrics and the backtest fields it reads.

Every expected number below is worked by hand from the definition in the
function's docstring, so a change of definition fails here before it changes
a research report.
"""
import json
import math
import statistics

import pytest

import strategy
from research import metrics
from tests.test_strategy_integrity import CONFIG, controlled_signals, history


# ----------------------------------------------------------------- trades
def test_trade_statistics_by_hand():
    out = metrics.trade_statistics([10, -5, 10, -5, 0])
    assert (out['trades'], out['wins'], out['losses'], out['flat']) == (5, 2, 2, 1)
    assert out['win_rate'] == pytest.approx(0.4)
    assert out['net'] == pytest.approx(10)
    assert (out['gross_profit'], out['gross_loss']) == (20, 10)
    assert out['profit_factor'] == pytest.approx(2.0)
    assert out['expectancy'] == pytest.approx(2.0)
    assert (out['average_win'], out['average_loss'], out['payoff_ratio']) == (10, -5, 2.0)
    assert (out['largest_win'], out['largest_loss']) == (10, -5)
    assert out['expected_shortfall'] == -5          # worst ceil(5 % of 5) = 1 trade
    assert (out['max_consecutive_wins'], out['max_consecutive_losses']) == (1, 1)
    assert out['t_stat'] == pytest.approx(2.0 / (statistics.stdev([10, -5, 10, -5, 0]) / math.sqrt(5)), rel=1e-3)


def test_trade_statistics_edge_cases():
    assert metrics.trade_statistics([])['trades'] == 0
    no_losses = metrics.trade_statistics([1, 2, 3])
    assert no_losses['profit_factor'] is None and no_losses['average_loss'] is None
    only_losses = metrics.trade_statistics([-1, -2, -3])
    assert only_losses['profit_factor'] == 0.0 and only_losses['max_consecutive_losses'] == 3
    assert metrics.trade_statistics([float('nan'), 1.0, float('inf')])['trades'] == 1
    assert metrics.trade_statistics([-1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -20])['expected_shortfall'] == -20


def test_bootstrap_interval_is_deterministic_and_contains_the_mean():
    constant = metrics.bootstrap_mean_ci([1.0] * 20)
    assert (constant['low'], constant['high']) == (1.0, 1.0)
    values = list(range(10))
    first, second = metrics.bootstrap_mean_ci(values, seed=3), metrics.bootstrap_mean_ci(values, seed=3)
    assert first == second
    assert first['low'] < 4.5 < first['high']
    assert metrics.bootstrap_mean_ci([1.0])['low'] is None


# ----------------------------------------------------------------- equity
def test_drawdowns_by_hand():
    recovered = metrics.drawdowns([100, 90, 95, 100, 100.5])
    assert recovered['max_drawdown'] == pytest.approx(0.1)
    assert recovered['max_duration'] == 4 and recovered['recovered'] is True
    open_at_end = metrics.drawdowns([100, 110, 99, 120, 108])
    assert open_at_end['max_drawdown'] == pytest.approx(0.1)
    assert open_at_end['max_duration'] == 2 and open_at_end['recovered'] is False
    assert metrics.drawdowns([])['max_drawdown'] is None
    assert metrics.drawdowns([1, 2, 3])['max_drawdown'] == 0.0


def test_equity_statistics_by_hand():
    out = metrics.equity_statistics([100, 120, 90, 120], 4)
    returns = [0.2, -0.25, 1 / 3]
    mean, sd = statistics.fmean(returns), statistics.stdev(returns)
    assert out['net_return'] == pytest.approx(0.2)
    assert out['cagr'] == pytest.approx(1.2 ** (4 / 3) - 1, rel=1e-5)
    assert out['sharpe'] == pytest.approx(mean / sd * 2, rel=1e-5)
    assert out['sharpe_per_period'] == pytest.approx(mean / sd, rel=1e-5)
    assert out['sortino'] == pytest.approx(mean / math.sqrt(0.0625 / 3) * 2, rel=1e-5)
    assert out['max_drawdown'] == pytest.approx(0.25)
    assert out['calmar'] == pytest.approx(out['cagr'] / 0.25, rel=1e-5)
    assert out['worst_period'] == pytest.approx(-0.25) and out['best_period'] == pytest.approx(1 / 3)
    assert out['expected_shortfall'] == pytest.approx(-0.25)
    assert out['volatility'] == pytest.approx(sd * 2, rel=1e-5)


def test_equity_statistics_without_variance_or_downside_returns_none():
    out = metrics.equity_statistics([100, 110, 121], 12, in_market=[True, False, False])
    assert out['sharpe'] is None and out['sortino'] is None and out['volatility'] == 0.0
    assert out['max_drawdown'] == 0.0 and out['calmar'] is None
    assert out['exposure'] == pytest.approx(1 / 3)
    assert metrics.equity_statistics([100], 12)['net_return'] is None
    assert metrics.equity_statistics([0, 5], 12)['net_return'] is None


def test_calendar_returns_do_not_overlap():
    def ms(text):
        from datetime import datetime, timezone
        return int(datetime.fromisoformat(text).replace(tzinfo=timezone.utc).timestamp() * 1000)
    curve = [{'time': ms('2024-06-01'), 'equity': 100}, {'time': ms('2024-12-31'), 'equity': 110},
             {'time': ms('2025-06-01'), 'equity': 121}]
    assert metrics.calendar_returns(curve) == {'2024': pytest.approx(0.1), '2025': pytest.approx(0.1)}
    assert metrics.calendar_returns(curve, start=50)['2024'] == pytest.approx(1.2)
    quarters = metrics.calendar_returns(curve, period='quarter')
    assert list(quarters) == ['2024Q2', '2024Q4', '2025Q2']
    with pytest.raises(ValueError):
        metrics.calendar_returns(curve, period='month')


def test_resample_keeps_the_last_point_of_each_bucket():
    day = 86_400_000
    curve = [{'time': 0, 'equity': 1}, {'time': 1000, 'equity': 2}, {'time': day + 5, 'equity': 3}]
    assert metrics.resample_curve(curve, day) == [{'time': 1000, 'equity': 2}, {'time': day + 5, 'equity': 3}]


# ---------------------------------------------------- selection adjustments
def test_probabilistic_sharpe_known_values():
    assert metrics.probabilistic_sharpe(0.0, 100) == pytest.approx(0.5)
    normal = metrics.probabilistic_sharpe(0.1, 101)
    assert normal == pytest.approx(statistics.NormalDist().cdf(1 / math.sqrt(1.005)), abs=1e-6)
    assert metrics.probabilistic_sharpe(0.1, 101, skew=-1.0) < normal      # fat left tail lowers it
    assert metrics.probabilistic_sharpe(0.1, 101, kurtosis=9.0) < normal
    assert metrics.probabilistic_sharpe(0.1, 1) is None


def test_expected_max_sharpe_grows_with_trials():
    assert metrics.expected_max_sharpe(1, 1.0) == 0.0
    assert metrics.expected_max_sharpe(50, 0.0) == 0.0
    ten, hundred = metrics.expected_max_sharpe(10, 1.0), metrics.expected_max_sharpe(100, 1.0)
    assert 0 < ten < hundred
    assert 2.4 < hundred < 2.7                       # (1 - g) z(0.99) + g z(1 - 1/(100 e)) with g = Euler's gamma
    assert metrics.expected_max_sharpe(0, 1.0) is None


def test_deflated_sharpe_equals_psr_for_one_trial_and_falls_with_trials():
    one = metrics.deflated_sharpe(0.1, 101, trials=1, sharpe_variance=0.01)
    assert one['deflated_sharpe'] == metrics.probabilistic_sharpe(0.1, 101)
    many = metrics.deflated_sharpe(0.1, 101, trials=50, sharpe_variance=0.01)
    assert many['expected_max_sharpe'] > 0
    assert many['deflated_sharpe'] < one['deflated_sharpe']


# ------------------------------------------------------------- monte carlo
def test_monte_carlo_ruin_extremes_and_determinism():
    losing = metrics.monte_carlo([-0.5] * 12, capital_fraction=1.0, horizon=5, runs=50)
    assert losing['ruin_probability'] == 1.0 and losing['terminal_p95'] < 0.2
    winning = metrics.monte_carlo([0.1] * 12, capital_fraction=0.5, horizon=5, runs=50)
    assert winning['ruin_probability'] == 0.0 and winning['max_drawdown_p95'] == 0.0
    assert winning['terminal_median'] == pytest.approx(1.05 ** 5)
    sample = [0.1, -0.05, 0.2, -0.1, 0.05, -0.02, 0.07, -0.08, 0.03, 0.01, -0.04, 0.06]
    assert metrics.monte_carlo(sample, capital_fraction=0.1, horizon=20, seed=1) == \
        metrics.monte_carlo(sample, capital_fraction=0.1, horizon=20, seed=1)
    assert metrics.monte_carlo(sample[:5], capital_fraction=0.1, horizon=20)['ruin_probability'] is None
    assert 'independent' in metrics.monte_carlo(sample, capital_fraction=0.1, horizon=20)['assumptions']


def test_turnover():
    assert metrics.turnover(2000, 1000, 2) == 1.0
    assert metrics.turnover(2000, 0, 2) is None


# ------------------------------------------------------------ backtest tie-in
def test_backtest_keeps_the_full_curve_only_on_request(monkeypatch):
    controlled_signals(monkeypatch)
    rows = history()
    rows[116][1:5] = [100, 112, 94, 100]
    plain = strategy.backtest(rows, CONFIG)
    assert 'curve_full' not in plain and 'in_market' not in plain
    full = strategy.backtest(rows, CONFIG, keep_curve=True)
    assert len(full['curve_full']) == full['coverage']['evaluation_candles'] == len(full['in_market'])
    assert full['curve_full'][-1] == full['equity_curve'][-1]
    sell = full['executions'][1]
    assert sell['quantity'] == pytest.approx(full['executions'][0]['quantity'])
    assert sell['proceeds'] == pytest.approx(sell['quantity'] * sell['price'])     # zero costs in CONFIG
    json.dumps(full)


def test_summarize_backtest_agrees_with_the_backtest_itself(monkeypatch):
    controlled_signals(monkeypatch, always=True, atr=5)
    rows = history(count=400)
    for i in range(116, 400, 40):
        rows[i][1:5] = [100, 112, 94, 100]           # a stop every forty bars
    result = strategy.backtest(rows, {**CONFIG, 'fee_pct': 0.1, 'slippage_pct': 0.2}, keep_curve=True)
    bundle = metrics.summarize_backtest(result, period_ms=3_600_000)
    assert bundle['period_ms'] == 86_400_000           # hourly bars are judged on a daily resample
    assert bundle['trades']['trades'] == result['closed_trades']
    assert bundle['trades']['net'] == pytest.approx(sum(ex['pnl'] for ex in result['executions'] if ex['type'] == 'sell'))
    assert bundle['equity']['net_return'] == pytest.approx(result['return_pct'] / 100, abs=1e-4)
    assert bundle['equity']['max_drawdown'] <= result['max_drawdown_pct'] / 100 + 5e-5   # daily points see no deeper dip than the bars
    assert 0 < bundle['equity']['exposure'] <= 1
    assert bundle['turnover_per_year'] > 0
    assert 'probabilistic_sharpe' in bundle or bundle['equity']['sharpe'] is None
    json.dumps(bundle)
