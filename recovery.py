"""Read-only local reconciliation report. Never resolves orders or contacts APIs."""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

from launcher import AlreadyRunning, InstanceLock, data_directory

EXCHANGES = ('mexc', 'kraken')
STATUSES = {'pending', 'submitted', 'uncertain', 'filled', 'accounted', 'rejected'}
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
    report = {'notice': NOTICE, 'database_exists': database.is_file(), 'schema_version': None,
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
            report['unresolved_order_count'] = connection.execute("SELECT COUNT(*) FROM order_journal WHERE status NOT IN ('accounted','rejected')").fetchone()[0]
            orders = connection.execute("SELECT intent_id,created_at,exchange,symbol,side,status FROM order_journal WHERE status NOT IN ('accounted','rejected') ORDER BY created_at LIMIT 1000")
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


def report(folder: Path | None = None) -> dict:
    location = Path(folder) if folder is not None else data_directory()
    with InstanceLock(location):
        return _read_report(location)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, help='Also save a new JSON report outside the application data folder (existing files are never overwritten).')
    args = parser.parse_args(argv)
    try:
        folder = data_directory().resolve()
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
        print('Close CryptoPilot before generating a recovery report. The application data folder is currently locked.', file=sys.stderr)
    except (OSError, sqlite3.Error, ValueError):
        print('Could not read the local database or create the report. No trading state was changed; check file access and the output location.', file=sys.stderr)
    return 1


if __name__ == '__main__':
    sys.exit(main())
