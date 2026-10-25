"""Research diagnostics use fixed rules, matched dates, and causal warmup only."""
import copy
import json
import math

import pandas as pd
import pytest

import strategy
from test_strategy_integrity import CONFIG, HOUR, START, controlled_signals, history


def varied_history(count=500):
    rows = history(count)
    for i, row in enumerate(rows):
        # Include large gaps so the oldest ATR true-range correction is exercised.
        price = (100 if (i // 41) % 2 else 70) + 2 * math.sin(i * .41)
        row[1:5] = [price, price + 1, price - 1, price + .2]
        row[5] = 1000 + i % 17
    return rows


@pytest.mark.parametrize('window', [strategy.PRIMARY_WINDOW, strategy.HIGHER_WINDOW])
def test_bounded_indicators_match_independently_restarted_windows(window):
    frame = strategy.frame_from_ohlcv(varied_history(), drop_open_candle=False)
    actual = strategy.indicators(frame, window=window)
    for index in (window - 1, window, 155, 299, 499):
        sample = frame.iloc[index - window + 1:index + 1]
        close = sample['close']
        for period in (20, 50):
            expected = close.ewm(span=period, adjust=False, min_periods=period).mean().iloc[-1]
            assert actual.iloc[index][f'ema{period}'] == pytest.approx(expected, abs=1e-10)
        change = close.diff()
        gain = change.clip(lower=0).ewm(alpha=1 / 14, adjust=False, min_periods=14).mean().iloc[-1]
        loss = (-change.clip(upper=0)).ewm(alpha=1 / 14, adjust=False, min_periods=14).mean().iloc[-1]
        expected_rsi = 100 - 100 / (1 + gain / loss)
        assert actual.iloc[index]['rsi'] == pytest.approx(expected_rsi, abs=1e-9)
        previous = close.shift(1)
        tr = pd.concat([sample['high'] - sample['low'], (sample['high'] - previous).abs(),
                        (sample['low'] - previous).abs()], axis=1).max(axis=1)
        expected_atr = tr.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean().iloc[-1]
        assert actual.iloc[index]['atr'] == pytest.approx(expected_atr, abs=1e-10)


def test_live_and_historical_confirmation_agree_despite_different_fetch_lengths():
    rows = history(300, duration=4 * HOUR)
    for i, row in enumerate(rows):
        price = 50 if i < 215 else 100 - (i - 215) / 84
        row[1:5] = [price, price + 1, price - 1, price]
    higher = strategy.frame_from_ohlcv(rows, drop_open_candle=False)
    live = higher.tail(85).reset_index(drop=True)
    primary = strategy.frame_from_ohlcv(history(1, start=rows[-1][0] + 3 * HOUR), drop_open_candle=False)
    assert strategy.regime_signal(live) == strategy.regime_signal(higher)
    assert strategy._historical_regimes(primary, higher, HOUR, 4 * HOUR)[-1] == strategy.regime_signal(live)
    full_primary = strategy.frame_from_ohlcv(varied_history(), drop_open_candle=False)
    long = strategy.indicators(full_primary).iloc[-1]
    short = strategy.indicators(full_primary.tail(175)).iloc[-1]
    for column in ('ema20', 'ema50', 'rsi', 'atr', 'vol_avg20', 'prior_high20'):
        assert long[column] == pytest.approx(short[column], abs=1e-9)
    assert strategy.entry_signal(long, True) == strategy.entry_signal(short, True)


@pytest.mark.parametrize('price,close,atr,expected', [(102, 100, 1, True), (102.01, 100, 1, False),
                                                    (105, 100, 8, True), (106, 100, 8, False),
                                                    (float('nan'), 100, 1, False), (True, 1, 1, False)])
def test_shared_entry_gap_policy_has_explicit_bounds(price, close, atr, expected):
    assert strategy.entry_price_ok(price, close, atr) is expected


def test_backtest_rejects_a_gap_the_executable_entry_policy_would_reject(monkeypatch):
    controlled_signals(monkeypatch)
    rows = history()
    rows[116][1:5] = [150, 151, 149, 150]
    assert not strategy.entry_price_ok(150, 100, 1)
    assert strategy.backtest(rows, CONFIG)['executions'] == []


def test_chronological_segments_start_flat_and_keep_warmup_outside_metrics(monkeypatch):
    controlled_signals(monkeypatch, atr=5)
    report = strategy.research_report(history(500), {**CONFIG, 'fee_pct': .1, 'slippage_pct': .2})
    diagnostics = report['research_report']
    dev, holdout = diagnostics['development'], diagnostics['holdout']
    assert dev['data_range']['end_ms'] == holdout['data_range']['start_ms'] == diagnostics['split_ms']
    assert dev['coverage']['evaluation_candles'] + holdout['coverage']['evaluation_candles'] == report['coverage']['evaluation_candles']
    assert dev['open_position_count'] == 1
    assert holdout['starting_equity'] == holdout['ending_equity'] == 10000
    assert holdout['open_position_count'] == 0
    assert diagnostics['parameters_tuned'] is False
    assert diagnostics['parameters']['risk_pct'] == CONFIG['risk_pct']
    assert diagnostics['parameters']['max_position_pct'] == CONFIG['max_position_pct']
    assert diagnostics['double_cost']['data_range'] == report['data_range']
    assert holdout['sample_notes']
    json.dumps(report, allow_nan=False)


def test_mutating_future_holdout_cannot_change_development_trades_or_curve(monkeypatch):
    controlled_signals(monkeypatch, atr=5)
    original = history(500)
    kwargs = {'timeframe': '1h', 'now_ms': START + 500 * HOUR}
    first = strategy.research_report(original, CONFIG, **kwargs)
    split = first['research_report']['split_ms']
    changed = copy.deepcopy(original)
    for row in changed:
        if row[0] >= split:
            row[1:5] = [150, 151, 149, 150]
    second = strategy.research_report(changed, CONFIG, **kwargs)
    assert first['research_report']['development'] == second['research_report']['development']
    before = strategy.backtest(original, CONFIG, end_ms=split, **kwargs)
    after = strategy.backtest(changed, CONFIG, end_ms=split, **kwargs)
    assert before['executions'] == after['executions']
    assert before['equity_curve'] == after['equity_curve']
    assert all(event['time'] < split for event in before['executions'])
    assert all(point['time'] <= split for point in before['equity_curve'])
    holdout = strategy.backtest(original, CONFIG, start_ms=split, **kwargs)
    assert all(event['time'] >= split for event in holdout['executions'])
    assert first['research_report']['data_fingerprints']['primary_sha256'] != second['research_report']['data_fingerprints']['primary_sha256']


def test_research_fingerprints_are_canonical_and_cost_stress_is_explicit(monkeypatch):
    controlled_signals(monkeypatch)
    rows = history(500)
    rows[116][1:5] = [100, 112, 99, 105]
    settings = {**CONFIG, 'max_position_pct': 8, 'fee_pct': .1, 'slippage_pct': .2}
    original = copy.deepcopy((rows, settings))
    one = strategy.research_report(rows, settings)
    two = strategy.research_report(list(reversed(rows)) + [rows[0]], settings)
    assert one == two
    assert (rows, settings) == original
    stressed = one['research_report']['double_cost']
    assert stressed['fee_pct'] == .2 and stressed['slippage_pct'] == .4
    assert stressed['costs_paid'] > one['costs_paid']


def test_insufficient_confirmation_coverage_is_reported_instead_of_implying_validation():
    primary = history(250)
    higher = history(80, duration=4 * HOUR, start=START - 1000 * HOUR)
    report = strategy.research_report(primary, CONFIG, timeframe='1h', higher_rows=higher,
                                      higher_timeframe='4h', now_ms=START + 500 * HOUR)
    assert report['coverage']['higher_ready_signal_candles'] == 0
    assert report['coverage']['higher_ready_pct'] == 0
    assert report['executions'] == []
    assert 'coverage' in report['warning']
    assert report['research_report']['holdout']['sample_notes']
