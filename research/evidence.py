"""Trade-level statistics from the committed research results (no market data needed).

Reads ``research/results/classic/*.json`` as written by the walk-forward
harnesses (``classic-walkforward-v1``, ``trend-research-v1``,
``trend-variants-v1``) and reports, for every candidate, cost model and
split, the statistics of its closed trades: count, win rate, profit factor,
expectancy, payoff ratio, worst trade, trade-level expected shortfall, the
t statistic and a bootstrap interval of the mean trade. It also counts the
trials those files record, which is the number a deflated Sharpe ratio has to
be read against.

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


def collect(directory: Path = RESULTS) -> dict:
    rows, trials = [], {}
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
    counted = {name: sorted(values) for name, values in trials.items()}
    return {'rows': rows, 'trials': counted, 'trial_count': sum(len(v) for v in counted.values()),
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
