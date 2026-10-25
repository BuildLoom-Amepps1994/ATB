"""Offline adversarial tests for chronology, candle data and modeled fills."""
import copy
import math

import pandas as pd
import pytest

import strategy


START = 1_700_000_000_000
HOUR = 3_600_000
CONFIG = {'fee_pct': 0, 'slippage_pct': 0, 'risk_pct': 1, 'max_position_pct': 20}


def history(count=150, duration=HOUR, start=START):
    return [[start + i * duration, 100, 101, 99, 100, 1000] for i in range(count)]


def controlled_signals(monkeypatch, *, atr=1.0, always=False):
    def fixed_indicators(frame, **kwargs):
        frame = frame.copy()
        for name, value in {'ema20': 90, 'ema50': 80, 'ema100': 70,
                            'rsi': 60, 'atr': atr, 'vol_avg20': 1000,
                            'prior_high20': 100}.items():
            frame[name] = value
        return frame
    monkeypatch.setattr(strategy, 'indicators', fixed_indicators)
    monkeypatch.setattr(strategy, 'entry_signal', lambda row, regime_ok=True:
                        ((always or row['timestamp'] == START + 115 * HOUR)
                         and regime_ok, 'controlled', 100))


@pytest.mark.parametrize('column,value', [
    (0, float('inf')), (0, -1), (0, START + .5), (1, 0), (1, -5),
    (2, 90), (3, 110), (4, float('nan')), (4, float('inf')), (5, -1),
])
def test_rejects_corrupt_market_data(column, value):
    rows = history(2)
    rows[0][column] = value
    with pytest.raises(ValueError):
        strategy.frame_from_ohlcv(rows)


def test_rejects_malformed_rows_and_conflicting_duplicates():
    with pytest.raises(ValueError, match='six'):
        strategy.frame_from_ohlcv([[START, 100]])
    rows = history(2)
    duplicate = rows[0].copy()
    duplicate[5] += 1
    with pytest.raises(ValueError, match='Conflicting'):
        strategy.frame_from_ohlcv(rows + [duplicate])


def test_exact_duplicates_and_unsorted_input_are_canonical():
    rows = history(3)
    actual = strategy.frame_from_ohlcv([rows[2], rows[0], rows[1], rows[0]],
                                      drop_open_candle=False)
    assert actual['timestamp'].tolist() == [row[0] for row in rows]


def test_completion_uses_close_time_and_retains_last_completed_candle():
    rows = history(3)
    current = START + 2 * HOUR + HOUR // 2
    actual = strategy.frame_from_ohlcv(rows, timeframe='1h', now_ms=current)
    assert actual['timestamp'].tolist() == [START, START + HOUR]
    at_boundary = strategy.frame_from_ohlcv(rows, timeframe='1h', now_ms=START + 3 * HOUR)
    assert len(at_boundary) == 3


def test_indicators_are_prefix_invariant_and_require_warmup():
    rows = history(180)
    for i, row in enumerate(rows):
        row[1:5] = [100 + i * .1, 102 + i * .1, 98 + i * .1, 100 + i * .1]
    frame = strategy.frame_from_ohlcv(rows, drop_open_candle=False)
    full = strategy.indicators(frame)
    prefix = strategy.indicators(frame.iloc[:130])
    pd.testing.assert_frame_equal(full.iloc[:130], prefix)
    assert math.isnan(full.iloc[5]['ema50'])
    assert not strategy.entry_signal(full.iloc[5], True)[0]


@pytest.mark.parametrize('atr,price', [(float('nan'), 100), (-1, 100), (1, 0), (1, float('inf'))])
def test_invalid_stops_cannot_produce_a_position_size(atr, price):
    with pytest.raises(ValueError):
        strategy.stop_fraction(atr, price)


def test_same_bar_stop_and_target_is_stop_first(monkeypatch):
    controlled_signals(monkeypatch)
    rows = history()
    rows[116][1:5] = [100, 112, 94, 100]
    result = strategy.backtest(rows, CONFIG)
    buy, sell = result['executions']
    assert buy['time'] == rows[116][0]
    assert sell['time'] == rows[116][0]
    assert sell['price'] == pytest.approx(96.5)
    assert sell['reason'] == 'stop_first_ambiguous_bar'
    assert sell['pnl'] < 0


def test_target_is_filled_at_target_on_trigger_bar_not_a_future_open(monkeypatch):
    controlled_signals(monkeypatch)
    rows = history()
    rows[116][1:5] = [100, 112, 99, 105]
    rows[117][1:5] = [130, 131, 129, 130]
    result = strategy.backtest(rows, CONFIG)
    sell = result['executions'][1]
    assert sell['time'] == rows[116][0]
    assert sell['price'] == pytest.approx(109.625)
    assert sell['reason'] == 'target'


def test_trailing_stop_is_not_raised_retroactively_inside_a_candle(monkeypatch):
    controlled_signals(monkeypatch, atr=5)
    rows = history()
    rows[116][1:5] = [100, 115, 99, 110]
    # New stop becomes 102.5 at the preceding close; next open gaps through it.
    rows[117][1:5] = [100, 101, 99, 100]
    result = strategy.backtest(rows, CONFIG)
    sell = result['executions'][1]
    assert sell['time'] == rows[117][0]
    assert sell['price'] == 100
    assert sell['reason'] == 'stop_gap'


def test_adverse_stop_gap_and_slippage_are_charged(monkeypatch):
    controlled_signals(monkeypatch, atr=5)
    rows = history()
    rows[117][1:5] = [80, 82, 78, 80]
    cfg = {**CONFIG, 'fee_pct': .1, 'slippage_pct': .2}
    result = strategy.backtest(rows, cfg)
    sell = result['executions'][1]
    assert sell['reason'] == 'stop_gap'
    assert sell['price'] == pytest.approx(80 * .998)
    assert result['fees_paid'] > 0
    assert result['slippage_costs'] > 0


def test_no_same_candle_exit_and_reentry_from_a_stale_setup(monkeypatch):
    controlled_signals(monkeypatch, always=True)
    rows = history()
    rows[117][1:5] = [90, 91, 89, 90]
    result = strategy.backtest(rows, CONFIG)
    at_exit = [event for event in result['executions'] if event['time'] == rows[117][0]]
    assert [event['type'] for event in at_exit] == ['sell']


def test_risk_sizing_includes_entry_and_stop_exit_costs(monkeypatch):
    controlled_signals(monkeypatch)
    rows = history()
    cfg = {**CONFIG, 'fee_pct': .5, 'slippage_pct': .5, 'max_position_pct': 30}
    # Trigger the original stop immediately; no overnight gap.
    rows[116][1:5] = [100, 101, 90, 100]
    result = strategy.backtest(rows, cfg)
    sell = result['executions'][1]
    assert -sell['pnl'] == pytest.approx(100, abs=.00001)
    assert result['ending_equity'] == pytest.approx(9900)


def test_open_equity_and_benchmark_include_liquidation_costs(monkeypatch):
    controlled_signals(monkeypatch, atr=5)
    cfg = {**CONFIG, 'fee_pct': .1, 'slippage_pct': .2}
    result = strategy.backtest(history(), cfg)
    assert result['open_position_count'] == 1
    assert result['ending_equity'] < result['starting_equity']
    assert result['buy_hold_pct'] < 0
    assert result['max_drawdown_pct'] > 0
    assert result['equity_curve'][-1]['equity'] == result['ending_equity']


def test_completed_higher_frame_is_only_available_at_its_close(monkeypatch):
    controlled_signals(monkeypatch)
    higher = strategy.frame_from_ohlcv(history(66, duration=4 * HOUR), drop_open_candle=False)
    close_of_65th = START + 65 * 4 * HOUR
    primary = strategy.frame_from_ohlcv(
        history(3, start=close_of_65th - 2 * HOUR), drop_open_candle=False)
    actual = strategy._historical_regimes(primary, higher, HOUR, 4 * HOUR)
    assert actual == [False, True, True]
    altered = higher.astype({'close': float})
    altered.loc[65, 'close'] = .01  # Future, unfinished higher candle cannot affect old decisions.
    assert strategy._historical_regimes(primary, altered, HOUR, 4 * HOUR) == actual


def test_stale_higher_frame_cannot_confirm_future_signals(monkeypatch):
    controlled_signals(monkeypatch)
    higher = strategy.frame_from_ohlcv(history(65, duration=4 * HOUR), drop_open_candle=False)
    primary = strategy.frame_from_ohlcv(
        history(2, start=START + 66 * 4 * HOUR), drop_open_candle=False)
    assert strategy._historical_regimes(primary, higher, HOUR, 4 * HOUR) == [False, False]


def test_backtest_rejects_gaps_and_unpaired_higher_frame_arguments():
    rows = history()
    del rows[20]
    with pytest.raises(ValueError, match='missing or irregular'):
        strategy.backtest(rows, CONFIG)
    with pytest.raises(ValueError, match='both'):
        strategy.backtest(history(), CONFIG, higher_timeframe='4h')
    with pytest.raises(ValueError, match='65 completed higher'):
        strategy.backtest(history(), CONFIG, timeframe='1h', higher_timeframe='4h',
                          higher_rows=history(20, duration=4 * HOUR),
                          now_ms=START + 500 * HOUR)


@pytest.mark.parametrize('name,value', [('fee_pct', float('nan')), ('risk_pct', 0),
                                       ('max_position_pct', -1), ('slippage_pct', True)])
def test_backtest_rejects_invalid_cost_and_risk_parameters(name, value):
    with pytest.raises(ValueError):
        strategy.backtest(history(), {**CONFIG, name: value})


def test_backtest_exact_higher_frame_mode_is_deterministic_and_input_immutable():
    rows = history(500)
    higher = history(200, duration=4 * HOUR, start=START - 200 * 4 * HOUR)
    original = copy.deepcopy((rows, higher))
    kwargs = {'timeframe': '1h', 'higher_rows': higher, 'higher_timeframe': '4h',
              'now_ms': START + 500 * HOUR}
    one = strategy.backtest(rows, CONFIG, **kwargs)
    two = strategy.backtest(rows, CONFIG, **kwargs)
    assert one == two
    assert (rows, higher) == original
    assert one['assumptions']['regime_mode'] == 'exact_completed_higher_timeframe'
    assert 'proxy' not in one['warning']
    assert one['data_range']['completed_candles'] == 500


def test_backtest_sampling_always_retains_final_equity():
    result = strategy.backtest(history(901), CONFIG)
    assert len(result['equity_curve']) <= 221
    assert result['equity_curve'][-1]['equity'] == result['ending_equity']
    assert result['equity_curve'][-1]['time'] == result['data_range']['end_ms']
