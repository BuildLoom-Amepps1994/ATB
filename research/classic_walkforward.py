"""Walk-forward evaluation of the classic strategies on real public candles.

Offline: reads OHLCV files collected beforehand (public data, no keys) and
calls the production ``strategy.backtest`` for every pair and split. Nothing
here places, sizes or simulates an order outside that function.

PROTOCOL (written before any result; see ``PROTOCOL``)
* Splits by calendar date, the same for every pair: development before
  2024-01-01, validation 2024-01-01 to 2025-05-01, holdout from 2025-05-01 to
  the end of the data. Each split starts flat with 10,000 USDT; earlier candles
  only warm the indicators.
* Candidates are chosen on development and validation. The holdout is read
  once, for the candidate chosen, and reported whatever it shows.
* Costs per fill: ``realistic`` = MEXC's public 0.05 % taker fee plus 0.05 %
  slippage (the measured spread on these pairs was under 1 bp); ``doubled``;
  ``app`` = the app's paper default (0.10 % fee, 0.20 % slippage).
* Edge is read per trade (net return of each closed trade, size-free) and per
  account (the equal-weight mean of the pairs' returns at the app's sizing).
* Baselines: buy and hold each pair over the split, and random entries with the
  strategy's own exits at the strategy's entry rate (``RANDOM_RUNS`` seeds).
* Verdict PROFITABLE only when, on the holdout, the mean trade and the account
  return are above zero at realistic AND doubled costs, and the account return
  is above the random-entry mean plus two standard deviations. Anything else
  is NOT SHOWN.

DATA FORMAT: ``<dir>/<BASE>_USDT_<tf>_long.json`` = {"provenance": {...},
"ohlcv": [[ms, open, high, low, close, volume], ...]}. Missing bars (exchange
maintenance) are filled with a flat bar at the previous close and zero volume,
and counted in the report. Weekly bars are built from daily bars (Monday open).
"""
from __future__ import annotations

import argparse
import hashlib
import inspect
import json
import math
import random
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import strategy  # noqa: E402
from research import metrics as perf  # noqa: E402

DAY_MS = 86_400_000
TF_MS = {'1h': 3_600_000, '4h': 14_400_000, '1d': DAY_MS, '1w': 7 * DAY_MS}
PROFILES = {'balanced': ('1h', '4h'), 'swing': ('4h', '1d'), 'position': ('1d', '1w'), 'trend': ('1d', '1w')}


def _ms(text: str) -> int:
    return int(datetime.fromisoformat(text).replace(tzinfo=timezone.utc).timestamp() * 1000)


PROTOCOL = {
    'version': 'classic-walkforward-v1',
    'splits': {'development': [None, '2024-01-01'], 'validation': ['2024-01-01', '2025-05-01'],
               'holdout': ['2025-05-01', None]},
    'costs': {'realistic': [0.05, 0.05], 'doubled': [0.10, 0.10], 'app': [0.10, 0.20]},
    'sizing': {'risk_pct': 0.65, 'max_position_pct': 8.0},
    'random_runs': 30,
    'verdict': 'holdout mean trade > 0 and account > 0 at realistic and doubled costs, '
               'account > random-entry mean + 2 sd',
    'selection': 'among candidates with a positive development mean trade and at least 30 validation trades, '
                 'the highest validation mean trade at realistic cost; ties go to fewer trades (lower cost)',
    'candidates': ['balanced', 'swing', 'position', 'balanced --no-filters', 'swing --no-filters',
                   'position --no-filters'],
}
RANDOM_RUNS = PROTOCOL['random_runs']


def protocol_hash() -> str:
    return hashlib.sha256(json.dumps(PROTOCOL, sort_keys=True).encode()).hexdigest()


# ------------------------------------------------------------------ data
def load(directory: Path, base: str, tf: str) -> tuple[list[list[float]], int]:
    """Rows on a regular grid, with the number of bars that had to be filled."""
    if tf == '1w':
        daily, filled = load(directory, base, '1d')
        return weekly_from_daily(daily), filled
    payload = json.loads((directory / f'{base}_USDT_{tf}_long.json').read_text(encoding='utf-8'))
    return fill_gaps(payload['ohlcv'], TF_MS[tf])


def fill_gaps(rows: list[list[float]], step: int) -> tuple[list[list[float]], int]:
    out, filled = [], 0
    for row in sorted(rows, key=lambda r: r[0]):
        if out and row[0] <= out[-1][0]:
            continue
        while out and row[0] - out[-1][0] > step:
            close = out[-1][4]
            out.append([out[-1][0] + step, close, close, close, close, 0.0])
            filled += 1
        out.append([int(row[0]), *map(float, row[1:6])])
    return out, filled


def weekly_from_daily(daily: list[list[float]]) -> list[list[float]]:
    weeks: dict[int, list[float]] = {}
    for ts, o, h, l, c, v in daily:
        day = datetime.fromtimestamp(ts / 1000, tz=timezone.utc)
        monday = int(ts - day.weekday() * DAY_MS)
        if monday not in weeks:
            weeks[monday] = [monday, o, h, l, c, v]
        else:
            w = weeks[monday]
            w[2], w[3], w[4], w[5] = max(w[2], h), min(w[3], l), c, w[5] + v
    rows = [weeks[k] for k in sorted(weeks)]
    if daily and rows and daily[-1][0] < rows[-1][0] + 6 * DAY_MS:
        rows = rows[:-1]          # the last week is still forming
    return rows


# ------------------------------------------------------------------ variants
_SOURCE = inspect.getsource(strategy.backtest)
_ENTRY = "buy, _, _ = entry_signal(previous, regime_ok=regimes[i - 1], **filter_args)"
assert _ENTRY in _SOURCE


def _compile(source: str, extra: dict | None = None):
    namespace = dict(vars(strategy))
    namespace.update(extra or {})
    exec(compile(source, 'backtest_variant', 'exec'), namespace)
    return namespace['backtest']


def random_backtest(probability: float, seed: int):
    """The production backtest with its entry signal replaced by a coin flip; exits unchanged."""
    rng = random.Random(seed)
    source = _SOURCE.replace(_ENTRY, 'buy = _rng.random() < _p')
    return _compile(source, {'_rng': rng, '_p': probability})


# ------------------------------------------------------------------ evaluation
def trade_returns(result: dict) -> list[float]:
    """Net return of each closed trade (sell proceeds after costs over the buy's all-in budget)."""
    out, budget = [], None
    for ex in result['executions']:
        if ex['type'] == 'buy':
            budget = ex['size_usdt']
        elif ex['type'] == 'sell' and budget:
            out.append(ex['pnl'] / budget)
            budget = None
    return out


def split_bounds(name: str, rows: list) -> tuple[int | None, int | None]:
    start, end = PROTOCOL['splits'][name]
    return (_ms(start) if start else None), (_ms(end) if end else None)


def run_pair(base: str, profile: str, data: Path, cost: str, splits: list[str], *, filters: bool = True,
             with_random: bool = False, backtest=None, now_ms: int | None = None,
             with_metrics: bool = False) -> dict:
    """One pair through every split; ``with_metrics`` adds ``research.metrics.summarize_backtest``."""
    tf, higher_tf = PROFILES[profile]
    rows, filled = load(data, base, tf)
    higher, filled_h = load(data, base, higher_tf)
    market = None
    if filters and base != 'BTC':
        market, _ = load(data, 'BTC', higher_tf)
    fee, slip = PROTOCOL['costs'][cost]
    settings = {'fee_pct': fee, 'slippage_pct': slip, 'market_filters': filters, 'time_horizon': profile,
                **PROTOCOL['sizing']}
    run = backtest or strategy.backtest
    report = {'filled_bars': filled + filled_h, 'splits': {}}
    for name in splits:
        start, end = split_bounds(name, rows)
        if end is not None and rows[0][0] >= end:
            report['splits'][name] = {'skipped': 'no data in this split'}
            continue
        if start is not None and start < rows[0][0] + 130 * TF_MS[tf]:
            start = rows[0][0] + 130 * TF_MS[tf]            # warmup inside the data, never before it
        try:
            res = run(rows, settings, timeframe=tf, higher_rows=higher, higher_timeframe=higher_tf,
                      now_ms=now_ms, start_ms=start, end_ms=end, market_rows=market, keep_curve=with_metrics)
        except ValueError as exc:
            report['splits'][name] = {'skipped': str(exc)}
            continue
        trades = trade_returns(res)
        entry = {'return_pct': res['return_pct'], 'buy_hold_pct': res['buy_hold_pct'],
                 'max_drawdown_pct': res['max_drawdown_pct'], 'trades': trades,
                 'evaluation_bars': res['coverage']['evaluation_candles'],
                 'open_at_end': res['open_position_count']}
        if with_metrics:
            # Daily-resampled risk statistics, trade statistics, calendar returns and
            # the probabilistic Sharpe ratio of this pair on this split.
            entry['metrics'] = perf.summarize_backtest(res, period_ms=TF_MS[tf])
        if with_random and trades:
            p = len(trades) / max(1, res['coverage']['evaluation_candles'])
            randoms = []
            for seed in range(RANDOM_RUNS):
                rres = random_backtest(p, seed * 7919 + sum(map(ord, base)))(
                    rows, settings, timeframe=tf, higher_rows=higher, higher_timeframe=higher_tf,
                    now_ms=now_ms, start_ms=start, end_ms=end, market_rows=market)
                randoms.append(rres['return_pct'])
            entry['random_returns'] = randoms
        report['splits'][name] = entry
    return report


def summarize(pairs: dict, split: str, *, trials: int = 1, sharpe_variance: float | None = None) -> dict:
    rows = [r['splits'][split] for r in pairs.values() if 'return_pct' in r['splits'].get(split, {})]
    if not rows:
        return {'pairs': 0}
    trades = [t for r in rows for t in r['trades']]
    out = {'pairs': len(rows), 'account_mean_pct': round(statistics.mean(r['return_pct'] for r in rows), 3),
           'buy_hold_mean_pct': round(statistics.mean(r['buy_hold_pct'] for r in rows), 3),
           'worst_drawdown_pct': max(r['max_drawdown_pct'] for r in rows),
           'trades': len(trades), 'wins': sum(t > 0 for t in trades)}
    if trades:
        mean = statistics.mean(trades)
        sd = statistics.stdev(trades) if len(trades) > 1 else 0.0
        out.update(mean_trade_pct=round(100 * mean, 4),
                   t_stat=round(mean / (sd / math.sqrt(len(trades))), 2) if sd > 0 else None)
    if all('metrics' in r for r in rows):
        # Equal-weight sleeve statistics: the mean of the pairs' daily-resampled
        # equity statistics, the pooled trade statistics and the selection-bias
        # adjustment for the number of candidate variants that read this data.
        sharpes = [r['metrics']['equity']['sharpe'] for r in rows if r['metrics']['equity']['sharpe'] is not None]
        per_period = [r['metrics']['equity']['sharpe_per_period'] for r in rows
                      if r['metrics']['equity']['sharpe_per_period'] is not None]
        pooled = perf.trade_statistics(trades)
        out['metrics'] = {
            'sharpe_mean': round(statistics.mean(sharpes), 3) if sharpes else None,
            'sortino_mean': round(statistics.mean(v), 3) if (v := [r['metrics']['equity']['sortino'] for r in rows
                                                                    if r['metrics']['equity']['sortino'] is not None]) else None,
            'calmar_mean': round(statistics.mean(v), 3) if (v := [r['metrics']['equity']['calmar'] for r in rows
                                                                   if r['metrics']['equity']['calmar'] is not None]) else None,
            'max_drawdown_periods_worst': max(r['metrics']['equity']['max_drawdown_periods'] for r in rows),
            'exposure_mean': round(statistics.mean(v), 3) if (v := [r['metrics']['equity']['exposure'] for r in rows
                                                                     if r['metrics']['equity']['exposure'] is not None]) else None,
            'turnover_per_year_mean': round(statistics.mean(v), 3) if (v := [r['metrics'].get('turnover_per_year') for r in rows
                                                                              if r['metrics'].get('turnover_per_year') is not None]) else None,
            'profit_factor': pooled['profit_factor'], 'expectancy_pct': None if pooled['expectancy'] is None else round(100 * pooled['expectancy'], 4),
            'payoff_ratio': pooled['payoff_ratio'], 'expected_shortfall_5pct': None if pooled['expected_shortfall'] is None else round(100 * pooled['expected_shortfall'], 3),
            'max_consecutive_losses': pooled['max_consecutive_losses'],
            'mean_trade_ci95_pct': [round(100 * v, 4) for v in (perf.bootstrap_mean_ci(trades)['low'], perf.bootstrap_mean_ci(trades)['high'])] if len(trades) > 1 else None,
            'probabilistic_sharpe_mean': round(statistics.mean(v), 3) if (v := [r['metrics'].get('probabilistic_sharpe') for r in rows
                                                                                 if r['metrics'].get('probabilistic_sharpe') is not None]) else None,
            'trials': trials,
        }
        if per_period and trials > 1 and sharpe_variance is not None:
            periods = min(r['metrics']['equity']['periods'] for r in rows)
            out['metrics']['deflated_sharpe_of_mean'] = perf.deflated_sharpe(
                statistics.mean(per_period), periods, trials=trials, sharpe_variance=sharpe_variance)
        if len(trades) >= 10:
            # Risk of ruin at the app's largest position size, over a year of trades at this rate.
            out['metrics']['monte_carlo'] = perf.monte_carlo(
                trades, capital_fraction=PROTOCOL['sizing']['max_position_pct'] / 100,
                horizon=max(10, len(trades)), ruin_fraction=0.2, runs=2000, seed=0)
    if all('random_returns' in r for r in rows if r['trades']):
        # equal-weight portfolio of the pairs' random runs, seed by seed
        runs = [r['random_returns'] for r in rows if r.get('random_returns')]
        if runs:
            portfolio = [statistics.mean(run[k] for run in runs) * len(runs) / len(rows)
                         for k in range(min(len(x) for x in runs))]
            out['random_mean_pct'] = round(statistics.mean(portfolio), 3)
            out['random_sd_pct'] = round(statistics.stdev(portfolio), 3) if len(portfolio) > 1 else 0.0
    return out


def trial_sharpe_variance(results: dict) -> float | None:
    """Variance of the per-period Sharpe ratios of every pair/candidate run on development or validation."""
    values = [entry['metrics']['equity']['sharpe_per_period']
              for block in results.values() for pair in block['pairs'].values()
              for split, entry in pair['splits'].items()
              if split != 'holdout' and isinstance(entry.get('metrics'), dict)
              and entry['metrics']['equity']['sharpe_per_period'] is not None]
    return round(statistics.variance(values), 8) if len(values) > 1 else None


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description='Classic strategy walk-forward (offline, public candles)')
    parser.add_argument('--data', required=True)
    parser.add_argument('--pairs', required=True, help='comma-separated bases, e.g. BTC,ETH,SOL')
    parser.add_argument('--profiles', default='balanced,swing,position')
    parser.add_argument('--costs', default='realistic,doubled,app')
    parser.add_argument('--splits', default='development,validation')
    parser.add_argument('--no-filters', action='store_true', help='turn the efficiency and BTC market filters off')
    parser.add_argument('--random', action='store_true', help='add the random-entry baseline')
    parser.add_argument('--metrics', action='store_true',
                        help='add research.metrics statistics (Sharpe, Sortino, Calmar, drawdown duration, '
                             'profit factor, expectancy, tail risk, bootstrap, Monte Carlo, deflated Sharpe)')
    parser.add_argument('--trials', type=int, default=None,
                        help='number of candidate variants that have read the development/validation data '
                             '(default: the protocol candidate list); feeds the deflated Sharpe ratio')
    parser.add_argument('--out', required=True)
    args = parser.parse_args(argv)
    if 'holdout' in args.splits.split(','):
        print('Reading the holdout. By protocol this is done once, for the chosen candidate.', flush=True)
    started = time.time()
    now_ms = int(started * 1000)
    data = Path(args.data)
    result = {'protocol': PROTOCOL, 'protocol_sha256': protocol_hash(), 'filters': not args.no_filters,
              'generated_utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), 'results': {}}
    trials = args.trials or len(PROTOCOL['candidates'])
    for profile in args.profiles.split(','):
        for cost in args.costs.split(','):
            pairs = {}
            for base in args.pairs.split(','):
                pairs[base] = run_pair(base, profile, data, cost, args.splits.split(','),
                                       filters=not args.no_filters, with_random=args.random, now_ms=now_ms,
                                       with_metrics=args.metrics)
            key = f'{profile}/{cost}'
            result['results'][key] = {'pairs': pairs}
    if args.metrics:
        # The variance of the per-period Sharpe ratios across every pair and
        # candidate run here, on development and validation: the spread of
        # trials the deflated Sharpe ratio discounts for.
        result['trials'] = trials
        result['trial_sharpe_variance'] = trial_sharpe_variance(result['results'])
    for key, block in result['results'].items():
        block['summary'] = {s: summarize(block['pairs'], s, trials=trials,
                                         sharpe_variance=result.get('trial_sharpe_variance'))
                            for s in args.splits.split(',')}
        print(key, json.dumps(block['summary']), flush=True)
    result['runtime_s'] = round(time.time() - started, 1)
    Path(args.out).write_text(json.dumps(result, indent=1), encoding='utf-8')
    print('wrote', args.out)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
