"""Trade-level statistics from the committed research results (no market data needed).

Reads ``research/results/classic/*.json`` as written by the walk-forward
harnesses (``classic-walkforward-v1``, ``trend-research-v1``,
``trend-variants-v1``) and reports, for every candidate, cost model and
split, the statistics of its closed trades: count, win rate, profit factor,
expectancy, payoff ratio, worst trade, trade-level expected shortfall, the
t statistic and a bootstrap interval of the mean trade. It also counts the
trials those files record, which is the number a deflated Sharpe ratio has to
be read against, and for the portfolio-level studies (``trend-portfolio-v1``,
``trend-rotation-v1``), which store an annualised Sharpe ratio per variant
and split, it reports the expected maximum Sharpe ratio of that many
variants with no skill and each variant's deflated Sharpe ratio.

The files hold each trade's net return on its own budget, not an equity
series, so Sharpe, Sortino, Calmar and drawdown duration cannot be computed
from them; those need a rerun of the harness with ``--metrics`` on the
candle data (``research/classic_walkforward.py``). Nothing here changes a
result; it only reads.

    python -m research.evidence                    # Markdown table to stdout
    python -m research.evidence --json out.json    # the same as JSON
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from research import metrics  # noqa: E402

RESULTS = ROOT / 'research' / 'results' / 'classic'


def _trades_by_split(payload: dict) -> list[tuple[str, str, list[float]]]:
    """(candidate, split, trade returns) for every candidate and split in one result file."""
    out = []
    for key, block in (payload.get('results') or {}).items():
        if not isinstance(block, dict):
            continue
        if 'pairs' in block:                                   # classic-walkforward-v1
            splits: dict[str, list[float]] = {}
            for pair in block['pairs'].values():
                for split, entry in (pair.get('splits') or {}).items():
                    if isinstance(entry, dict) and isinstance(entry.get('trades'), list):
                        splits.setdefault(split, []).extend(float(t) for t in entry['trades'])
            out.extend((key, split, trades) for split, trades in splits.items())
        elif 'per_pair' in block:                              # trend-research-v1 (split in the key)
            trades = [float(t) for pair in block['per_pair'].values() for t in pair.get('trades', [])]
            candidate, _, split = key.rpartition('/')
            out.append((candidate, split, trades))
        else:                                                  # trend-variants-v1 and portfolio files
            for split, entry in block.items():
                per_pair = entry.get('per_pair') if isinstance(entry, dict) else None
                if isinstance(per_pair, dict):
                    trades = [float(t) for pair in per_pair.values() for t in pair.get('trades', [])]
                    out.append((key, split, trades))
    return out


def _row(file: str, candidate: str, split: str, trades: list[float]) -> dict:
    stats = metrics.trade_statistics(trades)
    ci = metrics.bootstrap_mean_ci(trades, runs=1000, seed=0)
    return {'file': file, 'candidate': candidate, 'split': split, 'trades': stats['trades'],
            'win_rate': stats['win_rate'], 'profit_factor': stats['profit_factor'],
            'expectancy_pct': None if stats['expectancy'] is None else round(100 * stats['expectancy'], 3),
            'payoff_ratio': stats['payoff_ratio'],
            'average_win_pct': None if stats['average_win'] is None else round(100 * stats['average_win'], 3),
            'average_loss_pct': None if stats['average_loss'] is None else round(100 * stats['average_loss'], 3),
            'worst_trade_pct': None if stats['largest_loss'] is None else round(100 * stats['largest_loss'], 2),
            'expected_shortfall_5pct': None if stats['expected_shortfall'] is None else round(100 * stats['expected_shortfall'], 2),
            'max_consecutive_losses': stats['max_consecutive_losses'], 't_stat': stats['t_stat'],
            'mean_ci95_pct': None if ci['low'] is None else [round(100 * ci['low'], 3), round(100 * ci['high'], 3)]}


def _account_sharpes(payload: dict) -> list[tuple[str, str, dict]]:
    """(variant, split, figures) for portfolio-level files that store a Sharpe ratio per split.

    ``trend-portfolio-v1`` and ``trend-rotation-v1`` keep, per variant and
    split, the account return, the annualised Sharpe ratio, the maximum
    drawdown and the trade count instead of a trade list.
    """
    out = []
    for key, block in (payload.get('results') or {}).items():
        if not isinstance(block, dict):
            continue
        for split, entry in block.items():
            if isinstance(entry, dict) and isinstance(entry.get('sharpe'), (int, float)) and 'return_pct' in entry:
                out.append((key, split, entry))
    return out


def _validation_days(payload: dict) -> int | None:
    splits = (payload.get('protocol') or {}).get('splits') if isinstance(payload.get('protocol'), dict) else None
    window = (splits or {}).get('validation') if isinstance(splits, dict) else None
    if not (isinstance(window, (list, tuple)) and len(window) == 2 and all(window)):
        return None
    try:
        from datetime import date
        return (date.fromisoformat(window[1]) - date.fromisoformat(window[0])).days
    except (TypeError, ValueError):
        return None


def selection_adjusted(files: dict[str, dict], *, periods_per_year: int = 365) -> dict | None:
    """Read every validation Sharpe ratio of the portfolio-level studies against the number of variants tried.

    The expected maximum Sharpe ratio of ``n`` variants with no skill
    (Bailey and Lopez de Prado, 2014) is computed from the spread of the
    recorded validation Sharpe ratios; each variant's deflated Sharpe ratio
    is the probability that its true Sharpe ratio exceeds that maximum,
    judged over the validation window at daily resolution. A deflated value
    near 0.5 means the variant is about what the best of ``n`` noise trials
    would show.
    """
    trials, days = [], None
    for name, payload in files.items():
        for variant, split, entry in _account_sharpes(payload):
            if split == 'validation':
                trials.append((name, variant, float(entry['sharpe']), entry))
                days = days or _validation_days(payload)
    if len(trials) < 2 or not days:
        return None
    annual = [sharpe for _, _, sharpe, _ in trials]
    variance = metrics.statistics.variance(annual)
    rows = []
    for name, variant, sharpe, entry in sorted(trials, key=lambda t: -t[2]):
        per_period = sharpe / math.sqrt(periods_per_year)
        deflated = metrics.deflated_sharpe(per_period, days, trials=len(trials), sharpe_variance=variance / periods_per_year)
        rows.append({'file': name, 'variant': variant, 'validation_sharpe': round(sharpe, 3),
                     'validation_return_pct': entry.get('return_pct'), 'max_drawdown_pct': entry.get('max_drawdown_pct'),
                     'trades': entry.get('trades'),
                     'probabilistic_sharpe': metrics.probabilistic_sharpe(per_period, days),
                     'deflated_sharpe': deflated['deflated_sharpe']})
    return {'trials': len(trials), 'validation_days': days, 'sharpe_mean': round(metrics.statistics.fmean(annual), 3),
            'sharpe_sd': round(math.sqrt(variance), 3),
            'expected_max_sharpe_of_noise': metrics.expected_max_sharpe(len(trials), variance), 'rows': rows}


def collect(directory: Path = RESULTS) -> dict:
    rows, trials, account_files = [], {}, {}
    for path in sorted(directory.glob('*.json')):
        try:
            payload = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        entries = _trades_by_split(payload)
        protocol = (payload.get('protocol') or {}).get('version') if isinstance(payload.get('protocol'), dict) else None
        for candidate, split, trades in entries:
            rows.append(_row(path.name, candidate, split, trades))
            if split in ('development', 'validation'):
                trials.setdefault(protocol or path.name, set()).add(candidate)
        account = _account_sharpes(payload)
        if account:
            account_files[path.name] = payload
            for variant, split, _ in account:
                if split in ('development', 'validation'):
                    trials.setdefault(protocol or path.name, set()).add(variant)
    counted = {name: sorted(values) for name, values in trials.items()}
    return {'rows': rows, 'trials': counted, 'trial_count': sum(len(v) for v in counted.values()),
            'selection_adjusted': selection_adjusted(account_files),
            'note': ('Trade-level statistics only: each file stores every closed trade\'s net return on its own '
                     'budget, not an equity series, so Sharpe, Sortino, Calmar and drawdown duration are not '
                     'derivable here. The trial count is the number of distinct candidate variants that read the '
                     'development or validation data in these files; results chosen among them carry that '
                     'selection bias.')}


def _fmt(value) -> str:
    if value is None:
        return 'n/a'
    if isinstance(value, list):
        return f'[{value[0]:+.2f}, {value[1]:+.2f}]'
    if isinstance(value, float):
        return f'{value:.3g}' if abs(value) < 1000 else f'{value:.0f}'
    return str(value)


def markdown(report: dict) -> str:
    lines = ['| File | Candidate | Split | Trades | Win rate | Profit factor | Expectancy % | Payoff | Worst % | ES 5 % | Max losses in a row | t | Mean 95 % CI % |',
             '| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |']
    for r in report['rows']:
        lines.append('| ' + ' | '.join(_fmt(r[k]) for k in (
            'file', 'candidate', 'split', 'trades', 'win_rate', 'profit_factor', 'expectancy_pct', 'payoff_ratio',
            'worst_trade_pct', 'expected_shortfall_5pct', 'max_consecutive_losses', 't_stat', 'mean_ci95_pct')) + ' |')
    lines.append('')
    lines.append(f'Trials recorded on development or validation data: {report["trial_count"]} '
                 + ', '.join(f'{name}: {len(values)}' for name, values in report['trials'].items()))
    adjusted = report.get('selection_adjusted')
    if adjusted:
        lines.append('')
        lines.append(f'Portfolio-level studies: {adjusted["trials"]} variants read the validation window '
                     f'({adjusted["validation_days"]} days); their annualised Sharpe ratios average '
                     f'{adjusted["sharpe_mean"]:+.2f} (sd {adjusted["sharpe_sd"]:.2f}), so the best of that many '
                     f'variants with no skill would be expected to show about '
                     f'{adjusted["expected_max_sharpe_of_noise"]:.2f}.')
        lines.append('')
        lines.append('| File | Variant | Validation Sharpe | Return % | Max drawdown % | Trades | Probabilistic Sharpe | Deflated Sharpe |')
        lines.append('| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |')
        for r in adjusted['rows']:
            lines.append('| ' + ' | '.join(_fmt(r[k]) for k in (
                'file', 'variant', 'validation_sharpe', 'validation_return_pct', 'max_drawdown_pct', 'trades',
                'probabilistic_sharpe', 'deflated_sharpe')) + ' |')
    lines.append('')
    lines.append(report['note'])
    return '\n'.join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--results', default=str(RESULTS))
    parser.add_argument('--json', default=None, help='also write the report as JSON to this path')
    args = parser.parse_args(argv)
    report = collect(Path(args.results))
    print(markdown(report))
    if args.json:
        Path(args.json).write_text(json.dumps(report, indent=1), encoding='utf-8')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
