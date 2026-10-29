"""Deterministic long-only signals and conservative single-symbol simulations."""
from __future__ import annotations

import hashlib
import json
import math
import re
import time
from typing import Any

import numpy as np
import pandas as pd


OHLCV_COLUMNS = ['timestamp', 'open', 'high', 'low', 'close', 'volume']
PRIMARY_WINDOW = 110
HIGHER_WINDOW = 65


def entry_price_ok(executable_price: float, signal_close: float, atr: float, *,
                   observed_price: float | None = None) -> bool:
    """One anti-chasing rule for research and executable exchange quotes."""
    if any(isinstance(value, bool) for value in (executable_price, signal_close, atr, observed_price)):
        return False
    try:
        values = [float(executable_price), float(signal_close), float(atr)]
        if observed_price is not None:
            values.append(float(observed_price))
    except (ValueError, TypeError, OverflowError):
        return False
    if not all(math.isfinite(value) and value > 0 for value in values):
        return False
    price, close, volatility = values[:3]
    return (abs(price / close - 1) <= max(.02, min(.05, volatility / close)) + 1e-12
            and (observed_price is None or abs(price / values[3] - 1) <= .02 + 1e-12))


def timeframe_ms(timeframe: str) -> int:
    """Convert fixed-duration candles. Calendar months are unsupported."""
    match = re.fullmatch(r'([1-9][0-9]*)([mhdw])', str(timeframe))
    if not match:
        raise ValueError('Unsupported fixed-duration candle timeframe')
    return int(match[1]) * {'m': 60_000, 'h': 3_600_000,
                            'd': 86_400_000, 'w': 604_800_000}[match[2]]


def frame_from_ohlcv(rows: list[list[float]], drop_open_candle: bool = True, *,
                     timeframe: str | None = None,
                     now_ms: int | None = None) -> pd.DataFrame:
    """Validate OHLCV and retain candles whose close time has passed.

    Exchange timestamps represent candle opens. Without a supplied timeframe,
    conservatively exclude the final row because its close time is unknown.
    Contradictory records raise rather than silently affecting trading signals.
    """
    duration = timeframe_ms(timeframe) if timeframe is not None else None
    if not rows:
        return pd.DataFrame(columns=OHLCV_COLUMNS)
    if any(not isinstance(row, (list, tuple)) or len(row) != 6 for row in rows):
        raise ValueError('Every OHLCV candle must contain exactly six values')
    try:
        values = np.array(rows, dtype=float)
    except (ValueError, TypeError, OverflowError):
        raise ValueError('OHLCV candles must contain finite numeric values') from None
    if not np.isfinite(values).all():
        raise ValueError('OHLCV candles must contain finite numeric values')
    stamps, prices, volume = values[:, 0], values[:, 1:5], values[:, 5]
    if ((stamps <= 0) | (stamps % 1 != 0) | (stamps > 9_007_199_254_740_991)).any():
        raise ValueError('Candle timestamps must be positive integer milliseconds')
    if (prices <= 0).any() or (volume < 0).any():
        raise ValueError('Candle prices must be positive and volume nonnegative')
    opens, highs, lows, closes = prices.T
    if ((highs < np.maximum(np.maximum(opens, closes), lows))
            | (lows > np.minimum(np.minimum(opens, closes), highs))).any():
        raise ValueError('Candle high/low contradict its open or close')
    # Sorted exact-duplicate removal; any remaining shared timestamp conflicts.
    values = np.unique(values, axis=0)
    if (np.diff(values[:, 0]) == 0).any():
        raise ValueError('Conflicting candles share a timestamp')
    if drop_open_candle:
        if duration is None:
            values = values[:-1]
        else:
            cutoff = int(time.time() * 1000) if now_ms is None else now_ms
            if (isinstance(cutoff, bool) or not isinstance(cutoff, (int, float))
                    or not math.isfinite(float(cutoff)) or cutoff < 0):
                raise ValueError('Candle completion time must be finite milliseconds')
            values = values[values[:, 0] + duration <= cutoff]
    frame = {'timestamp': values[:, 0].astype('int64')}
    frame.update((name, values[:, i]) for i, name in enumerate(OHLCV_COLUMNS[1:], 1))
    return pd.DataFrame(frame)


def _shift(values: np.ndarray, periods: int) -> np.ndarray:
    result = np.full(len(values), np.nan)
    if periods < len(values):
        result[periods:] = values[:len(values) - periods]
    return result


def _bounded_ewm(values: np.ndarray, alpha: float, minimum: int, window: int) -> np.ndarray:
    """Equivalent to restarting adjust=False EWM on each trailing window."""
    if minimum > window:
        return np.full(len(values), np.nan)
    # The recursion itself stays in pandas' compiled EWM for identical rounding.
    full = pd.Series(values).ewm(alpha=alpha, adjust=False, min_periods=1).mean().to_numpy()
    preceding = _shift(full, window)
    bounded = full + (1 - alpha) ** window * (_shift(values, window - 1) - preceding)
    bounded = np.where(np.isnan(preceding), full, bounded)
    present = np.cumsum(~np.isnan(values))
    count = present - _shift(present, window)
    count = np.where(np.isnan(count), present, count)
    return np.where(count >= minimum, bounded, np.nan)


def _trailing(values: np.ndarray, length: int, reducer) -> np.ndarray:
    """Exact fixed-length trailing reduction; incomplete windows are NaN."""
    result = np.full(len(values), np.nan)
    if len(values) >= length:
        result[length - 1:] = reducer(np.lib.stride_tricks.sliding_window_view(values, length), axis=1)
    return result


EFFICIENCY_PERIOD = 20


def indicators(df: pd.DataFrame, *, window: int = PRIMARY_WINDOW) -> pd.DataFrame:
    """Causal indicators with an explicit bounded history shared by live/research.

    Primary signals use 110 completed candles; confirmation uses 65. Older
    observations cannot change a current decision merely because a research
    endpoint returned more history than the live scanner.
    """
    if df.empty:
        return df.copy()
    if isinstance(window, bool) or not isinstance(window, int) or window < 50:
        raise ValueError('Indicator window must contain at least 50 candles')
    close = df['close'].to_numpy(dtype=float)
    high, low = df['high'].to_numpy(dtype=float), df['low'].to_numpy(dtype=float)
    volume = df['volume'].to_numpy(dtype=float)
    columns = dict(df.items())
    for period in (20, 50, 100):
        columns[f'ema{period}'] = _bounded_ewm(close, 2 / (period + 1), period, window)
    change = close - _shift(close, 1)
    # A fresh window has no change for its oldest candle; use W-1 changes.
    with np.errstate(invalid='ignore', divide='ignore'):
        gain = _bounded_ewm(np.where(np.isnan(change), np.nan, np.maximum(change, 0)), 1 / 14, 14, window - 1)
        loss = _bounded_ewm(np.where(np.isnan(change), np.nan, -np.minimum(change, 0)), 1 / 14, 14, window - 1)
        rsi = 100 - (100 / (1 + gain / np.where(loss == 0, np.nan, loss)))
    rsi[(loss == 0) & (gain > 0)] = 100
    rsi[(gain == 0) & (loss > 0)] = 0
    rsi[(gain == 0) & (loss == 0)] = 50
    columns['rsi'] = rsi
    prev_close = _shift(close, 1)
    tr = np.fmax(np.fmax(high - low, np.abs(high - prev_close)), np.abs(low - prev_close))
    atr = _bounded_ewm(tr, 1 / 14, 14, window)
    # At the oldest window row, no preceding close exists; its true range is H-L.
    correction = _shift(high - low - tr, window - 1) * (1 - 1 / 14) ** (window - 1)
    columns['atr'] = atr + np.nan_to_num(correction, nan=0.0)
    columns['vol_avg20'] = pd.Series(volume).rolling(20).mean().to_numpy()
    columns['prior_high20'] = _trailing(_shift(high, 1), 20, np.max)
    # Kaufman efficiency: net move over the summed path of the last 20 changes.
    path = _trailing(np.abs(change), EFFICIENCY_PERIOD, np.sum)
    with np.errstate(invalid='ignore', divide='ignore'):
        efficiency = np.abs(close - _shift(close, EFFICIENCY_PERIOD)) / path
    columns['efficiency'] = np.where(path == 0, 0.0, efficiency)
    return pd.DataFrame(columns, index=df.index)


MIN_EFFICIENCY = .3


def entry_signal(last, regime_ok: bool = True, *, rapid: bool = False,
                 market_ok: bool | None = None, filters: bool = True) -> tuple[bool, str, int]:
    """Completed-candle breakout rules; every check must pass.

    With ``filters`` the 20-candle trend efficiency must show a directional
    move rather than noise (a random walk averages about 0.22). ``market_ok``
    is the BTC higher-timeframe trend for altcoins; None means not applied.
    Both can only block an entry, never size or place one.
    """
    keys = ['close', 'volume', 'ema20', 'ema50', 'rsi', 'atr', 'vol_avg20', 'prior_high20']
    if filters:
        keys.append('efficiency')
    try:
        values = {key: float(last[key]) for key in keys}
    except (TypeError, ValueError, KeyError):
        return False, 'Insufficient indicators', 0
    if not all(math.isfinite(value) for value in values.values()) or any(
            values[key] <= 0 for key in ('close', 'volume', 'atr', 'vol_avg20', 'prior_high20')):
        return False, 'Insufficient indicators', 0
    close = values['close']
    breakout = (close >= values['prior_high20'] - .25 * values['atr'] if rapid
                else close >= .997 * values['prior_high20'])
    volatility_ok = (.0003 <= values['atr'] / close <= .04 if rapid
                     else .003 <= values['atr'] / close <= .11)
    checks = [close > values['ema20'] > values['ema50'],
              52 <= values['rsi'] <= (72 if rapid else 76),
              breakout,
              values['volume'] >= values['vol_avg20'] * 0.65,
              volatility_ok,
              bool(regime_ok)]
    reasons = ['trend', 'RSI', 'breakout', 'volume', 'volatility', 'higher-timeframe trend']
    if filters:
        checks.append(values['efficiency'] >= MIN_EFFICIENCY)
        reasons.append('choppy market')
    if market_ok is not None:
        checks.append(bool(market_ok))
        reasons.append('BTC market trend')
    score = int(sum(checks) / len(checks) * 100)
    failed = [name for name, accepted in zip(reasons, checks) if not accepted]
    return all(checks), 'BUY setup' if all(checks) else 'Waiting: ' + ', '.join(failed), score


def setup_quality(last) -> float:
    """Rank qualifying setups: cleaner trends with stronger volume come first.

    Only orders candidates that already passed every entry check; capped so
    one volume spike cannot dominate. Unknown inputs rank last.
    """
    try:
        efficiency = float(last['efficiency'])
        relative_volume = float(last['volume']) / float(last['vol_avg20'])
    except (TypeError, ValueError, KeyError, ZeroDivisionError):
        return 0.0
    if not (math.isfinite(efficiency) and math.isfinite(relative_volume)):
        return 0.0
    return round(max(0.0, min(1.0, efficiency)) * max(0.0, min(3.0, relative_volume)), 6)


def regime_signal(higher: pd.DataFrame) -> bool:
    if len(higher) < 65:
        return False
    row = indicators(higher, window=HIGHER_WINDOW).iloc[-1]
    return bool(row['close'] > row['ema50'] and row['ema20'] > row['ema50'])


TRAIL_ARM_ATR = 1.0   # the trailing stop moves only after the high is this many ATRs above the entry fill


def trail_armed(highest: float, entry: float | None, atr: float, arm_multiple: float = TRAIL_ARM_ATR) -> bool:
    """True once the high has risen ``arm_multiple`` ATRs above the entry fill.

    The entry fill is at or above the ask (plus simulated slippage in paper), so
    a trail measured from it at once would sit above the bid on fast timeframes,
    where a few ATRs are smaller than the spread and slippage, and sell on the
    next check. Without a known entry the trail behaves as before.
    """
    return entry is None or highest >= entry + arm_multiple * atr


def stop_fraction(atr: float, price: float, *, rapid: bool = False) -> float:
    if not all(math.isfinite(float(value)) and float(value) > 0 for value in (atr, price)):
        raise ValueError('A protective stop requires finite positive price and ATR')
    minimum, maximum = (.008, .03) if rapid else (.035, .12)
    return max(minimum, min(maximum, 2.25 * atr / price))


def _contiguous_interval(frame: pd.DataFrame, duration: int | None = None) -> int:
    differences = frame['timestamp'].diff().dropna()
    if differences.empty:
        raise ValueError('At least two candles are required')
    duration = int(differences.iloc[0]) if duration is None else duration
    if duration <= 0 or not (differences == duration).all():
        raise ValueError('Candle history contains missing or irregular intervals')
    return duration


def _historical_regimes(primary: pd.DataFrame, higher: pd.DataFrame,
                        primary_ms: int, higher_ms: int) -> list[bool]:
    """Align only higher candles completed by each signal candle close."""
    if higher.empty:
        return [False] * len(primary)
    h = indicators(higher, window=HIGHER_WINDOW)
    eligible = ((h['close'] > h['ema50']) & (h['ema20'] > h['ema50'])
                & (h.index >= 64))
    left = pd.DataFrame({'close_time': primary['timestamp'] + primary_ms})
    right = pd.DataFrame({'higher_close_time': h['timestamp'] + higher_ms,
                          'regime': eligible})
    aligned = pd.merge_asof(left, right, left_on='close_time',
                            right_on='higher_close_time', direction='backward')
    age = aligned['close_time'] - aligned['higher_close_time']
    return (aligned['regime'].eq(True) & age.ge(0) & age.lt(higher_ms)).tolist()


def _higher_coverage(primary: pd.DataFrame, higher: pd.DataFrame,
                      primary_ms: int, higher_ms: int) -> list[bool]:
    left = pd.DataFrame({'close_time': primary['timestamp'] + primary_ms})
    right = pd.DataFrame({'higher_close_time': higher['timestamp'] + higher_ms,
                          'ready': higher.index >= HIGHER_WINDOW - 1})
    aligned = pd.merge_asof(left, right, left_on='close_time', right_on='higher_close_time', direction='backward')
    age = aligned['close_time'] - aligned['higher_close_time']
    return (aligned['ready'].eq(True) & age.ge(0) & age.lt(higher_ms)).tolist()


def backtest(rows: list[list[float]], settings: dict[str, Any], *,
             timeframe: str | None = None,
             higher_rows: list[list[float]] | None = None,
             higher_timeframe: str | None = None,
             now_ms: int | None = None,
             start_ms: int | None = None,
             end_ms: int | None = None,
             market_rows: list[list[float]] | None = None) -> dict[str, Any]:
    """Simulate next-open signals and conservative stop/target candle fills.

    A trailing stop is updated only after testing the existing stop for the bar.
    Ambiguous stop-and-target candles take the stop. Spread, order-book depth,
    exchange rounding and portfolio-wide limits are not modeled. ``market_rows``
    are BTC candles on the confirmation timeframe for the live market gate.
    """
    x = indicators(frame_from_ohlcv(rows, timeframe=timeframe, now_ms=now_ms))
    if len(x) < 125:
        raise ValueError('At least 125 completed candles are required for a backtest')
    duration = _contiguous_interval(x, timeframe_ms(timeframe) if timeframe else None)
    rapid = timeframe in ('1m', '5m')
    if (higher_rows is None) != (higher_timeframe is None):
        raise ValueError('Supply both higher-timeframe candles and their timeframe')
    higher_count = 0
    if higher_rows is not None:
        if timeframe is None:
            raise ValueError('Primary timeframe is required for exact higher-timeframe alignment')
        higher_duration = timeframe_ms(higher_timeframe)
        if higher_duration <= duration:
            raise ValueError('Confirmation timeframe must exceed the primary timeframe')
        higher = frame_from_ohlcv(higher_rows, timeframe=higher_timeframe, now_ms=now_ms)
        higher_count = len(higher)
        if higher_count < 65:
            raise ValueError('At least 65 completed higher-timeframe candles are required')
        _contiguous_interval(higher, higher_duration)
        regimes = _historical_regimes(x, higher, duration, higher_duration)
        covered = _higher_coverage(x, higher, duration, higher_duration)
        regime_mode = 'exact_completed_higher_timeframe'
    else:
        regimes = ((x['close'] > x['ema100']) & (x['ema50'] > x['ema100'])).tolist()
        covered = [False] * len(x)
        regime_mode = 'primary_ema_proxy'
    filters = settings.get('market_filters', True)
    if not isinstance(filters, bool):
        raise ValueError('market_filters must be true or false')
    market = None
    if market_rows is not None and filters:
        if higher_rows is None:
            raise ValueError('The BTC market gate requires the confirmation timeframe')
        market_frame = frame_from_ohlcv(market_rows, timeframe=higher_timeframe, now_ms=now_ms)
        if len(market_frame) < 65:
            raise ValueError('At least 65 completed BTC confirmation candles are required')
        _contiguous_interval(market_frame, higher_duration)
        market = _historical_regimes(x, market_frame, duration, higher_duration)
    parameters = {}
    for key, default, allow_zero in (('fee_pct', 0.1, True), ('slippage_pct', 0.2, True),
                                      ('risk_pct', 0.65, False), ('max_position_pct', 8.0, False)):
        try:
            value = float(settings.get(key, default))
        except (ValueError, TypeError):
            raise ValueError(f'{key} must be a finite percentage') from None
        if (not math.isfinite(value) or value >= 100 or value < 0
                or (not allow_zero and value == 0) or isinstance(settings.get(key), bool)):
            raise ValueError(f'{key} must be a finite percentage between 0 and 100')
        parameters[key] = value / 100
    fee, slip = parameters['fee_pct'], parameters['slippage_pct']
    starting = 10000.0
    cash, qty, entry_basis = starting, 0.0, 0.0
    highest = stop = target = entry_fill = 0.0
    pending_trend_exit = False
    fees_paid = slippage_costs = 0.0
    curve: list[dict[str, float]] = []
    executions: list[dict[str, Any]] = []
    profits: list[float] = []
    peak, max_dd = starting, 0.0
    records = x.to_dict('records')
    for boundary in (start_ms, end_ms):
        if boundary is not None and (isinstance(boundary, bool) or not isinstance(boundary, int) or boundary < 0):
            raise ValueError('Research boundaries must be nonnegative integer milliseconds')
    evaluated = [i for i in range(116, len(records))
                 if (start_ms is None or records[i]['timestamp'] >= start_ms)
                 and (end_ms is None or records[i]['timestamp'] < end_ms)]
    if not evaluated:
        raise ValueError('No completed evaluation candles remain after warmup and date boundaries')

    def sell(bar: dict, desired: float, reason: str) -> None:
        nonlocal cash, qty, entry_basis, highest, stop, target
        nonlocal pending_trend_exit, fees_paid, slippage_costs
        price = desired * (1 - slip)
        gross = qty * price
        exit_fee = gross * fee
        pnl = gross - exit_fee - entry_basis
        cash += gross - exit_fee
        fees_paid += exit_fee
        slippage_costs += qty * (desired - price)
        profits.append(pnl)
        executions.append({'type': 'sell', 'time': int(bar['timestamp']),
                           'price': price, 'pnl': pnl, 'reason': reason,
                           'fee_usdt': exit_fee})
        qty = entry_basis = highest = stop = target = 0.0
        pending_trend_exit = False

    for i in evaluated:
        previous, bar = records[i - 1], records[i]
        market_open = float(bar['open'])
        exited = False
        # Protective orders active at the open precede a close-derived exit.
        if qty > 0:
            if market_open <= stop:
                sell(bar, market_open, 'stop_gap')
            elif market_open >= target:
                sell(bar, target, 'target')
            elif pending_trend_exit:
                sell(bar, market_open, 'trend_next_open')
            exited = qty == 0
        if qty == 0 and not exited:
            signal_args = {'rapid': True} if rapid else {}
            filter_args = dict(signal_args)
            if not filters:
                filter_args['filters'] = False
            if market is not None:
                filter_args['market_ok'] = market[i - 1]
            buy, _, _ = entry_signal(previous, regime_ok=regimes[i - 1], **filter_args)
            if buy and entry_price_ok(market_open, float(previous['close']), float(previous['atr'])):
                fraction = stop_fraction(float(previous['atr']), float(previous['close']), **signal_args)
                loss_fraction = 1 - (1 - fraction) * (1 - slip) * (1 - fee) / (1 + fee)
                budget = min(cash * parameters['risk_pct'] / loss_fraction,
                             cash * parameters['max_position_pct'], cash * 0.95)
                if budget > 10:
                    price = market_open * (1 + slip)
                    qty = budget / (price * (1 + fee))
                    entry_basis = budget
                    cash -= budget
                    entry_fee = qty * price * fee
                    fees_paid += entry_fee
                    slippage_costs += qty * (price - market_open)
                    highest = entry_fill = price
                    stop = price * (1 - fraction)
                    target = price * (1 + 2.75 * fraction)
                    executions.append({'type': 'buy', 'time': int(bar['timestamp']),
                                       'price': price, 'size_usdt': budget,
                                       'quantity': qty, 'fee_usdt': entry_fee})
        if qty > 0:
            stop_touched = float(bar['low']) <= stop
            target_touched = float(bar['high']) >= target
            if stop_touched:
                sell(bar, min(market_open, stop),
                     'stop_first_ambiguous_bar' if target_touched else 'stop')
            elif target_touched:
                sell(bar, target, 'target')
            else:
                highest = max(highest, float(bar['high']))
                # This stop is active on the NEXT candle, never retroactively.
                # It trails only once the high is TRAIL_ARM_ATR above the entry
                # fill, as in the engine (decision.evaluate_exit).
                if trail_armed(highest, entry_fill, float(bar['atr'])):
                    stop = max(stop, highest - 2.5 * float(bar['atr']))
                pending_trend_exit = float(bar['close']) < float(bar['ema50'])
        liquidation = qty * float(bar['close']) * (1 - slip) * (1 - fee)
        equity = cash + liquidation
        peak = max(peak, equity)
        max_dd = max(max_dd, 100 * (peak - equity) / peak)
        curve.append({'time': int(bar['timestamp']) + duration, 'equity': round(equity, 2)})
    final = records[evaluated[-1]]
    last_equity = cash + qty * float(final['close']) * (1 - slip) * (1 - fee)
    first_open = float(records[evaluated[0]]['open'])
    benchmark_qty = starting / (first_open * (1 + slip) * (1 + fee))
    benchmark_end = benchmark_qty * float(final['close']) * (1 - slip) * (1 - fee)
    step = max(1, math.ceil(len(curve) / 220))
    sampled = curve[::step]
    if sampled[-1] != curve[-1]:
        sampled.append(curve[-1])
    warning = ('Single-symbol research simulation; no portfolio-wide limits, order-book depth, '
               'spread, exchange rounding, or out-of-sample validation are modeled. '
               'Stop gaps can exceed the risk budget. Forward paper testing is required. '
               'Past results do not predict future performance.')
    if regime_mode == 'primary_ema_proxy':
        warning = ('Higher-timeframe candles were not supplied; primary EMA100 is only a proxy '
                   'and is not equivalent to the live strategy. ' + warning)
    ready_count = sum(covered[i - 1] for i in evaluated)
    if regime_mode != 'primary_ema_proxy' and ready_count < len(evaluated):
        warning = ('Some evaluation candles lack completed, warmed-up higher-timeframe coverage; '
                   'entries are blocked on those candles. ' + warning)
    return {
        'starting_equity': starting, 'ending_equity': round(last_equity, 2),
        'return_pct': round((last_equity / starting - 1) * 100, 2),
        'buy_hold_pct': round((benchmark_end / starting - 1) * 100, 2),
        'benchmark_net_of_costs': True, 'max_drawdown_pct': round(max_dd, 2),
        'closed_trades': len(profits),
        'win_rate_pct': round(100 * sum(p > 0 for p in profits) / len(profits), 1) if profits else 0,
        'fees_paid': round(fees_paid, 6), 'slippage_costs': round(slippage_costs, 6),
        'costs_paid': round(fees_paid + slippage_costs, 6),
        'open_position_count': int(qty > 0),
        'unrealized_pnl': round(last_equity - cash - entry_basis, 6) if qty else 0.0,
        'equity_curve': sampled, 'executions': executions,
        'data_range': {'start_ms': int(records[evaluated[0]]['timestamp']),
                       'end_ms': int(final['timestamp']) + duration,
                       'completed_candles': len(records),
                       'higher_completed_candles': higher_count},
        'coverage': {'primary_available_candles': len(records),
                     'primary_window_candles': PRIMARY_WINDOW,
                     'evaluation_candles': len(evaluated),
                     'primary_ready_signal_candles': sum(i - 1 >= PRIMARY_WINDOW - 1 for i in evaluated),
                     'higher_available_candles': higher_count,
                     'higher_window_candles': HIGHER_WINDOW,
                     'higher_ready_signal_candles': ready_count,
                     'higher_ready_pct': round(100 * ready_count / len(evaluated), 2)},
        'assumptions': {'signal_timing': 'completed candle; following-open entry/trend exit',
                        'risk_profile': 'rapid' if rapid else 'standard',
                        'indicator_policy': {'primary_window': PRIMARY_WINDOW, 'higher_window': HIGHER_WINDOW},
                        'entry_price_policy': 'opening quote within 2-5 percent of signal close, bounded by ATR',
                        'fill_model': 'modeled OHLC stops/targets; adverse gaps and costs',
                        'intrabar_policy': 'stop first; trailing update effective next candle',
                        'fee_pct': fee * 100, 'slippage_pct': slip * 100,
                        'risk_pct': parameters['risk_pct'] * 100,
                        'max_position_pct': parameters['max_position_pct'] * 100,
                        'regime_mode': regime_mode, 'higher_timeframe': higher_timeframe,
                        'entry_filters': {'trend_efficiency_min': MIN_EFFICIENCY if filters else None,
                                          'btc_market_gate': ('exact_completed_higher_timeframe' if market is not None
                                                              else 'not_modeled' if filters else 'disabled')},
                        'equity_basis': 'estimated net liquidation including exit fee/slippage'},
        'warning': warning,
    }


def _candle_fingerprint(frame: pd.DataFrame) -> str:
    canonical = [[int(row[0]), *[float(value) for value in row[1:]]]
                 for row in frame[OHLCV_COLUMNS].itertuples(index=False, name=None)]
    return hashlib.sha256(json.dumps(canonical, separators=(',', ':'), allow_nan=False).encode('utf8')).hexdigest()


def _diagnostic_summary(result: dict) -> dict:
    fields = ('starting_equity', 'ending_equity', 'return_pct', 'buy_hold_pct',
              'max_drawdown_pct', 'closed_trades', 'win_rate_pct', 'costs_paid',
              'fees_paid', 'slippage_costs', 'open_position_count', 'unrealized_pnl')
    summary = {key: result[key] for key in fields}
    summary.update(available=True, data_range=dict(result['data_range']),
                   coverage=dict(result['coverage']), fee_pct=result['assumptions']['fee_pct'],
                   slippage_pct=result['assumptions']['slippage_pct'])
    observations = []
    if result['coverage']['evaluation_candles'] < 50:
        observations.append('Fewer than 50 evaluation candles; this is a short descriptive sample.')
    if result['closed_trades'] < 5:
        observations.append('Fewer than 5 closed trades; report the count without inferring a reliable success rate.')
    if result['coverage']['higher_ready_signal_candles'] == 0:
        observations.append('No signal candles have warmed-up higher-timeframe coverage; this cannot assess confirmed entries.')
    summary['sample_notes'] = observations
    return summary


def research_report(rows: list[list[float]], settings: dict[str, Any], *,
                    timeframe: str | None = None,
                    higher_rows: list[list[float]] | None = None,
                    higher_timeframe: str | None = None,
                    now_ms: int | None = None,
                    market_rows: list[list[float]] | None = None) -> dict[str, Any]:
    """Fixed-parameter chronological diagnostics; no search, tuning or forecast.

    The existing full-window result remains at the top level. Development and
    the final 30 percent holdout each start flat with 10,000 simulated USDT.
    Indicators may use earlier warmup history; no trade or metric crosses its
    evaluation boundary. Known fixed rules mean this is not an independent
    prospective validation, even though the calculations remain causal.
    """
    cutoff = int(time.time() * 1000) if now_ms is None else now_ms
    options = {'timeframe': timeframe, 'higher_rows': higher_rows,
               'higher_timeframe': higher_timeframe, 'now_ms': cutoff,
               'market_rows': market_rows}
    base = backtest(rows, settings, **options)
    frame = frame_from_ohlcv(rows, timeframe=timeframe, now_ms=cutoff)
    higher = frame_from_ohlcv(higher_rows, timeframe=higher_timeframe, now_ms=cutoff) if higher_rows is not None else None
    market = frame_from_ohlcv(market_rows, timeframe=higher_timeframe, now_ms=cutoff) if market_rows is not None else None
    count = base['coverage']['evaluation_candles']
    split_index = 116 + max(1, min(count - 1, int(count * .70)))
    split_time = int(frame.iloc[split_index]['timestamp'])
    development = backtest(rows, settings, end_ms=split_time, **options)
    holdout = backtest(rows, settings, start_ms=split_time, **options)
    stressed = dict(settings)
    stressed['fee_pct'] = 2 * base['assumptions']['fee_pct']
    stressed['slippage_pct'] = 2 * base['assumptions']['slippage_pct']
    if stressed['fee_pct'] >= 100 or stressed['slippage_pct'] >= 100:
        stress_summary = {'available': False, 'reason': 'Doubled cost percentages exceed the simulation bounds.'}
    else:
        stress_summary = _diagnostic_summary(backtest(rows, stressed, **options))
    base['research_report'] = {
        'method': 'fixed_parameters_chronological_diagnostics', 'parameters_tuned': False,
        'split_fraction': .70, 'split_ms': split_time,
        'primary_timeframe': timeframe, 'higher_timeframe': higher_timeframe,
        'parameters': {key: base['assumptions'][key] for key in
                       ('fee_pct', 'slippage_pct', 'risk_pct', 'max_position_pct', 'risk_profile',
                        'entry_filters')},
        'indicator_policy': {'primary_window': PRIMARY_WINDOW, 'higher_window': HIGHER_WINDOW},
        'data_fingerprints': {'primary_sha256': _candle_fingerprint(frame),
                              'higher_sha256': _candle_fingerprint(higher) if higher is not None else None,
                              'market_sha256': _candle_fingerprint(market) if market is not None else None},
        'development': _diagnostic_summary(development), 'holdout': _diagnostic_summary(holdout),
        'double_cost': stress_summary,
        'notes': ['The chronological split uses fixed known rules, with no parameter selection or tuning; it is not independent prospective validation.',
                  'Both segments start flat; earlier candles warm indicators but are excluded from segment returns and drawdown.',
                  'The higher-cost replay can change position sizes and trade paths; returns need not decline monotonically.',
                  'Coverage and trade counts describe the available sample, not confidence or a promise of future returns.'],
    }
    return base
