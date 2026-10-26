"""The micro mode is PAPER only at every boundary, with no bypass (brief items 27-29).

These tests build a real engine on a fixture exchange (public data only) and
try to reach a live micro order through every door: the mode switch, the
settings save, Start, the risk gate and the final submit. Each must refuse on
its own, so a defect in one layer is caught by the next.
"""
import ast
import dataclasses
import inspect
import json
import socket
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import credentials
import micro_policy
import micro_venue
import risk
import storage
import trader
from research.micro_fixtures import FixtureClock, FixtureExchange, SeriesMarket, flat


def micro_settings(**changes):
    cfg = dict(storage.settings())
    cfg.update({'time_horizon': 'micro', 'watchlist': ['ETH/USDT', 'BTC/USDT'], 'auto_discovery': False,
                'streaming_quotes': False, **changes})
    cfg.pop('paper_start_usdt', None)
    return cfg


@pytest.fixture
def engine(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, 'DATA_DIR', tmp_path)
    monkeypatch.setattr(storage, 'DB_PATH', tmp_path / 'portfolio.sqlite3')
    monkeypatch.setattr(storage, 'CONFIG_PATH', tmp_path / 'settings.json')
    monkeypatch.setattr(credentials, 'load', lambda _: None)
    monkeypatch.setattr(credentials, 'save', lambda *a: None)
    monkeypatch.setattr(credentials, 'delete', lambda *a: None)
    clock = FixtureClock()
    minute = clock.minute()
    markets = [SeriesMarket('ETH/USDT', minute - 300, flat(600, base=2500.0)),
               SeriesMarket('BTC/USDT', minute - 300, flat(600, base=60000.0))]
    exchange = FixtureExchange(clock, markets)
    monkeypatch.setattr(trader.TradingEngine, '_new_exchange', staticmethod(lambda *a, **k: exchange))
    instance = trader.TradingEngine()
    instance.update_config(micro_settings())
    yield instance
    instance.stop_event.set()
    if instance.thread:
        instance.thread.join(timeout=3)


def test_the_refusal_has_no_override_parameter_or_environment_switch():
    assert list(inspect.signature(micro_policy.live_refusal).parameters) == ['mode', 'horizon', 'kind']
    tree = ast.parse((ROOT / 'micro_policy.py').read_text(encoding='utf-8'))
    imported = {alias.name.split('.')[0] for node in ast.walk(tree) if isinstance(node, ast.Import)
                for alias in node.names} | {(node.module or '').split('.')[0] for node in ast.walk(tree)
                                            if isinstance(node, ast.ImportFrom)}
    assert imported <= {'__future__'}, imported           # no os, storage, settings or credential access at all
    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)} | \
            {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    for forbidden in ('environ', 'getenv', 'open', 'settings', 'cfg', 'get_meta'):
        assert forbidden not in names
    assert micro_policy.live_refusal('live', horizon='micro') == micro_policy.PAPER_ONLY
    assert micro_policy.live_refusal('live', kind='micro_exit') == micro_policy.PAPER_ONLY
    assert micro_policy.live_refusal('paper', horizon='micro') is None
    assert micro_policy.live_refusal('live', horizon='rapid', kind='entry') is None
    for text in (path.read_text(encoding='utf-8') for path in ROOT.glob('*.py')):
        for forbidden in ('force_live', 'force-live', 'ignore_risk', 'skip_risk', 'MICRO_LIVE'):
            assert forbidden not in text


def test_enabling_live_with_the_micro_strategy_is_refused_before_any_account_access(engine, monkeypatch):
    monkeypatch.setattr(engine, '_ensure_private', lambda: pytest.fail('no private client may be built'))
    with pytest.raises(storage.ConfigValidationError, match='PAPER only'):
        engine.set_mode('live', 'ENABLE LIVE TRADING', True)
    assert engine.mode == 'paper' and engine.private is None


def test_choosing_micro_while_live_is_refused(engine):
    engine.update_config(micro_settings(time_horizon='balanced'))
    engine.mode = 'live'          # as if live had been armed for the legacy strategy
    try:
        with pytest.raises(storage.ConfigValidationError, match='PAPER only'):
            engine.update_config(micro_settings())
        assert engine.cfg['time_horizon'] == 'balanced'
    finally:
        engine.mode = 'paper'


def test_start_refuses_a_live_micro_engine_however_it_got_there(engine, monkeypatch):
    monkeypatch.setattr(engine, '_ensure_private', lambda: pytest.fail('no private client may be built'))
    engine.mode = 'live'
    try:
        with pytest.raises(storage.ConfigValidationError, match='PAPER only'):
            engine.start()
        assert not engine.running and engine.thread is None
    finally:
        engine.mode = 'paper'


def gate_request(engine, kind, side, mode):
    return risk.OrderRequest(exchange=engine.exchange_name, mode=mode, symbol='ETH/USDT', side=side, kind=kind,
                             notional=24.9, price=2500.0, quantity=0.0099 if side == 'SELL' else None,
                             sizing={'eligible': True, 'max_debit': '24.9'} if kind == 'micro_entry' else None)


@pytest.mark.parametrize('kind,side', [('micro_entry', 'BUY'), ('micro_exit', 'SELL'), ('entry', 'BUY')])
def test_the_risk_gate_refuses_every_live_order_while_micro_is_selected(engine, kind, side):
    engine.mode = 'live'
    try:
        verdict = engine.risk.authorize(gate_request(engine, kind, side, 'live'), positions=[], cash=1000.0,
                                        equity=1000.0, marks_fresh=True, symbol_fresh=True)
    finally:
        engine.mode = 'paper'
    assert not verdict.allowed
    assert any(c['check'] == 'micro_paper_only' and not c['passed'] for c in verdict.checks)


def test_a_micro_entry_needs_an_eligible_sizing_that_matches_the_debit(engine):
    for sizing, ok in ((None, False), ({'eligible': False, 'max_debit': '24.9', 'code': 'DEPTH_UNKNOWN'}, False),
                       ({'eligible': True, 'max_debit': '24.8'}, False), ({'eligible': True, 'max_debit': '24.9'}, True)):
        request = dataclasses.replace(gate_request(engine, 'micro_entry', 'BUY', 'paper'), sizing=sizing)
        verdict = engine.risk.authorize(request, positions=[], cash=1000.0, equity=1000.0, marks_fresh=True,
                                        symbol_fresh=True)
        check = next(c for c in verdict.checks if c['check'] == 'micro_sizing')
        assert check['passed'] is ok
    over = dataclasses.replace(gate_request(engine, 'micro_entry', 'BUY', 'paper'), notional=25.01,
                               sizing={'eligible': True, 'max_debit': '25.01'})
    verdict = engine.risk.authorize(over, positions=[], cash=1000.0, equity=1000.0, marks_fresh=True, symbol_fresh=True)
    assert any(c['check'] == 'micro_all_in_cap' and not c['passed'] for c in verdict.checks)
    assert not any(c['check'] == 'min_order' for c in verdict.checks)     # the local 12 USDT floor is not the micro rule


class Recorder:
    """A client that records any order call. A refused submit must leave it untouched."""
    has = {}

    def __init__(self):
        self.calls = []

    def create_market_buy_order_with_cost(self, *args):
        self.calls.append(args)
        raise AssertionError('an order reached the client')

    create_order = create_market_buy_order_with_cost


def forged(request):
    return risk.Authorization(True, request, (), (), 'forged-token')


def journal_count():
    return storage.rows('SELECT COUNT(*) AS n FROM order_journal')[0]['n']


def test_the_final_submit_refuses_a_live_micro_order_even_with_a_valid_looking_authorization(engine):
    client = Recorder()
    engine.mode, engine.running = 'live', True
    engine.stop_event.clear()
    before = journal_count()
    try:
        result = engine._submit(client, 'ETH/USDT', 'BUY', cost=24.9,
                                authorization=forged(gate_request(engine, 'micro_entry', 'BUY', 'live')))
    finally:
        engine.mode, engine.running = 'paper', False
    assert result == (None, None)
    assert client.calls == [] and journal_count() == before


def test_micro_orders_only_reach_the_micro_paper_venue_and_it_only_takes_micro_orders(engine):
    engine.running = True
    engine.stop_event.clear()
    before = journal_count()
    try:
        # A micro entry sent to any other client (the legacy paper adapter here) is refused.
        assert engine._submit(Recorder(), 'ETH/USDT', 'BUY', cost=24.9,
                              authorization=forged(gate_request(engine, 'micro_entry', 'BUY', 'paper'))) == (None, None)
        # A legacy entry sent to the micro venue is refused too.
        venue = engine.micro.venue
        assert isinstance(venue, micro_venue.MicroPaperVenue)
        assert engine._submit(venue, 'ETH/USDT', 'BUY', cost=24.9,
                              authorization=forged(gate_request(engine, 'entry', 'BUY', 'paper'))) == (None, None)
    finally:
        engine.running = False
    assert journal_count() == before
    assert venue.records() == []


def test_a_mode_changed_after_authorization_is_refused_at_submit(engine):
    request = gate_request(engine, 'micro_entry', 'BUY', 'paper')
    engine.update_config(micro_settings(time_horizon='balanced'))
    engine.running = True
    engine.stop_event.clear()
    engine.mode = 'live'
    try:
        assert engine._submit(engine.micro.venue, 'ETH/USDT', 'BUY', cost=24.9, authorization=forged(request)) == (None, None)
    finally:
        engine.mode, engine.running = 'paper', False


def test_the_offline_campaign_cannot_reach_a_network_keyring_or_real_profile():
    import os
    import keyring
    with pytest.raises(ConnectionRefusedError):
        socket.create_connection(('203.0.113.10', 443), timeout=1)
    with pytest.raises(ConnectionRefusedError):
        socket.getaddrinfo('api.mexc.com', 443)
    assert type(keyring.get_keyring()).__module__.startswith('keyring.backends.null')
    data_dir = Path(os.environ['CRYPTOPILOT_DATA_DIR']).resolve()
    assert 'cryptopilot-tests-' in str(data_dir)
    assert Path(os.environ['LOCALAPPDATA']).resolve() != Path.home() / 'AppData' / 'Local'


def test_secret_canaries_are_redacted_from_activity_and_micro_status(engine):
    canary = 'CANARY-SECRET-7f3c2b91d04e4a6c'
    storage.register_secrets(canary)
    storage.audit('warning', f'adapter said: bad key {canary}')
    text = json.dumps(storage.rows('SELECT message FROM audit ORDER BY id DESC LIMIT 5'))
    assert canary not in text and '[REDACTED]' in text
    status = json.dumps(engine.micro.status(), default=str)
    assert canary not in status


@pytest.fixture
def client(engine, monkeypatch):
    import app
    monkeypatch.setattr(app, 'engine', engine)
    return TestClient(app.app, base_url='http://127.0.0.1:8765', client=('127.0.0.1', 50000)), app


def test_micro_actions_need_the_action_token_and_strict_input(client, monkeypatch):
    http, app = client
    calls = []
    monkeypatch.setattr(app.engine.micro, 'flatten', lambda: calls.append('flatten') or {'queued': False})
    assert http.post('/api/micro', json={'action': 'flatten'}).status_code == 403
    headers = {'x-cryptopilot-token': app.CSRF}
    for body in ({'action': 'buy'}, {'action': 'apply_recovery', 'cycle_id': "cyc_'; DROP TABLE"},
                 {'action': 'flatten', 'extra': 1}, {'action': 'clear_stop', 'confirmation': 'x' * 41}):
        assert http.post('/api/micro', headers=headers, json=body).status_code == 422
    assert calls == []
    assert http.post('/api/micro', headers=headers, json={'action': 'flatten'}).json() == {'queued': False}
    assert http.post('/api/micro', headers=headers, json={'action': 'clear_stop', 'confirmation': 'yes'}).status_code == 400
    status = http.get('/api/micro')
    assert status.status_code == 200 and status.json()['paper_only'] is True
