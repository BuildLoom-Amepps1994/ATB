"""Performance statistics for backtests, research reports and paper runs.

Pure functions over two kinds of input, both produced by ``strategy.backtest``
and by the paper ledger:

* **closed-trade outcomes** (one number per round trip: net P/L in USDT, or the
  net return on the capital the trade used), and
* **an equity series** sampled at a regular period (one value per bar or day).

Nothing here fits, selects or optimises anything. Every statistic states its
assumptions in its docstring; a figure that the data cannot support is
returned as ``None`` rather than guessed (a profit factor with no losing
trade, a Sharpe ratio with zero variance, a Calmar ratio with no drawdown).

References for the selection-bias adjustments:

* Bailey & López de Prado (2012), "The Sharpe ratio efficient frontier":
  the probabilistic Sharpe ratio, :func:`probabilistic_sharpe`.
* Bailey & López de Prado (2014), "The deflated Sharpe ratio": the expected
  maximum Sharpe ratio of ``N`` unrelated trials, :func:`expected_max_sharpe`,
  and :func:`deflated_sharpe`.

The Monte Carlo helpers resample recorded trades independently and with
replacement; that assumption (no regime dependence, no autocorrelation,
fixed sizing) is stated on every result they return.
"""
from __future__ import annotations

import math
import random
import statistics
from collections.abc import Sequence
from datetime import datetime, timezone

EULER_GAMMA = 0.5772156649015329
_NORMAL = statistics.NormalDist()


def _finite(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _round(value, places: int = 6):
    return None if value is None else round(float(value), places)


# ----------------------------------------------------------------- trades
def trade_statistics(outcomes: Sequence[float], *, tail_fraction: float = 0.05) -> dict:
    """Describe closed trades from their net outcomes (P/L in USDT or net return per trade).

    ``profit_factor`` is gross profit over gross loss and is ``None`` when
    there is no losing trade (it would be infinite). ``expectancy`` is the
    arithmetic mean outcome. ``expected_shortfall`` is the mean of the worst
    ``tail_fraction`` of trades (at least one trade), a trade-level tail
    estimate, not a daily one. ``t_stat`` is the mean over its standard
    error and says nothing about independence of the trades.
    """
    values = [v for v in (_finite(x) for x in outcomes) if v is not None]
    n = len(values)
    out: dict = {'trades': n, 'wins': 0, 'losses': 0, 'flat': 0, 'win_rate': None, 'net': 0.0,
                 'gross_profit': 0.0, 'gross_loss': 0.0, 'profit_factor': None, 'expectancy': None,
                 'average_win': None, 'average_loss': None, 'payoff_ratio': None, 'largest_win': None,
                 'largest_loss': None, 'stdev': None, 't_stat': None, 'expected_shortfall': None,
                 'max_consecutive_wins': 0, 'max_consecutive_losses': 0}
    if n == 0:
        return out
    wins = [v for v in values if v > 0]
    losses = [v for v in values if v < 0]
    out.update(wins=len(wins), losses=len(losses), flat=n - len(wins) - len(losses),
               win_rate=_round(len(wins) / n), net=_round(sum(values)),
               gross_profit=_round(sum(wins)), gross_loss=_round(-sum(losses)),
               expectancy=_round(statistics.mean(values)),
               largest_win=_round(max(values)) if wins else None,
               largest_loss=_round(min(values)) if losses else None)
    if losses:
        out['profit_factor'] = _round(sum(wins) / -sum(losses)) if wins else 0.0
    if wins:
        out['average_win'] = _round(statistics.mean(wins))
    if losses:
        out['average_loss'] = _round(statistics.mean(losses))
    if wins and losses:
        out['payoff_ratio'] = _round(statistics.mean(wins) / -statistics.mean(losses))
    if n > 1:
        sd = statistics.stdev(values)
        out['stdev'] = _round(sd)
        out['t_stat'] = _round(statistics.mean(values) / (sd / math.sqrt(n)), 3) if sd > 0 else None
    k = max(1, math.ceil(tail_fraction * n))
    out['expected_shortfall'] = _round(statistics.mean(sorted(values)[:k]))
    streak_w = streak_l = 0
    for v in values:
        streak_w, streak_l = (streak_w + 1, 0) if v > 0 else (0, streak_l + 1) if v < 0 else (0, 0)
        out['max_consecutive_wins'] = max(out['max_consecutive_wins'], streak_w)
        out['max_consecutive_losses'] = max(out['max_consecutive_losses'], streak_l)
    return out


def bootstrap_mean_ci(outcomes: Sequence[float], *, runs: int = 2000, seed: int = 0,
                      level: float = 0.95) -> dict:
    """Percentile bootstrap interval for the mean outcome (resampling with replacement).

    Assumes the outcomes are exchangeable; overlapping or clustered trades make
    the interval too narrow. Deterministic for a given ``seed``.
    """
    values = [v for v in (_finite(x) for x in outcomes) if v is not None]
    if len(values) < 2:
        return {'low': None, 'high': None, 'runs': 0, 'level': level, 'samples': len(values)}
    rng = random.Random(seed)
    n = len(values)
    means = sorted(statistics.fmean(rng.choices(values, k=n)) for _ in range(runs))
    lo_index = int((1 - level) / 2 * runs)
    hi_index = min(runs - 1, int((1 + level) / 2 * runs))
    return {'low': _round(means[lo_index]), 'high': _round(means[hi_index]), 'runs': runs,
            'level': level, 'samples': n}


# ----------------------------------------------------------------- equity
def periodic_returns(equity: Sequence[float]) -> list[float]:
    """Simple returns between consecutive equity values; a non-positive value yields no return."""
    out = []
    for before, after in zip(equity, equity[1:]):
        b, a = _finite(before), _finite(after)
        if b is not None and a is not None and b > 0:
            out.append(a / b - 1)
    return out


def drawdowns(equity: Sequence[float]) -> dict:
    """Maximum drawdown (fraction of the peak) and the longest peak-to-recovery stretch in periods.

    ``max_duration`` counts periods from a peak until equity first exceeds that
    peak again; a drawdown still open at the end counts to the end and is
    flagged ``recovered: False``.
    """
    values = [v for v in (_finite(x) for x in equity) if v is not None]
    if not values:
        return {'max_drawdown': None, 'max_duration': 0, 'recovered': True, 'max_drawdown_index': None}
    peak, peak_index, worst, worst_index = values[0], 0, 0.0, 0
    longest, recovered = 0, True
    for index, value in enumerate(values):
        if value > peak:
            longest = max(longest, index - peak_index)
            peak, peak_index = value, index
        elif peak > 0:
            dd = (peak - value) / peak
            if dd > worst:
                worst, worst_index = dd, index
    if peak_index < len(values) - 1:
        longest = max(longest, len(values) - 1 - peak_index)
        recovered = False
    return {'max_drawdown': _round(worst), 'max_duration': longest, 'recovered': recovered,
            'max_drawdown_index': worst_index}


def _moments(values: Sequence[float]) -> tuple[float, float, float, float]:
    """Mean, sample standard deviation, skewness and (non-excess) kurtosis."""
    n = len(values)
    mean = statistics.fmean(values)
    sd = statistics.stdev(values) if n > 1 else 0.0
    if sd == 0 or n < 3:
        return mean, sd, 0.0, 3.0
    m2 = sum((v - mean) ** 2 for v in values) / n
    m3 = sum((v - mean) ** 3 for v in values) / n
    m4 = sum((v - mean) ** 4 for v in values) / n
    return mean, sd, m3 / m2 ** 1.5, m4 / m2 ** 2


def equity_statistics(equity: Sequence[float], periods_per_year: float, *,
                      in_market: Sequence[bool] | None = None, tail_fraction: float = 0.05) -> dict:
    """Risk-adjusted statistics of an equity series sampled every period.

    Sharpe and Sortino use simple per-period returns against a zero benchmark
    and are annualised by ``sqrt(periods_per_year)``; Sortino's downside
    deviation is the root mean square of the negative returns over all
    periods. ``cagr`` compounds the net return to a year. ``calmar`` is the
    CAGR over the maximum drawdown and is ``None`` without a drawdown.
    ``expected_shortfall`` is the mean of the worst ``tail_fraction`` of
    period returns. ``exposure`` is the share of periods with a position when
    ``in_market`` is given. ``skew`` and ``kurtosis`` describe the period
    returns and feed the probabilistic Sharpe ratio.
    """
    values = [v for v in (_finite(x) for x in equity) if v is not None]
    returns = periodic_returns(values)
    out: dict = {'periods': len(values), 'periods_per_year': periods_per_year, 'net_return': None,
                 'cagr': None, 'volatility': None, 'sharpe': None, 'sortino': None, 'max_drawdown': None,
                 'max_drawdown_periods': 0, 'drawdown_recovered': True, 'calmar': None,
                 'expected_shortfall': None, 'worst_period': None, 'best_period': None,
                 'skew': None, 'kurtosis': None, 'exposure': None, 'sharpe_per_period': None}
    if len(values) < 2 or values[0] <= 0:
        return out
    years = (len(values) - 1) / periods_per_year
    net = values[-1] / values[0] - 1
    out['net_return'] = _round(net)
    if years > 0 and values[-1] > 0:
        out['cagr'] = _round((values[-1] / values[0]) ** (1 / years) - 1)
    dd = drawdowns(values)
    out.update(max_drawdown=dd['max_drawdown'], max_drawdown_periods=dd['max_duration'],
               drawdown_recovered=dd['recovered'])
    if dd['max_drawdown'] and out['cagr'] is not None:
        out['calmar'] = _round(out['cagr'] / dd['max_drawdown'])
    if len(returns) >= 2:
        mean, sd, skew, kurt = _moments(returns)
        out.update(volatility=_round(sd * math.sqrt(periods_per_year)), skew=_round(skew), kurtosis=_round(kurt),
                   worst_period=_round(min(returns)), best_period=_round(max(returns)))
        if sd > 0:
            out['sharpe_per_period'] = _round(mean / sd)
            out['sharpe'] = _round(mean / sd * math.sqrt(periods_per_year))
        downside = math.sqrt(sum(min(r, 0.0) ** 2 for r in returns) / len(returns))
        if downside > 0:
            out['sortino'] = _round(mean / downside * math.sqrt(periods_per_year))
        k = max(1, math.ceil(tail_fraction * len(returns)))
        out['expected_shortfall'] = _round(statistics.mean(sorted(returns)[:k]))
    if in_market is not None and len(in_market) > 0:
        out['exposure'] = _round(sum(1 for flag in in_market if flag) / len(in_market))
    return out


def calendar_returns(curve: Sequence[dict], *, period: str = 'year', start: float | None = None) -> dict:
    """Net return per calendar year or quarter from ``[{'time': ms, 'equity': ...}, ...]``.

    The return of a period is from the last equity before it (``start`` for
    the first period, else its first value) to its last value, so the periods
    do not overlap.
    """
    if period not in ('year', 'quarter'):
        raise ValueError('period must be year or quarter')
    labelled: list[tuple[str, float]] = []
    for point in curve:
        stamp, value = _finite(point.get('time')), _finite(point.get('equity'))
        if stamp is None or value is None:
            continue
        moment = datetime.fromtimestamp(stamp / 1000, tz=timezone.utc)
        label = str(moment.year) if period == 'year' else f'{moment.year}Q{(moment.month - 1) // 3 + 1}'
        labelled.append((label, value))
    out: dict[str, float | None] = {}
    previous = _finite(start)
    for label in dict.fromkeys(l for l, _ in labelled):
        values = [v for l, v in labelled if l == label]
        start = previous if previous is not None else values[0]
        out[label] = _round(values[-1] / start - 1) if start > 0 else None
        previous = values[-1]
    return out


def resample_curve(curve: Sequence[dict], period_ms: int) -> list[dict]:
    """The last point of every ``period_ms`` bucket of a ``[{'time', 'equity'}]`` curve."""
    buckets: dict[int, dict] = {}
    for point in curve:
        stamp = _finite(point.get('time'))
        if stamp is None:
            continue
        buckets[int(stamp // period_ms)] = point
    return [buckets[k] for k in sorted(buckets)]


# -------------------------------------------------- selection-bias adjustments
def probabilistic_sharpe(sharpe_per_period: float, periods: int, *, skew: float = 0.0,
                         kurtosis: float = 3.0, benchmark: float = 0.0) -> float | None:
    """Probability that the true per-period Sharpe ratio exceeds ``benchmark`` (Bailey & López de Prado 2012).

    ``sharpe_per_period`` is NOT annualised; ``periods`` is the number of
    returns it was estimated from; ``kurtosis`` is non-excess (3 for normal).
    """
    sr, t = _finite(sharpe_per_period), int(periods)
    if sr is None or t < 2:
        return None
    variance = 1 - skew * sr + (kurtosis - 1) / 4 * sr * sr
    if variance <= 0:
        return None
    z = (sr - benchmark) * math.sqrt(t - 1) / math.sqrt(variance)
    return _round(_NORMAL.cdf(z))


def expected_max_sharpe(trials: int, sharpe_variance: float) -> float | None:
    """Expected maximum per-period Sharpe ratio among ``trials`` unrelated zero-edge trials.

    ``sharpe_variance`` is the variance of the trials' estimated Sharpe ratios
    (Bailey & López de Prado 2014, eq. 7).
    """
    n, var = int(trials), _finite(sharpe_variance)
    if n < 1 or var is None or var < 0:
        return None
    if n == 1 or var == 0:
        return 0.0
    return _round(math.sqrt(var) * ((1 - EULER_GAMMA) * _NORMAL.inv_cdf(1 - 1 / n)
                                    + EULER_GAMMA * _NORMAL.inv_cdf(1 - 1 / (n * math.e))))


def deflated_sharpe(sharpe_per_period: float, periods: int, *, trials: int, sharpe_variance: float,
                    skew: float = 0.0, kurtosis: float = 3.0) -> dict:
    """The probabilistic Sharpe ratio against the expected maximum of ``trials`` attempts.

    A value under 0.95 means the observed Sharpe ratio is not distinguishable
    from the best of ``trials`` lucky, edge-less variants at that confidence.
    """
    threshold = expected_max_sharpe(trials, sharpe_variance)
    if threshold is None:
        return {'deflated_sharpe': None, 'expected_max_sharpe': None, 'trials': trials}
    return {'deflated_sharpe': probabilistic_sharpe(sharpe_per_period, periods, skew=skew, kurtosis=kurtosis,
                                                    benchmark=threshold),
            'expected_max_sharpe': threshold, 'trials': trials}


# ------------------------------------------------------------- monte carlo
def monte_carlo(trade_returns: Sequence[float], *, capital_fraction: float, horizon: int,
                ruin_fraction: float = 0.2, runs: int = 2000, seed: int = 0) -> dict:
    """Resample closed-trade returns to estimate the range of outcomes and a risk of ruin.

    Each run draws ``horizon`` trades independently and with replacement from
    ``trade_returns`` (net return on the capital each trade used) and
    compounds ``capital_fraction`` of equity per trade. Ruin is equity at or
    below ``1 - ruin_fraction`` of the start at any point. The assumptions are
    returned with the result: independent, identically distributed trades,
    no regime dependence, fixed fractional sizing, and no costs beyond those
    already inside the returns. A sample with fewer than 10 trades returns
    ``None`` for every estimate.
    """
    values = [v for v in (_finite(x) for x in trade_returns) if v is not None]
    assumptions = ('independent resampling with replacement of the recorded trades; '
                   'no autocorrelation or regime dependence; fixed fraction of equity per trade; '
                   'costs only as already included in each trade return')
    out = {'samples': len(values), 'runs': runs, 'horizon_trades': horizon,
           'capital_fraction': capital_fraction, 'ruin_fraction': ruin_fraction,
           'ruin_probability': None, 'terminal_median': None, 'terminal_p05': None, 'terminal_p95': None,
           'max_drawdown_median': None, 'max_drawdown_p95': None, 'assumptions': assumptions}
    if len(values) < 10 or horizon < 1 or runs < 1 or not 0 < capital_fraction <= 1:
        return out
    rng = random.Random(seed)
    ruined, terminals, worst = 0, [], []
    floor = 1 - ruin_fraction
    for _ in range(runs):
        equity, peak, dd, hit = 1.0, 1.0, 0.0, False
        for r in rng.choices(values, k=horizon):
            equity *= max(0.0, 1 + capital_fraction * r)
            peak = max(peak, equity)
            dd = max(dd, (peak - equity) / peak)
            if equity <= floor:
                hit = True
        ruined += hit
        terminals.append(equity)
        worst.append(dd)
    terminals.sort()
    worst.sort()

    def pct(values_sorted, q):
        return values_sorted[min(len(values_sorted) - 1, int(q * len(values_sorted)))]
    out.update(ruin_probability=_round(ruined / runs), terminal_median=_round(pct(terminals, 0.5)),
               terminal_p05=_round(pct(terminals, 0.05)), terminal_p95=_round(pct(terminals, 0.95)),
               max_drawdown_median=_round(pct(worst, 0.5)), max_drawdown_p95=_round(pct(worst, 0.95)))
    return out


def turnover(traded_notional: float, average_equity: float, years: float) -> float | None:
    """Traded notional (buys plus sells) per unit of average equity per year."""
    notional, equity, span = _finite(traded_notional), _finite(average_equity), _finite(years)
    if notional is None or not equity or not span or equity <= 0 or span <= 0:
        return None
    return _round(notional / equity / span)


# ------------------------------------------------------------------ bundle
def summarize_backtest(result: dict, *, period_ms: int, trials: int = 1, sharpe_variance: float | None = None) -> dict:
    """All statistics for one ``strategy.backtest`` result (needs its full ``curve``).

    ``result['curve_full']`` is the bar-level equity curve and ``result['in_market']``
    the per-bar position flags when the backtest supplies them; otherwise the
    sampled ``equity_curve`` is used and exposure is unknown. Period statistics
    are computed on a daily resample when the bar is shorter than a day, so
    Sharpe ratios of 1-minute and daily research are comparable.
    """
    curve = list(result.get('curve_full') or result.get('equity_curve') or [])
    starting = _finite(result.get('starting_equity'))
    day = 86_400_000
    sample_ms = max(int(period_ms), day) if curve else day
    sampled = resample_curve(curve, sample_ms) if period_ms < day else curve
    # The curve starts at the end of the first bar; the run starts flat before it.
    equity = ([starting] if sampled and starting else []) + [p['equity'] for p in sampled]
    periods_per_year = 365.25 * day / sample_ms
    trade_pnls = [ex['pnl'] for ex in result.get('executions', []) if ex.get('type') == 'sell']
    stats = equity_statistics(equity, periods_per_year, in_market=result.get('in_market'))
    trades = trade_statistics(trade_pnls)
    bundle = {'period_ms': sample_ms, 'equity': stats, 'trades': trades,
              'trade_mean_ci95': bootstrap_mean_ci(trade_pnls), 'calendar': calendar_returns(sampled, start=starting)}
    if stats['sharpe_per_period'] is not None:
        bundle['probabilistic_sharpe'] = probabilistic_sharpe(stats['sharpe_per_period'], len(equity) - 1,
                                                              skew=stats['skew'] or 0.0, kurtosis=stats['kurtosis'] or 3.0)
        if trials > 1 and sharpe_variance is not None:
            bundle['deflated'] = deflated_sharpe(stats['sharpe_per_period'], len(equity) - 1, trials=trials,
                                                 sharpe_variance=sharpe_variance, skew=stats['skew'] or 0.0,
                                                 kurtosis=stats['kurtosis'] or 3.0)
    buys = sum(float(ex.get('size_usdt') or 0) for ex in result.get('executions', []) if ex.get('type') == 'buy')
    sells = sum(float(ex.get('proceeds') or 0) for ex in result.get('executions', []) if ex.get('type') == 'sell')
    if equity and len(equity) > 1:
        bundle['turnover_per_year'] = turnover(buys + sells, statistics.fmean(equity), (len(equity) - 1) / periods_per_year)
    return bundle
