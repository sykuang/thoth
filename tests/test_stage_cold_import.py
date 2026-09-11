"""A fresh process must record failure without optional browser packages."""
from pathlib import Path
import os
import subprocess
import sys


def test_cold_diagnostics_and_runner_without_browser(tmp_path):
    source = r'''
import builtins
import sys
from types import ModuleType
sys.path.insert(0, sys.argv[1])
original = builtins.__import__
def denied(name, *args, **kwargs):
    if name == 'backend.core.base' or name.startswith(('scrapling', 'patchright', 'playwright')):
        raise ImportError('synthetic private dependency detail')
    return original(name, *args, **kwargs)
builtins.__import__ = denied
def audit(event, args):
    if event in ('sqlite3.connect', 'socket.connect', 'socket.getaddrinfo'):
        raise AssertionError('unexpected external access')
sys.addaudithook(audit)
from backend.core import error_diagnostics as d
assert d.format_failure(ValueError('PRIVATE')) == 'sync_failed:ValueError;stage=unknown;code=crawler_failed'
from backend.server import sync_runner as runner
from backend.server import rules_repo
rules_repo.list_rules = lambda **kw: []
events = ModuleType('backend.server.card_events')
events.snapshot_cards = lambda **kw: []
sys.modules[events.__name__] = events
runner.get_job = lambda _: dict(user_id=1, bank='scsb', history_mode='full')
runner.sync_jobs_repo.claim_queued = lambda _: True
recorded = []
runner.sync_jobs_repo.mark_failed = lambda *args: recorded.append(args)
runner._send_sync_notification = lambda **kw: None
assert runner._exec_sync(1) is True
assert len(recorded) == 1
assert recorded[0][0] == 1
assert recorded[0][1] == 'sync_failed:Exception;stage=init;code=init_failed'
from cli import cli
from types import SimpleNamespace
assert cli.cmd_sync(SimpleNamespace(bank='scsb', headless=True)) == 1
assert not any(name.startswith(('scrapling', 'patchright', 'playwright')) for name in sys.modules)
print('COLD_DIAGNOSTICS_OK')
'''
    env = {'HOME': str(tmp_path), 'PATH': os.defpath,
           'PYTHON_DOTENV_DISABLED': '1', 'BANK_DATA_ROOT': str(tmp_path / 'data'),
           'DB_BACKEND': 'sqlite', 'THOTH_DISABLE_SCHEDULER': '1'}
    result = subprocess.run([sys.executable, '-I', '-B', '-c', source,
                             str(Path(__file__).resolve().parents[1])],
                            cwd=tmp_path, env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert 'COLD_DIAGNOSTICS_OK' in result.stdout
    assert 'synthetic private dependency detail' not in result.stdout
    assert 'PRIVATE' not in result.stdout
