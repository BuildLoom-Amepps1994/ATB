"""Offline boundary, migration and crash-consistency tests; no OS secrets used."""
import importlib.util
from contextlib import closing
import json
import re
import sqlite3
import sys
import threading
import types
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import credentials
import storage


@pytest.fixture
def isolated_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, 'DATA_DIR', tmp_path)
    monkeypatch.setattr(storage, 'DB_PATH', tmp_path / 'portfolio.sqlite3')
    monkeypatch.setattr(storage, 'CONFIG_PATH', tmp_path / 'settings.json')
    monkeypatch.setattr(storage, '_SECRETS', set())
    return tmp_path


@pytest.fixture
def db(isolated_paths):
    storage.init_db()
    return storage


@pytest.fixture
def local_app(db, monkeypatch):
    # Replace construction before importing the ASGI app; real CCXT/keyring are
    # never initialized or contacted by this HTTP boundary test.
    import trader
    stub = types.SimpleNamespace(
        lock=threading.RLock(), running=False, mode='paper', exchange_name='mexc',
        snapshot=lambda: {'mode': 'paper', 'running': False}, stop=lambda: None,
        scan_once=lambda allow_orders: [], history=lambda symbol, timeframe: [],
    )
    monkeypatch.setattr(trader, 'TradingEngine', lambda: stub)
    spec = importlib.util.spec_from_file_location('_cryptopilot_security_app', Path(__file__).resolve().parents[1] / 'app.py')
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module


def client(module, **kwargs):
    return TestClient(module.app, base_url='http://127.0.0.1:8765', client=('127.0.0.1', 50000), **kwargs)


@pytest.mark.parametrize('host', ['evil.invalid', '127.0.0.1:8765.evil.invalid', 'localhost:8765/', '127.0.0.1@evil.invalid', '127.0.0.1:0', '[', '[::1', 'localhost:bad', 'localhost:8765:1'])
def test_host_rebinding_blocked_even_with_old_test_env(local_app, monkeypatch, host):
    monkeypatch.setenv('CRYPTOPILOT_TEST', '1')
    response = client(local_app).get('/api/state', headers={'host': host})
    assert response.status_code == 403
    assert response.headers['cache-control'] == 'no-store'


@pytest.mark.parametrize('origin', ['http://127.0.0.1:8766', 'http://localhost:8765', 'https://127.0.0.1:8765', 'http://localhost:8765.evil.invalid', 'http://127.0.0.1:8765@evil.invalid', 'null', 'http://127.0.0.1:8765/', 'http://127.0.0.1:8765#', 'http://127.0.0.1:8765?'])
def test_exact_origin_required(local_app, origin):
    response = client(local_app).post('/api/scan', headers={'origin': origin, 'x-cryptopilot-token': local_app.CSRF})
    assert response.status_code == 403


def test_remote_client_and_cross_site_metadata_denied(local_app):
    remote = TestClient(local_app.app, base_url='http://127.0.0.1:8765', client=('192.0.2.1', 50000))
    assert remote.get('/api/state').status_code == 403
    assert client(local_app).get('/api/state', headers={'sec-fetch-site': 'cross-site'}).status_code == 403


def test_local_action_token_and_security_headers(local_app):
    browser = client(local_app)
    assert browser.post('/api/scan').status_code == 403
    response = browser.post('/api/scan', headers={'origin': 'http://127.0.0.1:8765', 'x-cryptopilot-token': local_app.CSRF})
    assert response.status_code == 200
    assert response.json() == {'markets': []}
    homepage = browser.get('/')
    nonce = re.search(r"'nonce-([^']+)'", homepage.headers['content-security-policy']).group(1)
    assert f'nonce="{nonce}"' in homepage.text
    assert local_app.CSRF in homepage.text
    assert homepage.headers['cache-control'] == 'no-store'
    assert homepage.headers['x-frame-options'] == 'DENY'
    assert homepage.headers['referrer-policy'] == 'no-referrer'
    assert browser.get('/api/state').headers['cache-control'] == 'no-store'


def test_validation_errors_never_echo_submitted_credentials(local_app):
    headers = {'x-cryptopilot-token': local_app.CSRF}
    payload = {'api_key': 'test-sensitive-key', 'api_secret': 'test-sensitive-secret', 'remember': 'true'}
    response = client(local_app).post('/api/connect', headers=headers, json=payload)
    assert response.status_code == 422
    assert 'test-sensitive-' not in response.text
    assert 'true' not in response.text


def test_service_exception_text_never_exposed(local_app, monkeypatch):
    def fail_connect(key, secret, remember):
        raise ValueError(f'apiKey={key} secret={secret}')
    monkeypatch.setattr(local_app.engine, 'connect', fail_connect, raising=False)
    response = client(local_app).post('/api/connect', headers={'x-cryptopilot-token': local_app.CSRF}, json={'api_key': 'test-sensitive-key', 'api_secret': 'test-sensitive-secret'})
    assert response.status_code == 400
    assert 'test-sensitive-' not in response.text
    def fail_state():
        raise RuntimeError('test-sensitive-secret signed request')
    monkeypatch.setattr(local_app.engine, 'snapshot', fail_state)
    response = client(local_app).get('/api/state')
    assert response.status_code == 502
    assert 'test-sensitive-secret' not in response.text
    assert 'test-sensitive-secret' not in json.dumps(storage.rows('SELECT * FROM audit'))


def test_data_unavailable_reports_retryable_service_status(local_app, monkeypatch):
    from market_data import DataUnavailable
    def unavailable(*args):
        raise DataUnavailable('test-sensitive-secret in malformed exchange response')
    monkeypatch.setattr(local_app.engine, 'history', unavailable)
    response = client(local_app).get('/api/history')
    assert response.status_code == 503
    assert response.headers['retry-after'] == '30'
    assert response.headers['cache-control'] == 'no-store'
    assert 'test-sensitive-secret' not in response.text


@pytest.mark.parametrize('settings', [
    {'max_positions': 2.5}, {'scan_seconds': True}, {'risk_pct': float('nan')},
    {'fee_pct': float('inf')}, {'watchlist': ['BTC/USDT:USDT']},
    {'watchlist': ['<script>/USDT']}, {'api_secret': 'do-not-save'},
    {'auto_discovery': 'true'}, {'max_position_pct': 15, 'max_exposure_pct': 10},
])
def test_invalid_settings_rejected_without_persistence(db, settings):
    original = storage.CONFIG_PATH.read_bytes()
    with pytest.raises(storage.ConfigValidationError):
        storage.save_settings({**storage.DEFAULTS, **settings})
    assert storage.CONFIG_PATH.read_bytes() == original


def test_config_load_whitelists_and_validates(db):
    storage.CONFIG_PATH.write_text(json.dumps({**storage.DEFAULTS, 'api_key': 'do-not-expose'}))
    assert 'api_key' not in storage.settings()
    storage.CONFIG_PATH.write_text(json.dumps({**storage.DEFAULTS, 'max_positions': 1.5}))
    assert storage.settings() == storage.DEFAULTS
    storage.CONFIG_PATH.write_text('[')
    assert storage.settings() == storage.DEFAULTS


def test_failed_atomic_config_replace_preserves_previous_file(db, monkeypatch):
    original = storage.CONFIG_PATH.read_bytes()
    def fail_replace(*args):
        raise PermissionError('simulated locked file')
    monkeypatch.setattr(storage.os, 'replace', fail_replace)
    with pytest.raises(PermissionError):
        storage.save_settings({**storage.DEFAULTS, 'risk_pct': 1.0})
    assert storage.CONFIG_PATH.read_bytes() == original
    assert not list(storage.DATA_DIR.glob('settings-*.tmp'))


def test_database_transaction_rolls_back_on_failure(db):
    with pytest.raises(RuntimeError):
        with storage.connection() as connection:
            connection.execute("INSERT INTO meta VALUES ('rollback-check','no')")
            raise RuntimeError('simulated ledger failure')
    assert storage.get_meta('rollback-check') == ''


def test_order_journal_survives_connections_and_atomic_ledger_failure(db):
    intent = storage.create_order_intent('mexc', 'BTC/USDT', 'BUY', cost=100)
    assert storage.unresolved_orders()[0]['intent_id'] == intent
    storage.update_order_intent(intent, 'uncertain', order_id='fake-order')
    with pytest.raises(RuntimeError):
        with storage.connection() as connection:
            connection.execute("INSERT INTO meta VALUES ('ledger-check','changed')")
            storage.update_order_intent(intent, 'accounted', db=connection)
            raise RuntimeError('simulated crash before commit')
    assert storage.get_meta('ledger-check') == ''
    assert storage.unresolved_orders('mexc')[0]['status'] == 'uncertain'
    assert storage.unresolved_orders('kraken') == []
    with storage.connection() as connection:
        storage.update_order_intent(intent, 'accounted', db=connection)
    assert storage.unresolved_orders() == []


def test_legacy_migration_preserves_rows_and_quarantines_live(isolated_paths):
    storage.save_settings({**storage.DEFAULTS, 'exchange': 'kraken'})
    with closing(sqlite3.connect(storage.DB_PATH)) as connection, connection:
        connection.executescript('''
            CREATE TABLE positions(mode TEXT,symbol TEXT,qty REAL,entry_price REAL,cost REAL,highest REAL,stop REAL,target REAL,opened_at TEXT,PRIMARY KEY(mode,symbol));
            INSERT INTO positions VALUES ('paper','BTC/USDT',1,100,100,100,95,110,'2026-10-01');
            INSERT INTO positions VALUES ('live','BTC/USDT',2,100,200,100,95,110,'2026-10-01');
            CREATE TABLE trades(id INTEGER PRIMARY KEY,created_at TEXT,mode TEXT,symbol TEXT,side TEXT,price REAL,quantity REAL,gross REAL,fee REAL,realized REAL,reason TEXT,order_id TEXT);
            INSERT INTO trades VALUES (1,'2026-10-01','live','BTC/USDT','BUY',100,2,200,0.2,NULL,'legacy','fake-old-order');
            CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT);
            INSERT INTO meta VALUES ('paper_cash','1234');
            INSERT INTO meta VALUES ('baseline.paper.2026-10-23','5000');
        ''')
    storage.init_db()
    storage.init_db()
    assert len(storage.rows('SELECT * FROM positions')) == 2
    assert storage.rows("SELECT exchange FROM positions WHERE mode='live'")[0]['exchange'] == 'legacy-unassigned'
    assert storage.rows("SELECT exchange FROM positions WHERE mode='paper'")[0]['exchange'] == 'kraken'
    assert storage.legacy_live_records() == 2
    assert storage.get_meta(storage.scope_key('paper_cash', 'kraken')) == '1234'
    storage.save_settings({**storage.DEFAULTS, 'exchange': 'mexc'})
    storage.init_db()
    assert storage.get_meta(storage.scope_key('baseline.paper.2026-10-23', 'mexc')) == ''
    assert storage.get_meta(storage.scope_key('paper_cash', 'mexc')) == '10000.0'
    assert storage.get_meta(storage.scope_key('baseline.paper.2026-10-23', 'kraken')) == '5000'
    storage.reset_paper('mexc')
    assert len(storage.rows("SELECT * FROM positions WHERE mode='paper'")) == 1
    assert storage.get_meta(storage.scope_key('paper_cash', 'kraken')) == '1234'


def test_schema_migration_rename_is_rolled_back_on_failure(isolated_paths, monkeypatch):
    with closing(sqlite3.connect(storage.DB_PATH)) as connection, connection:
        connection.executescript('''
            CREATE TABLE positions(mode TEXT,symbol TEXT,qty REAL,entry_price REAL,cost REAL,highest REAL,stop REAL,target REAL,opened_at TEXT,PRIMARY KEY(mode,symbol));
            INSERT INTO positions VALUES ('paper','BTC/USDT',1,100,100,100,95,110,'2026-10-01');
        ''')
    def fail_after_rename(connection):
        raise RuntimeError('simulated migration failure')
    monkeypatch.setattr(storage, '_create_positions', fail_after_rename)
    with pytest.raises(RuntimeError):
        storage.init_db()
    with closing(sqlite3.connect(storage.DB_PATH)) as connection:
        assert connection.execute('SELECT COUNT(*) FROM positions').fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='positions_v1'").fetchone()[0] == 0


def test_audit_redacts_registered_keys_and_signed_headers(db):
    storage.register_secrets('test-private-secret')
    storage.audit('error', 'failed test-private-secret apiKey=key-value signature=signed-value')
    message = storage.rows('SELECT message FROM audit')[0]['message']
    assert 'test-private-secret' not in message
    assert 'key-value' not in message
    assert 'signed-value' not in message
    assert '[REDACTED]' in message


class FakeVault:
    def __init__(self):
        self.values = {}
        self.writes = []
    def get_password(self, service, account):
        return self.values.get((service, account))
    def set_password(self, service, account, value):
        self.values[service, account] = value
        self.writes.append(account)
    def delete_password(self, service, account):
        self.values.pop((service, account), None)


def test_credential_pair_is_saved_atomically_scoped_and_forgotten(monkeypatch):
    vault = FakeVault()
    monkeypatch.setattr(credentials, '_backend', lambda: vault)
    credentials.save('mexc', 'fake-key', 'fake-secret')
    assert vault.writes == ['mexc.credentials.v2']
    assert credentials.load('mexc') == ('fake-key', 'fake-secret')
    assert credentials.load('kraken') is None
    credentials.delete('mexc')
    assert credentials.load('mexc') is None


def test_legacy_os_credentials_migrate_as_pair(monkeypatch):
    vault = FakeVault()
    vault.values[credentials.SERVICE, 'mexc.key'] = 'old-key'
    vault.values[credentials.SERVICE, 'mexc.secret'] = 'old-secret'
    monkeypatch.setattr(credentials, '_backend', lambda: vault)
    assert credentials.load('mexc') == ('old-key', 'old-secret')
    assert vault.writes == ['mexc.credentials.v2']
    assert (credentials.SERVICE, 'mexc.key') not in vault.values
    assert (credentials.SERVICE, 'mexc.secret') not in vault.values


def test_insecure_keyring_backend_refused_without_reading_or_writing(monkeypatch):
    class Plaintext:
        def get_password(self, *args):
            pytest.fail('Untrusted backend was read')
        def set_password(self, *args):
            pytest.fail('Untrusted backend was written')
    monkeypatch.setitem(sys.modules, 'keyring', types.SimpleNamespace(get_keyring=lambda: Plaintext()))
    assert credentials.load('mexc') is None
    with pytest.raises(credentials.CredentialStoreUnavailable):
        credentials.save('mexc', 'fake-key', 'fake-secret')
    with pytest.raises(credentials.CredentialStoreUnavailable):
        credentials.delete('mexc')


def test_failed_credential_delete_is_visible(monkeypatch):
    vault = FakeVault()
    vault.values[credentials.SERVICE, 'mexc.credentials.v2'] = '{"key":"fake-key","secret":"fake-secret"}'
    monkeypatch.setattr(credentials, '_backend', lambda: vault)
    monkeypatch.setattr(vault, 'delete_password', lambda *args: None)
    with pytest.raises(credentials.CredentialStoreUnavailable):
        credentials.delete('mexc')
