# Dependency-only Windows runtime. Application sources/assets are shipped as
# editable files in ../app by the installer/release packager, never in the PYZ.
import ast
import ssl
from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata

root = Path(SPECPATH)
local_modules = {path.stem for path in root.glob('*.py') if path.stem != 'windows_runtime'}
hidden = set()
# Discover direct dependencies used by the external source, including stdlib
# modules that are not imported by the small bootstrap itself.
for source in root.glob('*.py'):
    if source.stem == 'windows_runtime':
        continue
    for node in ast.walk(ast.parse(source.read_text(encoding='utf-8-sig'))):
        if isinstance(node, ast.Import):
            hidden.update(alias.name for alias in node.names if alias.name.split('.')[0] not in local_modules)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            if node.module.split('.')[0] not in local_modules:
                hidden.add(node.module)
hidden.update(collect_submodules('uvicorn'))
hidden.update(collect_submodules('keyring.backends'))
hidden.update({'keyring.backends.Windows', 'keyring.backends.fail'})
data = collect_data_files('ccxt') + collect_data_files('certifi') + copy_metadata('keyring')
# Some Python distributions attach an explicit CA distrust policy to ssl.py.
# SSLContext.__new__ loads it relative to ssl.__file__, including when frozen.
# Preserve that policy beside the bundled stdlib; never work around it by
# disabling certificate or hostname verification.
ssl_policy = Path(ssl.__file__).with_name('codex-rejected-cas.pem')
if ssl_policy.is_file():
    data.append((str(ssl_policy), '.'))
# Fail during the build if this interpreter's TLS installation is incomplete.
ssl.create_default_context()

a = Analysis([str(root / 'windows_runtime.py')], pathex=[str(root)], binaries=[],
             datas=data, hiddenimports=sorted(hidden), hookspath=[], hooksconfig={},
             runtime_hooks=[], excludes=sorted(local_modules | {'tkinter', 'matplotlib', 'IPython', 'pytest'}))
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='CryptoPilotRuntime',
          debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
          console=True, disable_windowed_traceback=False)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name='CryptoPilotRuntime')
