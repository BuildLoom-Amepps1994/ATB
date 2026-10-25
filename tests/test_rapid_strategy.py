"""Minute signal behavior remains chronological, conservative, and cost-aware."""
import math
import pandas as pd
import pytest
import strategy
import storage


def test_minute_timeframes_exclude_forming_candle():
    rows = [[60000*i,100,101,99,100,1000] for i in range(1,181)]
    frame = strategy.frame_from_ohlcv(rows,timeframe='1m',now_ms=180*60000+30000)
    assert len(frame)==179
    assert frame.iloc[-1]['timestamp']==179*60000


def test_rapid_signal_accepts_lower_volatility_but_keeps_strict_breakout():
    row = pd.Series(dict(close=100.,volume=1000,ema20=99.99,ema50=99.9,rsi=60.,
                         atr=.05,vol_avg20=1000,prior_high20=100.005))
    assert strategy.entry_signal(row,rapid=True)[0]
    assert not strategy.entry_signal(row)[0]
    row['prior_high20']=100.2
    assert not strategy.entry_signal(row,rapid=True)[0]


def test_rapid_requires_confirmation_and_limits_overbought():
    row = pd.Series(dict(close=100.,volume=1000,ema20=99.99,ema50=99.9,rsi=60.,
                         atr=.05,vol_avg20=1000,prior_high20=100.005))
    assert not strategy.entry_signal(row,regime_ok=False,rapid=True)[0]
    row['rsi']=75
    assert not strategy.entry_signal(row,rapid=True)[0]


def test_rapid_stops_have_explicit_bounds_and_preserve_standard_defaults():
    assert strategy.stop_fraction(.05,100,rapid=True)==.008
    assert strategy.stop_fraction(10,100,rapid=True)==.03
    assert strategy.stop_fraction(.05,100)==.035
    with pytest.raises(ValueError):
        strategy.stop_fraction(float('nan'),100,rapid=True)


def test_rapid_config_is_opt_in_and_validated():
    defaults = storage.validate_settings({})
    assert defaults['time_horizon']=='balanced' and defaults['protective_poll_seconds']==5
    cfg = storage.validate_settings({'time_horizon':'rapid','scan_seconds':10,'protective_poll_seconds':2})
    assert cfg['scan_seconds']==10
    for fields in ({'scan_seconds':1},{'protective_poll_seconds':1},{'protective_poll_seconds':2.5}):
        with pytest.raises(storage.ConfigValidationError):
            storage.validate_settings(fields)


def test_paper_reset_clears_new_execution_metadata_only_for_selected_exchange(tmp_path,monkeypatch):
    monkeypatch.setattr(storage,'DATA_DIR',tmp_path)
    monkeypatch.setattr(storage,'DB_PATH',tmp_path/'portfolio.sqlite3')
    monkeypatch.setattr(storage,'CONFIG_PATH',tmp_path/'settings.json')
    storage.init_db()
    for name in ('last_exit.paper.BTC/USDT.1m','manual_request.paper.synthetic'):
        for exchange in ('mexc','kraken'):
            storage.set_meta(storage.scope_key(name,exchange),'sentinel')
    storage.reset_paper('mexc')
    assert storage.get_meta('last_exit.paper.BTC/USDT.1m.mexc')==''
    assert storage.get_meta('manual_request.paper.synthetic.mexc')==''
    assert storage.get_meta('manual_request.paper.synthetic.kraken')=='sentinel'


def test_rapid_backtest_identifies_profile_and_exact_completed_higher_frame():
    end = 1_800_000_000_000
    def rows(count,step):
        return [[end-(count-i)*step,100,100.1,99.9,100,1000] for i in range(count)]
    result = strategy.backtest(rows(250,60000),storage.DEFAULTS,timeframe='1m',
        higher_rows=rows(300,300000),higher_timeframe='5m',now_ms=end)
    assert result['assumptions']['risk_profile']=='rapid'
    assert result['assumptions']['regime_mode']=='exact_completed_higher_timeframe'
    assert result['benchmark_net_of_costs']
    assert all(math.isfinite(p['equity']) for p in result['equity_curve'])
