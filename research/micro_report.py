"""Render a micro research result file as Markdown tables (no network, no recomputation).

    python -m research.micro_report research/results/micro_fixtures.json > report.md

Every number comes from the JSON that ``research.micro_research`` wrote; this
only formats it, so a report can be regenerated and checked against its
source. Amounts are USDT of simulated cash, rounded to 4 decimals for
display; the JSON keeps 6.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SPLITS = ('development', 'validation', 'holdout')


def _n(value, places: int = 4) -> str:
    if value is None:
        return 'n/a'
    if isinstance(value, float) and value != value:     # NaN: not computable
        return 'n/a'
    if isinstance(value, (int, float)):
        text = f'{value:.{places}f}'
        return '0' if float(text) == 0 else text
    return str(value)


def series_table(series: dict, budget: str = '25') -> list[str]:
    rows = ['| Split | Bars | Cycles | Net | Fees | Gross before fees | Fees / gains before fees | Worst | Max drawdown '
            '| Exposure % | Doubled net (cycles) | Severe net (cycles) |',
            '|---|---|---|---|---|---|---|---|---|---|---|---|']
    per_budget = series['by_budget'].get(budget, {})
    for split in SPLITS:
        result = per_budget.get(split)
        if not result:
            continue
        start, end = series['splits'][split]
        base = result['strategy']['baseline']
        doubled, severe = result['strategy']['doubled'], result['strategy']['severe']
        rows.append(f"| {split} | {end - start} | {base['closed_cycles']} | {_n(base['net'])} | {_n(base['fees'])} "
                    f"| {_n(base['gross_pnl_before_fees'])} | {_n(base['fees_share_of_gross_gains'], 3)} "
                    f"| {_n(base['worst'])} | {_n(base['max_drawdown'])} | {_n(base['exposure_pct_of_time'], 1)} "
                    f"| {_n(doubled['net'])} ({doubled['closed_cycles']}) | {_n(severe['net'])} ({severe['closed_cycles']}) |")
    return rows


def baseline_table(series: dict, budget: str = '25') -> list[str]:
    rows = ['| Split | Strategy net | Cash | Buy and hold | Random entries mean (sd, n) | Strategy percentile '
            '| Always enter net (cycles) | Unchanged ATB rapid |',
            '|---|---|---|---|---|---|---|---|']
    per_budget = series['by_budget'].get(budget, {})
    for split in SPLITS:
        result = per_budget.get(split)
        if not result:
            continue
        b = result['baselines']
        rnd = b['random_entries']
        random_text = ('not run (the strategy made no entry)' if not rnd['repetitions'] else
                       f"{_n(rnd['mean'])} ({_n(rnd['sd'])}, {rnd['repetitions']})")
        atb = b['atb_rapid_unchanged']
        atb_text = (f"{_n(atb.get('return_pct'), 2)} % of 10,000, {atb.get('closed_trades')} trades"
                    if isinstance(atb, dict) and 'return_pct' in atb else str(atb))
        always = b['always_enter']
        rows.append(f"| {split} | {_n(result['strategy']['baseline']['net'])} | 0 | {_n(b['buy_and_hold'])} "
                    f"| {random_text} | {_n(rnd['strategy_percentile'], 1) if rnd['repetitions'] else 'n/a'} "
                    f"| {_n(always['net'])} ({always['closed_cycles']}) | {atb_text} |")
    return rows


def budget_table(report: dict) -> list[str]:
    budgets = sorted({b for s in report['series'] for b in s['by_budget']}, key=float)
    rows = ['| Series | Split | ' + ' | '.join(f'{b} USDT net (cycles)' for b in budgets) + ' |',
            '|---|---|' + '---|' * len(budgets)]
    for series in report['series']:
        for split in SPLITS:
            cells = []
            for budget in budgets:
                result = series['by_budget'].get(budget, {}).get(split)
                base = result['strategy']['baseline'] if result else None
                cells.append(f"{_n(base['net'])} ({base['closed_cycles']})" if base else 'n/a')
            rows.append(f"| {series['symbol']} | {split} | " + ' | '.join(cells) + ' |')
    return rows


def render(report: dict) -> str:
    out = [f"Protocol `{report['protocol']['version']}`, SHA-256 `{report['protocol_sha256']}`; features "
           f"`{report['feature_version']}`, decisions `{report['decision_version']}`; run time "
           f"{report.get('runtime_seconds')} s.", '',
           f"Real data: {report['real_data'] if isinstance(report['real_data'], str) else 'see provenance below'}", '']
    for series in report['series']:
        out += [f"### {series['symbol']}: {series.get('fixture', '')}", '', f"Verdict: **{series['verdict']}**", '',
                'Strategy at the 25 USDT micro budget, three cost scenarios:', '']
        out += series_table(series)
        out += ['', 'Baselines over the same bars and costs (25 USDT):', '']
        out += baseline_table(series)
        out.append('')
    out += ['### Budgets through the production sizing (baseline costs)', '']
    out += budget_table(report)
    if not isinstance(report['real_data'], str):
        out += ['', '### Data provenance', '', '```json', json.dumps(report['real_data'], indent=1), '```']
    return '\n'.join(out) + '\n'


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('result', type=Path)
    args = parser.parse_args(argv)
    sys.stdout.write(render(json.loads(args.result.read_text(encoding='utf-8'))))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
