"""Crash after a fill commit must not lose its durable request result."""
import json
import uuid
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import storage
import trader
from test_engine import arm_fake_live, engine


@pytest.mark.parametrize('mode', ['paper', 'live'])
@pytest.mark.parametrize('side', ['buy', 'sell'])
def test_restart_after_fill_commit_replays_result_without_another_order(engine, monkeypatch, mode, side):
    client = arm_fake_live(engine) if mode == 'live' else engine.public
    if side == 'sell':
        assert engine._buy('BTC/USDT', 200, 100, .04)
    engine.running = False
    identifier, context = str(uuid.uuid4()), engine.manual_order_context
    method = '_buy' if side == 'buy' else '_sell'
    original = getattr(engine, method)
    def crash(*args, **kwargs):
        assert original(*args, **kwargs)
        raise SystemExit('simulated crash immediately after ledger commit')
    with monkeypatch.context() as patch:
        patch.setattr(engine, method, crash)
        with pytest.raises(SystemExit):
            engine.manual_trade('BTC/USDT', side, 200 if side == 'buy' else None,
                                f'PLACE LIVE {side.upper()}' if mode == 'live' else '',
                                request_id=identifier, expected_context=context)
    before = len(client.orders)
    result = engine.manual_trade('BTC/USDT', side, 200 if side == 'buy' else None,
                                f'PLACE LIVE {side.upper()}' if mode == 'live' else '',
                                request_id=identifier, expected_context=context)
    assert result['status'] in ('filled', 'partial')
    assert result['request_id'] == identifier
    assert len(client.orders) == before
    raw = storage.get_meta(engine._key(f'manual_request.{mode}.{identifier}'))
    assert json.loads(raw)['result'] == result
    if side == 'sell':
        assert storage.get_meta(engine._key(f'last_exit.{mode}.BTC/USDT.1h'))
    else:
        assert storage.get_meta(engine._key(f'last_entry.{mode}.BTC/USDT'))


@pytest.mark.parametrize('mode', ['paper', 'live'])
def test_completion_write_failure_rolls_back_ledger_and_retains_live_intent(engine, monkeypatch, mode):
    client = arm_fake_live(engine) if mode == 'live' else engine.public
    engine.running = False
    def fail(*args):
        raise OSError('simulated request-result disk failure')
    monkeypatch.setattr(engine, '_complete_manual_request', fail)
    with pytest.raises(storage.ConfigValidationError):
        engine.manual_trade('BTC/USDT', 'buy', 200,
                            'PLACE LIVE BUY' if mode == 'live' else '',
                            request_id=str(uuid.uuid4()), expected_context=engine.manual_order_context)
    assert storage.rows('SELECT * FROM trades') == []
    assert engine._active_positions(mode) == []
    assert storage.get_meta(engine._key(f'last_entry.{mode}.BTC/USDT')) == ''
    if mode == 'live':
        assert len(client.orders) == 1
        assert storage.unresolved_orders() and engine.live_halted
    else:
        assert engine._cash('paper') == 10000


def test_post_commit_audit_failure_does_not_overwrite_a_confirmed_request(engine, monkeypatch):
    identifier = str(uuid.uuid4())
    def fail(*args):
        raise OSError('simulated audit failure after commit')
    with monkeypatch.context() as patch:
        patch.setattr(storage, 'audit', fail)
        result = engine.manual_trade('BTC/USDT', 'buy', 200, request_id=identifier)
    assert result['status'] == 'filled'
    assert json.loads(storage.get_meta(engine._key(f'manual_request.paper.{identifier}')))['status'] == 'filled'
    assert len(storage.rows('SELECT * FROM trades')) == 1
    assert engine.manual_trade('BTC/USDT', 'buy', 200, request_id=identifier) == result


def test_settings_review_blocks_new_live_buys_but_keeps_deliberate_exits_available(engine):
    client = arm_fake_live(engine)
    assert engine._buy('BTC/USDT',200,100,.04)
    engine.running = False
    storage.CONFIG_PATH.write_text('{damaged-config')
    with pytest.raises(storage.ConfigValidationError,match='recovered settings'):
        engine.manual_trade('ETH/USDT','buy',200,'PLACE LIVE BUY',
                            request_id=str(uuid.uuid4()),expected_context=engine.manual_order_context)
    assert len(client.orders) == 1
    result = engine.manual_trade('BTC/USDT','sell',confirmation='PLACE LIVE SELL',
                                request_id=str(uuid.uuid4()),expected_context=engine.manual_order_context)
    assert result['status'] == 'filled' and len(client.orders) == 2


def test_confirmed_request_can_be_read_without_reexecution_after_a_later_live_halt(engine):
    client = arm_fake_live(engine)
    engine.running = False
    identifier, context = str(uuid.uuid4()), engine.manual_order_context
    result = engine.manual_trade('BTC/USDT','buy',200,'PLACE LIVE BUY',
                                request_id=identifier,expected_context=context)
    engine.live_halted = True
    storage.CONFIG_PATH.write_text('{damaged-config')
    assert engine.manual_trade('BTC/USDT','buy',200,'PLACE LIVE BUY',
                              request_id=identifier,expected_context=context) == result
    assert len(client.orders) == 1
