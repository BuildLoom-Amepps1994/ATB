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

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts' / 'micro_soak.py'


def soak(tmp_path, *args, timeout=120):
    env = dict(os.environ)
    env.pop('CRYPTOPILOT_DATA_DIR', None)                     # the script must set it itself
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


def test_a_short_fixture_soak_runs_in_paper_and_records_the_source_it_ran(tmp_path):
    folder = tmp_path / 'soak-new'
    done = soak(tmp_path, '--data-dir', str(folder), '--minutes', '0.1', '--sample-seconds', '2', '--fixture')
    assert done.returncode == 0, done.stderr[-2000:]
    summary = json.loads((folder / 'soak' / 'summary.json').read_text(encoding='utf-8'))
    assert summary['outcome'] == 'completed' and summary['mode'] == 'paper'
    assert summary['market_data'] == 'synthetic fixture on the real clock'
    assert summary['source']['files_sha256'] and summary['source_changed_during_run'] is False
    assert summary['samples'] >= 1 and summary['faults'] == []
    assert any('synthetic markets only' in line for line in summary['not_tested'])
    settings = json.loads((folder / 'settings.json').read_text(encoding='utf-8'))
    assert settings['time_horizon'] == 'micro'                    # mode is never saved: every launch is PAPER
    assert not (tmp_path / 'localappdata').exists()              # the normal profile was never created
