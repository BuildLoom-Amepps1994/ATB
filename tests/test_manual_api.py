"""Offline manual-order HTTP contract; the exchange implementation is never used."""
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from test_web_with_mock_exchange import webapp


@pytest.fixture
def client(webapp):
    return TestClient(webapp.app, base_url='http://127.0.0.1:8765', client=('127.0.0.1', 50000))


def payload(**changes):
    return {'symbol': 'BTC/USDT', 'side': 'buy', 'quote_amount': 25,
            'request_id': str(uuid4()), 'expected_context': str(uuid4()), **changes}


def test_settings_recovery_is_visible_and_live_cannot_arm_until_reviewed_save(client, webapp, monkeypatch):
    import storage
    original = dict(webapp.engine.cfg)
    headers = {'x-cryptopilot-token':webapp.CSRF}
    storage.CONFIG_PATH.write_text('{SENSITIVE-CORRUPT-CONFIG')
    monkeypatch.setattr(webapp.engine,'_ensure_private',lambda: pytest.fail('recovery must block before authentication'))
    try:
        state = client.get('/api/state')
        assert state.status_code == 200
        assert state.json()['settings_health']['review_required']
        assert 'SENSITIVE-CORRUPT-CONFIG' not in state.text
        response = client.post('/api/mode',headers=headers,json={
            'mode':'live','confirmation':'ENABLE LIVE TRADING','eligible':True})
        assert response.status_code == 400
        assert webapp.engine.mode == 'paper'
        response = client.post('/api/config',headers=headers,json={
            'settings':{key:value for key,value in original.items() if key != 'paper_start_usdt'}})
        assert response.status_code == 200
        assert not client.get('/api/state').json()['settings_health']['review_required']
    finally:
        storage.save_settings(original)


def test_manual_order_requires_action_token(client, webapp, monkeypatch):
    calls = []
    monkeypatch.setattr(webapp.engine, 'manual_trade', lambda *args, **kwargs: calls.append((args, kwargs)))
    assert client.post('/api/order', json=payload()).status_code == 403
    assert calls == []


@pytest.mark.parametrize('changes', [
    {'side': 'BUY'}, {'side': 'cancel'}, {'quote_amount': 0}, {'quote_amount': -1},
    {'quote_amount': '25'}, {'quantity': 1}, {'request_id': 'invalid'},
    {'confirmation': 'x' * 31}, {'expected_context': None}, {'expected_context': ''},
])
def test_manual_order_invalid_input_never_reaches_engine(client, webapp, monkeypatch, changes):
    calls = []
    monkeypatch.setattr(webapp.engine, 'manual_trade', lambda *args, **kwargs: calls.append((args, kwargs)))
    response = client.post('/api/order', headers={'x-cryptopilot-token': webapp.CSRF}, json=payload(**changes))
    assert response.status_code == 422
    assert calls == []


def test_manual_order_uuid_syntax_checked_before_engine(client, webapp, monkeypatch):
    calls = []
    monkeypatch.setattr(webapp.engine, 'manual_trade', lambda *args, **kwargs: calls.append((args, kwargs)))
    response = client.post('/api/order', headers={'x-cryptopilot-token': webapp.CSRF},
                           json=payload(request_id='x' * 36))
    assert response.status_code == 400
    assert calls == []


def test_manual_order_delegates_explicit_confirmation_and_identity(client, webapp, monkeypatch):
    calls = []
    def submit(*args, **kwargs):
        calls.append((args, kwargs))
        return {'status': 'filled', 'mode': 'live', 'side': 'buy', 'symbol': 'BTC/USDT'}
    monkeypatch.setattr(webapp.engine, 'manual_trade', submit)
    body = payload(symbol='btc/usdt', confirmation='PLACE LIVE BUY')
    response = client.post('/api/order', headers={'x-cryptopilot-token': webapp.CSRF}, json=body)
    assert response.status_code == 200
    assert response.json()['status'] == 'filled'
    assert calls == [(('BTC/USDT', 'buy'), {'quote_amount': 25.0,
        'confirmation': 'PLACE LIVE BUY', 'request_id': body['request_id'],
        'expected_context': body['expected_context']})]


def test_manual_order_context_uuid_checked_before_engine(client, webapp, monkeypatch):
    calls = []
    monkeypatch.setattr(webapp.engine, 'manual_trade', lambda *args, **kwargs: calls.append((args, kwargs)))
    response = client.post('/api/order', headers={'x-cryptopilot-token': webapp.CSRF},
                           json=payload(expected_context='x' * 36))
    assert response.status_code == 400
    assert calls == []


def test_manual_order_context_cannot_be_omitted(client, webapp, monkeypatch):
    calls = []
    monkeypatch.setattr(webapp.engine, 'manual_trade', lambda *args, **kwargs: calls.append((args, kwargs)))
    body = payload()
    body.pop('expected_context')
    response = client.post('/api/order', headers={'x-cryptopilot-token': webapp.CSRF}, json=body)
    assert response.status_code == 422
    assert calls == []


def test_paper_reset_blocks_reserved_manual_operation(client, webapp, monkeypatch):
    import storage
    before = storage.rows('SELECT * FROM positions')
    monkeypatch.setattr(webapp.engine, 'mode', 'paper')
    monkeypatch.setattr(webapp.engine, 'control_busy', True)
    response = client.post('/api/reset-paper', headers={'x-cryptopilot-token': webapp.CSRF})
    assert response.status_code == 400
    assert storage.rows('SELECT * FROM positions') == before


def test_manual_order_never_echoes_exchange_error_or_automatically_retries(client, webapp, monkeypatch):
    calls = []
    def submit(*args, **kwargs):
        calls.append(1)
        raise RuntimeError('SIGNED-URL-SECRET')
    monkeypatch.setattr(webapp.engine, 'manual_trade', submit)
    response = client.post('/api/order', headers={'x-cryptopilot-token': webapp.CSRF}, json=payload())
    assert response.status_code == 502
    assert 'SIGNED-URL-SECRET' not in response.text
    assert len(calls) == 1


@pytest.mark.parametrize('timeframe', ['1m', '5m'])
def test_minute_backtests_reach_engine(client, webapp, monkeypatch, timeframe):
    calls = []
    monkeypatch.setattr(webapp.engine, 'research_backtest', lambda symbol, tf: calls.append((symbol, tf)) or {'ok': True})
    response = client.post('/api/backtest', headers={'x-cryptopilot-token': webapp.CSRF},
                           json={'symbol': 'BTC/USDT', 'timeframe': timeframe})
    assert response.status_code == 200
    assert calls == [('BTC/USDT', timeframe)]
