"""The abnormal-volatility breaker, the liquidity-share cap and the consecutive-loss stop.

Three independent risk-gate checks added 2026-11-04. All exchanges are fakes;
no network, no credentials, paper mode throughout.
"""
import json
import time
import uuid
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

import risk
import storage
import trader
from test_engine import engine  # noqa: F401  (fixture)
from test_web_with_mock_exchange import webapp  # noqa: F401  (fixture)


def candidate(symbol='ETH/USDT', atr=5):
    # (score, volume, symbol, observed price, atr, candle, signal close)
    return (100, 20000000, symbol, 100.01, atr, 123, 100)


def observe(engine, *symbols):
    for symbol in symbols:
        engine._observe_ticker(symbol, {'bid': 100, 'ask': 100.01, 'last': 100, 'quoteVolume': 20000000,
                                        'timestamp': int(time.time() * 1000)})


def losing_round_trip(engine, symbol='BTC/USDT'):
    engine.cfg['max_orders_per_minute'] = 60          # these round trips are faster than the order-rate limit
    assert engine._buy(symbol, 200, 100, .04, volume=20000000)
    position = next(p for p in engine._active_positions('paper') if p['symbol'] == symbol)
    assert engine._sell(position, 90, 'test loss')


def winning_round_trip(engine, symbol='BTC/USDT'):
    engine.cfg['max_orders_per_minute'] = 60
    assert engine._buy(symbol, 200, 100, .04, volume=20000000)
    position = next(p for p in engine._active_positions('paper') if p['symbol'] == symbol)
    assert engine._sell(position, 120, 'test win')


# ------------------------------------------------------------ settings
def test_new_controls_have_defaults_bounds_and_an_off_switch():
    cfg = storage.validate_settings({})
    assert (cfg['max_entry_atr_pct'], cfg['max_volume_share_pct'], cfg['max_consecutive_losses']) == (15.0, 0.5, 4)
    assert storage.validate_settings({'max_entry_atr_pct': 0, 'max_volume_share_pct': 0, 'max_consecutive_losses': 0})
    for key, bad in (('max_entry_atr_pct', 101), ('max_volume_share_pct', 11), ('max_consecutive_losses', 51),
                     ('max_consecutive_losses', 2.5), ('max_entry_atr_pct', -1)):
        with pytest.raises(storage.ConfigValidationError):
            storage.validate_settings({key: bad})
    # A profile saved before the controls existed adopts them without a review latch.
    assert {'max_entry_atr_pct', 'max_volume_share_pct', 'max_consecutive_losses'} <= storage.LATER_FIELDS


# ---------------------------------------------------------- volatility
def test_abnormal_volatility_blocks_the_entry_and_only_the_entry(engine):
    assert not engine._buy('BTC/USDT', 200, 100, .04, atr=20, volume=20000000)     # ATR 20 % of price
    assert engine.risk.last_block['reasons'][0].startswith('Abnormal volatility')
    assert engine._active_positions('paper') == []
    assert engine._buy('BTC/USDT', 200, 100, .04, atr=14.9, volume=20000000)
    position = engine._active_positions('paper')[0]
    # The exit is never judged on volatility.
    assert engine._sell(position, 50, 'crash exit')
    assert engine._active_positions('paper') == []


def test_volatility_breaker_can_be_switched_off(engine):
    engine.cfg['max_entry_atr_pct'] = 0
    assert engine._buy('BTC/USDT', 200, 100, .04, atr=60, volume=20000000)


def test_scan_candidate_with_abnormal_volatility_is_withheld_as_risk_gate(engine, monkeypatch):
    events = []
    monkeypatch.setattr(trader.observability, 'event', lambda kind, **f: events.append((kind, f)) or {})
    engine.running = True
    observe(engine, 'ETH/USDT')
    engine._enter_candidates([candidate(atr=50)])
    assert engine._active_positions('paper') == []
    withheld = [f for kind, f in events if kind == 'decision.withheld']
    assert withheld and withheld[0]['reason'] == 'risk_gate'
    blocked = [f for kind, f in events if kind == 'risk.blocked']
    assert any(r.startswith('Abnormal volatility') for r in blocked[0]['reasons'])
    checks = {c['check']: c for c in engine._last_authorization.checks}
    assert checks['volatility']['atr_pct'] == pytest.approx(100 * 50 / 100.01, rel=1e-6)


# ------------------------------------------------------------- liquidity
def test_order_above_the_turnover_share_is_blocked(engine):
    # 0.5 % of a 20,000 USDT day is 100 USDT: a 200 USDT order is refused, a 90 USDT one passes.
    assert not engine._buy('ETH/USDT', 200, 100, .04, volume=20000)
    assert "24h turnover" in engine.risk.last_block['reasons'][0]
    assert engine._buy('ETH/USDT', 90, 100, .04, volume=20000)
    checks = {c['check']: c for c in engine._last_authorization.checks}
    assert checks['liquidity_share']['passed'] is True
    assert (checks['liquidity_share']['volume_24h'], checks['liquidity_share']['limit']) == (20000.0, 100.0)


def test_turnover_share_is_skipped_without_a_volume_and_off_at_zero(engine):
    assert engine._buy('ETH/USDT', 200, 100, .04)                       # no 24 h volume supplied
    assert 'liquidity_share' not in {c['check'] for c in engine._last_authorization.checks}
    engine.cfg['max_volume_share_pct'] = 0
    observe(engine, 'ETH/USDT')                                         # the held asset needs a fresh mark
    assert engine._buy('BTC/USDT', 200, 100, .04, volume=1)


def test_zero_turnover_fails_closed(engine):
    assert not engine._buy('ETH/USDT', 200, 100, .04, volume=0)
    assert "24h turnover" in engine.risk.last_block['reasons'][0]


def test_manual_buy_passes_its_quote_volume_to_the_gate(engine, monkeypatch):
    engine.cfg['max_volume_share_pct'] = 0.0005          # 0.0005 % of 20,000,000 = 100 USDT
    with pytest.raises(storage.ConfigValidationError, match='24h turnover'):
        engine.manual_trade('ETH/USDT', 'buy', 200, request_id=str(uuid.uuid4()))
    assert engine._active_positions('paper') == []
    engine.manual_trade('ETH/USDT', 'buy', 90, request_id=str(uuid.uuid4()))
    assert [p['symbol'] for p in engine._active_positions('paper')] == ['ETH/USDT']


# ------------------------------------------------------ consecutive losses
def test_four_losing_exits_in_a_row_latch_entries_for_the_day(engine):
    for _ in range(3):
        losing_round_trip(engine)
    assert engine.risk.loss_streak('paper') == {'date': risk._today(), 'count': 3, 'latched_at': None}
    assert engine._buy('BTC/USDT', 200, 100, .04, volume=20000000)        # still allowed at three
    position = engine._active_positions('paper')[0]
    assert engine._sell(position, 90, 'fourth loss')
    latched = engine.risk.loss_streak_stop('paper')
    assert latched
    assert not engine._buy('BTC/USDT', 200, 100, .04, volume=20000000)
    assert engine.risk.last_block['reasons'][0].startswith('Consecutive-loss stop latched')
    # Exits remain possible while latched: a manual sell of a holding is not an entry.
    status = engine.risk.status()
    assert status['loss_streak_stop'] == latched and status['loss_streak']['count'] == 4
    # Restart: the latch is durable.
    other = trader.TradingEngine()
    assert other.risk.loss_streak_stop('paper') == latched
    assert not other._buy('BTC/USDT', 200, 100, .04, volume=20000000)


def test_a_winning_exit_resets_the_streak(engine):
    losing_round_trip(engine)
    losing_round_trip(engine)
    losing_round_trip(engine)
    winning_round_trip(engine)
    assert engine.risk.loss_streak('paper')['count'] == 0
    losing_round_trip(engine)
    assert engine.risk.loss_streak('paper')['count'] == 1 and not engine.risk.loss_streak_stop('paper')


def test_streak_stop_clears_with_the_phrase_or_the_next_utc_day(engine, monkeypatch):
    for _ in range(4):
        losing_round_trip(engine)
    assert engine.risk.loss_streak_stop('paper')
    with pytest.raises(storage.ConfigValidationError, match='RESUME TRADING'):
        engine.risk.clear_loss_streak_stop('paper', 'yes')
    engine.risk.clear_loss_streak_stop('paper', risk.RELEASE_PHRASE)
    assert engine.risk.loss_streak('paper') == {'date': risk._today(), 'count': 0, 'latched_at': None}
    assert engine._buy('BTC/USDT', 200, 100, .04, volume=20000000)
    position = engine._active_positions('paper')[0]
    assert engine._sell(position, 90, 'loss after clearing')
    assert engine.risk.loss_streak('paper')['count'] == 1
    for _ in range(3):
        losing_round_trip(engine)
    assert engine.risk.loss_streak_stop('paper')
    tomorrow = (date.fromisoformat(risk._today()) + timedelta(days=1)).isoformat()
    monkeypatch.setattr(risk, '_today', lambda: tomorrow)
    assert engine.risk.loss_streak('paper') == {'date': tomorrow, 'count': 0, 'latched_at': None}
    assert engine._buy('BTC/USDT', 200, 100, .04, volume=20000000)


def test_streak_stop_is_off_at_zero_and_counts_manual_exits(engine):
    engine.cfg['max_consecutive_losses'] = 0
    for _ in range(6):
        losing_round_trip(engine)
    assert engine.risk.loss_streak('paper') == {'date': risk._today(), 'count': 6, 'latched_at': None}
    engine.cfg['max_consecutive_losses'] = 2
    # A limit set below the streak already run blocks the next entry at once.
    assert not engine._buy('BTC/USDT', 200, 100, .04, volume=20000000)
    assert engine.risk.loss_streak_stop('paper')
    engine.risk.clear_loss_streak_stop('paper', risk.RELEASE_PHRASE)
    assert engine._buy('BTC/USDT', 200, 100, .04, volume=20000000)
    engine.public.price = 90
    engine.manual_trade('BTC/USDT', 'sell', request_id=str(uuid.uuid4()))
    assert engine.risk.loss_streak('paper')['count'] == 1 and not engine.risk.loss_streak_stop('paper')
    losing_round_trip(engine)
    assert engine.risk.loss_streak_stop('paper')


def test_unreadable_streak_record_counts_from_zero(engine):
    storage.set_meta(engine.risk._key('loss_streak', 'paper'), '{not json')
    assert engine.risk.loss_streak('paper')['count'] == 0
    storage.set_meta(engine.risk._key('loss_streak', 'paper'), json.dumps({'date': risk._today(), 'count': 'x'}))
    assert engine.risk.loss_streak('paper')['count'] == 0


# ------------------------------------------------------------------ API
def test_risk_endpoint_clears_the_streak_stop_with_the_phrase(webapp):
    client = TestClient(webapp.app, base_url='http://127.0.0.1:8765', client=('127.0.0.1', 50000))
    headers = {'x-cryptopilot-token': webapp.CSRF}
    engine = webapp.engine
    for _ in range(4):
        losing_round_trip(engine)
    assert client.get('/api/state').json()['risk']['loss_streak_stop']
    assert client.post('/api/risk', headers=headers, json={'action': 'clear_loss_streak_stop', 'confirmation': 'no'}).status_code == 400
    r = client.post('/api/risk', headers=headers, json={'action': 'clear_loss_streak_stop', 'confirmation': 'RESUME TRADING'})
    assert r.status_code == 200
    assert r.json()['loss_streak_stop'] is None and r.json()['loss_streak']['count'] == 0
