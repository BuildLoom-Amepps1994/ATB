"""Features, regime, entry candidates and exit policy for the paper-only micro mode.

A micro cycle buys at most a few USDT of one spot pair and sells it within
minutes. At that size the round trip (two taker fees, the spread, a latency
margin and a minimum net target) is a large multiple of a typical one-minute
move, so this module's main job is to abstain. Every function is pure:
candles, the book summary, the sizing result, the manager's gates and ``now``
are explicit inputs; nothing reads the clock, the network, the disk or the
settings, and the same inputs give the same decision and the same id.

The two entry candidates are hypotheses to be measured in paper trading, not
proven strategies. Nothing here is a probability or an expected value. The
only ranking number, ``heuristic_rank`` in an entry proposal, is the expected
range divided by the required move; it orders candidates and is not
calibrated against outcomes.

Features (``FEATURE_VERSION``), from completed bars only. The caller drops the
open candle (``strategy.frame_from_ohlcv``) and the last row passed is the
decision bar. Only the newest ``WINDOW_1M`` / ``WINDOW_5M`` rows are read, and
5m bars that close after the decision bar closes are ignored, so neither
older history nor a newer higher-timeframe bar can change a decision. A 5m
frame whose newest usable bar closed more than one 5m bar before the decision
bar closed is a feed that stopped, not a slower one: no features
(``STALE: ...``). ``Features`` refuses non-finite or mistyped fields, so a
record rebuilt by other code cannot carry a NaN past a ``>`` test.

* EMAs: ``ewm(span, adjust=False)`` seeded at the first row read; 9 and 21 on
  1m closes, 20 and 50 on 5m closes.
* ``atr_bps``: the simple mean of the last 14 true ranges (``max(high - low,
  |high - previous close|, |low - previous close|)``), not Wilder's smoothing,
  so it depends on the last 15 bars only; bps of the close.
* ``vol_bps``: population standard deviation of the last 30 1m log returns.
* ``efficiency``: Kaufman, ``|close - close 20 bars earlier|`` over the sum of
  the 20 absolute one-bar changes; 0 when that path is 0.
* ``zscore``: ``(close - mean20) / std20`` with the population std; 0 when the
  std is 0. ``volume_ratio``: last volume over the 20-bar mean including it
  (0 when no volume traded). ``drop_bps``: close against the highest close of
  the last 15 bars (<= 0). ``down_bars``: consecutive strictly lower closes at
  the end. ``slope_bps``: ema_slow change over 5 bars; ``higher_slope_bps``:
  5m ema50 change over 6 bars; both in bps of the latest close of that frame.
* ``last_change_bps``: the decision bar's close against the previous close.
  It is not in the contract's field list; it exists because "the last bar
  closed up" cannot be read from ``down_bars`` (an unchanged close is not
  lower either). It defaults to None (unknown), which never counts as up.

Regime (``classify_regime``), first match wins:

1. ``stale_uncertain``: the data is not fresh, or there are no features.
2. ``illiquid``: depth is not confirmed, or the spread is unknown or above
   ``params.max_spread_bps``.
3. ``excessively_volatile``: ``vol_bps > params.max_vol_bps``.
4. ``trending_down``: a collapse (``drop_bps <= -4 * atr_bps``), or
   ``efficiency >= params.min_efficiency_trend`` with ``slope_bps < 0``. The
   collapse test runs before the up-trend test: a sharp drop outranks a
   still-rising slow EMA.
5. ``trending_up``: ``efficiency >= params.min_efficiency_trend``,
   ``slope_bps > 0`` and the 5m trend is up.
6. ``ranging``: ``efficiency <= params.max_efficiency_range`` and
   ``|higher_slope_bps| <= 10``.
7. ``no_reliable_edge``: anything else.

Entry (``evaluate_entry``) records every check, abstains when any fails, and
names one dominant code by ``CODE_PRIORITY``: who may trade (REVIEW, RISK,
NOT_SELECTED, CAPACITY, COOLDOWN), then whether the data can be trusted
(STALE_DATA, WARMUP), then whether the pair can be traded at this size
(SPREAD, INSUFFICIENT_DEPTH, BUDGET_MINIMUM), then the market (VOLATILITY,
WEAK_TREND), then the economics (FEES, NO_ECONOMIC_EDGE), and ERROR last.
WEAK_TREND covers both a regime that offers no candidate and a candidate
whose own conditions are not met on this bar. A gate that was not supplied is
unknown and fails; it is never read as a pass, and a block value that is
neither None, False nor a reason string (a NaN, a number) is a block.

Entry checks beyond the contract's list, each failing closed:

* the decision bar has closed by ``now`` (an open candle is ERROR) and closed
  at most ``MAX_BAR_AGE_S`` before it (else STALE_DATA). ``now`` is on the
  candle clock, the venue's time, as the candle cut-off is.
* the sizing result names this symbol, and an eligible one keeps
  ``max_debit = gross_quote + fee_reserve <= target_budget <= budget_cap <=
  MICRO_MAX_ALL_IN_USDT`` (else ERROR): the proposal is scaled from that cap.
* the spread used is the larger of the reported one and the one the best bid
  and ask imply.
* ``stop_beyond_entry_cost``: selling at the best bid the moment the position
  is bought already loses the round-trip cost (``max_debit * (1 - best_bid /
  break_even_bid)``). The contract's ``clamp(2 ATR, 25, max_loss)`` stop does
  not count it, so a stop inside it fires on the first exit check with the
  book unchanged. The stop must sit at least ``STOP_MIN_ROOM_ATR`` ATR beyond
  that cost (else NO_ECONOMIC_EDGE).

Exit (``evaluate_exit``): safety exits (stop loss, maximum hold, regime shift)
need no profit, no entry signal and no depth; profit exits (take profit,
profit protection) need an estimate from a walk of the bid book, which the
estimate itself must state (``depth_model == 'book'`` and ``depth_exhausted is
False``; a missing flag is not depth evidence).
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass, fields
from decimal import Decimal

import numpy as np
import pandas as pd

import money
import venue_rules

FEATURE_VERSION = 'micro-features-v1'
DECISION_VERSION = 'micro-entry-v1'
WARMUP_BARS_1M = 60
WARMUP_BARS_5M = 50
WINDOW_1M = 120          # EMA21's seed weighs (20/22)**119, about 1e-5, at this length
WINDOW_5M = 150          # EMA50's seed weighs (49/51)**149, about 0.3%
BAR_MS_1M, BAR_MS_5M = 60_000, 300_000
OHLCV = ('timestamp', 'open', 'high', 'low', 'close', 'volume')

# Reason codes (dashboard counters).
WARMUP = 'WARMUP'
STALE_DATA = 'STALE_DATA'
SPREAD = 'SPREAD'
INSUFFICIENT_DEPTH = 'INSUFFICIENT_DEPTH'
BUDGET_MINIMUM = 'BUDGET_MINIMUM'
VOLATILITY = 'VOLATILITY'
WEAK_TREND = 'WEAK_TREND'
NO_ECONOMIC_EDGE = 'NO_ECONOMIC_EDGE'
FEES = 'FEES'
COOLDOWN = 'COOLDOWN'
CAPACITY = 'CAPACITY'
NOT_SELECTED = 'NOT_SELECTED'
REVIEW = 'REVIEW'
RISK = 'RISK'
ERROR = 'ERROR'
CODE_PRIORITY = (REVIEW, RISK, NOT_SELECTED, CAPACITY, COOLDOWN, STALE_DATA, WARMUP, SPREAD,
                 INSUFFICIENT_DEPTH, BUDGET_MINIMUM, VOLATILITY, WEAK_TREND, FEES, NO_ECONOMIC_EDGE, ERROR)
STATUS_LABELS = {REVIEW: 'Review pending', RISK: 'Risk block', NOT_SELECTED: 'Not selected',
                 CAPACITY: 'No free cycle', COOLDOWN: 'Cooling down', STALE_DATA: 'Data not fresh',
                 WARMUP: 'Warming up', SPREAD: 'Spread too wide', INSUFFICIENT_DEPTH: 'Book too thin',
                 BUDGET_MINIMUM: 'Budget below venue minimum', VOLATILITY: 'Too volatile',
                 WEAK_TREND: 'No setup', FEES: 'Costs too high', NO_ECONOMIC_EDGE: 'No economic edge',
                 ERROR: 'Cannot evaluate'}

# Sizing codes (micro_economics) mapped onto the strategy's codes; anything
# else, venue refusals included, is ERROR with the sizing code kept in the check.
SIZING_CODES = {'BUDGET_BELOW_VENUE_MINIMUM': BUDGET_MINIMUM, 'EXIT_BELOW_VENUE_MINIMUM': BUDGET_MINIMUM,
                'CASH_INSUFFICIENT': BUDGET_MINIMUM, 'ALLOCATION_EXHAUSTED': BUDGET_MINIMUM,
                'INSUFFICIENT_ASK_DEPTH': INSUFFICIENT_DEPTH, 'INSUFFICIENT_BID_DEPTH': INSUFFICIENT_DEPTH,
                'DEPTH_UNKNOWN': INSUFFICIENT_DEPTH, 'PRICE_UNKNOWN': STALE_DATA,
                venue_rules.METADATA_STALE: STALE_DATA, 'FEE_UNKNOWN': FEES}

REGIMES = ('stale_uncertain', 'illiquid', 'excessively_volatile', 'trending_up', 'trending_down',
           'ranging', 'no_reliable_edge')
CANDIDATES = {'trending_up': 'momentum_pullback', 'ranging': 'range_reversion'}

# Fixed regime reasons that evaluate_entry reads back to tell staleness from
# warmup and thin depth from a wide spread.
REASON_NOT_FRESH = 'market data is not fresh'
REASON_NO_FEATURES = 'features unavailable'
REASON_DEPTH = 'order-book depth is insufficient or unknown'
REASON_SPREAD_UNKNOWN = 'spread is unknown'

# Rule thresholds (contract values).
COLLAPSE_ATR = 4.0                   # regime: drop of 4 ATR from the 15-bar high is a collapse
RANGE_MAX_ABS_HIGHER_SLOPE_BPS = 10.0
MOMENTUM_MAX_ZSCORE = 0.5
MOMENTUM_PULLBACK_ATR = 1.0
MOMENTUM_COLLAPSE_ATR = 3.0
RANGE_MAX_ZSCORE = -1.5
RANGE_MAX_DOWN_BARS = 4
RANGE_COLLAPSE_ATR = 4.0
RANGE_MIN_HIGHER_SLOPE_BPS = -5.0
STOP_ATR_MULTIPLE = 2.0
STOP_FLOOR_BPS = 25.0
FEES_SPREAD_SHARE = 0.35             # the spread may be at most this share of the required move
PROTECT_PEAK_SHARE = Decimal('0.75')
PROTECT_FLOOR_SHARE = Decimal('0.25')
AMOUNT_STEP = Decimal('0.00000001')  # proposal amounts: target rounded up, stop rounded down
# micro_economics.MICRO_MAX_ALL_IN_USDT, repeated here so that a defective or
# stale sizing record cannot scale a proposal above the absolute micro cap.
MICRO_MAX_ALL_IN_USDT = Decimal('25')
MAX_BAR_AGE_S = 2 * BAR_MS_1M / 1000  # the decision bar closed at most two 1m bars before now
STOP_MIN_ROOM_ATR = 1.0              # the stop sits at least one ATR beyond the loss carried at entry
MAX_RECORD_INT = 2 ** 63 - 1         # a larger integer is not stored in a decision record

# MicroParams ranges: the contract's settings bounds where a setting exists
# (target 5..200 bps, loss cap 20..400 bps, hold 2..240 min); elsewhere the
# range beyond which a threshold in effect switches its gate off. A None
# minimum means "above zero".
PARAM_LIMITS = {'target_net_bps': (5.0, 200.0), 'max_loss_bps': (20.0, 400.0), 'max_hold_s': (120.0, 14_400.0),
                'uncertainty_floor_bps': (0.0, 100.0), 'max_spread_bps': (None, 100.0),
                'max_vol_bps': (None, 1000.0), 'min_efficiency_trend': (0.0, 1.0),
                'max_efficiency_range': (0.0, 1.0), 'edge_ratio': (None, 1.0)}

GATE_KEYS = ('selected', 'capacity', 'cooldown_remaining_s', 'fresh_after_exit', 'review_block', 'risk_block',
             'latched')
BLOCK_KEYS = ('review_block', 'risk_block', 'latched')
BOOK_KEYS = ('best_bid', 'best_ask', 'spread_bps', 'generation', 'sequence', 'age_s', 'source')
SIZING_KEYS = ('symbol', 'eligible', 'code', 'detail', 'budget_cap', 'target_budget', 'gross_quote', 'fee_reserve',
               'max_debit', 'expected_base', 'sellable_base', 'entry_vwap', 'break_even_bid', 'target_bid',
               'required_move_bps', 'minimum_feasible_budget')
EXIT_TRIGGERS = ('stop_loss', 'max_hold', 'regime_shift', 'take_profit', 'profit_protection')


# --------------------------------------------------------------------- helpers
def _number(value) -> float | None:
    """A finite float, or None when the value is missing, boolean or not a finite number."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str):
        try:
            value = money.parse(value, field='number', allow_negative=True)
        except money.MoneyError:
            return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _decimal(value) -> Decimal | None:
    if value is None:
        return None
    try:
        return money.parse(value, field='amount', allow_negative=True)
    except money.MoneyError:
        return None


def _plain(value):
    """A JSON-safe, bounded copy: Decimals as text, containers recursively.

    A non-finite number, a Decimal ``money.parse`` refuses (a huge exponent
    would expand into an unbounded digit string) and an integer beyond
    ``MAX_RECORD_INT`` become None (unknown). Other objects are kept as one
    bounded line of text.
    """
    if isinstance(value, np.generic):
        value = value.item()
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, int):
        return value if abs(value) <= MAX_RECORD_INT else None
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Decimal):
        try:
            return money.text(money.parse(value, field='record value', allow_negative=True))
        except money.MoneyError:
            return None
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return _sentence(value, 120)


def _gate_value(key: str, value):
    """A gate value as recorded. A block that is neither None, False nor a reason
    string is an unreadable block, and stays a block after the JSON round trip
    (``_plain`` would turn a NaN into None, which reads as "no block")."""
    if isinstance(value, np.generic):
        value = value.item()
    if key in BLOCK_KEYS and not (value is None or isinstance(value, (bool, str))):
        return f'unreadable {type(value).__name__} value'
    return _plain(value)


def _reason_code(reason: str) -> str:
    """The code of a ``compute_features`` reason: ``ERROR: ...``, ``STALE: ...`` or a warmup."""
    if reason.startswith('ERROR'):
        return ERROR
    if reason.startswith('STALE'):
        return STALE_DATA
    return WARMUP


def _sentence(text, limit: int = 240) -> str:
    """One dashboard line: no line breaks, bounded length."""
    flat = ' '.join(str(text).split())
    return flat if len(flat) <= limit else flat[:limit - 1] + '…'


# -------------------------------------------------------------------- features
@dataclass(frozen=True)
class Features:
    symbol: str
    bar_ts_ms: int
    bars_1m: int
    bars_5m: int
    close: float
    ema_fast: float          # EMA 9 of 1m closes
    ema_slow: float          # EMA 21 of 1m closes
    atr_bps: float           # 14-bar simple-mean ATR on 1m, bps of close
    vol_bps: float           # population std of 30 1m log returns, bps
    efficiency: float        # 20-bar Kaufman efficiency on 1m
    slope_bps: float         # ema_slow change over 5 bars, bps of close
    zscore: float            # (close - SMA20) / std20 on 1m
    volume_ratio: float      # last bar volume / 20-bar mean
    drop_bps: float          # close vs highest close of the last 15 bars, bps (<= 0)
    down_bars: int           # consecutive strictly lower closes at the end
    higher_trend_up: bool    # 5m ema20 > ema50 and 5m close > ema50
    higher_slope_bps: float  # 5m ema50 change over 6 bars, bps of the 5m close
    last_change_bps: float | None = None   # decision bar close vs previous close; None is unknown

    def __post_init__(self):
        # compute_features only builds finite features, but a Features rebuilt
        # from a record or by other code is not filtered by it. A NaN fails
        # every comparison, so "vol_bps > max_vol_bps" would read as calm.
        if not isinstance(self.symbol, str):
            raise ValueError('symbol must be a string')
        for name in ('bar_ts_ms', 'bars_1m', 'bars_5m', 'down_bars'):
            value = getattr(self, name)
            if isinstance(value, (bool, np.bool_)) or not isinstance(value, (int, np.integer)) or value < 0:
                raise ValueError(f'{name} must be a non-negative integer')
            object.__setattr__(self, name, int(value))
        if self.bar_ts_ms <= 0:
            raise ValueError('bar_ts_ms must be a positive time')
        if not isinstance(self.higher_trend_up, (bool, np.bool_)):
            raise ValueError('higher_trend_up must be a boolean')
        object.__setattr__(self, 'higher_trend_up', bool(self.higher_trend_up))
        for name in ('close', 'ema_fast', 'ema_slow', 'atr_bps', 'vol_bps', 'efficiency', 'slope_bps', 'zscore',
                     'volume_ratio', 'drop_bps', 'higher_slope_bps', 'last_change_bps'):
            value = getattr(self, name)
            if value is None and name == 'last_change_bps':
                continue
            if isinstance(value, (bool, np.bool_)) or not isinstance(value, (int, float, np.integer, np.floating)) \
                    or not math.isfinite(float(value)):
                raise ValueError(f'{name} must be a finite number')
            object.__setattr__(self, name, float(value))
        for name in ('close', 'ema_fast', 'ema_slow'):
            if getattr(self, name) <= 0:
                raise ValueError(f'{name} must be positive')
        for name in ('atr_bps', 'vol_bps', 'volume_ratio'):
            if getattr(self, name) < 0:
                raise ValueError(f'{name} must not be negative')

    def as_dict(self) -> dict:
        return asdict(self)


def _bars(frame, label: str) -> tuple[dict | None, str | None]:
    """Validated numpy columns of one candle frame, or an ``ERROR: ...`` reason."""
    if frame is None:
        return {name: np.empty(0) for name in OHLCV}, None
    if not isinstance(frame, pd.DataFrame):
        return None, f'ERROR: the {label} candles are not a table'
    missing = [name for name in OHLCV if name not in frame.columns]
    if missing:
        return None, f'ERROR: the {label} candles lack {", ".join(missing)}'
    try:
        columns = {name: frame[name].to_numpy(dtype=float) for name in OHLCV}
    except (TypeError, ValueError):
        return None, f'ERROR: the {label} candles hold non-numeric values'
    if not all(np.isfinite(values).all() for values in columns.values()):
        return None, f'ERROR: the {label} candles hold non-finite values'
    prices = np.stack([columns[name] for name in ('open', 'high', 'low', 'close')])
    if (prices <= 0).any() or (columns['volume'] < 0).any():
        return None, f'ERROR: the {label} candles hold non-positive prices or negative volume'
    if (np.diff(columns['timestamp']) <= 0).any():
        return None, f'ERROR: the {label} candle timestamps are not strictly increasing'
    return columns, None


def _ema(values: np.ndarray, span: int) -> np.ndarray:
    return pd.Series(values).ewm(span=span, adjust=False).mean().to_numpy()


def compute_features(symbol: str, frame_1m, frame_5m) -> tuple[Features | None, str | None]:
    """Features of the last completed 1m bar, or ``(None, 'WARMUP: ...' | 'STALE: ...' | 'ERROR: ...')``."""
    one, problem = _bars(frame_1m, '1m')
    if problem:
        return None, problem
    five, problem = _bars(frame_5m, '5m')
    if problem:
        return None, problem
    if len(one['close']) < WARMUP_BARS_1M:
        return None, f'WARMUP: {len(one["close"])} of {WARMUP_BARS_1M} completed 1m bars'
    # A 5m bar that closes after the decision bar closes is newer information
    # than the decision bar: ignoring it keeps the two frames on one timeline.
    cutoff = one['timestamp'][-1] + BAR_MS_1M
    usable = five['timestamp'] + BAR_MS_5M <= cutoff
    if not usable.all():
        five = {name: values[usable] for name, values in five.items()}
    if len(five['close']) < WARMUP_BARS_5M:
        return None, f'WARMUP: {len(five["close"])} of {WARMUP_BARS_5M} completed 5m bars'
    # The newest usable 5m bar must close within one 5m bar of the decision
    # bar's close; a feed that stopped earlier would set the higher-timeframe
    # trend from hours-old data.
    lag_ms = cutoff - (five['timestamp'][-1] + BAR_MS_5M)
    if lag_ms > BAR_MS_5M:
        return None, (f'STALE: the newest 5m bar closed {lag_ms / 60_000:.0f} min before the decision bar '
                      f'(at most {BAR_MS_5M // 60_000} min)')
    one = {name: values[-WINDOW_1M:] for name, values in one.items()}
    five = {name: values[-WINDOW_5M:] for name, values in five.items()}

    close, high, low, volume = one['close'], one['high'], one['low'], one['volume']
    last = close[-1]
    ema_fast, ema_slow = _ema(close, 9), _ema(close, 21)
    previous = close[:-1]
    true_range = np.maximum.reduce([high[1:] - low[1:], np.abs(high[1:] - previous), np.abs(low[1:] - previous)])
    atr = true_range[-14:].mean()
    returns = np.diff(np.log(close))[-30:]
    changes = np.abs(np.diff(close[-21:]))
    path = changes.sum()
    efficiency = 0.0 if path == 0 else abs(last - close[-21]) / path
    window20 = close[-20:]
    mean20, std20 = window20.mean(), window20.std(ddof=0)
    # Twenty equal closes can leave a rounding-level std; that is still no spread.
    zscore = 0.0 if std20 <= abs(mean20) * 1e-12 else (last - mean20) / std20
    volume_mean = volume[-20:].mean()
    lower = (close[1:] < close[:-1])[::-1]
    down_bars = int(lower.size if lower.all() else np.argmin(lower))

    close5 = five['close']
    ema20_5, ema50_5 = _ema(close5, 20), _ema(close5, 50)
    values = dict(
        close=float(last), ema_fast=float(ema_fast[-1]), ema_slow=float(ema_slow[-1]),
        atr_bps=float(atr / last * 1e4), vol_bps=float(returns.std(ddof=0) * 1e4), efficiency=float(efficiency),
        slope_bps=float((ema_slow[-1] - ema_slow[-6]) / last * 1e4), zscore=float(zscore),
        volume_ratio=float(volume[-1] / volume_mean) if volume_mean > 0 else 0.0,
        drop_bps=float((last / close[-15:].max() - 1) * 1e4),
        higher_slope_bps=float((ema50_5[-1] - ema50_5[-7]) / close5[-1] * 1e4),
        last_change_bps=float((last / close[-2] - 1) * 1e4))
    if not all(math.isfinite(value) for value in values.values()):
        return None, 'ERROR: a feature is not a finite number'
    higher_up = bool(ema20_5[-1] > ema50_5[-1] and close5[-1] > ema50_5[-1])
    return Features(symbol=str(symbol), bar_ts_ms=int(one['timestamp'][-1]), bars_1m=len(close),
                    bars_5m=len(close5), down_bars=down_bars, higher_trend_up=higher_up, **values), None


# ---------------------------------------------------------------------- params
@dataclass(frozen=True)
class MicroParams:
    target_net_bps: float          # minimum meaningful net target, bps of the budget cap
    max_loss_bps: float            # hard cap on estimated net loss per cycle, bps of the budget cap
    max_hold_s: float
    uncertainty_floor_bps: float   # minimum uncertainty margin per exit estimate
    max_spread_bps: float          # entry spread limit
    max_vol_bps: float             # above this 1m volatility the regime is excessively volatile
    min_efficiency_trend: float    # trend regimes need at least this efficiency
    max_efficiency_range: float    # range regime needs at most this efficiency
    edge_ratio: float              # required move must be <= edge_ratio * expected range

    def __post_init__(self):
        # A NaN threshold makes every comparison False, which would read as
        # "not too wide", "not too volatile": refuse it here instead. An
        # absurd finite one (a 1e300 bps spread limit) switches its gate off
        # just as well, and a target beyond money.parse's range would raise
        # on the enter path: every field is bounded by PARAM_LIMITS.
        for item in fields(self):
            value = getattr(self, item.name)
            number = None if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)) else float(value)
            if number is None or not math.isfinite(number):
                raise ValueError(f'{item.name} must be a finite number')
            low, high = PARAM_LIMITS[item.name]
            if (number <= 0 if low is None else number < low) or number > high:
                raise ValueError(f'{item.name} must be {"above 0" if low is None else f"at least {low:g}"} '
                                 f'and at most {high:g}')
            object.__setattr__(self, item.name, number)

    def as_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------- regime
def classify_regime(features: Features | None, *, spread_bps, depth_ok: bool, data_fresh: bool,
                    params: MicroParams) -> tuple[str, tuple[str, ...]]:
    """``(regime, reasons)``; the rules and their order are in the module docstring."""
    if not isinstance(params, MicroParams):
        raise TypeError('params must be MicroParams')
    reasons: list[str] = []
    if data_fresh is not True:
        reasons.append(REASON_NOT_FRESH)
    if not isinstance(features, Features):
        reasons.append(REASON_NO_FEATURES)
    if reasons:
        return 'stale_uncertain', tuple(reasons)
    if depth_ok is not True:
        reasons.append(REASON_DEPTH)
    spread = _number(spread_bps)
    if spread is None or spread < 0:
        reasons.append(REASON_SPREAD_UNKNOWN)
    elif spread > params.max_spread_bps:
        reasons.append(f'spread {spread:.2f} bps is above the {params.max_spread_bps:g} bps limit')
    if reasons:
        return 'illiquid', tuple(reasons)
    f = features
    if f.vol_bps > params.max_vol_bps:
        return 'excessively_volatile', (f'1m volatility {f.vol_bps:.1f} bps is above {params.max_vol_bps:g} bps',)
    strong = f.efficiency >= params.min_efficiency_trend
    if f.drop_bps < 0 and f.drop_bps <= -COLLAPSE_ATR * f.atr_bps:
        return 'trending_down', (f'collapse: {f.drop_bps:.1f} bps from the 15-bar high, '
                                 f'at least {COLLAPSE_ATR:g} ATR ({f.atr_bps:.1f} bps)',)
    if strong and f.slope_bps < 0:
        return 'trending_down', (f'efficiency {f.efficiency:.2f} >= {params.min_efficiency_trend:g}',
                                 f'slow EMA falling {f.slope_bps:.1f} bps over 5 bars')
    if strong and f.slope_bps > 0 and f.higher_trend_up:
        return 'trending_up', (f'efficiency {f.efficiency:.2f} >= {params.min_efficiency_trend:g}',
                               f'slow EMA rising {f.slope_bps:.1f} bps over 5 bars', '5m trend up')
    if f.efficiency <= params.max_efficiency_range and abs(f.higher_slope_bps) <= RANGE_MAX_ABS_HIGHER_SLOPE_BPS:
        return 'ranging', (f'efficiency {f.efficiency:.2f} <= {params.max_efficiency_range:g}',
                           f'5m slope {f.higher_slope_bps:.1f} bps within ±{RANGE_MAX_ABS_HIGHER_SLOPE_BPS:g}')
    return 'no_reliable_edge', (f'efficiency {f.efficiency:.2f}, slow EMA slope {f.slope_bps:.1f} bps, '
                                f'5m slope {f.higher_slope_bps:.1f} bps, 5m trend '
                                f'{"up" if f.higher_trend_up else "not up"}: neither a clean trend nor a range',)


def stop_net_bps(features: Features, params: MicroParams) -> float | None:
    """Stop distance in bps of the budget cap: ``2 * atr_bps`` floored at 25, never above ``max_loss_bps``.

    None when ``2 * atr_bps`` exceeds ``max_loss_bps``: a stop the loss cap
    allows would sit inside ordinary noise. When ``max_loss_bps`` is below the
    25 bps floor the cap wins (the hard cap is never exceeded).
    """
    atr = _number(getattr(features, 'atr_bps', None))
    if atr is None or atr < 0:
        return None
    raw = STOP_ATR_MULTIPLE * atr
    if raw > params.max_loss_bps:
        return None
    return min(max(raw, STOP_FLOOR_BPS), params.max_loss_bps)


# ----------------------------------------------------------------------- entry
@dataclass(frozen=True)
class MicroDecision:
    decision_id: str
    symbol: str
    decided_at: float
    action: str                    # 'enter' | 'abstain'
    code: str | None               # dominant reason code
    status: str                    # one dashboard-safe sentence
    reasons: tuple[str, ...]       # every failed check's detail, in evaluation order
    codes: tuple[str, ...]         # the code of each failed check, parallel to reasons
    checks: tuple[dict, ...]       # every check: check, passed, code, detail, values
    candidate: str | None          # the hypothesis the regime pointed to, entered or not
    regime: str
    inputs: dict                   # everything evaluate_entry read, JSON-safe (replayable)
    proposal: dict | None          # enter only; amounts are decimal strings
    version: str = DECISION_VERSION

    def as_dict(self) -> dict:
        return asdict(self)


def _decision_id(symbol: str, decided_at: float, action: str, inputs: dict) -> str:
    canonical = json.dumps({'symbol': symbol, 'at': round(float(decided_at), 3), 'action': action,
                            'inputs': inputs, 'version': DECISION_VERSION}, sort_keys=True, default=str)
    return 'mdec_' + hashlib.sha256(canonical.encode('utf8')).hexdigest()[:16]


def _dominant_code(codes) -> str | None:
    """The highest-priority code among ``codes`` (unknown codes rank as ERROR)."""
    ranked = [CODE_PRIORITY.index(code) if code in CODE_PRIORITY else len(CODE_PRIORITY) - 1 for code in codes]
    return CODE_PRIORITY[min(ranked)] if ranked else None


def _sizing_amounts(sizing: dict) -> tuple[dict | None, str]:
    """The amounts of an eligible sizing result, or ``(None, why they cannot be used)``.

    The proposal's target and stop are scaled from ``budget_cap``, so the
    contract's all-in invariant is checked here again rather than trusted:
    ``max_debit = gross_quote + fee_reserve <= target_budget <= budget_cap <=
    MICRO_MAX_ALL_IN_USDT``, every amount present and the cap above zero.
    """
    names = ('budget_cap', 'target_budget', 'gross_quote', 'fee_reserve', 'max_debit')
    amounts = {name: _decimal(sizing.get(name)) for name in names}
    missing = [name for name in names if amounts[name] is None]
    if missing:
        return None, f'{", ".join(missing)} missing or unreadable'
    cap, target, debit = amounts['budget_cap'], amounts['target_budget'], amounts['max_debit']
    gross, reserve = amounts['gross_quote'], amounts['fee_reserve']
    if not money.ZERO < cap <= MICRO_MAX_ALL_IN_USDT:
        return None, (f'budget cap {money.text(cap)} is outside the micro all-in cap '
                      f'(0 to {money.text(MICRO_MAX_ALL_IN_USDT)} USDT)')
    if gross <= 0 or reserve < 0 or money.add(gross, reserve) != debit:
        return None, (f'max debit {money.text(debit)} is not gross {money.text(gross)} plus fee reserve '
                      f'{money.text(reserve)}')
    if not debit <= target <= cap:
        return None, (f'max debit {money.text(debit)} <= target {money.text(target)} <= cap {money.text(cap)} '
                      'does not hold')
    return amounts, ''


def _stop_room(check, stop_bps: float, atr_bps: float, *, budget_cap: Decimal, max_debit: Decimal, break_even,
               best_bid) -> None:
    """Record ``stop_beyond_entry_cost``: the stop must clear the loss carried at entry by one ATR.

    The estimated net of selling ``sellable`` at exit bid ``X`` is linear in
    ``X`` and zero at the break-even bid, so selling at the current best bid
    the moment the position is bought nets ``-max_debit * (1 - best_bid /
    break_even_bid)``: fees, latency and the spread of both legs. A stop
    smaller than that fires on the first exit check with the book unchanged.
    """
    even, bid = _decimal(break_even), _decimal(best_bid)
    if even is None or even <= 0 or bid is None or bid <= 0:
        check('stop_beyond_entry_cost', False, ERROR, 'break-even bid unknown: the loss carried at entry cannot be '
              'measured', break_even_bid=break_even)
        return
    carried = money.mul(max_debit, money.sub(money.ONE, money.div(bid, even))) if even > bid else money.ZERO
    carried_bps = float(money.mul(money.div(carried, budget_cap), money.BPS))
    room, need = stop_bps - carried_bps, STOP_MIN_ROOM_ATR * atr_bps
    check('stop_beyond_entry_cost', room >= need, NO_ECONOMIC_EDGE,
          f'stop {stop_bps:.1f} bps leaves {room:.1f} bps beyond the {carried_bps:.1f} bps lost at entry (needs '
          f'{need:.1f} bps, {STOP_MIN_ROOM_ATR:g} ATR)', stop_net_bps=stop_bps, entry_cost_bps=carried_bps,
          room_bps=room, min_room_bps=need, break_even_bid=even, best_bid=bid)


def evaluate_entry(*, symbol, features, regime, regime_reasons, sizing: dict, book_summary: dict,
                   params: MicroParams, gates: dict, now: float, feature_reason: str | None = None) -> MicroDecision:
    """Entry decision for one symbol on its last completed bar.

    Returns ``enter`` only when every gate, data, liquidity, sizing, regime,
    candidate and economic check passes. ``feature_reason`` (optional, an
    addition to the contract) is the second value of :func:`compute_features`:
    when features are missing it tells a warmup (WARMUP) from a stale 5m feed
    (STALE_DATA) and unusable candles (ERROR); without it a missing feature set
    is recorded as WARMUP. ``now`` is on the candle clock (the venue's time,
    as the candle cut-off is): the decision bar must have closed by then. The
    manager's risk gate still has the final word at order time.
    """
    if not isinstance(params, MicroParams):
        raise TypeError('params must be MicroParams')
    if _number(now) is None:
        raise ValueError('now must be a finite time')
    symbol = str(symbol)
    bad_features = features is not None and not isinstance(features, Features)
    inputs = {
        'feature_version': FEATURE_VERSION,
        'features': _plain(features.as_dict()) if isinstance(features, Features) else None,
        'feature_reason': _sentence(feature_reason) if feature_reason is not None else (
            'ERROR: features have an unexpected type' if bad_features else None),
        'regime': str(regime) if regime is not None else None,
        'regime_reasons': [str(reason) for reason in
                           ((regime_reasons,) if isinstance(regime_reasons, str) else (regime_reasons or ()))],
        'book': {key: _plain(book_summary[key]) for key in BOOK_KEYS if key in book_summary}
        if isinstance(book_summary, dict) else None,
        'sizing': {key: _plain(sizing[key]) for key in SIZING_KEYS if key in sizing}
        if isinstance(sizing, dict) else None,
        'gates': {key: _gate_value(key, gates[key]) for key in GATE_KEYS if key in gates}
        if isinstance(gates, dict) else {},
        'params': params.as_dict(),
    }
    # Everything below reads the JSON-safe inputs only, so a decision rebuilt
    # from its own record is the same decision with the same id.
    features = features if isinstance(features, Features) else None
    return _decide(symbol, float(now), inputs, features, params)


def _decide(symbol: str, now: float, inputs: dict, features: Features | None, params: MicroParams) -> MicroDecision:
    checks: list[dict] = []

    def check(name: str, passed: bool, code: str, detail: str, **values):
        checks.append({'check': name, 'passed': bool(passed), 'code': None if passed else code,
                       'detail': _sentence(detail), **{key: _plain(value) for key, value in values.items()}})

    gates, book, sizing = inputs['gates'], inputs['book'] or {}, inputs['sizing']
    regime, regime_reasons = inputs['regime'], inputs['regime_reasons']

    # ---- who may trade (decided by the manager; an absent gate is unknown and fails)
    def block(key: str, name: str, code: str, label: str):
        if key not in gates:
            check(name, False, code, f'{label} state was not supplied')
        elif gates[key]:
            reason = gates[key] if isinstance(gates[key], str) else 'active'
            check(name, False, code, f'{label}: {reason}')
        else:
            check(name, True, code, f'no {label.lower()}')

    block('review_block', 'review', REVIEW, 'Review block')
    block('risk_block', 'risk', RISK, 'Risk block')
    block('latched', 'latch', RISK, 'Safety latch')
    check('selected', gates.get('selected') is True, NOT_SELECTED,
          'selected for this scan' if gates.get('selected') is True else 'not selected for this scan')
    capacity = gates.get('capacity')
    has_capacity = capacity is True or (_number(capacity) is not None and _number(capacity) > 0)
    check('capacity', has_capacity, CAPACITY, 'a cycle slot is free' if has_capacity else
          ('cycle capacity was not supplied' if capacity is None else 'every micro cycle slot is in use'),
          value=capacity)
    remaining = _number(gates.get('cooldown_remaining_s'))
    check('cooldown', remaining is not None and remaining <= 0, COOLDOWN,
          'cooldown state was not supplied' if remaining is None else
          (f'cooldown {remaining:.0f} s remaining' if remaining > 0 else 'no cooldown'), value=remaining)
    fresh_bar = gates.get('fresh_after_exit') is True
    check('fresh_after_exit', fresh_bar, COOLDOWN, 'a completed bar is newer than the last exit' if fresh_bar
          else 'no completed bar since the last exit')

    # ---- can the data be trusted
    feature_reason = inputs['feature_reason'] or ''
    supplied = features is not None
    if features is None:
        check('features', False, _reason_code(feature_reason),
              feature_reason or 'features unavailable (warmup or unusable candles)')
    elif features.symbol != symbol:
        check('features', False, ERROR, f'features were computed for {features.symbol}, not {symbol}')
        features = None
    else:
        # The decision bar must be complete at ``now`` (no look-ahead into an
        # open candle) and recent (a bar hours old is not this market).
        closes_ms = features.bar_ts_ms + BAR_MS_1M
        age_s = now - closes_ms / 1000
        timing = dict(bar_ts_ms=features.bar_ts_ms, bar_close_ms=closes_ms, bar_age_s=age_s)
        if age_s < 0:
            check('features', False, ERROR, f'the decision bar closes {-age_s:.1f} s after now: an open candle is '
                  'not a completed bar', **timing)
            features = None
        elif age_s > MAX_BAR_AGE_S:
            check('features', False, STALE_DATA, f'the decision bar closed {age_s:.0f} s ago (at most '
                  f'{MAX_BAR_AGE_S:g} s)', **timing)
            features = None
        else:
            check('features', True, WARMUP, f'{features.bars_1m} 1m and {features.bars_5m} 5m completed bars; '
                  f'the decision bar closed {age_s:.1f} s ago', **timing)
    bid, ask = _number(book.get('best_bid')), _number(book.get('best_ask'))
    quote_ok = bid is not None and ask is not None and 0 < bid < ask
    check('book', quote_ok, STALE_DATA, 'best bid below best ask' if quote_ok else 'no valid best bid and ask',
          best_bid=book.get('best_bid'), best_ask=book.get('best_ask'), generation=book.get('generation'),
          age_s=book.get('age_s'))

    # ---- can the pair be traded at this size
    # A reported spread narrower than the best bid and ask imply is not
    # believed: the larger of the two is the spread every check below uses.
    reported, quoted = _number(book.get('spread_bps')), None
    if quote_ok:
        bid_d, ask_d = _decimal(book.get('best_bid')), _decimal(book.get('best_ask'))
        if bid_d is not None and ask_d is not None:
            mid = money.div(money.add(bid_d, ask_d), Decimal(2))
            quoted = float(money.mul(money.div(money.sub(ask_d, bid_d), mid), money.BPS))
    if reported is None or reported < 0:
        check('spread', False, SPREAD, 'spread is unknown', value=reported, from_quote=quoted)
        spread = None
    else:
        spread = reported if quoted is None else max(reported, quoted)
        check('spread', spread <= params.max_spread_bps, SPREAD,
              f'spread {spread:.2f} bps against the {params.max_spread_bps:g} bps limit (reported {reported:.2f}'
              + (f', from the best bid and ask {quoted:.2f})' if quoted is not None else ')'),
              value=spread, reported=reported, from_quote=quoted, threshold=params.max_spread_bps)

    # ---- the market
    candidate = CANDIDATES.get(regime) if features is not None else None
    if regime in CANDIDATES:
        check('regime', True, WEAK_TREND, f'regime {regime}')
    else:
        if regime == 'stale_uncertain':
            stale = REASON_NOT_FRESH in regime_reasons or supplied
            code = STALE_DATA if stale else _reason_code(feature_reason)
        elif regime == 'illiquid':
            code = INSUFFICIENT_DEPTH if REASON_DEPTH in regime_reasons else SPREAD
        elif regime == 'excessively_volatile':
            code = VOLATILITY
        elif regime in ('trending_down', 'no_reliable_edge'):
            code = WEAK_TREND
        else:
            code = ERROR
        detail = f'regime {regime}' + (f': {"; ".join(regime_reasons)}' if regime_reasons else '')
        check('regime', False, code, detail if regime in REGIMES else f'unknown regime {regime!r}')

    # ---- sizing (micro_economics.SizingResult.as_dict())
    budget_cap = max_debit = None
    own_sizing = False               # a sizing result for this symbol (another market's numbers are never used)
    if sizing is None:
        check('sizing', False, ERROR, 'no sizing result')
    elif sizing.get('symbol') != symbol:
        named = sizing.get('symbol')
        check('sizing', False, ERROR, f'the sizing result is for {named}, not {symbol}' if named is not None
              else 'the sizing result does not name its symbol', sizing_symbol=named)
    else:
        own_sizing = True
        eligible, sizing_code = sizing.get('eligible') is True, sizing.get('code')
        detail = str(sizing.get('detail') or '')
        if eligible and sizing_code is None:
            amounts, problem = _sizing_amounts(sizing)
            values = {key: sizing.get(key) for key in ('max_debit', 'target_budget', 'budget_cap')}
            if amounts is None:
                check('sizing', False, ERROR, f'eligible sizing refused: {problem}', **values,
                      micro_max=MICRO_MAX_ALL_IN_USDT)
            else:
                budget_cap, max_debit = amounts['budget_cap'], amounts['max_debit']
                check('sizing', True, ERROR, f'eligible: max debit {money.text(max_debit)} of a '
                      f'{money.text(amounts["target_budget"])} target and a {money.text(budget_cap)} cap', **values,
                      micro_max=MICRO_MAX_ALL_IN_USDT)
        else:
            code = ERROR if eligible else SIZING_CODES.get(sizing_code, ERROR)
            check('sizing', False, code, detail or f'sizing refused ({sizing_code})', sizing_code=sizing_code)

    # ---- the candidate's own conditions
    if candidate == 'momentum_pullback':
        f = features
        atr_price = f.atr_bps / 1e4 * f.close
        check('above_slow_ema', f.close > f.ema_slow, WEAK_TREND,
              f'close {f.close:.8g} against slow EMA {f.ema_slow:.8g}')
        near_fast = abs(f.close - f.ema_fast) <= MOMENTUM_PULLBACK_ATR * atr_price
        pulled_back = f.zscore <= MOMENTUM_MAX_ZSCORE and near_fast
        check('pullback', pulled_back, WEAK_TREND,
              f'z-score {f.zscore:.2f} (max {MOMENTUM_MAX_ZSCORE:g}), close {abs(f.close - f.ema_fast):.8g} from '
              f'the fast EMA (max {MOMENTUM_PULLBACK_ATR:g} ATR = {atr_price:.8g})')
        rose = f.last_change_bps is not None and f.last_change_bps > 0
        check('last_bar_up', rose, WEAK_TREND, 'last bar closed up' if rose else 'last bar did not close up',
              value=f.last_change_bps)
        check('no_collapse', f.drop_bps > -MOMENTUM_COLLAPSE_ATR * f.atr_bps, WEAK_TREND,
              f'{f.drop_bps:.1f} bps from the 15-bar high (limit {-MOMENTUM_COLLAPSE_ATR * f.atr_bps:.1f})')
    elif candidate == 'range_reversion':
        f = features
        check('oversold', f.zscore <= RANGE_MAX_ZSCORE, WEAK_TREND,
              f'z-score {f.zscore:.2f} (needs <= {RANGE_MAX_ZSCORE:g})')
        check('down_bars', f.down_bars <= RANGE_MAX_DOWN_BARS, WEAK_TREND,
              f'{f.down_bars} consecutive lower closes (max {RANGE_MAX_DOWN_BARS})')
        check('no_collapse', f.drop_bps > -RANGE_COLLAPSE_ATR * f.atr_bps, WEAK_TREND,
              f'{f.drop_bps:.1f} bps from the 15-bar high (limit {-RANGE_COLLAPSE_ATR * f.atr_bps:.1f})')
        check('higher_slope', f.higher_slope_bps >= RANGE_MIN_HIGHER_SLOPE_BPS, WEAK_TREND,
              f'5m slope {f.higher_slope_bps:.1f} bps (min {RANGE_MIN_HIGHER_SLOPE_BPS:g})')

    # ---- economics: a feasibility heuristic, never an expected value
    stop_bps = expected_range = required = None
    if features is not None:
        stop_bps = stop_net_bps(features, params)
        check('stop_within_loss_cap', stop_bps is not None, VOLATILITY,
              f'stop {stop_bps:.1f} bps within the {params.max_loss_bps:g} bps loss cap' if stop_bps is not None else
              f'a 2 ATR stop ({2 * features.atr_bps:.1f} bps) exceeds the {params.max_loss_bps:g} bps loss cap')
        expected_range = features.atr_bps * math.sqrt(params.max_hold_s / 60)
        if stop_bps is not None and budget_cap is not None and quote_ok:
            _stop_room(check, stop_bps, features.atr_bps, budget_cap=budget_cap, max_debit=max_debit,
                       break_even=sizing.get('break_even_bid'), best_bid=book.get('best_bid'))
    if own_sizing:
        required = _number(sizing.get('required_move_bps'))
        if required is not None and required <= 0:
            check('required_move', False, ERROR, f'required move {required:.2f} bps is not positive')
            required = None
        elif required is None and budget_cap is not None:
            check('required_move', False, ERROR, 'eligible sizing without a required move')
    if expected_range is not None and required is not None:
        limit = params.edge_ratio * expected_range
        check('economic_edge', required <= limit, NO_ECONOMIC_EDGE,
              f'required move {required:.1f} bps against {params.edge_ratio:g} x {expected_range:.1f} bps '
              f'expected range ({limit:.1f})', required_move_bps=required, expected_range_bps=expected_range)
    if required is not None and spread is not None:
        check('fees', spread <= FEES_SPREAD_SHARE * required, FEES,
              f'spread {spread:.2f} bps against {FEES_SPREAD_SHARE:g} x the {required:.1f} bps required move',
              value=spread, threshold=FEES_SPREAD_SHARE * required)

    target_net = stop_net = None
    if candidate is not None and budget_cap is not None and stop_bps is not None:
        try:
            target_net = money.q_up(money.mul(budget_cap, money.bps(money.parse(params.target_net_bps))),
                                    AMOUNT_STEP)
            stop_net = money.q_down(money.mul(budget_cap, money.bps(money.parse(stop_bps))), AMOUNT_STEP)
            problem = None if target_net > 0 and stop_net > 0 else 'target or stop rounds to zero'
        except money.MoneyError as exc:
            problem = str(exc)
        if problem is not None:      # recorded only when it fails: amounts are not a market condition
            check('amounts', False, ERROR, f'target and stop amounts cannot be computed ({problem})')

    failed = [item for item in checks if not item['passed']]
    codes = tuple(item['code'] for item in failed)
    reasons = tuple(item['detail'] for item in failed)
    proposal = None
    if not failed and candidate is not None:
        action, code = 'enter', None
        proposal = {'opportunity_key': f'{symbol}|{candidate}|{features.bar_ts_ms}',
                    'target_net': money.text(target_net), 'stop_net': money.text(stop_net),
                    'stop_net_bps': stop_bps, 'max_hold_s': params.max_hold_s,
                    'expected_range_bps': expected_range, 'required_move_bps': sizing.get('required_move_bps'),
                    'heuristic_rank': round(expected_range / required, 4),
                    'heuristic_note': ('Feasibility heuristic, not an expected value: the required move fits in '
                                       'edge_ratio x the ATR-scaled range for the hold time. heuristic_rank is '
                                       'expected range / required move and is not calibrated.')}
        status = (f'{candidate.replace("_", " ")} entry: needs +{required:.1f} bps within '
                  f'{params.edge_ratio:g} x {expected_range:.1f} bps expected range')
    else:
        action = 'abstain'
        code = _dominant_code(codes) if codes else ERROR
        first = next((item['detail'] for item in failed if item['code'] == code), 'no candidate for this regime')
        status = f'{STATUS_LABELS[code]}: {first}'
    return MicroDecision(decision_id=_decision_id(symbol, now, action, inputs), symbol=symbol, decided_at=now,
                         action=action, code=code, status=_sentence(status), reasons=reasons, codes=codes,
                         checks=tuple(checks), candidate=candidate, regime=str(regime), inputs=inputs,
                         proposal=proposal)


# ------------------------------------------------------------------------ exit
@dataclass(frozen=True)
class ExitDecision:
    action: str                 # 'exit' | 'hold' | 'cannot_evaluate'
    trigger: str | None         # one of EXIT_TRIGGERS
    reason: str
    new_peak: Decimal | None    # highest book-based est_net seen so far
    needs_depth: bool           # True for profit exits
    checks: tuple[dict, ...]    # every trigger: check, fired, detail, values as strings

    def as_dict(self) -> dict:
        return {'action': self.action, 'trigger': self.trigger, 'reason': self.reason,
                'new_peak': money.text(self.new_peak), 'needs_depth': self.needs_depth, 'checks': list(self.checks)}


def evaluate_exit(*, estimate: dict | None, depth_model: str, target_net: Decimal, stop_net: Decimal,
                  peak_est_net: Decimal | None, opened_wall: float, max_hold_s: float,
                  regime: str | None, now: float) -> ExitDecision:
    """Exit policy for one held cycle from a fresh exit estimate (``ExitEstimate.as_dict()``).

    Order: no estimate -> ``cannot_evaluate``; ``est_net <= -stop_net`` ->
    ``stop_loss``; held ``>= max_hold_s`` -> ``max_hold``; regime
    ``excessively_volatile`` or ``trending_down`` with ``est_net < 0`` ->
    ``regime_shift``; then, only when both ``depth_model`` and the estimate
    say ``'book'`` and the estimate says the walk was not exhausted
    (``depth_exhausted is False``; a missing flag is no evidence):
    ``est_net - uncertainty >= target_net`` -> ``take_profit``; ``peak >= 0.75
    target``, ``0 < est_net - uncertainty <= 0.25 target`` ->
    ``profit_protection``; else ``hold``.

    Safer readings, all on the side of exiting or of not claiming a profit:
    ``est_net`` is the smaller of the estimate's ``est_net_pnl`` and
    ``net_proceeds - allocated_basis`` when both are present, so a gross
    figure passed by mistake cannot trigger a profit exit; an open time or a
    hold limit that cannot be read fires ``max_hold``; an unknown ``est_net``,
    stop or target never reads as zero and returns ``cannot_evaluate`` unless a
    safety exit fires; the peak only ratchets on book-based estimates, the
    only ones a profit exit may use.
    """
    if _number(now) is None:
        raise ValueError('now must be a finite time')
    checks: list[dict] = []

    def record(name: str, fired: bool, detail: str, **values):
        checks.append({'check': name, 'fired': bool(fired), 'detail': detail,
                       **{key: money.text(value) if isinstance(value, Decimal) else _plain(value)
                          for key, value in values.items()}})

    stop = _decimal(stop_net)
    stop = stop if stop is not None and stop > 0 else None
    target = _decimal(target_net)
    target = target if target is not None and target > 0 else None
    peak = _decimal(peak_est_net)
    opened, limit = _number(opened_wall), _number(max_hold_s)
    if opened is None or limit is None or limit <= 0:
        held, timed_out = None, True
        hold_detail = 'hold time cannot be established; exit'
    else:
        held = float(now) - opened
        timed_out = held >= limit
        hold_detail = f'held {held:.0f} s of {limit:.0f} s'

    if not isinstance(estimate, dict):
        record('max_hold', timed_out, hold_detail + ('; waiting for a fresh estimate' if timed_out else ''),
               held_s=held, max_hold_s=limit)
        return ExitDecision('cannot_evaluate', None, 'no fresh exit estimate', peak, False, tuple(checks))

    est_net = _decimal(estimate.get('est_net_pnl'))
    net_proceeds, basis = _decimal(estimate.get('net_proceeds')), _decimal(estimate.get('allocated_basis'))
    if est_net is not None and net_proceeds is not None and basis is not None:
        est_net = min(est_net, money.sub(net_proceeds, basis))
    uncertainty = _decimal(estimate.get('uncertainty'))
    uncertainty = uncertainty if uncertainty is not None and uncertainty >= 0 else None
    book_based = (depth_model == 'book' and estimate.get('depth_model') == 'book'
                  and estimate.get('depth_exhausted') is False)
    new_peak = peak
    if book_based and est_net is not None:
        new_peak = est_net if peak is None else max(peak, est_net)

    stop_fired = est_net is not None and stop is not None and est_net <= -stop
    record('stop_loss', stop_fired, 'estimated net at or below the stop' if stop_fired else (
        'stop not reached' if est_net is not None and stop is not None else 'est_net or stop unknown'),
        est_net=est_net, stop_net=stop)
    record('max_hold', timed_out, hold_detail, held_s=held, max_hold_s=limit)
    hostile = regime in ('excessively_volatile', 'trending_down')
    regime_fired = hostile and est_net is not None and est_net < 0
    record('regime_shift', regime_fired, f'regime {regime}' + (' with an estimated loss' if regime_fired else ''),
           regime=regime, est_net=est_net)
    take = protect = False
    margin = None
    if book_based and est_net is not None and uncertainty is not None and target is not None:
        margin = money.sub(est_net, uncertainty)
        take = margin >= target
        protect = (not take and new_peak is not None and new_peak >= money.mul(PROTECT_PEAK_SHARE, target)
                   and margin > money.ZERO and margin <= money.mul(PROTECT_FLOOR_SHARE, target))
        profit_detail = f'net after uncertainty {money.text(margin)} against target {money.text(target)}'
    elif not book_based:
        profit_detail = f'profit exits need a book-depth estimate (depth model {depth_model})'
    else:
        profit_detail = 'est_net, uncertainty or target unknown'
    record('take_profit', take, profit_detail, margin=margin, target_net=target, uncertainty=uncertainty)
    record('profit_protection', protect, f'peak {money.text(new_peak)}; ' + profit_detail, peak=new_peak,
           margin=margin)

    fired = {'stop_loss': stop_fired, 'max_hold': timed_out, 'regime_shift': regime_fired,
             'take_profit': take, 'profit_protection': protect}
    trigger = next((name for name in EXIT_TRIGGERS if fired[name]), None)
    if trigger is not None:
        detail = next(item['detail'] for item in checks if item['check'] == trigger)
        return ExitDecision('exit', trigger, f'{trigger.replace("_", " ")}: {detail}', new_peak,
                            trigger in ('take_profit', 'profit_protection'), tuple(checks))
    known = (('estimated net', est_net), ('stop', stop), ('target', target))
    missing = [name for name, value in known if value is None]
    if missing:
        return ExitDecision('cannot_evaluate', None, f'{", ".join(missing)} unknown or invalid', new_peak, False,
                            tuple(checks))
    return ExitDecision('hold', None, 'no exit condition met', new_peak, False, tuple(checks))

