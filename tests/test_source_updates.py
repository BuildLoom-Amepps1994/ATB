"""Offline installed-source updates, failures and recovery; no app imports."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('source_updates', ROOT/'maintenance/update_installation.py')
update = importlib.util.module_from_spec(spec)
spec.loader.exec_module(update)
sys.path.insert(0,str(ROOT))
from windows_runtime import InstallationLock


@pytest.fixture
def installation(tmp_path,monkeypatch):
    root, source, data = tmp_path/'installed', tmp_path/'source', tmp_path/'portfolio'
    data.mkdir()
    (data/'portfolio.sqlite3').write_bytes(b'do not change portfolio')
    (data/'settings.json').write_text('{"sentinel":true}')
    monkeypatch.setenv('CRYPTOPILOT_DATA_DIR',str(data))
    for folder in (root/'app',source):
        (folder/'static').mkdir(parents=True)
        for name in update.MODULES:
            (folder/name).write_text('VALUE = 1\n',encoding='utf8')
        for name in update.REQUIRED_ASSETS:
            (folder/name).write_text('asset',encoding='utf8')
        (folder/'requirements-lock.txt').write_text('package==1\n',encoding='utf8')
    manifest = {'format':1,'product':'CryptoPilot','installed_version':'1.1.0',
                'requirements_sha256':hashlib.sha256((source/'requirements-lock.txt').read_bytes()).hexdigest(),
                'app_files':update._inventory(root/'app')}
    (root/'installation.json').write_text(json.dumps(manifest))
    return root,source,data


def test_update_backs_up_source_and_preserves_data(installation):
    root,source,data = installation
    (source/'strategy.py').write_text('VALUE = 2\n')
    result = update.update_installation(root,source)
    assert result['updated'] and result['changed_files']==['strategy.py']
    backup = Path(result['backup'])
    assert (backup/'app/strategy.py').read_text()=='VALUE = 1\n'
    assert (root/'app/strategy.py').read_text()=='VALUE = 2\n'
    assert update.read_manifest(root)['app_files']==update._inventory(root/'app')
    assert (data/'portfolio.sqlite3').read_bytes()==b'do not change portfolio'
    assert (data/'settings.json').read_text()=='{"sentinel":true}'
    assert not (root/update.JOURNAL).exists()


def test_noop_does_not_create_backups(installation):
    root,source,_ = installation
    assert not update.update_installation(root,source)['updated']
    assert not (root/'backups').exists()


def test_running_install_blocks_updates_even_with_a_different_data_profile(installation):
    root,source,_ = installation
    (source/'app.py').write_text('VALUE = 3')
    with InstallationLock(root),pytest.raises(update.InstallationBusy):
        update.update_installation(root,source)
    assert (root/'app/app.py').read_text()=='VALUE = 1\n'


def test_direct_local_edits_require_explicit_override_and_are_backed_up(installation):
    root,source,_ = installation
    (root/'app/trader.py').write_text('VALUE = 42')
    with pytest.raises(update.UpdateError,match='local changes'):
        update.update_installation(root,source)
    result = update.update_installation(root,source,replace_local_changes=True)
    assert (Path(result['backup'])/'app/trader.py').read_text()=='VALUE = 42'


def test_extra_local_module_is_detected_and_preserved_in_backup(installation):
    root,source,_ = installation
    (root/'app/extra.py').write_text('local work')
    (source/'app.py').write_text('VALUE = 3')
    with pytest.raises(update.UpdateError,match='local changes'):
        update.update_installation(root,source)
    result = update.update_installation(root,source,replace_local_changes=True)
    assert (Path(result['backup'])/'app/extra.py').read_text()=='local work'


@pytest.mark.parametrize('relative,content', [('trader.py','invalid python :'),('requirements-lock.txt','different==2')])
def test_invalid_syntax_or_dependency_change_preserves_installation(installation,relative,content):
    root,source,_ = installation
    old = (root/'installation.json').read_bytes()
    (source/relative).write_text(content)
    with pytest.raises(update.UpdateError):
        update.update_installation(root,source)
    assert (root/'installation.json').read_bytes()==old
    assert not (root/'backups').exists()


def test_missing_or_unsupported_assets_are_rejected(installation):
    root,source,_ = installation
    (source/'static/private.secret').write_text('do not copy')
    with pytest.raises(update.UpdateError,match='Unsupported'):
        update.update_installation(root,source)
    (source/'static/private.secret').unlink()
    (source/'launcher.py').unlink()
    with pytest.raises(update.UpdateError):
        update.update_installation(root,source)


def test_second_rename_failure_rolls_back_old_source(installation,monkeypatch):
    root,source,_ = installation
    (source/'strategy.py').write_text('VALUE = 2')
    original = update.os.replace
    failed = []
    def replace(first,second):
        first = Path(first)
        if first.name=='app' and first.parent.name.startswith('.update-') and not failed:
            failed.append(True)
            raise OSError('simulated interrupted swap')
        return original(first,second)
    monkeypatch.setattr(update.os,'replace',replace)
    with pytest.raises(update.UpdateError):
        update.update_installation(root,source)
    assert (root/'app/strategy.py').read_text()=='VALUE = 1\n'
    assert update.read_manifest(root)['app_files']==update._inventory(root/'app')
    assert not (root/update.JOURNAL).exists()


@pytest.mark.parametrize('phase',['before_move','after_old_move','after_new_move'])
def test_interrupted_update_restores_previous_source(installation,phase):
    root,source,_ = installation
    backup = root/'backups/update-interrupted'
    backup.mkdir(parents=True)
    shutil.copy2(root/'installation.json',backup/'installation.json')
    staging = root/'.update-interrupted'
    staging.mkdir()
    if phase!='before_move':
        os.replace(root/'app',backup/'app')
    if phase=='after_new_move':
        shutil.copytree(source,root/'app')
        (root/'app/app.py').write_text('VALUE = 99')
    (root/update.JOURNAL).write_text(json.dumps({'format':1,'product':'CryptoPilot',
        'backup':'backups/update-interrupted','staging':'.update-interrupted'}))
    with update.InstallationLock(root):
        assert update.recover_if_needed(root)
    assert (root/'app/app.py').read_text()=='VALUE = 1\n'
    assert not (root/update.JOURNAL).exists()


@pytest.mark.parametrize('badpath',['../trader.py','static/../../settings.json','static/CON.png','static/x:stream.js'])
def test_unsafe_manifest_paths_rejected(installation,badpath):
    root,_,_ = installation
    record = json.loads((root/'installation.json').read_text())
    record['app_files'][badpath]='0'*64
    (root/'installation.json').write_text(json.dumps(record))
    with pytest.raises(update.UpdateError):
        update.read_manifest(root)


def test_data_overlap_and_source_inside_install_refused(installation):
    root,source,data = installation
    with pytest.raises(update.UpdateError,match='outside'):
        update.update_installation(root,root/'app')
    with pytest.raises(update.UpdateError,match='portfolio'):
        update.update_installation(data,source)


def test_junction_source_is_rejected_before_copy(installation,tmp_path):
    root,source,_ = installation
    link = tmp_path/'linked-source'
    if os.name=='nt':
        subprocess.run(['cmd','/c','mklink','/J',str(link),str(source)],capture_output=True,check=True)
    else:
        link.symlink_to(source,target_is_directory=True)
    with pytest.raises(update.UpdateError,match='junction'):
        update.update_installation(root,link)


def interrupted_setup(root, source, old_modules):
    """Setup's state after it moved the old app aside and before it activated the new one.
    The backup record is the one the replaced release wrote, listing its own modules."""
    backup = root/'backups/setup-20261026020000'
    (backup/'app/static').mkdir(parents=True)
    for name in (*old_modules, *update.REQUIRED_ASSETS, 'requirements-lock.txt'):
        (backup/'app'/name).write_text('OLD = 1\n' if name.endswith('.py') else 'asset', encoding='utf8')
    record = {'format':1,'product':'CryptoPilot','installed_version':'1.4.0',
              'requirements_sha256':hashlib.sha256((backup/'app/requirements-lock.txt').read_bytes()).hexdigest(),
              'app_files':{name: hashlib.sha256((backup/'app'/name).read_bytes()).hexdigest()
                           for name in (*old_modules, *update.REQUIRED_ASSETS, 'requirements-lock.txt')}}
    (backup/'installation.json').write_text(json.dumps(record))
    shutil.rmtree(root/'app')
    shutil.copytree(source, root/'.setup-20261026020000/app')
    (root/update.JOURNAL).write_text(json.dumps({'format':1,'product':'CryptoPilot',
        'backup':'backups/setup-20261026020000','staging':'.setup-20261026020000'}))
    return record


def test_an_interrupted_upgrade_from_a_release_with_fewer_modules_recovers(installation):
    root,source,_ = installation
    record = interrupted_setup(root, source, update.BASE_MODULES)      # 1.4.0 shipped nine modules
    with update.InstallationLock(root):
        assert update.recover_if_needed(root)
    assert (root/'app/trader.py').read_text()=='OLD = 1\n' and not (root/'app/decision.py').exists()
    assert json.loads((root/'installation.json').read_text())==record
    assert not (root/update.JOURNAL).exists()          # so Setup can run again


def test_an_interrupted_downgrade_from_a_release_with_more_modules_recovers(installation):
    root,source,_ = installation
    interrupted_setup(root, source, (*update.MODULES, 'later_module.py'))
    with update.InstallationLock(root):
        assert update.recover_if_needed(root)
    assert (root/'app/later_module.py').exists()


@pytest.mark.parametrize('badname',['../trader.py','CON.py','nested/module.py','module.pyc','a b.py'])
def test_a_backup_record_from_another_release_still_rejects_unsafe_names(installation,badname):
    root,source,_ = installation
    interrupted_setup(root, source, update.BASE_MODULES)
    record_path = root/'backups/setup-20261026020000/installation.json'
    record = json.loads(record_path.read_text())
    record['app_files'][badname]='0'*64
    record_path.write_text(json.dumps(record))
    with update.InstallationLock(root), pytest.raises(update.UpdateError):
        update.recover_if_needed(root)
    assert (root/update.JOURNAL).exists()


def test_a_backup_record_missing_a_module_every_release_ships_is_rejected(installation):
    root,source,_ = installation
    interrupted_setup(root, source, tuple(name for name in update.BASE_MODULES if name != 'trader.py'))
    with update.InstallationLock(root), pytest.raises(update.UpdateError, match='missing'):
        update.recover_if_needed(root)
