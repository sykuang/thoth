"""Optional real PostgreSQL race test; only an explicitly isolated Unix socket."""
import os
from pathlib import Path
import subprocess
import sys
from urllib.parse import parse_qs, urlparse

import pytest


@pytest.mark.skipif(not os.environ.get("THOTH_TEST_PG_DSN"), reason="isolated PostgreSQL socket not provided")
def test_loan_repayment_windows_do_not_mix_concurrent_snapshots():
    dsn = os.environ["THOTH_TEST_PG_DSN"]
    parsed = urlparse(dsn)
    hosts = parse_qs(parsed.query).get("host", [])
    assert parsed.scheme == "postgresql" and not parsed.netloc
    assert len(hosts) == 1 and hosts[0].startswith("/tmp/thoth-loan-pg-") and hosts[0].endswith("/socket")
    env = {key: value for key, value in os.environ.items() if key in {"HOME", "PATH", "TMPDIR", "THOTH_TEST_PG_DSN"}}
    result = subprocess.run([sys.executable, "-c", PROBE], cwd=Path(__file__).resolve().parents[1],
                            env=env, text=True, capture_output=True, timeout=40)
    assert result.returncode == 0, result.stdout + result.stderr


PROBE = r'''
"""Deterministic local-only PG overlapping-window concurrency regression."""
import os,sys,threading,time,json
from pathlib import Path
ROOT=Path.cwd();sys.path.insert(0,str(ROOT))
os.environ.update(DB_BACKEND='postgres',DATABASE_URL=os.environ['THOTH_TEST_PG_DSN'],PG_POOL_MIN_SIZE='0',PYTHON_DOTENV_DISABLED='1')
from backend.core import creds
creds._ENV_LOADED=True
from backend.core import bank_pg
from backend.core.store import BankStore
from backend.core.persist.sinopac import _parse_sinopac_repayment
from tests.test_sinopac_loan import _repayment
from copy import deepcopy
stores=[BankStore('sinopac',user_id=9003,source_account_id=7) for _ in range(3)]
a,b,observe=stores
account='0123456789012';scope=(account,'99-0001','TWD','2026-08-01','2026-09-07')
r=_repayment()
original=_parse_sinopac_repayment(account,r)
x=deepcopy(r);x['records'][0]['DataValue4']='301';x['records'][0]['DataValue3']='10,301'
y=deepcopy(r);y['records'][0]['DataValue4']='302';y['records'][0]['DataValue3']='10,302'
rows_a=_parse_sinopac_repayment(account,x);rows_b=_parse_sinopac_repayment(account,y)
errors=[];started=threading.Event()
def second():
 try:
  started.set();b.replace_loan_repayments(*scope,rows_b);b.conn.commit()
 except Exception as exc:
  errors.append(type(exc).__name__);b.conn.rollback()
try:
 a.conn.execute('DELETE FROM loan_repayments WHERE user_id=9003');a.conn.commit()
 a.replace_loan_repayments(*scope,original);a.conn.commit()
 pid=b.conn.execute('SELECT pg_backend_pid() AS pid').fetchone()['pid'];b.conn.rollback()
 a.replace_loan_repayments(*scope,rows_a)
 thread=threading.Thread(target=second);thread.start();assert started.wait(3)
 deadline=time.monotonic()+5;blocked=False
 while time.monotonic()<deadline:
  row=observe.conn.execute('SELECT wait_event_type FROM pg_stat_activity WHERE pid=?',(pid,)).fetchone()
  observe.conn.rollback()
  if row and row['wait_event_type']=='Lock':blocked=True;break
  time.sleep(.02)
 assert blocked,'second writer was not observed waiting'
 a.conn.commit();thread.join(8);assert not thread.is_alive()
 rows=a.conn.execute('SELECT interest FROM loan_repayments WHERE user_id=9003').fetchall();a.conn.rollback()
 print(json.dumps({'second_writer_blocked':blocked,'rows_after_two_serialized_windows':len(rows),'errors':errors,'only_second_snapshot':len(rows)==1 and rows[0]['interest']=='302'}))
 assert not errors and len(rows)==1 and rows[0]['interest']=='302','overlapping windows mixed snapshots'
finally:
 a.conn.rollback()
 for s in stores:s.close()
 if bank_pg._pg_pool:bank_pg._pg_pool.close()
'''
