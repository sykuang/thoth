"""Offline browser facts; intentional origin rejection is raised by Python."""
from importlib import import_module
import json
from pathlib import Path
import re
import subprocess

import pytest

from backend.banks.ctbc import CtbcCrawler
from backend.core.error_codes import exception_error_id, format_error_id
from tests.test_ctbc_final_provenance import crawler_for, history, run_sink
from tests.test_ctbc_history_sync import _collector, _template


CASES = [
    ('protocol', 'http:', 'CTBC-05-041-001', '頁面協定不符', ['protocol']),
    ('hostname', 'foreign.invalid', 'CTBC-05-041-002', '頁面主機不符', ['protocol', 'hostname']),
    ('port', '444', 'CTBC-05-041-003', '頁面連接埠不符', ['protocol', 'hostname', 'port']),
]


class OriginPage:
    url = 'https://www.ctbcbank.com/twrbc/twrbc-deposit/qu002/010'

    def __init__(self, *, location=None, sdk='patchright', native_fault=None):
        self.location = {'protocol': 'https:', 'hostname': 'www.ctbcbank.com', 'port': ''}
        self.location.update(location or {})
        self.sdk, self.native_fault = sdk, native_fault
        self.events, self.calls, self.observation = [], 0, None

    def goto(self, *args, **kwargs):
        pass

    def wait_for_timeout(self, ms):
        pass

    def evaluate(self, script, payload):
        self.calls += 1
        harness = r"""
const fs = require('fs');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const events = [];
global.location = {};
for (const key of ['protocol', 'hostname', 'port']) Object.defineProperty(location, key, {
  get() { events.push(key); if (input.native_fault === key) throw Error('PRIVATE_SENTINEL'); return input.location[key]; }
});
global.AbortController = class { constructor() { events.push('controller'); this.signal = {}; } };
global.setTimeout = () => { events.push('timer'); return 1; };
global.clearTimeout = () => events.push('clear');
global.fetch = async () => {
  events.push('fetch');
  if (input.native_fault === 'fetch') throw Error('PRIVATE_SENTINEL');
  return {status: 200, url: input.payload.url, redirected: false,
    headers: {get: () => { events.push('headers'); return 'application/json'; }},
    json: async () => {
      events.push('json');
      const query = input.payload.body.rqData;
      return {code: '0000', rsData: {accountId: query.accountId, type: query.type,
        count: 0, totalPages: 1, detailList: []}};
    }};
};
(async () => {
  try { const result = await eval('(' + input.script + ')')(input.payload);
    process.stdout.write(JSON.stringify({result, events})); }
  catch (error) { process.stdout.write(JSON.stringify({thrown: {
    name: error.name, message: error.message, stack: error.stack}, events})); }
})();
"""
        run = subprocess.run(['node', '-e', harness], input=json.dumps({
            'script': script, 'payload': payload, 'location': self.location,
            'native_fault': self.native_fault,
        }), text=True, capture_output=True, check=True)
        self.observation = json.loads(run.stdout)
        self.events.extend(self.observation['events'])
        if 'thrown' in self.observation:
            raise import_module(self.sdk + '._impl._helper').parse_error(self.observation['thrown'])
        return self.observation['result']


def fetch(page):
    hit = _template('synthetic')
    return CtbcCrawler._fetch_qu002_011(page, hit.url, hit.req_body,
                                       'synthetic', 'm0', 'Bearer synthetic-token')


@pytest.mark.parametrize('sdk', ['patchright', 'playwright'])
@pytest.mark.parametrize('field,value,code,reason,reads', CASES)
def test_origin_rejection_returns_marker_then_python_raises(sdk, field, value, code, reason, reads):
    page = OriginPage(location={field: value}, sdk=sdk)
    with pytest.raises(RuntimeError) as caught:
        fetch(page)
    assert type(caught.value) is RuntimeError
    assert str(caught.value) == 'ctbc-origin-mismatch'
    assert exception_error_id(caught.value, bank='ctbc') == code
    assert format_error_id(code, bank='ctbc') == f'[{code}] 驗證月份查詢執行頁面：{reason}'
    assert page.observation == {'result': {'origin_rejection': field}, 'events': reads}
    assert page.events == reads and page.calls == 1
    assert (caught.value.__cause__, caught.value.__context__, caught.value.__suppress_context__) == (None, None, False)


@pytest.mark.parametrize('field,value,code,reason,reads', CASES)
def test_origin_leaf_reaches_monthly_typed_run_sink(monkeypatch, field, value, code, reason, reads):
    page, crawler, collector = OriginPage(location={field: value}), crawler_for(), _collector(['synthetic'])
    result, errors = run_sink(monkeypatch, crawler, page, collector,
                              lambda: history(crawler, page, collector))
    terminal, = errors
    assert type(terminal) is RuntimeError and str(terminal) == 'ctbc-twd-history-fetch'
    assert result['collect_diagnostics']['error_id'] == code
    assert f'[{code}] 驗證月份查詢執行頁面：{reason}' in result['error']
    assert type(terminal.__context__) is RuntimeError
    assert str(terminal.__context__) == 'ctbc-origin-mismatch'
    assert terminal.__cause__ is None and terminal.__suppress_context__ is True
    assert page.calls == 1 and page.events == reads


@pytest.mark.parametrize('port', ['', '443'])
def test_valid_origin_keeps_one_request_and_cleanup(port):
    page = OriginPage(location={'port': port})
    assert fetch(page) == []
    assert page.calls == 1
    assert page.events == ['protocol', 'hostname', 'port', 'controller', 'timer', 'fetch', 'json', 'headers', 'clear']


@pytest.mark.parametrize('sdk', ['patchright', 'playwright'])
@pytest.mark.parametrize('at', ['protocol', 'hostname', 'port', 'fetch'])
def test_native_browser_failure_is_not_converted_to_business_marker(sdk, at):
    page = OriginPage(sdk=sdk, native_fault=at)
    family = import_module(sdk + '._impl._errors').Error
    with pytest.raises(family) as caught:
        fetch(page)
    assert exception_error_id(caught.value, bank='ctbc') == 'CTBC-05-400-001'
    assert page.observation is not None
    assert page.calls == 1 and 'result' not in page.observation
    assert page.events.count('fetch') == (1 if at == 'fetch' else 0)
    assert page.events.count('clear') == (1 if at == 'fetch' else 0)


@pytest.mark.parametrize('marker', [None, 1, [], {}, 'unknown', 'CTBC-05-041-001'])
def test_unknown_internal_marker_keeps_existing_fail_closed_response_validation(marker):
    class Page:
        def evaluate(self, script, payload):
            return {'origin_rejection': marker}
    with pytest.raises(RuntimeError) as caught:
        fetch(Page())
    assert exception_error_id(caught.value, bank='ctbc') == 'CTBC-05-040-015'


def test_production_crawler_sources_have_no_explicit_js_throw():
    backend = Path(__file__).parents[1] / 'backend'
    findings = []
    for directory in ['banks', 'core']:
        for path in sorted((backend / directory).rglob('*.py')):
            for number, line in enumerate(path.read_text().splitlines(), 1):
                if re.search(r'\bthrow\b', line):
                    findings.append((str(path.relative_to(backend)), number))
    assert findings == []
