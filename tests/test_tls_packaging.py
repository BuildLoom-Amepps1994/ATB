"""Offline dependency-collection regression for frozen TLS trust policy."""
from pathlib import Path
import runpy
import ssl
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('has_policy', [True, False])
def test_editable_spec_preserves_optional_interpreter_trust_policy(monkeypatch, tmp_path, has_policy):
    ssl_source = tmp_path / 'ssl.py'
    ssl_source.write_text('# fake build interpreter ssl module')
    policy = tmp_path / 'codex-rejected-cas.pem'
    if has_policy:
        policy.write_text('interpreter trust policy')
    monkeypatch.setattr(ssl, '__file__', str(ssl_source))
    preflight = []
    monkeypatch.setattr(ssl, 'create_default_context', lambda: preflight.append(True))
    hooks = SimpleNamespace(collect_data_files=lambda _: [], collect_submodules=lambda _: [], copy_metadata=lambda _: [])
    monkeypatch.setitem(sys.modules, 'PyInstaller.utils.hooks', hooks)
    captured = {}

    def analysis(*_args, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(pure=[], scripts=[], binaries=[], datas=[])

    runpy.run_path(str(ROOT / 'CryptoPilotEditable.spec'), init_globals={
        'SPECPATH': str(ROOT), 'Analysis': analysis, 'PYZ': lambda *_args, **_kwargs: None,
        'EXE': lambda *_args, **_kwargs: None, 'COLLECT': lambda *_args, **_kwargs: None,
    })
    assert ((str(policy), '.') in captured['datas']) is has_policy
    assert preflight == [True]
