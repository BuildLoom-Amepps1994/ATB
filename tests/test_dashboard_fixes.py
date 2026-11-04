"""Regression tests for the dashboard lens of the 2026-11-04 audit (server side).

* A ``ValueError`` inside an API action was answered with "review local
  activity" but wrote no activity line.
* A corrupt paper-cash row made ``GET /api/state`` fail, taking every control
  on the page, including the kill switch, offline while the engine kept running.

The page-side checks (kill switch usable while a poll fails, one warning per
open circuit breaker, dust quantities never shown as zeros) are in
``tests/test_dashboard.js`` and ``tests/test_dashboard_browser.cjs``.
"""

import pytest
from fastapi.testclient import TestClient

import storage
from test_web_with_mock_exchange import webapp  # noqa: F401  (fixture)


@pytest.fixture
def client(webapp):
    return TestClient(webapp.app, base_url='http://127.0.0.1:8765', client=('127.0.0.1', 50000))


def _audit_count():
    return storage.rows('SELECT COUNT(*) AS n FROM audit')[0]['n']


def test_a_value_error_in_an_action_writes_the_activity_line_its_answer_promises(client, webapp, monkeypatch):
    def failing(*args, **kwargs):
        raise ValueError('SIGNED-URL-SECRET')
    monkeypatch.setattr(webapp.engine, 'scan_once', failing)
    before = _audit_count()
    response = client.post('/api/scan', headers={'x-cryptopilot-token': webapp.CSRF})
    assert response.status_code == 400 and 'review local activity' in response.text
    assert 'SIGNED-URL-SECRET' not in response.text
    assert _audit_count() == before + 1
    assert storage.rows('SELECT message FROM audit ORDER BY id DESC LIMIT 1')[0]['message'] == 'Local service request failed: ValueError'


def test_a_corrupt_paper_cash_row_leaves_the_state_read_and_its_controls_available(client, webapp):
    engine = webapp.engine
    key = engine._key('paper_cash')
    saved = storage.get_meta(key, '')
    try:
        storage.set_meta(key, 'nan')
        response = client.get('/api/state', headers={'x-cryptopilot-token': webapp.CSRF})
        assert response.status_code == 200
        body = response.json()
        assert body['mode'] == 'paper' and body['cash'] is None and body['equity'] is None and body['paper_cash'] is None
        assert body['ledger_error'] == 'The paper cash ledger is invalid; reset the paper account or restore the data folder.'
        assert 'kill_switch' in body['risk'] and isinstance(body['positions'], list)
    finally:
        storage.set_meta(key, saved or str(engine.cfg['paper_start_usdt']))
    response = client.get('/api/state', headers={'x-cryptopilot-token': webapp.CSRF})
    assert response.status_code == 200 and response.json()['ledger_error'] is None and response.json()['cash'] is not None
