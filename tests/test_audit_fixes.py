"""Regression tests for the defects confirmed by the 2026-11-04 audit (risk and ledger lenses).

* The runaway exit-rate bound was shared by every pair, so one protective
  exit that the exchange kept rejecting every poll used up the allowance and
  every other position's protective exit was refused.
* A verified dust remainder withheld its pair from automated entries for
  good; only a manual buy could consolidate it.
* The live state snapshot read the paper cash ledger unconditionally, so a
  corrupt paper row took GET /api/state offline in live mode.
* A paper reset kept the consecutive-loss record.

All exchanges are fakes; no network, no credentials.
"""

import pytest

import risk
import storage
import trader
from test_engine import arm_fake_live, engine  # noqa: F401  (fixture)
from test_risk_governance import candidate, losing_round_trip, observe


def capture(monkeypatch):
    log = []
    monkeypatch.setattr(trader.observability, 'event', lambda kind, **fields: log.append((kind, fields)) or {})
    return log


# ------------------------------------------------------------- exit rate
def test_one_pairs_rejected_exits_do_not_starve_another_pairs_exit(engine):
    assert engine._buy('BTC/USDT', 200, 100, .04)
    observe(engine, 'BTC/USDT')
    assert engine._buy('ETH/USDT', 200, 100, .04)
    engine.cfg['max_orders_per_minute'] = 1                        # exit allowance: 3 per minute per pair
    for _ in range(3):                                             # BTC's exit was refused three times this minute
        intent = storage.create_order_intent(engine.exchange_name, 'BTC/USDT', 'SELL', quantity=1., mode='paper')
        storage.update_order_intent(intent, 'rejected')
    positions = engine._active_positions('paper')
    blocked = engine._authorize('BTC/USDT', 'SELL', 'exit', 100., 100., positions=positions, cash=None, quantity=1.)
    assert not blocked.allowed and blocked.failed[0].startswith('Runaway order rate for BTC/USDT')
    allowed = engine._authorize('ETH/USDT', 'SELL', 'exit', 100., 100., positions=positions, cash=None, quantity=1.)
    assert allowed.allowed
    checks = {c['check']: c for c in allowed.checks}
    assert checks['exit_order_rate']['recent'] == 1                   # ETH's own buy a moment ago


def test_the_pairs_own_runaway_rate_is_still_bounded(engine):
    assert engine._buy('BTC/USDT', 200, 100, .04)
    engine.cfg['max_orders_per_minute'] = 1
    positions = engine._active_positions('paper')
    for _ in range(risk.EXIT_RATE_MULTIPLE):
        intent = storage.create_order_intent(engine.exchange_name, 'BTC/USDT', 'SELL', quantity=1., mode='paper')
        storage.update_order_intent(intent, 'rejected')
    assert not engine._sell(positions[0], 90, 'test')
    assert engine.risk.last_block['reasons'][0].startswith('Runaway order rate')


# ------------------------------------------------------- dust consolidation
def dust_row(engine, symbol='BTC/USDT', qty=.05, entry=300.0):
    with storage.connection() as db:
        db.execute('''INSERT INTO positions(exchange,mode,symbol,qty,entry_price,cost,highest,stop,target,opened_at)
                      VALUES (?,?,?,?,?,?,?,?,?,?)''',
                   (engine.exchange_name, 'paper', symbol, qty, entry, qty * entry, entry, entry * .96, entry * 1.11,
                    trader.now()))


def test_a_scan_entry_consolidates_a_verified_dust_remainder(engine, monkeypatch):
    events = capture(monkeypatch)
    dust_row(engine)                                               # 0.05 BTC: 5 USDT at the current price, below the 10 USDT minimum
    observe(engine, 'BTC/USDT')
    engine.running = True
    engine._enter_candidates([candidate('BTC/USDT')])
    merged = engine._active_positions('paper')
    assert len(merged) == 1 and merged[0]['qty'] > .05
    assert merged[0]['highest'] == pytest.approx(merged[0]['entry_price'])
    assert merged[0]['stop'] < 100 < merged[0]['target']
    trade = storage.rows("SELECT gross, fee FROM trades WHERE side='BUY'")[0]
    assert merged[0]['cost'] == pytest.approx(15 + trade['gross'] + trade['fee'], rel=1e-6)   # blended basis
    assert [f for kind, f in events if kind == 'decision.withheld'] == []
    assert any('anchored on the new fill' in row['message'] for row in storage.rows('SELECT message FROM audit'))


def test_a_scan_entry_still_withholds_a_tracked_position_above_dust(engine, monkeypatch):
    events = capture(monkeypatch)
    assert engine._buy('BTC/USDT', 200, 100, .04)
    observe(engine, 'BTC/USDT')
    engine.running = True
    engine._enter_candidates([candidate('BTC/USDT')])
    assert len(engine._active_positions('paper')) == 1
    assert [f['reason'] for kind, f in events if kind == 'decision.withheld'] == ['held']


# ---------------------------------------------------------------- snapshot
def test_live_snapshot_survives_a_corrupt_paper_ledger(engine):
    arm_fake_live(engine)
    storage.set_meta(engine._key('paper_cash'), 'not a number')
    state = engine.snapshot()
    assert state['mode'] == 'live' and state['paper_cash'] is None


def test_paper_snapshot_still_reports_a_corrupt_ledger(engine):
    storage.set_meta(engine._key('paper_cash'), 'nan')
    with pytest.raises(ValueError):
        engine.snapshot()


# ------------------------------------------------------------- paper reset
def test_paper_reset_clears_the_consecutive_loss_record(engine):
    for _ in range(4):
        losing_round_trip(engine)
    assert engine.risk.loss_streak_stop('paper')
    engine.reset_paper_portfolio()
    assert engine.risk.loss_streak('paper') == {'date': risk._today(), 'count': 0, 'latched_at': None}
    assert engine.risk.loss_streak_stop('paper') is None
    assert engine._buy('BTC/USDT', 200, 100, .04, volume=20000000)
