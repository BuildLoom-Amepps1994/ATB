"""Executable-side quote and small-remainder controls; fake market only."""
import time
import uuid

import pytest

from test_engine import engine
import storage


def quote(bid=100, ask=100.01, last=100):
    return {'bid':bid,'ask':ask,'last':last,'timestamp':int(time.time()*1000),'quoteVolume':20000000}


def test_stale_last_trade_cannot_hide_a_stop_breach_at_the_bid(engine):
    assert engine._buy('BTC/USDT', 200, 100, .04)
    engine.public.fetch_ticker = lambda _: quote(bid=90, ask=91, last=100)
    engine.running = True
    assert engine._monitor_if_due(force=True) == {'BTC/USDT'}
    fill = storage.rows("SELECT * FROM trades WHERE side='SELL'")[0]
    assert fill['price'] == pytest.approx(90 * .998)
    assert engine._active_positions('paper') == []


def test_buy_uses_ask_while_portfolio_keeps_bid_and_ignores_last(engine):
    engine.public.fetch_ticker = lambda _: quote(bid=100, ask=100.01, last=500)
    result = engine.manual_trade('BTC/USDT','buy',200,request_id=str(uuid.uuid4()))
    assert result['status'] == 'filled'
    assert storage.rows('SELECT * FROM trades')[0]['price'] == pytest.approx(100.01 * 1.002)
    position = engine.snapshot()['positions'][0]
    assert position['mark'] == 100 and position['price_basis'] == 'bid'
    assert position['unrealized'] == pytest.approx(round(position['qty'] * 100 * .998 * .999 - position['cost'],2))


def test_missing_ask_can_monitor_a_sell_but_cannot_support_a_new_buy(engine):
    ticker = quote(bid=90,ask=None,last=100)
    assert engine._observe_ticker('BTC/USDT',ticker)[0] == 90
    with pytest.raises(ValueError):
        engine._observe_ticker('BTC/USDT',ticker,for_buy=True)
    assert not engine._fresh('BTC/USDT')


@pytest.mark.parametrize('ticker', [quote(bid=None), quote(ask=99), quote(bid=-1), quote(ask=-1),
                                    quote(bid=True), quote(ask=float('nan')), {'last':100}])
def test_invalid_quotes_never_fall_back_to_last_trade_and_invalidate_old_marks(engine,ticker):
    engine._observe_ticker('BTC/USDT',quote())
    assert engine._fresh('BTC/USDT')
    with pytest.raises(ValueError):
        engine._observe_ticker('BTC/USDT',ticker,for_buy=True)
    assert not engine._fresh('BTC/USDT')


def test_subminimum_owned_dust_does_not_block_other_coins_or_use_a_position_slot(engine):
    assert engine._buy('BTC/USDT',1,100,.04)
    original = engine._active_positions('paper')[0]
    engine.public.fetch_ticker = lambda symbol: quote(bid=90,ask=90.01) if symbol=='BTC/USDT' else quote()
    engine._observe_ticker('BTC/USDT',engine.public.fetch_ticker('BTC/USDT'))
    engine._observe_ticker('ETH/USDT',quote())
    engine.cfg['max_positions'] = 1
    engine.running = True
    engine._enter_candidates([(100,20000000,'ETH/USDT',100.01,5,123,100)])
    positions = {p['symbol']:p for p in engine._active_positions('paper')}
    assert set(positions) == {'BTC/USDT','ETH/USDT'}
    assert positions['BTC/USDT']['cost'] == original['cost']
    assert positions['BTC/USDT']['qty'] == original['qty']
    dust = next(p for p in engine.snapshot()['positions'] if p['symbol']=='BTC/USDT')
    assert dust['dust'] and dust['exit_untradable']
    eth_value = positions['ETH/USDT']['qty'] * engine.last_prices['ETH/USDT']
    dust_value = positions['BTC/USDT']['qty'] * engine.last_prices['BTC/USDT']
    engine.cfg['max_exposure_pct'] = (eth_value + 100) / 100
    assert engine._entry_budget('SOL/USDT',[positions['ETH/USDT']],9000,10000,.04)[0] == pytest.approx(100)
    assert engine._entry_budget('SOL/USDT',list(positions.values()),9000,10000,.04)[0] == pytest.approx(100 - dust_value)


def test_stale_small_holdings_still_block_new_risk(engine):
    assert engine._buy('BTC/USDT',1,100,.04)
    engine._observe_ticker('ETH/USDT',quote())
    engine.running = True
    engine._enter_candidates([(100,20000000,'ETH/USDT',100.01,5,123,100)])
    assert [p['symbol'] for p in engine._active_positions('paper')] == ['BTC/USDT']
    assert 'stale' in engine.risk_block.lower()


def test_unknown_market_minimum_cannot_turn_a_stopped_position_into_ignored_dust(engine):
    assert engine._buy('BTC/USDT',1,100,.04)
    engine.public.markets['BTC/USDT']['limits'] = {}
    engine._observe_ticker('BTC/USDT',quote(bid=90,ask=90.01))
    engine._observe_ticker('ETH/USDT',quote())
    engine.running = True
    engine._enter_candidates([(100,20000000,'ETH/USDT',100.01,5,123,100)])
    assert [p['symbol'] for p in engine._active_positions('paper')] == ['BTC/USDT']
    assert 'protective exit' in engine.risk_block
