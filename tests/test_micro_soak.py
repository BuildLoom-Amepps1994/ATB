"""The opt-in wall-clock soak script: isolation before import, and an honest summary.

Each case runs ``scripts/micro_soak.py`` in a child process, so the script's
own isolation (a new folder, the null keyring, no credentials) is what is
tested, not this test process's. ``--fixture`` uses synthetic markets and
needs no network.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts' / 'micro_soak.py'


def soak(tmp_path, *args, timeout=120, inherited=None):
    env = dict(os.environ)
    env.pop('CRYPTOPILOT_DATA_DIR', None)                     # the script must set it itself
    if inherited is not None:
        env['CRYPTOPILOT_DATA_DIR'] = str(inherited)           # a profile the calling shell points the app at
    env['LOCALAPPDATA'] = str(tmp_path / 'localappdata')       # where a "normal" profile would live
    return subprocess.run([sys.executable, str(SCRIPT), *args], cwd=ROOT, env=env, capture_output=True,
                          text=True, timeout=timeout)


def test_a_folder_that_already_holds_files_is_refused_before_anything_runs(tmp_path):
    used = tmp_path / 'used'
    used.mkdir()
    (used / 'portfolio.sqlite3').write_bytes(b'not ours')
    done = soak(tmp_path, '--data-dir', str(used), '--minutes', '1', '--fixture')
    assert done.returncode != 0 and 'NEW, empty folder' in done.stderr
    assert sorted(p.name for p in used.iterdir()) == ['portfolio.sqlite3']     # nothing was written


def test_the_normal_profile_and_folders_around_it_are_refused(tmp_path):
    normal = tmp_path / 'localappdata' / 'CryptoPilot'
    for folder in (normal, normal / 'soak', tmp_path / 'localappdata'):
        done = soak(tmp_path, '--data-dir', str(folder), '--minutes', '1', '--fixture')
        assert done.returncode != 0 and 'overlaps the normal CryptoPilot profile' in done.stderr
    assert not normal.exists()


def test_a_profile_that_an_inherited_data_dir_names_is_refused(tmp_path):
    custom = tmp_path / 'elsewhere' / 'profile'
    for folder in (custom, custom / 'soak', custom.parent):
        done = soak(tmp_path, '--data-dir', str(folder), '--minutes', '1', '--fixture', inherited=custom)
        assert done.returncode != 0 and 'the CryptoPilot profile that CRYPTOPILOT_DATA_DIR names' in done.stderr
    assert not custom.exists()


@pytest.mark.parametrize('value', ['0', '-5', 'nan', '0.5', '3601'])
def test_an_unusable_sample_interval_is_refused_before_anything_runs(tmp_path, value):
    folder = tmp_path / 'soak-new'
    done = soak(tmp_path, '--data-dir', str(folder), '--minutes', '1', f'--sample-seconds={value}', '--fixture')
    assert done.returncode != 0 and '--sample-seconds must be between 1 and 3600' in done.stderr
    assert not folder.exists()


def test_the_source_identity_covers_the_harness_and_admits_when_git_cannot_say(tmp_path, monkeypatch):
    sys.path.insert(0, str(SCRIPT.parent))
    import micro_soak
    tree = tmp_path / 'tree'
    (tree / 'scripts').mkdir(parents=True)
    (tree / 'app.py').write_text('app = 1\n')
    (tree / 'scripts' / 'micro_soak.py').write_text('harness = 1\n')
    monkeypatch.setattr(micro_soak, 'ROOT', tree)
    first = micro_soak.source_identity()
    assert first['files'] == 2 and first['commit'] is None and first['dirty'] is None     # not a git checkout
    (tree / 'scripts' / 'micro_soak.py').write_text('harness = 2\n')
    assert micro_soak.source_identity()['files_sha256'] != first['files_sha256']

    def no_git(*_args, **_kwargs):
        raise FileNotFoundError('git')
    monkeypatch.setattr(micro_soak.subprocess, 'run', no_git)
    assert {k: micro_soak.source_identity()[k] for k in ('commit', 'dirty')} == {'commit': None, 'dirty': None}


def test_a_short_fixture_soak_runs_in_paper_and_records_the_source_it_ran(tmp_path):
    folder = tmp_path / 'soak-new'
    done = soak(tmp_path, '--data-dir', str(folder), '--minutes', '0.1', '--sample-seconds', '2', '--fixture')
    assert done.returncode == 0, done.stderr[-2000:]
    summary = json.loads((folder / 'soak' / 'summary.json').read_text(encoding='utf-8'))
    assert summary['outcome'] == 'completed' and summary['mode'] == 'paper'
    assert summary['market_data'] == 'synthetic fixture on the real clock'
    assert summary['remote_network_refused'] is True
    assert summary['source']['files_sha256'] and summary['source_changed_during_run'] is False
    assert summary['samples'] >= 1 and summary['faults'] == []
    assert any('synthetic markets only' in line for line in summary['not_tested'])
    settings = json.loads((folder / 'settings.json').read_text(encoding='utf-8'))
    assert settings['time_horizon'] == 'micro'                    # mode is never saved: every launch is PAPER
    assert not (tmp_path / 'localappdata').exists()              # the normal profile was never created
