"""Standalone synthetic PostgreSQL lock regression; starts only a private Unix-socket PG16.
Run: python tests/loan_edit_pg_probe.py (requires installed Homebrew PostgreSQL 16).
"""
import concurrent.futures
import os
from pathlib import Path
import subprocess
import tempfile
import time
from types import SimpleNamespace


def main():
    binaries = Path('/opt/homebrew/opt/postgresql@16/bin')
    assert (binaries / 'initdb').is_file()
    with tempfile.TemporaryDirectory(prefix='loan-pg-', dir='/tmp') as directory:
        root = Path(directory)
        data = root / 'pgdata'
        socket = root / 'socket'
        socket.mkdir()
        env = {'PATH': '/usr/bin:/bin:/opt/homebrew/bin', 'HOME': str(root), 'LC_ALL': 'C'}
        subprocess.run([str(binaries / 'initdb'), '-D', str(data), '-A', 'trust', '-U', 'loan_test', '--no-locale', '--encoding=UTF8'],
                       env=env, capture_output=True, check=True)
        subprocess.run([str(binaries / 'pg_ctl'), '-D', str(data), '-l', str(root / 'postgres.log'),
                        '-o', f"-k {socket} -h '' -p 55437", '-w', 'start'], env=env, capture_output=True, check=True)
        try:
            os.environ.clear()
            os.environ.update(env, DB_BACKEND='postgres', PYTHON_DOTENV_DISABLED='1',
                              DATABASE_URL=f'dbname=postgres user=loan_test host={socket} port=55437',
                              BANK_DATA_ROOT=str(root / 'bank'), THOTH_DISABLE_SCHEDULER='1')
            import sys
            sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
            import psycopg
            from backend.core.store import BankStore
            from backend.server import db
            from backend.server.db_facade import db_api, loans
            from backend.core import bank_pg
            loans.AccountsRepo = lambda: SimpleNamespace(list_for_user=lambda _: [SimpleNamespace(id=1, bank='sinopac')])
            rows = [dict(due_date='2026-06-01', paid_on='2026-06-02', principal='10', interest='1', penalty='0',
                         paid_total='11', principal_balance='90', status='paid', raw_json='{"synthetic":1}')]
            # Seed B then A so an unordered sequential scan takes B before blocked A.
            writer = BankStore('sinopac', user_id=1, source_account_id=1)
            for account in ('B', 'A'):
                writer.replace_loan_repayments(account, '', 'TWD', '2026-06-01', '2026-06-30', rows)
            writer.commit()
            target = next(f for f in db_api.list_loan_repayments(bank='sinopac', user_id=1) if f.account_no == 'B')
            open_connection = db.open_bank_conn
            editor_pid = []

            def editor_connection(bank):
                con = open_connection(bank)
                assert con is not None
                con.execute('SET enable_indexscan=off')
                con.execute('SET enable_bitmapscan=off')
                con.execute("SET statement_timeout='8s'")
                editor_pid.append(con.execute('SELECT pg_backend_pid()').fetchone()[0])
                return con

            db.open_bank_conn = editor_connection
            writer.conn.execute("SET statement_timeout='8s'")
            writer.replace_loan_repayments('A', '', 'TWD', '2026-06-01', '2026-06-30', rows)
            failures = []
            with (psycopg.connect(os.environ['DATABASE_URL'], autocommit=True) as monitor,
                  concurrent.futures.ThreadPoolExecutor(1) as pool):
                future = pool.submit(db_api.update_loan_transaction, bank='sinopac', user_id=1,
                                     txn_id=target.id + ':interest', changes={'category': 'edited'})
                deadline = time.monotonic() + 5
                while not future.done():
                    if editor_pid and monitor.execute('SELECT EXISTS (SELECT 1 FROM pg_locks WHERE pid=%s AND NOT granted)', (editor_pid[0],)).fetchone()[0]:
                        break
                    assert time.monotonic() < deadline, 'editor neither completed nor reached a lock wait'
                    time.sleep(0.01)
                try:
                    writer.replace_loan_repayments('B', '', 'TWD', '2026-06-01', '2026-06-30', rows)
                    writer.commit()
                except Exception as exc:
                    failures.append(type(exc).__name__)
                    writer.conn.rollback()
                try:
                    future.result(timeout=10)
                except Exception as exc:
                    failures.append(type(exc).__name__)
            writer.close()
            assert not failures, f'writer/editor lock regression: {failures}'
            current = db_api.get_loan_repayment(bank='sinopac', user_id=1, fact_id=target.id)
            assert current is not None
            assert current.component_overrides['interest']['category'] == 'edited'
            # Real PostgreSQL advisory lock must still serialize sparse sidecar merges.
            with concurrent.futures.ThreadPoolExecutor(2) as pool:
                list(pool.map(lambda change: db_api.update_loan_transaction(bank='sinopac', user_id=1,
                    txn_id=target.id + ':interest', changes=change), [{'subcategory': 'retained'}, {'auto_excluded': True}]))
            current = db_api.get_loan_repayment(bank='sinopac', user_id=1, fact_id=target.id)
            assert current is not None
            assert current.component_overrides['interest'] == {'category': 'edited', 'subcategory': 'retained', 'auto_excluded': True}
            assert bank_pg._pg_pool is not None
            bank_pg._pg_pool.close()
            print('PRIVATE_PG16_WRITER_EDITOR_AND_PARTIAL_MERGE_PASS')
        finally:
            subprocess.run([str(binaries / 'pg_ctl'), '-D', str(data), '-m', 'immediate', '-w', 'stop'],
                           env=env, capture_output=True, check=True)


if __name__ == '__main__':
    main()
