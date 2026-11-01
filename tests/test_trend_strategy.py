"""The Trend strategy: a daily close 5 % above its 100-day average, PAPER only."""
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import decision  # noqa: E402
import micro_policy  # noqa: E402
import storage  # noqa: E402
import strategy  # noqa: E402
from test_engine import engine  # noqa: E402,F401  (fixture)

DAY = 86_400_000


def daily(closes, start=1_700_000_000_000 // DAY * DAY):
    rows = []
    for i, close in enumerate(closes):
        rows.append([start + i * DAY, close, close * 1.01, close * .99, close, 1000.0])
    return rows


def test_the_trend_signal_is_the_close_against_its_100_day_average():
    rows = daily(list(np.linspace(100, 200, 160)))
    x = strategy.indicators(strategy.frame_from_ohlcv(rows, drop_open_candle=False))
    last = x.iloc[-1]
    assert last['sma100'] == pytest.approx(np.mean([r[4] for r in rows[-100:]]))
    assert last['close'] > last['sma100'] * 1.05
    assert strategy.entry_signal(last, regime_ok=False, market_ok=False, trend_rule=True) == (True, 'BUY setup', 100)
    inside = last.copy()
    inside['close'] = inside['sma100'] * 1.01          # above the average but inside the 5 % band: no entry, no exit
    ok, status, score = strategy.entry_signal(inside, trend_rule=True)
    assert not ok and status == 'Waiting: close within 5% of its 100-day average' and score == 50
    assert not strategy.trend_broken(inside, trend_rule=True) and not strategy.trend_broken(last, trend_rule=True)
    below = last.copy()
    below['close'] = below['sma100'] * .94
    ok, status, score = strategy.entry_signal(below, trend_rule=True)
    assert not ok and status == 'Waiting: close below its 100-day average' and score == 0
    assert strategy.trend_broken(below, trend_rule=True)
    assert strategy.stop_fraction(5., 100., trend=True) == strategy.TREND_STOP_FRACTION


def test_a_trend_decision_records_its_average_and_other_decisions_are_unchanged():
    rows = daily(list(np.linspace(100, 200, 160)))
    last = strategy.indicators(strategy.frame_from_ohlcv(rows, drop_open_candle=False)).iloc[-1]
    cfg = {'min_quote_volume': 1, 'max_spread_pct': 1, 'data_max_age_seconds': 120}
    common = dict(symbol='BTC/USDT', last=last, regime_ok=False, market_ok=False, rapid=False, filters=True,
                  price=float(last['close']), volume=1e9, spread_pct=.01, quote_age_s=1., quote_ts=1., cfg=cfg, now=10.)
    trend = decision.evaluate_entry(**common, trend_rule=True)
    assert trend.action == 'enter' and trend.inputs['trend_rule'] is True
    assert trend.inputs['features']['sma100'] == pytest.approx(float(last['sma100']))
    classic = decision.evaluate_entry(**common)
    assert classic.action == 'abstain' and 'trend_rule' not in classic.inputs and 'sma100' not in classic.inputs['features']


def test_the_trend_backtest_holds_through_the_rise_and_exits_below_the_average():
    closes = list(np.linspace(100, 100.5, 130)) + list(np.linspace(100.5, 180, 120)) + list(np.linspace(180, 120, 60))
    rows = daily(closes)
    res = strategy.backtest(rows, {'time_horizon': 'trend', 'fee_pct': .05, 'slippage_pct': .05,
                                   'market_filters': False, 'risk_pct': .65, 'max_position_pct': 8},
                            timeframe='1d', now_ms=rows[-1][0] + 2 * DAY)
    sells = [e for e in res['executions'] if e['type'] == 'sell']
    assert sells and sells[0]['reason'] in ('trend_next_open', 'stop_gap', 'stop')
    assert all(e['reason'] != 'target' for e in sells)
    assert res['return_pct'] > 0
    buys = [e for e in res['executions'] if e['type'] == 'buy']
    assert len(buys) == 1                      # one long holding through the rise, not trailing exits and re-entries


def test_trend_is_paper_only_at_every_boundary(engine):
    assert micro_policy.live_refusal('live', horizon='trend') == micro_policy.PAPER_ONLY_HORIZONS['trend']
    assert micro_policy.live_refusal('paper', horizon='trend') is None
    engine.update_config({'time_horizon': 'trend'})
    with pytest.raises(storage.ConfigValidationError, match='PAPER only'):
        engine.set_mode('live', 'ENABLE LIVE TRADING', True)
    assert engine.mode == 'paper'
    engine.update_config({'time_horizon': 'balanced'})
    engine.mode = 'live'
    try:
        with pytest.raises(storage.ConfigValidationError, match='PAPER only'):
            engine.update_config({'time_horizon': 'trend'})
        engine.cfg['time_horizon'] = 'trend'
        with pytest.raises(storage.ConfigValidationError, match='PAPER only'):
            engine.start()
    finally:
        engine.mode = 'paper'


def test_a_trend_entry_stores_a_disaster_stop_and_no_profit_target(engine):
    engine.update_config({'time_horizon': 'trend'})
    # The engine sizes a Trend entry from the risk budget: 0.65 % of 10,000 over a 25 % stop is about 260 USDT.
    assert engine._buy('BTC/USDT', 250, 100, strategy.TREND_STOP_FRACTION)
    position = engine._active_positions('paper')[0]
    fill = position['entry_price']
    assert position['stop'] == pytest.approx(fill * (1 - strategy.TREND_STOP_FRACTION))
    assert position['target'] == pytest.approx(fill * strategy.TREND_TARGET_MULTIPLE)
    engine.running = True
    # No trailing: a mark with an ATR far above the arm level leaves the stop where it was.
    engine._mark_positions('BTC/USDT', fill * 1.5, 0.0, False)
    assert engine._active_positions('paper')[0]['stop'] == pytest.approx(position['stop'])


def test_a_trend_scan_reads_daily_candles_only_and_a_weekly_failure_cannot_block_it(engine, monkeypatch):
    engine.update_config({'time_horizon': 'trend', 'watchlist': ['BTC/USDT', 'ETH/USDT']})
    requested = []
    original = engine.data.candles

    def candles(symbol, timeframe, limit=None, **kwargs):
        requested.append(timeframe)
        if timeframe == '1w':
            raise RuntimeError('weekly candles unavailable')
        return original(symbol, timeframe, limit=limit, **kwargs)

    monkeypatch.setattr(engine.data, 'candles', candles)
    markets = engine.scan_once(allow_orders=False)
    assert requested and set(requested) == {'1d'}
    assert all('Higher-timeframe data unavailable' not in (m.get('status') or '') for m in markets)


def test_volatility_scaling_shrinks_a_trend_entry_in_proportion_and_is_off_by_default(engine, monkeypatch):
    closes = [100 * (1.08 if i % 2 else 1 / 1.08) ** 1 for i in range(30)]       # about 8 % a day: far above a 60 % target
    sigma = strategy.annualised_volatility(closes)
    assert sigma is not None and sigma > 1.0
    assert strategy.trend_size_scale(sigma, 0) == 1.0 and strategy.trend_size_scale(None, 60) == 1.0
    assert strategy.trend_size_scale(0.3, 60) == 1.0 and strategy.trend_size_scale(1.2, 60) == pytest.approx(0.5)
    assert strategy.annualised_volatility(closes[:10]) is None and strategy.annualised_volatility(['x'] * 30) is None

    engine.update_config({'time_horizon': 'trend'})
    positions, cash = [], engine._cash('paper')
    equity = engine._equity('paper', cash, positions)
    full, _, _ = engine._entry_budget('BTC/USDT', positions, cash, equity, strategy.TREND_STOP_FRACTION)
    assert engine._trend_scale('BTC/USDT') == 1.0                                 # off by default
    engine.update_config({'time_horizon': 'trend', 'trend_vol_target_pct': 60})
    import pandas as pd
    monkeypatch.setattr(engine.data, 'candles', lambda *a, **k: pd.DataFrame({'close': closes}))
    scale = engine._trend_scale('BTC/USDT')
    assert 0 < scale < 1 and scale == pytest.approx(0.6 / sigma)
    scaled, _, _ = engine._entry_budget('BTC/USDT', positions, cash, equity, strategy.TREND_STOP_FRACTION, scale=scale)
    assert scaled == pytest.approx(full * scale)
    engine.update_config({'time_horizon': 'balanced', 'trend_vol_target_pct': 60})
    assert engine._trend_scale('BTC/USDT') == 1.0                                 # other strategies never scale


def test_a_profile_saved_before_the_volatility_target_existed_loads_without_review(tmp_path, monkeypatch):
    import json
    monkeypatch.setattr(storage, 'DATA_DIR', tmp_path)
    monkeypatch.setattr(storage, 'CONFIG_PATH', tmp_path / 'settings.json')
    monkeypatch.setattr(storage, 'DB_PATH', tmp_path / 'portfolio.sqlite3')
    old = {k: v for k, v in storage.DEFAULTS.items() if k != 'trend_vol_target_pct'}
    storage.save_settings(old)
    (tmp_path / 'settings.json').write_text(json.dumps(old), encoding='utf8')
    storage._SETTINGS_CACHE = None
    cfg = storage.settings()
    assert cfg['trend_vol_target_pct'] == 0.0 and not storage.settings_health()['review_required']
