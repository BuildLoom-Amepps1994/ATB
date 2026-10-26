"""Offline reconciliation report, plus two explicit, confirmed repairs.

The report never resolves orders or contacts APIs. --clear-halt only records a
reconciliation a person has already completed against the exchange's own
order history; it never retries, cancels or guesses an order outcome.
--prepare-downgrade returns the order journal to the layout builds before
schema 3 can write, after saving a full copy of the database.
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from launcher import AlreadyRunning, InstanceLock, data_directory

EXCHANGES = ('mexc', 'kraken')
STATUSES = {'pending', 'submitted', 'acked', 'uncertain', 'filled', 'accounted', 'rejected', 'reconciled'}
RESOLVED = ('accounted', 'rejected', 'reconciled')


def confirmation_phrase(exchange: str) -> str:
    return f'I HAVE RECONCILED {exchange.upper()} ORDERS'


DOWNGRADE_PHRASE = 'I AM ROLLING BACK CRYPTOPILOT'
# The order journal as builds before schema 3 create it and insert into it, by position.
LEGACY_JOURNAL = ('intent_id', 'created_at', 'updated_at', 'exchange', 'symbol', 'side',
                  'quantity', 'cost', 'status', 'order_id')
SETTINGS_REASONS = {'invalid_json', 'invalid_values', 'unreadable', 'missing',
                    'save_in_progress', 'recovery_record_invalid', 'backup_unavailable'}
MANUAL_KEY = re.compile(r'^manual_request\.(paper|live)\.([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.(mexc|kraken)$')
NOTICE = ('Read-only report. No credentials, exchange requests, orders, migrations or halt resets were performed. '
          'Reconcile actual exchange orders and balances independently before any reviewed ledger repair. '
          'Review incomplete manual requests against trade history even when the exchange order journal is resolved. '
          'An unresolved or legacy record continues to block live trading; paper trading remains available.')


def _exchange(value):
    return value if value in (*EXCHANGES, 'legacy-unassigned') else 'unknown'


def _timestamp(value):
    return value if isinstance(value, str) and re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:\+00:00|Z)', value) else None


def _manual_requests(connection):
    counts = {'pending': 0, 'failed': 0, 'malformed': 0}
    requests = []
    for row in connection.execute("SELECT key,value FROM meta WHERE key LIKE 'manual_request.%'"):
        matched = MANUAL_KEY.fullmatch(row['key']) if isinstance(row['key'], str) else None
        if not matched:
            counts['malformed'] += 1
            continue
        mode, identifier, exchange = matched.groups()
        record = None
        try:
            value = row['value']
            if not isinstance(value, (str, bytes)) or len(value) > 65536:
                raise ValueError('Invalid record')
            record = json.loads(value)
            if not isinstance(record, dict) or record.get('status') not in ('pending', 'failed', 'filled'):
                raise ValueError('Invalid record')
            status = record['status']
        except (ValueError, UnicodeError, TypeError, RecursionError):
            status = 'malformed'
        if status == 'filled':
            continue
        counts[status] += 1
        if len(requests) < 1000:
            requests.append({'request_id': identifier, 'exchange': exchange, 'mode': mode,
                             'status': status, 'created_at': _timestamp(record.get('created_at')) if isinstance(record, dict) else None})
    return counts, requests


def _read_report(folder: Path) -> dict:
    """Caller owns the instance lock. Do not call any application initializer."""
    database = folder / 'portfolio.sqlite3'
    report = {'notice': NOTICE, 'data_folder': str(folder), 'database_exists': database.is_file(), 'schema_version': None,
              'configured_exchange': None, 'portfolio_scopes': [], 'unresolved_orders': [],
              'unresolved_order_count': 0, 'legacy_live_record_count': 0,
              'live_halt_exchanges': [], 'incomplete_manual_request_counts': {'pending': 0, 'failed': 0, 'malformed': 0},
              'incomplete_manual_requests': [], 'report_warnings': [],
              'settings_review_recorded': False, 'settings_recovery_reason': None}
    try:
        config = json.loads((folder / 'settings.json').read_text(encoding='utf8'))
        if isinstance(config, dict) and config.get('exchange') in EXCHANGES:
            report['configured_exchange'] = config['exchange']
    except (OSError, ValueError):
        pass
    marker = folder / 'settings.recovery.json'
    if marker.exists():
        report['settings_review_recorded'] = True
        try:
            recorded = json.loads(marker.read_text(encoding='utf8'))
            reason = recorded.get('reason') if isinstance(recorded, dict) else None
            report['settings_recovery_reason'] = reason if isinstance(reason, str) and reason in SETTINGS_REASONS else 'recovery_record_invalid'
        except (OSError, ValueError, UnicodeError, RecursionError):
            report['settings_recovery_reason'] = 'recovery_record_invalid'
    if not database.is_file():
        return report
    # mode=ro fails if missing and cannot create or migrate the database.
    connection = sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True, timeout=5)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute('PRAGMA query_only=ON')
        connection.execute('BEGIN')
        report['schema_version'] = int(connection.execute('PRAGMA user_version').fetchone()[0])
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'meta' in tables:
            columns = {row[1] for row in connection.execute('PRAGMA table_info(meta)')}
            if {'key', 'value'} <= columns:
                for exchange in EXCHANGES:
                    row = connection.execute('SELECT value FROM meta WHERE key=?', ('live_halt.' + exchange,)).fetchone()
                    if row and row['value'] == '1':
                        report['live_halt_exchanges'].append(exchange)
                report['incomplete_manual_request_counts'], report['incomplete_manual_requests'] = _manual_requests(connection)
            else:
                report['report_warnings'].append('metadata_schema_unrecognized')
        scopes = defaultdict(lambda: {'positions': 0, 'trades': 0})
        for table in ('positions', 'trades'):
            if table not in tables:
                continue
            columns = {row[1] for row in connection.execute(f'PRAGMA table_info({table})')}
            if 'mode' not in columns:
                report['report_warnings'].append(table + '_schema_unrecognized')
                continue
            has_exchange = 'exchange' in columns
            exchange_sql = 'exchange' if has_exchange else "'legacy-unassigned'"
            for row in connection.execute(f'SELECT mode,{exchange_sql} AS exchange,COUNT(*) AS count FROM {table} GROUP BY mode,{exchange_sql}'):
                mode = row['mode'] if row['mode'] in ('paper', 'live') else 'unknown'
                exchange = _exchange(row['exchange'])
                if not has_exchange and mode == 'paper':
                    exchange = report['configured_exchange'] or 'legacy-unassigned'
                scopes[exchange, mode][table] += row['count']
                if mode == 'live' and exchange in ('legacy-unassigned', 'unknown'):
                    report['legacy_live_record_count'] += row['count']
        report['portfolio_scopes'] = [{'exchange': exchange, 'mode': mode, **counts}
                                      for (exchange, mode), counts in sorted(scopes.items())]
        if 'order_journal' in tables:
            columns = {row[1] for row in connection.execute('PRAGMA table_info(order_journal)')}
            if not {'intent_id', 'created_at', 'exchange', 'symbol', 'side', 'status'} <= columns:
                report['unresolved_order_count'] = None
                report['report_warnings'].append('order_journal_schema_unrecognized')
                return report
            # Never print raw free-text reasons, arbitrary extra fields, API
            # account fingerprints, audit messages, settings or exchange order IDs.
            # Paper intents (schema 3+) have no exchange-side effect and never block live trading.
            live_only = " AND mode='live'" if 'mode' in columns else ''
            report['unresolved_order_count'] = connection.execute("SELECT COUNT(*) FROM order_journal WHERE status NOT IN ('accounted','rejected','reconciled')" + live_only).fetchone()[0]
            orders = connection.execute("SELECT intent_id,created_at,exchange,symbol,side,status FROM order_journal WHERE status NOT IN ('accounted','rejected','reconciled')" + live_only + " ORDER BY created_at LIMIT 1000")
            for row in orders:
                report['unresolved_orders'].append({
                    'intent_id': row['intent_id'] if isinstance(row['intent_id'], str) and re.fullmatch('[0-9a-f]{32}', row['intent_id']) else None,
                    'created_at': _timestamp(row['created_at']), 'exchange': _exchange(row['exchange']),
                    'symbol': row['symbol'] if isinstance(row['symbol'], str) and re.fullmatch(r'[A-Z0-9][A-Z0-9._-]{0,23}/USDT', row['symbol']) else None,
                    'side': row['side'] if row['side'] in ('BUY', 'SELL') else 'unknown',
                    'status': row['status'] if row['status'] in STATUSES else 'unknown',
                })
        return report
    finally:
        connection.close()


def _existing_folder(folder: Path | None) -> Path:
    location = Path(folder) if folder is not None else data_directory()
    if not location.is_dir():
        # The lock would otherwise create an empty data folder and report on it.
        raise FileNotFoundError(f'No CryptoPilot data folder exists at {location}.')
    return location


def report(folder: Path | None = None) -> dict:
    location = _existing_folder(folder)
    with InstanceLock(location):
        return _read_report(location)


def clear_halt(folder: Path | None, exchange: str, confirmation: str) -> dict:
    """Record a completed manual reconciliation for one exchange.

    Unresolved order intents become 'reconciled' and the durable halt flag is
    removed, in one transaction with an audit entry. Legacy live records are
    untouched: they need a reviewed ledger repair, not a reset.
    """
    if exchange not in EXCHANGES:
        raise ValueError('Unsupported exchange.')
    if confirmation != confirmation_phrase(exchange):
        raise ValueError('The exact confirmation phrase is required: ' + confirmation_phrase(exchange))
    location = _existing_folder(folder)
    with InstanceLock(location):
        database = location / 'portfolio.sqlite3'
        if not database.is_file():
            raise FileNotFoundError('No local database exists; there is no halt to clear.')
        connection = sqlite3.connect(str(database), timeout=5)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute('BEGIN IMMEDIATE')
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not {'meta', 'order_journal', 'audit'} <= tables:
                raise ValueError('The database schema is not one this tool can update.')
            timestamp = datetime.now(timezone.utc).isoformat(timespec='milliseconds')
            placeholders = ','.join('?' * len(RESOLVED))
            journal_columns = {row[1] for row in connection.execute('PRAGMA table_info(order_journal)')}
            live_only = " AND mode='live'" if 'mode' in journal_columns else ''
            reconciled = connection.execute(
                f'UPDATE order_journal SET status=?,updated_at=? WHERE exchange=? AND status NOT IN ({placeholders})' + live_only,
                ('reconciled', timestamp, exchange, *RESOLVED)).rowcount
            removed = connection.execute('DELETE FROM meta WHERE key=? AND value=?', ('live_halt.' + exchange, '1')).rowcount
            connection.execute('INSERT INTO audit(created_at,level,message) VALUES (?,?,?)',
                               (timestamp, 'warning', f'{exchange.upper()} live halt cleared by the recovery tool after confirmed manual reconciliation; '
                                                      f'{reconciled} order intent(s) marked reconciled.'))
            legacy = 0
            for table in ('positions', 'trades'):
                if table in tables and {'mode', 'exchange'} <= {row[1] for row in connection.execute(f'PRAGMA table_info({table})')}:
                    legacy += connection.execute(f"SELECT COUNT(*) FROM {table} WHERE mode='live' AND exchange='legacy-unassigned'").fetchone()[0]
            others = connection.execute(
                f'SELECT COUNT(*) FROM order_journal WHERE exchange<>? AND status NOT IN ({placeholders})' + live_only,
                (exchange, *RESOLVED)).fetchone()[0]
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()
    if legacy:
        notice = 'Legacy live records still block live trading; they require a reviewed ledger repair.'
    elif others:
        notice = (f'This exchange is no longer halted by the journal, but {others} unresolved live order intent(s) on '
                  'another exchange still block live trading; reconcile that exchange and run --clear-halt for it.')
    else:
        notice = 'Live trading for this exchange is no longer halted by the journal.'
    return {'exchange': exchange, 'reconciled_orders': reconciled, 'halt_flag_removed': bool(removed),
            'legacy_live_record_count': legacy, 'other_exchange_unresolved': others, 'notice': notice}


def prepare_downgrade(folder: Path | None, confirmation: str) -> dict:
    """Make the database writable by a build from before schema 3.

    Those builds insert journal rows by position into ten columns, so after
    the additive schema-3 migration every order they journal fails, and a live
    protective exit halts instead of being sent. This keeps the live journal
    rows in the old layout and drops the paper ones, which older builds would
    misread as live. A full copy of the database is saved first, and the
    newer build migrates the file forward again at its next start.
    """
    if confirmation != DOWNGRADE_PHRASE:
        raise ValueError('The exact confirmation phrase is required: ' + DOWNGRADE_PHRASE)
    location = _existing_folder(folder)
    with InstanceLock(location):
        database = location / 'portfolio.sqlite3'
        if not database.is_file():
            raise FileNotFoundError('No local database exists; there is nothing to prepare.')
        connection = sqlite3.connect(str(database), timeout=5)
        connection.row_factory = sqlite3.Row
        try:
            version = connection.execute('PRAGMA user_version').fetchone()[0]
            journal_columns = [row[1] for row in connection.execute('PRAGMA table_info(order_journal)')]
            if version < 3 and tuple(journal_columns) in (LEGACY_JOURNAL, ()):
                return {'changed': False, 'schema_version': version, 'backup': None,
                        'notice': 'The database already has the layout older builds use. Nothing was changed.'}
            if not set(LEGACY_JOURNAL) <= set(journal_columns):
                raise ValueError('The database schema is not one this tool can update.')
            placeholders = ','.join('?' * len(RESOLVED))
            open_intents = connection.execute(
                f'SELECT COUNT(*) FROM order_journal WHERE status NOT IN ({placeholders})', RESOLVED).fetchone()[0]
            if open_intents:
                raise ValueError(f'{open_intents} order intent(s) are not final. Stop the bot and reconcile them '
                                 '(see the report) before rolling back.')
            stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
            backups = location / 'backups'
            backups.mkdir(exist_ok=True)
            backup = backups / f'portfolio-before-downgrade-{stamp}.sqlite3'
            copy = sqlite3.connect(str(backup))
            try:
                connection.backup(copy)
            finally:
                copy.close()
            columns = ','.join(LEGACY_JOURNAL)
            live_only = " WHERE mode='live'" if 'mode' in journal_columns else ''
            connection.execute('BEGIN IMMEDIATE')
            connection.execute('CREATE TABLE order_journal_legacy ('
                               'intent_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, '
                               'exchange TEXT NOT NULL, symbol TEXT NOT NULL, side TEXT NOT NULL, '
                               'quantity REAL, cost REAL, status TEXT NOT NULL, order_id TEXT)')
            kept = connection.execute(f'INSERT INTO order_journal_legacy({columns}) '
                                      f'SELECT {columns} FROM order_journal{live_only}').rowcount
            total = connection.execute('SELECT COUNT(*) FROM order_journal').fetchone()[0]
            connection.execute('DROP TABLE order_journal')
            connection.execute('ALTER TABLE order_journal_legacy RENAME TO order_journal')
            connection.execute('CREATE INDEX IF NOT EXISTS orders_unresolved ON order_journal(status,exchange)')
            timestamp = datetime.now(timezone.utc).isoformat(timespec='milliseconds')
            connection.execute('INSERT INTO audit(created_at,level,message) VALUES (?,?,?)',
                               (timestamp, 'warning', 'Order journal returned to the pre-schema-3 layout for a rollback; '
                                                      f'{kept} live row(s) kept, {total - kept} paper row(s) removed. '
                                                      f'Full copy saved as {backup.name}.'))
            connection.execute('PRAGMA user_version=2')
            connection.commit()
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()
    return {'changed': True, 'schema_version': 2, 'live_rows_kept': kept, 'paper_rows_removed': total - kept,
            'backup': str(backup),
            'notice': 'An older build can now journal orders in this data folder. The current build migrates it '
                      'forward again when it next starts.'}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--output', type=Path, help='Also save a new JSON report outside the application data folder (existing files are never overwritten).')
    parser.add_argument('--clear-halt', choices=EXCHANGES, metavar='EXCHANGE',
                        help='After reconciling every listed order against the exchange, mark the intents reconciled and remove the live halt for this exchange.')
    parser.add_argument('--confirm', default='',
                        help='Required with --clear-halt: the exact phrase "I HAVE RECONCILED <EXCHANGE> ORDERS". '
                             f'Required with --prepare-downgrade: "{DOWNGRADE_PHRASE}".')
    parser.add_argument('--prepare-downgrade', action='store_true',
                        help='Before running a build older than schema 3: save a copy of the database and return the '
                             'order journal to the layout that build writes.')
    args = parser.parse_args(argv)
    try:
        folder = data_directory().resolve()
        if args.prepare_downgrade:
            if args.clear_halt or args.output is not None:
                print('Use --prepare-downgrade on its own, with --confirm.', file=sys.stderr)
                return 1
            result = prepare_downgrade(folder, args.confirm)
            print(json.dumps(result, indent=2, allow_nan=False))
            return 0
        if args.clear_halt:
            if args.output is not None:
                print('Use --output with a report, not with --clear-halt.', file=sys.stderr)
                return 1
            result = clear_halt(folder, args.clear_halt, args.confirm)
            print(json.dumps(result, indent=2, allow_nan=False))
            return 0
        destination = args.output.expanduser().resolve() if args.output is not None else None
        if destination is not None and (destination == folder or folder in destination.parents):
            print('Report output must be outside the application data folder. No report was written.', file=sys.stderr)
            return 1
        result = report(folder)
        encoded = json.dumps(result, indent=2, allow_nan=False)
        if destination is not None:
            with destination.open('x', encoding='utf8') as handle:
                handle.write(encoded + '\n')
        print(encoded)
        return 0
    except AlreadyRunning:
        print('Close CryptoPilot before using the recovery tool. The application data folder is currently locked.', file=sys.stderr)
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
    except ValueError as exc:
        if args.clear_halt or args.prepare_downgrade:
            print(str(exc) + ' Nothing was changed.', file=sys.stderr)
        else:
            print('Could not read the local database or create the report. No trading state was changed; check file access and the output location.', file=sys.stderr)
    except (OSError, sqlite3.Error):
        print('Could not read the local database or create the report. No trading state was changed; check file access and the output location.', file=sys.stderr)
    return 1


if __name__ == '__main__':
    sys.exit(main())
