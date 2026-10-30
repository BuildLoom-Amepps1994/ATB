"""The micro-trading mode's single PAPER-only rule.

Every boundary that could put a micro-trading order in front of a real venue
asks :func:`live_refusal`: switching the engine to live while the micro profile
is selected, selecting the profile while live, starting the worker, the risk
gate, and the engine's final submit. The answer depends only on the trading
mode, the selected strategy horizon and the order kind. Nothing here reads an
environment variable, a setting, the database, a credential or a UI flag, so
none of those can turn it off; enabling funded micro trading needs a reviewed
code change to this module, which this build does not contain.
"""
from __future__ import annotations

MICRO_HORIZON = 'micro'
MICRO_KINDS = ('micro_entry', 'micro_exit')
PAPER_ONLY = ('The micro-trading mode is PAPER only in this build. Live micro trading has not been '
              'authorized, independently reviewed or certified.')
# Other strategies that this build runs in PAPER only, with the reason shown when live is asked for.
PAPER_ONLY_HORIZONS = {'trend': 'The Trend strategy is PAPER only in this build: it has not shown a profit '
                                'after costs on held-back data (docs/CLASSIC_WALKFORWARD.md).'}


def is_micro(*, horizon: str | None = None, kind: str | None = None) -> bool:
    return horizon == MICRO_HORIZON or kind in MICRO_KINDS


def live_refusal(mode: str, *, horizon: str | None = None, kind: str | None = None) -> str | None:
    """The refusal message when a micro horizon or micro order meets any mode but paper; else None."""
    if is_micro(horizon=horizon, kind=kind) and mode != 'paper':
        return PAPER_ONLY
    if horizon in PAPER_ONLY_HORIZONS and mode != 'paper':
        return PAPER_ONLY_HORIZONS[horizon]
    return None
