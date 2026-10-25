"""Explicitly trusted OS credential backends, with atomic paired secret records."""
from __future__ import annotations

import json
import sys

import storage

SERVICE = 'CryptoPilot.Local'


class CredentialStoreUnavailable(RuntimeError):
    pass


def _backend():
    import keyring
    backend = keyring.get_keyring()
    # Never write secrets to arbitrary third-party/plaintext/chainer backends.
    allowed = {
        'win32': ('keyring.backends.Windows', 'WinVaultKeyring'),
        'darwin': ('keyring.backends.macOS', 'Keyring'),
    }
    candidates = [allowed[sys.platform]] if sys.platform in allowed else [
        ('keyring.backends.SecretService', 'Keyring'), ('keyring.backends.libsecret', 'Keyring')]
    if (type(backend).__module__, type(backend).__name__) not in candidates:
        raise CredentialStoreUnavailable('A trusted operating-system credential store is unavailable.')
    return backend


def _account(exchange: str) -> str:
    if exchange not in storage.EXCHANGES:
        raise ValueError('Unsupported credential exchange.')
    return exchange + '.credentials.v2'


def load(exchange: str) -> tuple[str, str] | None:
    account = _account(exchange)
    try:
        backend = _backend()
        record = backend.get_password(SERVICE, account)
        if record:
            pair = json.loads(record)
            if not isinstance(pair, dict) or not all(isinstance(pair.get(k), str) and pair[k] for k in ('key', 'secret')):
                return None
            key, secret = pair['key'], pair['secret']
        else:
            key = backend.get_password(SERVICE, exchange + '.key')
            secret = backend.get_password(SERVICE, exchange + '.secret')
            if not key or not secret:
                return None
            save(exchange, key, secret)
        storage.register_secrets(key, secret)
        return key, secret
    except Exception:
        return None


def save(exchange: str, key: str, secret: str) -> None:
    account = _account(exchange)
    if not isinstance(key, str) or not key or not isinstance(secret, str) or not secret:
        raise ValueError('Both credentials are required.')
    storage.register_secrets(key, secret)
    try:
        backend = _backend()
        record = json.dumps({'key': key, 'secret': secret}, separators=(',', ':'))
        backend.set_password(SERVICE, account, record)
        if backend.get_password(SERVICE, account) != record:
            raise CredentialStoreUnavailable('Credential storage could not be verified.')
        for suffix in ('.key', '.secret'):
            try:
                if backend.get_password(SERVICE, exchange + suffix) is not None:
                    backend.delete_password(SERVICE, exchange + suffix)
            except Exception:
                # The new paired record is verified. A failure cleaning old
                # entries must not misreport it as having been kept in memory.
                storage.audit('warning', 'Legacy OS credential cleanup could not be verified. Use Forget credentials to retry removal.')
    except Exception:
        raise CredentialStoreUnavailable('Secure credential storage could not be verified; credentials remain available in this session.') from None


def delete(exchange: str) -> None:
    accounts = (_account(exchange), exchange + '.key', exchange + '.secret')
    try:
        backend = _backend()
        for account in accounts:
            if backend.get_password(SERVICE, account) is not None:
                backend.delete_password(SERVICE, account)
        if any(backend.get_password(SERVICE, account) is not None for account in accounts):
            raise CredentialStoreUnavailable('Credential removal could not be verified.')
    except Exception:
        raise CredentialStoreUnavailable('Saved credential removal could not be verified. Check the operating-system credential manager.') from None
