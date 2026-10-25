"""Offline unit tests. Run with: python -m pytest -q"""
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pandas as pd
import pytest
from strategy import backtest, entry_signal, frame_from_ohlcv, indicators, stop_fraction


def fake_history(n=250):
    rows=[]
    for i in range(n):
        price=150 + .12*i + 3.0*math.sin(i*.31)
        rows.append([1700000000000 + 3600000*i, price-.3,price+1.5,price-1.7,price,500000+10000*(i%8)])
    return rows


def test_indicators_are_finite():
    x=indicators(frame_from_ohlcv(fake_history()))
    assert len(x)==249
    assert x.iloc[-1]['ema20']>0
    assert 0 <= x.iloc[-1]['rsi'] <= 100
    assert x.iloc[-1]['atr']>0


def test_unfinished_candle_not_used():
    hist=fake_history(170)
    x=frame_from_ohlcv(hist)
    assert len(x)==169
    assert x.iloc[-1]['timestamp']==hist[-2][0]


def test_rsi_handles_constant_rise():
    rows=[]
    for i in range(150):
        p=100+i*.1
        rows.append([1700000000000+i*3600000,p,p+1,p-1,p,1000])
    x=indicators(frame_from_ohlcv(rows))
    assert x.iloc[-1]['rsi']==100
    ok,_,_=entry_signal(x.iloc[-1],True)
    assert not ok  # Avoid chasing extremely overbought markets.


def test_position_sizing_floor_and_cap():
    assert abs(stop_fraction(1,100)-.035)<1e-10
    assert abs(stop_fraction(10,100)-.12)<1e-10


def test_backtest_is_deterministic_and_cost_aware():
    cfg={'fee_pct':.1,'slippage_pct':.2,'risk_pct':.65,'max_position_pct':8}
    a=backtest(fake_history(440),cfg)
    b=backtest(fake_history(440),cfg)
    assert a==b
    assert a['starting_equity']==10000
    assert a['closed_trades']>=0
    assert 0<=a['win_rate_pct']<=100
    assert a['max_drawdown_pct']>=0


def test_backtest_rejects_tiny_dataset():
    with pytest.raises(ValueError):
        backtest(fake_history(50),{})
