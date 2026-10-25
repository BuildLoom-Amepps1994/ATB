"""Settings corruption and legacy reset regressions; temporary local files only."""
import json
from contextlib import closing
from pathlib import Path
import sqlite3
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import storage
from test_engine import engine


@pytest.fixture
def profile(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, 'DATA_DIR', tmp_path)
    monkeypatch.setattr(storage, 'DB_PATH', tmp_path/'portfolio.sqlite3')
    monkeypatch.setattr(storage, 'CONFIG_PATH', tmp_path/'settings.json')
    monkeypatch.setattr(storage, '_SETTINGS_CACHE', None)
    return tmp_path


def strict_settings():
    return {**storage.DEFAULTS, 'risk_pct': .1, 'max_position_pct': 2,
            'max_exposure_pct': 5, 'max_meme_position_pct': .5,
            'max_meme_exposure_pct': 1, 'daily_loss_limit_pct': .5}


@pytest.mark.parametrize('damage', ['json', 'invalid_count', 'missing_risk_field', 'missing_file'])
def test_damaged_settings_keep_last_approved_limits_until_reviewed_save(profile, damage):
    strict = strict_settings()
    storage.save_settings(strict)
    if damage == 'missing_file':
        storage.CONFIG_PATH.unlink()
        damaged = None
    else:
        changed = dict(strict)
        if damage == 'invalid_count':
            changed['max_positions'] = 0
        elif damage == 'missing_risk_field':
            changed.pop('risk_pct')
        damaged = b'{damaged-config' if damage == 'json' else json.dumps(changed).encode()
        storage.CONFIG_PATH.write_bytes(damaged)
    assert storage.settings() == strict
    health = storage.settings_health()
    assert health['review_required'] and health['using_last_known_good']
    if damaged is not None:
        assert storage.CONFIG_PATH.read_bytes() == damaged
        assert any(p.read_bytes() == damaged for p in profile.glob('settings.invalid-*.json'))
    # Forget the process cache to reproduce a restart, retaining the review latch.
    storage._SETTINGS_CACHE = None
    assert storage.settings() == strict
    assert storage.settings_health()['review_required']
    storage.save_settings(strict)
    assert not storage.settings_health()['review_required']
    assert storage.settings() == strict


def test_unreadable_original_is_not_overwritten_and_backup_is_available(profile, monkeypatch):
    strict = strict_settings()
    storage.save_settings(strict)
    read = Path.read_bytes
    def unreadable(path):
        if path == storage.CONFIG_PATH:
            raise PermissionError('fixture-only error')
        return read(path)
    with monkeypatch.context() as patch:
        patch.setattr(Path, 'read_bytes', unreadable)
        patch.setattr(storage, '_SETTINGS_CACHE', None)
        assert storage.settings() == strict
        assert storage.settings_health()['reason'] == 'unreadable'
        with pytest.raises(OSError):
            storage.save_settings(strict)
    assert json.loads(storage.CONFIG_PATH.read_text()) == strict
    assert storage.settings_health()['review_required']


def test_interrupted_settings_save_requires_review_after_restart(profile, monkeypatch):
    strict = strict_settings()
    storage.save_settings(strict)
    write = storage._atomic_json
    def fail_backup(path, value):
        if path.name == 'settings.last-good.json':
            raise OSError('simulated interrupted backup write')
        return write(path, value)
    with monkeypatch.context() as patch:
        patch.setattr(storage, '_atomic_json', fail_backup)
        with pytest.raises(OSError):
            storage.save_settings({**strict, 'risk_pct': .2})
    storage._SETTINGS_CACHE = None
    assert storage.settings_health()['review_required']
    assert storage.settings_health()['reason'] == 'save_in_progress'
    storage.save_settings(strict)
    assert not storage.settings_health()['review_required']


def test_corrupt_recovery_marker_never_enables_live_silently(profile):
    storage.save_settings(strict_settings())
    (profile/'settings.recovery.json').write_text('{invalid')
    assert storage.settings_health()['review_required']
    assert storage.settings_health()['reason'] == 'recovery_record_invalid'


def test_live_activation_blocks_before_credentials_when_settings_need_review(engine, monkeypatch):
    storage.CONFIG_PATH.write_text('{bad-settings')
    monkeypatch.setattr(engine, '_ensure_private', lambda: pytest.fail('must not authenticate'))
    with pytest.raises(storage.ConfigValidationError, match='settings'):
        engine.set_mode('live', 'ENABLE LIVE TRADING', True)
    assert engine.mode == 'paper'
    assert engine.snapshot()['settings_health']['review_required']


@pytest.mark.parametrize('existing_v2', [False, True])
def test_paper_reset_does_not_resurrect_legacy_risk_metadata(profile, existing_v2):
    storage.save_settings(storage.DEFAULTS)
    with closing(sqlite3.connect(storage.DB_PATH)) as db, db:
        db.execute('CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)')
        db.executemany('INSERT INTO meta VALUES (?,?)', [
            ('baseline.paper.2026-10-23', '50000'), ('breached.paper.2026-10-23', '1'),
            ('last_entry.paper.BTC/USDT', '12345'), ('paper_cash', '7890')])
        if existing_v2:
            db.execute("INSERT INTO meta VALUES ('migration.legacy_paper_exchange','mexc')")
            db.execute("INSERT INTO meta VALUES ('paper_cash.mexc','10000')")
    storage.init_db()
    if not existing_v2:
        assert storage.get_meta('baseline.paper.2026-10-23.mexc') == '50000'
        assert storage.get_meta('paper_cash.mexc') == '7890'
    storage.reset_paper('mexc')
    storage.init_db()
    storage.init_db()
    assert storage.get_meta('paper_cash.mexc') == '10000.0'
    assert storage.get_meta('baseline.paper.2026-10-23.mexc') == ''
    assert storage.get_meta('breached.paper.2026-10-23.mexc') == ''
    assert storage.get_meta('last_entry.paper.BTC/USDT.mexc') == ''
    assert storage.get_meta('baseline.paper.2026-10-23') == '50000'
