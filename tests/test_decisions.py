"""Decision records are pure, replayable and free of look-ahead.

Every check here runs offline: indicator rows are built by hand or from
synthetic candles, and the engine tests use the fake exchange from
``test_engine``. Nothing contacts a network or a credential store.
"""
import json
import random
import sys
import time
from pathlib import Path

import pandas as pd
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import decision
import storage
import strategy
import trader
from test_engine import FakeExchange, engine  # noqa: F401  (fixture)
from test_web_with_mock_exchange import webapp  # noqa: F401  (fixture)

CFG = {'min_quote_volume': 5_000_000, 'max_spread_pct': .85, 'data_max_age_seconds': 120}
SIGNAL_TS = 1_761_436_800_000
NOW = SIGNAL_TS / 1000 + 3600 + 1.25


def signal_row(**changes) -> pd.Series:
    """An indicator row that passes every completed-candle entry check."""
    row = dict(timestamp=SIGNAL_TS, close=100., volume=1500., ema20=99., ema50=98., rsi=60., atr=1.,
               vol_avg20=1000., prior_high20=100., efficiency=.5)
    row.update(changes)
    return pd.Series(row)


def decide(**changes) -> decision.Decision:
    kwargs = dict(symbol='ETH/USDT', last=signal_row(), regime_ok=True, market_ok=True, rapid=False, filters=True,
                  price=100.2, volume=20_000_000., spread_pct=.02, quote_age_s=.4, quote_ts=NOW - .4, cfg=CFG, now=NOW)
    kwargs.update(changes)
    return decision.evaluate_entry(**kwargs)


# --- Determinism and replay ----------------------------------------------------

def test_same_inputs_replay_to_the_same_decision_and_id():
    first, second = decide(), decide()
    assert first == second and first.decision_id == second.decision_id
    assert first.action == 'enter' and first.status == 'BUY setup' and first.reasons == ()
    assert first.proposal == {'price': 100.2, 'atr': 1.0, 'signal_close': 100.0, 'quality': strategy.setup_quality(signal_row()),
                              'candle_ts_ms': SIGNAL_TS, 'volume': 20_000_000.0, 'spread_pct': .02}
    assert first.decision_id.startswith('dec_') and len(first.decision_id) == 20
    # Any change to the inputs or the decision time is a different record.
    assert decide(price=100.3).decision_id != first.decision_id
    assert decide(now=NOW + 1).decision_id != first.decision_id
    assert decide(regime_ok=False).decision_id != first.decision_id


def test_decision_record_is_json_serializable_and_lists_every_check():
    record = decide(volume=1_000.).as_dict()
    json.dumps(record)
    assert [c['check'] for c in record['checks']] == ['strategy_signal', 'liquidity', 'spread', 'anti_chasing',
                                                       'quote_fresh', 'not_held', 'no_reentry_same_candle']
    assert record['inputs']['signal_candle_ts_ms'] == SIGNAL_TS and record['inputs']['quote_ts'] == NOW - .4
    assert record['inputs']['limits'] == CFG
    assert record['version'] == decision.DECISION_VERSION


def test_decision_never_reads_the_clock(monkeypatch):
    monkeypatch.setattr(time, 'time', lambda: pytest.fail('decisions must not read the wall clock'))
    assert decide().action == 'enter'


# --- Look-ahead and candle ordering -------------------------------------------

def trending_rows(count=130, duration=3_600_000, start=1_761_000_000_000):
    rows, price = [], 100.0
    for index in range(count):
        price *= 1.004 if index % 7 else .998
        rows.append([start + index * duration, price, price * 1.006, price * .995, price * 1.003, 1000 + 20 * index])
    return rows


def decision_for(rows, *, now_ms, timestamp=None):
    frame = strategy.frame_from_ohlcv(rows, timeframe='1h', now_ms=now_ms)
    table = strategy.indicators(frame)
    last = table.iloc[-1] if timestamp is None else table[table['timestamp'] == timestamp].iloc[-1]
    return decision.evaluate_entry(symbol='ETH/USDT', last=last, regime_ok=True, market_ok=None, rapid=False, filters=True,
                                   price=float(last['close']), volume=20_000_000., spread_pct=.02, quote_age_s=.3,
                                   quote_ts=now_ms / 1000 - .3, cfg=CFG, now=now_ms / 1000)


def test_a_forming_candle_cannot_change_the_decision():
    rows = trending_rows()
    close_ms = rows[-1][0] + 3_600_000
    baseline = decision_for(rows, now_ms=close_ms + 5_000)
    # The exchange also returns the candle that is still open; it must be ignored
    # whatever it says, including a crash that would fail every check.
    forming = [close_ms, 50., 50., 1., 1., 10 ** 9]
    assert decision_for(rows + [forming], now_ms=close_ms + 5_000) == baseline


def test_future_completed_candles_do_not_alter_an_earlier_decision():
    rows = trending_rows()
    close_ms = rows[-1][0] + 3_600_000
    baseline = decision_for(rows, now_ms=close_ms + 5_000)
    later = trending_rows(count=133)
    stamp, opened, turnover = later[-1][0], later[-1][1], later[-1][5]
    later[-1] = [stamp, opened, opened * 1.006, opened * .4, opened * .5, turnover]   # a future collapse
    replay = decision_for(later, now_ms=close_ms + 5_000 + 3 * 3_600_000, timestamp=rows[-1][0])
    assert replay.checks == baseline.checks and replay.score == baseline.score and replay.action == baseline.action
    assert replay.inputs['signal_candle_ts_ms'] == baseline.inputs['signal_candle_ts_ms']


def test_out_of_order_and_duplicated_candles_replay_identically():
    rows = trending_rows()
    close_ms = rows[-1][0] + 3_600_000
    baseline = decision_for(rows, now_ms=close_ms + 5_000)
    shuffled = list(rows) + rows[:5]
    random.Random(7).shuffle(shuffled)
    assert decision_for(shuffled, now_ms=close_ms + 5_000) == baseline
    conflicting = rows + [[rows[-1][0], 1., 2., .5, 1.5, 1.]]
    with pytest.raises(ValueError, match='share a timestamp'):
        decision_for(conflicting, now_ms=close_ms + 5_000)


# --- Abstention reasons and status precedence ----------------------------------

@pytest.mark.parametrize('changes, status, reason', [
    ({'volume': 10.}, decision.STATUS_LOW_VOLUME, decision.STATUS_LOW_VOLUME),
    ({'spread_pct': None}, decision.STATUS_WIDE_SPREAD, decision.STATUS_WIDE_SPREAD),
    ({'spread_pct': 2.}, decision.STATUS_WIDE_SPREAD, decision.STATUS_WIDE_SPREAD),
    ({'price': 109.}, decision.STATUS_MOVED, decision.STATUS_MOVED),
    ({'quote_age_s': None}, 'BUY setup', decision.STATUS_STALE),
    ({'quote_age_s': 500.}, 'BUY setup', decision.STATUS_STALE),
    ({'exited_this_candle': True}, 'BUY setup', decision.STATUS_EXITED),
    ({'last': signal_row(rsi=90.)}, 'Waiting: RSI', 'Waiting: RSI'),
    ({'higher_error': 'NetworkError'}, 'Higher-timeframe data unavailable: NetworkError',
     'Higher-timeframe data unavailable: NetworkError'),
])
def test_every_abstention_names_the_failed_check(changes, status, reason):
    verdict = decide(**changes)
    assert verdict.action == 'abstain' and verdict.proposal is None
    assert verdict.status == status and reason in verdict.reasons
    assert any(not c['passed'] for c in verdict.checks)


def test_low_volume_outranks_spread_and_price_move_in_the_dashboard_status():
    verdict = decide(volume=10., spread_pct=5., price=120.)
    assert verdict.status == decision.STATUS_LOW_VOLUME
    assert set(verdict.reasons) == {decision.STATUS_LOW_VOLUME, decision.STATUS_WIDE_SPREAD, decision.STATUS_MOVED}
    assert decide(spread_pct=5., price=120.).status == decision.STATUS_WIDE_SPREAD


def test_held_symbol_is_a_hold_not_an_entry():
    verdict = decide(held=True)
    assert verdict.action == 'hold' and verdict.proposal is None and decision.STATUS_HELD in verdict.reasons


def test_higher_timeframe_failure_reports_no_confidence():
    verdict = decide(higher_error='Timeout')
    assert verdict.confidence is None and verdict.score == 0


# --- Confidence heuristic --------------------------------------------------------

def test_confidence_is_bounded_and_discounted_by_spread_age_and_regime():
    base = decide().confidence
    assert 0 < base <= 1
    assert decide(spread_pct=.8).confidence < base
    assert decide(quote_age_s=110.).confidence < base
    assert decide(regime_ok=False).confidence == pytest.approx(base * .5, rel=1e-3) or decide(regime_ok=False).confidence < base
    assert decide(last=signal_row(efficiency=.95, volume=3000.)).confidence > base
    assert decide(last=signal_row(efficiency=float('nan'))).confidence is None
    for value in (decide(spread_pct=.8).confidence, decide(quote_age_s=110.).confidence):
        assert 0 <= value <= 1


# --- Exit policy ---------------------------------------------------------------

def position(**changes):
    row = {'symbol': 'ETH/USDT', 'qty': 1., 'cost': 100., 'highest': 104., 'stop': 97., 'target': 112.}
    row.update(changes)
    return row


def test_exit_policy_checks_in_the_engines_order():
    assert decision.evaluate_exit(position(), 97.)['reason'] == 'Stored stop / trailing stop (software monitored)'
    assert decision.evaluate_exit(position(), 112.)['reason'] == 'Stored profit target (software monitored)'
    # Trailing stop update: a new high raises the stop, and a later bid at or
    # under the raised stop exits for that reason.
    held = decision.evaluate_exit(position(), 110., atr=2.)
    assert held['action'] == 'hold' and held['highest'] == 110. and held['stop'] == 105.
    assert decision.evaluate_exit(position(highest=110., stop=105.), 104.9, atr=2.)['reason'] == 'Stored stop / trailing stop (software monitored)'
    raised = decision.evaluate_exit(position(highest=110.), 104., atr=2.)
    assert raised['reason'] == 'Updated trailing stop (software monitored)' and raised['stop'] == 105.
    assert decision.evaluate_exit(position(), 101., atr=2., trend_broken=True)['reason'] == 'Completed-candle trend reversal'
    # Without ATR (protective poll) the trailing levels are left alone.
    poll = decision.evaluate_exit(position(), 101.)
    assert poll['action'] == 'hold' and poll['stop'] == 97. and poll['highest'] == 104.


def test_verified_dust_is_skipped_before_any_exit_rule():
    verdict = decision.evaluate_exit(position(), 50., dust=True)
    assert verdict['action'] == 'skip' and verdict['stop'] == 97.


def test_trailing_stop_never_moves_down():
    verdict = decision.evaluate_exit(position(stop=103.), 104., atr=5.)
    assert verdict['stop'] == 103. and verdict['action'] == 'hold'


# --- Decision log and engine wiring -------------------------------------------

def test_decision_log_is_bounded_and_keeps_the_latest_per_symbol():
    log = decision.DecisionLog(limit=5)
    for index in range(8):
        log.add(decide(symbol='A/USDT' if index % 2 else 'B/USDT', now=NOW + index))
    assert len(log) == 5
    assert log.latest['A/USDT'].decided_at == NOW + 7 and log.latest['B/USDT'].decided_at == NOW + 6
    assert [d['decided_at'] for d in log.recent()] == [NOW + 7, NOW + 6, NOW + 5, NOW + 4, NOW + 3]
    assert all(d['symbol'] == 'A/USDT' for d in log.recent('A/USDT'))
    assert len(log.recent(limit=2)) == 2 and len(log.recent(limit=0)) == 1


def test_every_scanned_symbol_gets_a_decision_record(engine):
    rows = engine.scan_once(allow_orders=False)
    assert len(rows) == 8 and set(engine.decisions.latest) == {row['symbol'] for row in rows}
    for row in rows:
        record = engine.decisions.latest[row['symbol']]
        assert row['decision_id'] == record.decision_id and row['decision'] == record.action == 'abstain'
        assert row['reasons'] == list(record.reasons) and record.reasons
        assert record.inputs['signal_candle_ts_ms'] is not None and record.inputs['quote_age_s'] is not None


def test_entry_carries_its_decision_id_into_the_order_journal(engine, monkeypatch):
    monkeypatch.setattr(trader, 'entry_signal', lambda *a, **kw: (True, 'BUY setup', 100))
    monkeypatch.setattr(trader, 'entry_price_ok', lambda *a, **kw: True)
    engine.running = True
    engine.scan_once(allow_orders=True)
    held = engine._active_positions('paper')
    assert held
    journal = {row['decision_id']: row['status']
               for row in storage.rows("SELECT decision_id, status FROM order_journal WHERE side='BUY'")}
    assert journal and all(status == 'accounted' for status in journal.values())
    for symbol in {p['symbol'] for p in held}:
        record = engine.decisions.latest[symbol]
        assert record.action == 'enter' and record.decision_id in journal


def test_decisions_api_filters_by_symbol_and_bounds_the_limit(webapp):
    client = TestClient(webapp.app, base_url='http://127.0.0.1:8765', client=('127.0.0.1', 50000))
    assert client.get('/api/decisions').json() == {'decisions': []}
    assert client.post('/api/scan', headers={'x-cryptopilot-token': webapp.CSRF}).status_code == 200
    everything = client.get('/api/decisions').json()['decisions']
    assert len(everything) == 8 and {d['action'] for d in everything} == {'abstain'}
    assert {'decision_id', 'checks', 'reasons', 'inputs', 'confidence', 'version'} <= set(everything[0])
    only_btc = client.get('/api/decisions', params={'symbol': 'btc/usdt', 'limit': 3}).json()['decisions']
    assert [d['symbol'] for d in only_btc] == ['BTC/USDT']
    assert len(client.get('/api/decisions', params={'limit': 2}).json()['decisions']) == 2
    assert len(client.get('/api/decisions', params={'limit': 0}).json()['decisions']) == 1
    assert client.get('/api/decisions', params={'limit': 'x'}).status_code == 422
