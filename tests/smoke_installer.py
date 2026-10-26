"""Opt-in real Windows Setup/edit/update/upgrade/uninstall check in a temp folder.

Usage: python tests/smoke_installer.py --setup PATH --work EXISTING_SCRATCH_FOLDER
Never uses the normal installation, credential store or portfolio. No scans/orders.
"""
from __future__ import annotations
import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--setup',type=Path,required=True)
    parser.add_argument('--work',type=Path,required=True)
    args = parser.parse_args()
    if os.name!='nt':
        raise RuntimeError('This smoke check requires Windows')
    setup, work = args.setup.resolve(), args.work.resolve()
    assert setup.is_file() and work.is_dir()
    scratch = Path(tempfile.mkdtemp(prefix='cryptopilot-install-test-',dir=work)).resolve()
    assert scratch.parent==work and scratch.name.startswith('cryptopilot-install-test-')
    installation, data, source = scratch/'Installed CryptoPilot',scratch/'portfolio',scratch/'updated source'
    data.mkdir()
    (data/'settings.json').write_text(json.dumps({'exchange':'mexc','scan_seconds':240}))
    (data/'sentinel.txt').write_bytes(b'keep test portfolio data')
    env = dict(os.environ,CRYPTOPILOT_DATA_DIR=str(data),CRYPTOPILOT_NO_BROWSER='1',
               CRYPTOPILOT_PORT='18767',CRYPTOPILOT_NO_UPDATE_CHECK='1',PYTHON_KEYRING_BACKEND='keyring.backends.null.Keyring')
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    base = 'http://127.0.0.1:18767'
    creation = subprocess.CREATE_NO_WINDOW
    def run(command,timeout=180):
        return subprocess.run([str(item) for item in command],env=env,capture_output=True,text=True,
                              timeout=timeout,creationflags=creation)
    def install():
        result = run([setup,'/TESTINSTALL=1','/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART','/NOICONS',
                      '/TASKS=',f'/DIR={installation}',f'/LOG={scratch}/setup.log'])
        assert result.returncode==0, f'Setup failed {result.returncode}: {result.stdout} {result.stderr}; {scratch}/setup.log'
        assert (installation/'app/app.py').is_file()
    def get(path):
        with opener.open(base+path,timeout=3) as response:
            return response.read()
    def start():
        exe = installation/'runtime/CryptoPilotRuntime.exe'
        handle = (scratch/'app-console.log').open('w')
        child = subprocess.Popen([str(exe)],env=env,stdout=handle,stderr=subprocess.STDOUT,
                                 cwd=scratch,creationflags=creation)
        handle.close()
        for _ in range(150):
            if child.poll() is not None:
                raise AssertionError('Installed application exited: '+(scratch/'app-console.log').read_text(errors='replace'))
            try:
                state = json.loads(get('/api/state'))
                assert state['mode']=='paper' and not state['running'] and not state['connected']
                return child
            except OSError:
                time.sleep(.1)
        child.terminate()
        child.wait(10)
        raise AssertionError('Installed application did not start')
    def stop(child):
        child.terminate()
        child.wait(timeout=15)
        # The process has stopped; allow Windows loader file handles to drain.
        time.sleep(.25)
    def prove(marker):
        payload = json.loads(get('/api/installed-source-proof'))
        assert payload['marker']==marker
        assert Path(payload['file']).resolve()==(installation/'app/app.py').resolve()
    def add_marker(folder,marker):
        path = folder/'app.py'
        with path.open('a',encoding='utf8') as handle:
            handle.write("\n@app.get('/api/installed-source-proof')\ndef installed_source_proof():\n"
                         f"    return {{'marker':{marker!r},'file':__file__}}\n")
    def assert_data():
        assert (data/'sentinel.txt').read_bytes()==b'keep test portfolio data'
        assert json.loads((data/'settings.json').read_text())['scan_seconds']==240
        with closing(sqlite3.connect(data/'portfolio.sqlite3')) as db:
            assert float(db.execute("SELECT value FROM meta WHERE key='paper_cash.mexc'").fetchone()[0])==4321

    install()
    exe = installation/'runtime/CryptoPilotRuntime.exe'
    diagnostics = run([exe,'--check-runtime'])
    assert diagnostics.returncode==0,diagnostics.stdout+diagnostics.stderr
    tls = json.loads(diagnostics.stdout)
    assert tls['valid'] and tls['certificate_verification'] and tls['hostname_verification']
    executable_hash = digest(exe)
    shutil.copytree(installation/'app',source)
    child = start()
    try:
        assert b'CryptoPilot' in get('/')
        assert b'requestJson' in get('/static/dashboard-core.js')
    finally:
        stop(child)
    with closing(sqlite3.connect(data/'portfolio.sqlite3')) as db, db:
        db.execute("UPDATE meta SET value='4321' WHERE key='paper_cash.mexc'")
    add_marker(installation/'app','direct-edit-A')
    add_marker(source,'source-update-B')
    child = start()
    try:
        prove('direct-edit-A')
        blocked = run([exe,'--update-source',source])
        assert blocked.returncode!=0 and 'Close CryptoPilot' in blocked.stderr
        busy_setup = run([setup,'/TESTINSTALL=1','/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART','/NOICONS',
                          '/TASKS=',f'/DIR={installation}',f'/LOG={scratch}/busy-setup.log'])
        assert busy_setup.returncode!=0
        prove('direct-edit-A')
    finally:
        stop(child)
    refused = run([exe,'--update-source',source])
    assert refused.returncode!=0 and 'local changes' in refused.stderr
    updated = run([exe,'--update-source',source,'--replace-local-changes'])
    assert updated.returncode==0,updated.stdout+updated.stderr
    record = json.loads(updated.stdout)
    assert record['updated'] and (Path(record['backup'])/'app/app.py').is_file()
    assert 'direct-edit-A' in (Path(record['backup'])/'app/app.py').read_text()
    assert digest(exe)==executable_hash
    assert_data()
    child = start()
    try:
        prove('source-update-B')
        assert_data()
    finally:
        stop(child)
    previous_runtime = installation/'runtime/previous-runtime.marker'
    previous_runtime.write_text('previous')
    install()  # Same Setup upgrades existing editable source and saves a backup.
    assert any('source-update-B' in p.read_text() for p in (installation/'backups').glob('setup-*/app/app.py'))
    # Setup writes a complete new runtime and does not keep the old one once the new source is active.
    assert not previous_runtime.exists()
    assert not list((installation/'backups').glob('setup-*/runtime'))
    assert not list((installation/'backups').glob('setup-*/maintenance'))
    assert_data()
    child = start()
    try:
        assert json.loads(get('/api/state'))['paper_cash']==4321
        try:
            get('/api/installed-source-proof')
            raise AssertionError('Setup did not replace the edited source')
        except urllib.error.HTTPError as exc:
            assert exc.code==404
    finally:
        stop(child)
    # A window whose working folder is inside app\ blocks the source swap. Setup must then put
    # the previous runtime back next to the previous source and report it with exit code 9.
    previous_runtime.write_text('previous')
    add_marker(installation/'app','kept-after-failed-update-C')
    record = (installation/'installation.json').read_bytes()
    holder = subprocess.Popen([getattr(sys,'_base_executable',sys.executable),'-c','import time; time.sleep(900)'],
                              cwd=installation/'app',creationflags=creation)
    try:
        time.sleep(1)
        failed = run([setup,'/TESTINSTALL=1','/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART','/NOICONS','/TASKS=',
                      f'/DIR={installation}',f'/LOG={scratch}/failed-setup.log'],timeout=600)
    finally:
        holder.terminate()
        holder.wait(15)
        time.sleep(.5)
    assert failed.returncode==9, f'Expected exit code 9, got {failed.returncode}; {scratch}/failed-setup.log'
    assert previous_runtime.read_text()=='previous'
    assert (installation/'installation.json').read_bytes()==record
    assert not (installation/'update-transaction.json').exists()
    assert 'Restored the previous runtime' in (scratch/'failed-setup.log').read_text(errors='replace')
    child = start()
    try:
        prove('kept-after-failed-update-C')
    finally:
        stop(child)
    # A Setup that stopped mid-swap (power cut) left a journal and no app\ folder. The next Setup
    # recovers it itself instead of asking for an app start that cannot work.
    token = 'setup-20990101000000'
    interrupted = installation/'backups'/token
    interrupted.mkdir()
    shutil.copy2(installation/'installation.json',interrupted/'installation.json')
    os.replace(installation/'app',interrupted/'app')
    journal = {'format':1,'product':'CryptoPilot','backup':'backups/'+token,'staging':'.'+token}
    (installation/'update-transaction.json').write_text(json.dumps(journal,separators=(',',':')))
    install()
    assert not (installation/'update-transaction.json').exists()
    assert any('kept-after-failed-update-C' in p.read_text() for p in (installation/'backups').glob('setup-*/app/app.py'))
    assert_data()
    # The same stop with the new runtime already in place: starting the app finishes the swap.
    token = 'setup-20990101000001'
    staged, interrupted = installation/('.'+token), installation/'backups'/token
    interrupted.mkdir()
    shutil.copy2(installation/'installation.json',interrupted/'installation.json')
    shutil.copytree(installation/'app',staged/'app')
    shutil.copy2(installation/'installation.json',staged/'installation.json')
    add_marker(staged/'app','rolled-forward-D')
    journal = {'format':1,'product':'CryptoPilot','backup':'backups/'+token,'staging':'.'+token}
    (installation/'update-transaction.json').write_text(json.dumps(journal,separators=(',',':')))
    child = start()
    try:
        prove('rolled-forward-D')
    finally:
        stop(child)
    assert not (installation/'update-transaction.json').exists() and (interrupted/'app/app.py').is_file()
    # In-app update path: Setup starts while the app runs and waits for it to exit.
    child = start()
    waiting = subprocess.Popen([str(setup),'/TESTINSTALL=1','/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART',
                                '/NOICONS','/TASKS=','/WAITLOCK=90',f'/DIR={installation}',
                                f'/LOG={scratch}/waiting-setup.log'],env=env,creationflags=creation)
    try:
        time.sleep(3)
        assert waiting.poll() is None, 'Setup did not wait for the running app'
    finally:
        stop(child)
    assert waiting.wait(timeout=180)==0, f'Waiting Setup failed; {scratch}/waiting-setup.log'
    assert_data()
    child = start()
    stop(child)
    uninstaller = next(installation.glob('unins*.exe'))
    result = run([uninstaller,'/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART',f'/LOG={scratch}/uninstall.log'])
    assert result.returncode==0,result.stdout+result.stderr
    for _ in range(100):
        if not exe.exists():
            break
        time.sleep(.1)
    assert not exe.exists()
    assert (installation/'app/app.py').is_file() and (installation/'backups').is_dir()
    assert_data()
    result = {'passed':True,'setup':str(setup),'setup_sha256':digest(setup),'test_folder':str(scratch),
              'tests':['isolated install','paper stopped and disconnected default','source assets',
                       'direct installed Python edit applied on restart','running app blocks source update and Setup',
                       'local edits require explicit replacement','source update applies without executable change',
                       'backup contains prior edits','Setup upgrade preserves prior source backup',
                       'Setup installs a fresh runtime and drops the old one once active',
                       'blocked source swap keeps the previous runtime and source (exit 9)',
                       'Setup recovers a swap interrupted before the runtime changed',
                       'app start finishes a swap interrupted after the runtime changed',
                       'portfolio/settings survive update and upgrade',
                       'Setup started by the in-app updater waits for the app to exit','uninstall preserves source/backups/data'],
              'live_orders':0,'exchange_requests':0,'credential_backend':'disabled'}
    (scratch/'result.json').write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps(result,indent=2))
    # Retain this deliberately isolated test folder and logs for review.


if __name__=='__main__':
    main()
