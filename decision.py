"""Decision records: what the engine decided, from which data, and why.

A decision is a pure function of its inputs (the last completed candles'
indicator row, the confirmation regime, the quote just observed, settings
and an explicit ``now``). It never reads the clock or the network, so the
same recorded inputs replay to the same decision, including the same
decision id. Every scan produces one per symbol, abstentions included: a
no-trade is a successful result whenever edge, freshness, liquidity or risk
is insufficient, and the record says which.

Modules by responsibility: features are :func:`strategy.indicators`, the
signal is :func:`strategy.entry_signal`, regime detection is
:func:`strategy.regime_signal`, confidence lives here, risk gating is
:mod:`risk`, and the execution policy (market orders bounded by the
anti-chasing rule) is :func:`strategy.entry_price_ok` plus :mod:`orders`.
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass, field
from typing import Any

from strategy import MIN_EFFICIENCY, entry_price_ok, entry_signal, setup_quality

DECISION_VERSION = 'entry-v1'

# Status strings are shown in the dashboard and asserted by tests; keep them stable.
STATUS_LOW_VOLUME = 'Low or unknown 24h quote volume'
STATUS_WIDE_SPREAD = 'Wide or unknown bid-ask spread'
STATUS_MOVED = 'Market price moved too far from the completed signal candle'
STATUS_STALE = 'Quote is stale; entry withheld'
STATUS_HELD = 'Position already held'
STATUS_EXITED = 'Exited this candle; no re-entry'


def _finite(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) and not isinstance(value, bool) else None


@dataclass(frozen=True)
class Decision:
    decision_id: str
    symbol: str
    decided_at: float
    action: str                               # enter | hold | abstain | exit
    status: str                               # one-line, dashboard-safe reason
    reasons: tuple[str, ...]                  # every failed check, in evaluation order
    checks: tuple[dict, ...]                  # every check: name, passed, value, threshold
    score: int                                # share of strategy checks passed (0-100)
    confidence: float | None                  # heuristic 0..1; None when data was insufficient
    inputs: dict                              # data timestamps and observed values used
    proposal: dict | None = None              # for enter: price, atr, signal_close, quality
    version: str = DECISION_VERSION
    notes: tuple[str, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict:
        return asdict(self)


def _decision_id(symbol: str, decided_at: float, action: str, inputs: dict) -> str:
    canonical = json.dumps({'symbol': symbol, 'at': round(float(decided_at), 3), 'action': action,
                            'inputs': inputs, 'version': DECISION_VERSION}, sort_keys=True, default=str)
    return 'dec_' + hashlib.sha256(canonical.encode('utf8')).hexdigest()[:16]


def confidence_estimate(last, *, regime_ok: bool, spread_pct: float | None, max_spread_pct: float,
                        quote_age_s: float | None, max_age_s: float, score: int) -> float | None:
    """Transparent 0..1 ranking heuristic, not a calibrated probability.

    Starts from the share of strategy checks that passed and discounts for
    weak trend efficiency, thin relative volume, a wide spread and an ageing
    quote. Reported with every decision so a reviewer can see why one setup
    outranked another; it must never be read as a win probability.
    """
    try:
        efficiency = float(last['efficiency'])
        relative_volume = float(last['volume']) / float(last['vol_avg20'])
    except (TypeError, ValueError, KeyError, ZeroDivisionError):
        return None
    if not (math.isfinite(efficiency) and math.isfinite(relative_volume)):
        return None
    value = max(0.0, min(1.0, score / 100))
    # Efficiency between the filter floor and 0.6 scales 0.5..1; below the floor is already blocked.
    value *= 0.5 + 0.5 * max(0.0, min(1.0, (efficiency - MIN_EFFICIENCY) / (0.6 - MIN_EFFICIENCY)))
    value *= 0.8 + 0.2 * max(0.0, min(1.0, (relative_volume - 1.0)))
    if spread_pct is not None and max_spread_pct > 0:
        value *= 1.0 - 0.3 * max(0.0, min(1.0, spread_pct / max_spread_pct))
    if quote_age_s is not None and max_age_s > 0:
        value *= 1.0 - 0.2 * max(0.0, min(1.0, quote_age_s / max_age_s))
    if not regime_ok:
        value *= 0.5
    return round(max(0.0, min(1.0, value)), 4)


def evaluate_entry(*, symbol: str, last, regime_ok: bool, market_ok: bool | None, rapid: bool, filters: bool,
                   price: float, volume: float, spread_pct: float | None, quote_age_s: float | None,
                   quote_ts: float | None, cfg: dict, now: float, held: bool = False,
                   exited_this_candle: bool = False, higher_candle_ts: int | None = None,
                   higher_error: str | None = None, signal_fn=None, price_ok_fn=None,
                   quality_fn=None) -> Decision:
    """Entry decision for one symbol from already-computed, completed-candle features.

    ``last`` is the indicator row of the last *completed* primary candle;
    ``price`` the executable ask observed moments ago; ``quote_age_s`` its age
    at ``now``. Returns ``enter`` only when every strategy, liquidity, spread,
    anti-chasing and freshness check passes and the symbol is neither held
    nor just exited. The risk gate still has the final word at order time.
    ``signal_fn``, ``price_ok_fn`` and ``quality_fn`` default to the strategy
    module's functions; the engine passes its own references so tests that
    patch them keep working.
    """
    signal_fn = signal_fn or entry_signal
    price_ok_fn = price_ok_fn or entry_price_ok
    quality_fn = quality_fn or setup_quality
    checks: list[dict] = []
    reasons: list[str] = []

    def check(name: str, passed: bool, reason: str, **values):
        checks.append({'check': name, 'passed': bool(passed), **values})
        if not passed:
            reasons.append(reason)

    signal_ts = _finite(last['timestamp']) if 'timestamp' in last else None
    signal_close, atr = _finite(last.get('close')), _finite(last.get('atr'))
    inputs = {'signal_candle_ts_ms': int(signal_ts) if signal_ts else None,
              'higher_candle_ts_ms': higher_candle_ts, 'quote_ts': quote_ts, 'quote_age_s': quote_age_s,
              'price': price, 'volume': volume, 'spread_pct': spread_pct, 'regime_ok': bool(regime_ok),
              'market_ok': market_ok, 'rapid': rapid, 'filters': filters,
              'limits': {'min_quote_volume': cfg['min_quote_volume'], 'max_spread_pct': cfg['max_spread_pct'],
                         'data_max_age_seconds': cfg['data_max_age_seconds']}}
    if higher_error:
        signal, status, score = False, f'Higher-timeframe data unavailable: {higher_error}', 0
        check('higher_timeframe_data', False, status)
    else:
        signal, status, score = signal_fn(last, regime_ok=regime_ok, rapid=rapid, market_ok=market_ok, filters=filters)
        check('strategy_signal', signal, status, score=score)
    liquid = volume >= cfg['min_quote_volume']
    check('liquidity', liquid, STATUS_LOW_VOLUME, value=volume, threshold=cfg['min_quote_volume'])
    spread_ok = spread_pct is not None and spread_pct <= cfg['max_spread_pct']
    check('spread', spread_ok, STATUS_WIDE_SPREAD, value=spread_pct, threshold=cfg['max_spread_pct'])
    close_enough = bool(signal_close and atr and price_ok_fn(price, signal_close, atr))
    check('anti_chasing', close_enough, STATUS_MOVED, price=price, signal_close=signal_close, atr=atr)
    fresh = quote_age_s is not None and 0 <= quote_age_s <= cfg['data_max_age_seconds'] and _finite(price) and price > 0
    check('quote_fresh', fresh, STATUS_STALE, age_s=quote_age_s, threshold=cfg['data_max_age_seconds'])
    check('not_held', not held, STATUS_HELD)
    check('no_reentry_same_candle', not exited_this_candle, STATUS_EXITED)
    # Dashboard status precedence, unchanged from the previous inline logic.
    if not liquid:
        status = STATUS_LOW_VOLUME
    elif not spread_ok:
        status = STATUS_WIDE_SPREAD
    if signal and liquid and spread_ok and not close_enough:
        status = STATUS_MOVED
    enter = signal and liquid and spread_ok and close_enough and fresh and not held and not exited_this_candle
    if held:
        action = 'hold'
    elif enter:
        action = 'enter'
    else:
        action = 'abstain'
    confidence = confidence_estimate(last, regime_ok=regime_ok, spread_pct=spread_pct,
                                     max_spread_pct=cfg['max_spread_pct'], quote_age_s=quote_age_s,
                                     max_age_s=cfg['data_max_age_seconds'], score=score) if not higher_error else None
    proposal = None
    if action == 'enter':
        proposal = {'price': price, 'atr': atr, 'signal_close': signal_close, 'quality': quality_fn(last),
                    'candle_ts_ms': int(signal_ts) if signal_ts else None, 'volume': volume, 'spread_pct': spread_pct}
    return Decision(decision_id=_decision_id(symbol, now, action, inputs), symbol=symbol, decided_at=now,
                    action=action, status=status, reasons=tuple(reasons), checks=tuple(checks), score=int(score),
                    confidence=confidence, inputs=inputs, proposal=proposal)


def evaluate_exit(position: dict, price: float, *, atr: float | None = None, trend_broken: bool = False,
                  dust: bool = False, trailing_multiple: float = 2.5) -> dict:
    """Exit policy for one tracked position at an observed executable bid.

    Returns ``{'action': 'exit'|'hold'|'skip', 'reason': str, 'highest': float,
    'stop': float}`` where ``highest``/``stop`` are the trailing levels to
    persist (unchanged unless ``atr`` is supplied). The order of checks is the
    one the engine has always used: a stored stop or target first, then the
    updated trailing stop, then a completed-candle trend reversal. A verified
    sub-minimum remainder is skipped (``skip``) rather than ordered.
    """
    highest, stop, target = float(position['highest']), float(position['stop']), float(position['target'])
    if dust:
        return {'action': 'skip', 'reason': 'Below exchange minimum; retained', 'highest': highest, 'stop': stop}
    if price <= stop:
        return {'action': 'exit', 'reason': 'Stored stop / trailing stop (software monitored)', 'highest': highest, 'stop': stop}
    if price >= target:
        return {'action': 'exit', 'reason': 'Stored profit target (software monitored)', 'highest': highest, 'stop': stop}
    new_highest = max(highest, price)
    new_stop = max(stop, new_highest - trailing_multiple * atr) if atr and math.isfinite(atr) and atr > 0 else stop
    if atr is not None and price <= new_stop:
        return {'action': 'exit', 'reason': 'Updated trailing stop (software monitored)', 'highest': new_highest, 'stop': new_stop}
    if trend_broken:
        return {'action': 'exit', 'reason': 'Completed-candle trend reversal', 'highest': new_highest, 'stop': new_stop}
    return {'action': 'hold', 'reason': '', 'highest': new_highest, 'stop': new_stop}


class DecisionLog:
    """Bounded in-memory history of decisions for the dashboard and replay checks."""

    def __init__(self, limit: int = 600):
        from collections import deque
        self.limit = limit
        self._items: Any = deque(maxlen=limit)
        self.latest: dict[str, Decision] = {}

    def add(self, decision: Decision) -> None:
        self._items.append(decision)
        self.latest[decision.symbol] = decision

    def recent(self, symbol: str | None = None, limit: int = 50) -> list[dict]:
        items = [d for d in reversed(self._items) if symbol is None or d.symbol == symbol]
        return [d.as_dict() for d in items[:max(1, min(limit, self.limit))]]

    def __len__(self) -> int:
        return len(self._items)
