"""Recovery reporting is offline, read-only and does not clear live safeguards."""
import json
from contextlib import closing
import sqlite3
import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import recovery
import storage
from launcher import AlreadyRunning, InstanceLock


def test_recovery_includes_incomplete_manual_requests_without_raw_payloads(recovery_data):
    pending, failed, complete = [str(uuid.uuid4()) for _ in range(3)]
    for identifier, status in [(pending,'pending'), (failed,'failed'), (complete,'filled')]:
        storage.set_meta(f'manual_request.live.{identifier}.mexc',json.dumps({
            'status':status,'created_at':'2026-10-23T10:00:00+00:00',
            'sensitive-field':'sensitive-request-data','result':{'api_secret':'sensitive-secret'}}))
    storage.set_meta(f'manual_request.paper.{uuid.uuid4()}.kraken','{sensitive-bad-json')
    storage.set_meta('manual_request.sensitive-key','sensitive-data')
    before = storage.DB_PATH.read_bytes()
    result = recovery.report(recovery_data)
    assert result['unresolved_order_count'] == 0
    assert result['incomplete_manual_request_counts'] == {'pending':1,'failed':1,'malformed':2}
    assert {r['request_id'] for r in result['incomplete_manual_requests']} >= {pending, failed}
    assert complete not in {r['request_id'] for r in result['incomplete_manual_requests']}
    assert 'sensitive-' not in json.dumps(result)
    assert storage.DB_PATH.read_bytes() == before


def test_recovery_settings_review_marker_is_safe_and_read_only(recovery_data):
    marker = recovery_data/'settings.recovery.json'
    marker.write_text(json.dumps({'reason':'sensitive-secret','evidence_file':'sensitive-path'}))
    original = marker.read_bytes()
    result = recovery.report(recovery_data)
    assert result['settings_review_recorded']
    assert result['settings_recovery_reason'] == 'recovery_record_invalid'
    assert 'sensitive-' not in json.dumps(result)
    assert marker.read_bytes() == original


@pytest.fixture
def recovery_data(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, 'DATA_DIR', tmp_path)
    monkeypatch.setattr(storage, 'DB_PATH', tmp_path / 'portfolio.sqlite3')
    monkeypatch.setattr(storage, 'CONFIG_PATH', tmp_path / 'settings.json')
    monkeypatch.setenv('CRYPTOPILOT_DATA_DIR', str(tmp_path))
    storage.init_db()
    return tmp_path


def test_recovery_report_excludes_secrets_and_preserves_database(recovery_data):
    intent = storage.create_order_intent('mexc', 'BTC/USDT', 'BUY', cost=100)
    storage.update_order_intent(intent, 'uncertain', order_id='sensitive-order-id')
    storage.audit('error', 'sensitive-audit-text')
    with storage.connection() as connection:
        connection.execute("INSERT INTO meta VALUES ('sensitive-account-key','sensitive-account-value')")
    config = {**storage.DEFAULTS, 'api_secret': 'sensitive-configuration'}
    storage.CONFIG_PATH.write_text(json.dumps(config))
    original_db = storage.DB_PATH.read_bytes()
    original_config = storage.CONFIG_PATH.read_bytes()
    result = recovery.report(recovery_data)
    assert result['schema_version'] == 4
    assert result['unresolved_order_count'] == 1
    assert result['unresolved_orders'][0]['intent_id'] == intent
    assert result['unresolved_orders'][0]['status'] == 'uncertain'
    assert 'sensitive-' not in json.dumps(result)
    assert storage.DB_PATH.read_bytes() == original_db
    assert storage.CONFIG_PATH.read_bytes() == original_config
    assert storage.unresolved_orders()[0]['status'] == 'uncertain'


def test_recovery_refuses_active_instance(recovery_data):
    with InstanceLock(recovery_data):
        with pytest.raises(AlreadyRunning):
            recovery.report(recovery_data)


def test_recovery_does_not_create_or_migrate_database(tmp_path):
    assert recovery.report(tmp_path)['database_exists'] is False
    assert not (tmp_path / 'portfolio.sqlite3').exists()
    with closing(sqlite3.connect(tmp_path / 'portfolio.sqlite3')) as connection, connection:
        connection.executescript("CREATE TABLE positions(mode TEXT,symbol TEXT); INSERT INTO positions VALUES ('live','BTC/USDT');")
    old_bytes = (tmp_path / 'portfolio.sqlite3').read_bytes()
    result = recovery.report(tmp_path)
    assert result['schema_version'] == 0
    assert result['legacy_live_record_count'] == 1
    assert (tmp_path / 'portfolio.sqlite3').read_bytes() == old_bytes
    with closing(sqlite3.connect(tmp_path / 'portfolio.sqlite3')) as connection:
        assert [row[1] for row in connection.execute('PRAGMA table_info(positions)')] == ['mode', 'symbol']


def test_report_unknown_database_fields_are_not_echoed(recovery_data):
    with storage.connection() as connection:
        connection.execute("INSERT INTO order_journal(intent_id,created_at,updated_at,exchange,symbol,side,quantity,cost,status,order_id) VALUES ('sensitive-id','sensitive-time','sensitive-time','sensitive-exchange','sensitive-symbol','sensitive-side',NULL,NULL,'sensitive-status','sensitive-order')")
    result = recovery.report(recovery_data)
    assert result['unresolved_order_count'] == 1
    assert 'sensitive-' not in json.dumps(result)
    assert result['unresolved_orders'][0]['exchange'] == 'unknown'


def test_recovery_reports_durable_halt_without_order_record(recovery_data):
    storage.set_meta('live_halt.mexc', '1')
    result = recovery.report(recovery_data)
    assert result['live_halt_exchanges'] == ['mexc']
    assert result['unresolved_order_count'] == 0
    assert storage.get_meta('live_halt.mexc') == '1'


def test_cli_output_is_new_file_outside_data_folder(recovery_data, tmp_path, capsys):
    assert recovery.main(['--output', str(recovery_data / 'settings.json')]) == 1
    assert 'outside' in capsys.readouterr().err
    target = recovery_data.parent / (recovery_data.name + '-report.json')
    try:
        assert recovery.main(['--output', str(target)]) == 0
        assert json.loads(target.read_text())['schema_version'] == 4
        capsys.readouterr()
        original = target.read_bytes()
        assert recovery.main(['--output', str(target)]) == 1
        assert target.read_bytes() == original
    finally:
        target.unlink(missing_ok=True)
