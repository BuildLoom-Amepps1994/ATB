# Reproducible one-folder Windows console build. User state is never bundled.
from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata

root = Path(SPECPATH)
data = [(str(root / 'static'), 'static')]
data += collect_data_files('ccxt') + collect_data_files('certifi')
data += copy_metadata('keyring')
hidden = collect_submodules('uvicorn') + collect_submodules('keyring.backends')
hidden += ['keyring.backends.Windows', 'keyring.backends.fail']

a = Analysis([str(root / 'launcher.py')], pathex=[str(root)], binaries=[],
             datas=data, hiddenimports=hidden, hookspath=[], hooksconfig={},
             runtime_hooks=[], excludes=['tkinter', 'matplotlib', 'IPython', 'pytest'])
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='CryptoPilot',
          debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
          console=True, disable_windowed_traceback=False)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name='CryptoPilot')
