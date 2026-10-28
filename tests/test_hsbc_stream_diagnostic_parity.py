"""Frozen HSBC versus instrumented HSBC: synthetic Python + isolated Node VM only."""
from copy import deepcopy
from datetime import date
import ast
import json
from pathlib import Path
import subprocess
from types import ModuleType, SimpleNamespace

import pytest

import backend.banks.hsbc as current
from backend.core.error_catalog.hsbc import CODES
from backend.core.error_codes import exception_error_id

ROOT = Path(__file__).resolve().parents[1]
BASELINE = "16f2d4b130f44973d346b420f3d486ac839f71ac"
PROOFS = []

# The evaluated bank script has no require/process/socket/browser or native fetch.
# Only this outer runner reads stdin; all response/reader/timer objects are synthetic.
RUNNER = r"""
const vm = require('node:vm');
const NativeDecoder = require('node:util').TextDecoder;
const input = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
const {script, args, config} = input;
const trace = [];
let reads=0, typeReads=0, timerCallback;
const fail=()=>{throw Error('synthetic');};
class FakeAbortController {
  constructor(){trace.push(['controller']);this.signal={aborted:false};}
  abort(){trace.push(['abort']);this.signal.aborted=true;}
}
class FakeDecoder {
  constructor(...a){trace.push(['decoder',...a]);this.decoder=new NativeDecoder(...a);}
  decode(bytes){trace.push(['decode',Array.from(bytes)]);return this.decoder.decode(bytes);}
}
const reader = {
  async read(){
    trace.push(['read']);
    if(config.timeout==='read'){timerCallback();fail();}
    if(config.reason===10)fail();
    if(reads++===0)return {done:false,value:new Uint8Array(config.reason===6?args.maxBytes+1:config.reason===11?[255]:config.chunks[0])};
    if(reads===2&&config.chunks.length>1)return {done:false,value:new Uint8Array(config.chunks[1])};
    return {done:true};
  },
  async cancel(){trace.push(['reader.cancel']);if(config.cancelError)fail();}
};
const body = {
  getReader(){trace.push(['getReader']);if(config.reason===9)fail();return reader;},
  async cancel(){trace.push(['body.cancel']);if(config.cancelError)fail();}
};
const response = {
  get url(){trace.push(['url']);return config.reason===1?args.url+'/wrong':args.url;},
  get status(){trace.push(['status']);return config.reason===2?500:200;},
  get redirected(){trace.push(['redirected']);if(config.reason===12)fail();return !!config.redirected;},
  get body(){trace.push(['body']);return config.noBody?null:body;},
  get headers(){trace.push(['headers']);return {get(name){
    trace.push(['header',name]);
    if(config.reason===8)fail();
    if(name==='content-type'){
      typeReads++;
      if(config.envelopeHeaderError&&typeReads===2)fail();
      return config.reason===3?'text/html':config.contentType||'application/json; charset=utf-8';
    }
    return config.reason===4?'bad':config.reason===5?String(args.maxBytes+1):config.length??null;
  }};}
};
const sandbox={
  args, Uint8Array, TextDecoder:FakeDecoder, AbortController:FakeAbortController,
  setTimeout(callback,ms){trace.push(['setTimeout',ms]);timerCallback=callback;return 17;},
  clearTimeout(timer){trace.push(['clearTimeout',timer]);},
  fetch:async(url,options)=>{
    trace.push(['fetch',url,options]);
    if(config.timeout==='fetch'){timerCallback();fail();}
    if(config.reason===7)fail();
    return response;
  }
};
(async()=>{
  const value=await vm.runInNewContext('('+script+')(args)',sandbox,
    {timeout:1000,contextCodeGeneration:{strings:false,wasm:false}});
  process.stdout.write(JSON.stringify({value,trace}));
})().catch(error=>{console.error(error);process.exitCode=1;});
"""


@pytest.fixture(scope="module")
def original():
    source = subprocess.run(
        ["git", "show", f"{BASELINE}:backend/banks/hsbc.py"], cwd=ROOT,
        check=True, text=True, capture_output=True,
    ).stdout
    module = ModuleType("frozen_hsbc_stream_parity")
    module.__file__ = str(ROOT / "backend/banks/hsbc.py")
    exec(compile(source, "<frozen-hsbc>", "exec"), module.__dict__)
    return module


class NodePage:
    def __init__(self, config=None, target=None):
        self.config = config or {}
        self.target = target
        self.calls = []
        self.waits = []

    def evaluate(self, script, args):
        url = args["url"]
        snapshot = {"pageInfo": {"currentPageIndex": 0, "totalPages": 0}, "content": []}
        payload = snapshot if "/posted?" in url else [] if "/unposted?" in url else {"details": []}
        if url.endswith("/cards"):
            payload = cards()
        text = json.dumps({"success": True, "error": None, "payload": payload}, separators=(",", ":"))
        raw = text.encode()
        config = {"chunks": [list(raw[:7]), list(raw[7:])]}
        selected = (
            self.target is None
            or self.target == "replay" and "expectedSnapshot" in args
            or self.target == "detail" and url.endswith("/synthetic-card")
            or self.target not in {None, "replay", "detail"} and self.target in url
        )
        if selected:
            config.update(self.config)
            if config.get("padToBudget"):
                config["chunks"] = [list(raw + b" " * (args["maxBytes"] - len(raw)))]
        result = subprocess.run(
            ["node", "-e", RUNNER],
            input=json.dumps({"script": script, "args": args, "config": config}),
            text=True, capture_output=True, check=True, timeout=5,
        )
        output = json.loads(result.stdout)
        self.calls.append({"args": deepcopy(args), **deepcopy(output)})
        return output["value"]

    def wait_for_timeout(self, ms):
        self.waits.append(ms)


def cards():
    return [{"id": "synthetic-card", "maskedCardNumber": "0000-****-****-0001", "cardStatusDisplay": "ACTIVATED"}]


def collector():
    return SimpleNamespace(auth_token_events=[{
        "url": "https://card.hsbc.com.tw/ibk-bff/api/v1/cards", "token": "Bearer synthetic",
        "sequence": 1, "redirected": False,
    }])


def crawler(module):
    obj = object.__new__(module.HsbcCrawler)
    obj.name = "hsbc"
    obj.transaction_cursors = {}
    return obj


CASES = [(f"reason-{n}", {"reason": n}, n) for n in range(1, 13)] + [
    ("success", {}, None),
    ("cancel-rejection", {"reason": 1, "cancelError": True}, 1),
    ("overflow-cancel-rejection", {"reason": 6, "cancelError": True}, 6),
    ("fetch-timeout", {"timeout": "fetch"}, 7),
    ("read-timeout", {"timeout": "read"}, 10),
    ("missing-body", {"noBody": True}, 9),
    ("envelope-header", {"envelopeHeaderError": True}, 12),
    ("exact-content-length", {"length": "500"}, None),
    ("redirect-flag", {"redirected": True}, None),
    ("rejection-missing-body", {"reason": 1, "noBody": True}, 1),
    ("exact-stream-budget", {"padToBudget": True, "length": "500"}, None),
]
PREFIX = {"inventory": "HSBC-04-601", "posted": "HSBC-06-601", "detail": "HSBC-06-611", "unposted": "HSBC-06-621", "attest": "HSBC-06-631"}


@pytest.mark.parametrize("role", list(PREFIX))
@pytest.mark.parametrize("case,config,reason", CASES, ids=[c[0] for c in CASES])
def test_actual_js_and_python_helper_parity(original, role, case, config, reason):
    pages = [NodePage(config), NodePage(config)]
    outputs = []
    diagnostics = ["stale"]
    for module, page in zip((original, current), pages):
        kwargs = dict(url="https://card.hsbc.com.tw/ibk-bff/api/v1/cards/synthetic-card", token="Bearer synthetic", timeout_ms=1234, max_bytes=500)
        if module is current:
            kwargs["diagnostics"] = diagnostics
        if role == "attest":
            kwargs["expected_snapshot"] = {"pageInfo": None, "content": None}
            output = module.HsbcCrawler._attest_api_page(page, **kwargs)
        else:
            if module is current:
                kwargs["diagnostic_role"] = role
            output = module.HsbcCrawler._fetch_api_page(page, **kwargs)
        outputs.append(output)
    assert outputs[0] == outputs[1]
    old, new = (page.calls[0] for page in pages)
    assert old["args"] == new["args"]
    assert old["trace"] == new["trace"]
    assert old["trace"].count(["clearTimeout", 17]) == 1
    assert sum(t[0] == "fetch" for t in old["trace"]) == 1
    request = next(t for t in old["trace"] if t[0] == "fetch")
    assert request[2] == {"credentials": "include", "redirect": "error", "signal": {"aborted": bool(config.get("timeout"))}, "headers": {"Accept": "application/json", "Authorization": "Bearer synthetic"}}
    if reason is not None:
        expected = f"{PREFIX[role]}-{reason:03d}"
        assert diagnostics == [expected]
        assert expected in CODES
        if reason == 6 and role != "attest":
            assert old["value"] == {"bytes": 501, "exceeded": True}
            assert new["value"] == {"bytes": 501, "exceeded": True, "_diagnostic": 6}
        else:
            assert old["value"] is None
            assert new["value"] == {"_diagnostic": reason}
    else:
        assert diagnostics == []
        assert old["value"] == new["value"]
    PROOFS.append({"kind": "helper", "role": role, "case": case, "reason": reason, "public": outputs[1], "diagnostics": diagnostics, "old": old, "new": new})


def outcome(call):
    try:
        return {"result": call(), "error": None}
    except Exception as error:
        return {"error": [type(error).__name__, str(error)], "id": exception_error_id(error, bank="hsbc")}


@pytest.mark.parametrize("role", ["inventory", "posted", "detail", "unposted", "replay"])
@pytest.mark.parametrize("reason", range(1, 13))
def test_real_caller_returns_budgets_and_terminal_reason(original, monkeypatch, role, reason):
    monkeypatch.setattr(current.time, "monotonic", lambda: 10.0)
    target = {"inventory": "/cards", "posted": "/posted?", "detail": "/synthetic-card", "unposted": "/unposted?", "replay": "replay"}[role]
    # Detail must fail only on its exact resource, not sibling transaction URLs.
    observations = []
    for module in (original, current):
        page = NodePage({"reason": reason}, target)
        if role == "detail":
            page.target = "detail"  # Never inject the failure into a sibling resource.
        budget = [5000]
        obj = crawler(module)
        if role == "inventory":
            call = lambda: obj._fetch_card_inventory(page, "Bearer synthetic", budget)
        elif role in {"posted", "replay"}:
            call = lambda: obj._fetch_posted_history(page, card_id="synthetic-card", identity=cards()[0]["maskedCardNumber"], token="Bearer synthetic", start=date(2025, 9, 1), end=date(2026, 8, 31), byte_budget=budget)
        else:
            call = lambda: obj._collect_card_details(page, collector(), cards(), end=date(2026, 8, 31), byte_budget=budget)
        result = outcome(call)
        observations.append({"outcome": result, "budget": budget, "calls": page.calls, "waits": page.waits})
    old, new = observations
    assert old["budget"] == new["budget"]
    assert old["waits"] == new["waits"]
    assert [c["args"] for c in old["calls"]] == [c["args"] for c in new["calls"]]
    assert [c["trace"] for c in old["calls"]] == [c["trace"] for c in new["calls"]]
    assert old["outcome"].get("error") == new["outcome"].get("error")
    assert old["outcome"].get("result") == new["outcome"].get("result")
    terminal = role in {"inventory", "posted", "replay"} or reason == 6
    if terminal:
        prefix = PREFIX["attest" if role == "replay" else role]
        assert new["outcome"]["id"] == f"{prefix}-{reason:03d}"
        assert old["outcome"]["id"] is None
    if role in {"detail", "unposted"} and reason != 6:
        assert new["outcome"]["error"] is None  # Best-effort absence stays quiet.
    if role == "detail" and reason == 6:
        assert new["budget"] == [0]
        assert len(new["calls"]) == 1  # No posted or unposted request after overflow.
    PROOFS.append({"kind": "caller", "role": role, "reason": reason, "old": old, "new": new})


class TracedDict(dict):
    def __init__(self, values, trace, label, fail=None):
        super().__init__(values)
        self.trace, self.label, self.fail = trace, label, fail

    def get(self, key, default=None):
        self.trace.append((self.label, "get", key))
        if self.fail == key:
            raise ValueError("synthetic-getter")
        return super().get(key, default)

    def __getitem__(self, key):
        self.trace.append((self.label, "item", key))
        return super().__getitem__(key)


class TracedList(list):
    def __init__(self, values, trace):
        super().__init__(values)
        self.trace = trace

    def __iter__(self):
        self.trace.append(("events", "iter"))
        for index, value in enumerate(super().__iter__()):
            self.trace.append(("events", "next", index))
            yield value
        self.trace.append(("events", "stop"))


@pytest.mark.parametrize("bad", [None, "token", "url", "redirected", "sequence"])
def test_token_candidate_getter_order_and_max_sequence(original, bad):
    observed = []
    for module in (original, current):
        trace = []
        valid = collector().auth_token_events[0]
        invalid = {**valid, "token": "invalid"}
        events = TracedList([
            TracedDict(invalid, trace, "invalid", "url" if bad == "url" else None),
            TracedDict({**valid, "sequence": 3, "token": "Bearer newest"}, trace, "newest", bad if bad != "url" else None),
            TracedDict(valid, trace, "older"),
        ], trace)
        result = outcome(lambda: module.HsbcCrawler._history_token(SimpleNamespace(auth_token_events=events)))
        observed.append({"trace": trace, "outcome": result})
    assert observed[0]["trace"] == observed[1]["trace"]
    assert observed[0]["outcome"].get("result") == observed[1]["outcome"].get("result")
    assert observed[0]["outcome"].get("error") == observed[1]["outcome"].get("error")
    if bad is None:
        assert observed[1]["outcome"] == {"result": "Bearer newest", "error": None}
    PROOFS.append({"kind": "token", "case": bad, "old": observed[0], "new": observed[1]})


@pytest.mark.parametrize("field", ["id", "name", "cardType", "paymentDueDate", "statementDate", None])
@pytest.mark.parametrize("bad_value", [123, "x" * 257])
def test_bounded_fields_preserve_lazy_getter_order(original, field, bad_value):
    observed = []
    for module in (original, current):
        trace = []
        card = {**cards()[0], "name": "synthetic", "cardType": "synthetic", "paymentDueDate": None, "statementDate": None}
        if field:
            card[field] = bad_value
        card = TracedDict(card, trace, "card")
        hit = SimpleNamespace(url="https://card.hsbc.com.tw/ibk-bff/api/v1/cards", body_size=100, method="GET", status=200, content_type="application/json", resp_json={"success": True, "error": None, "payload": [card]})
        result = outcome(lambda: module.HsbcCrawler._card_inventory(SimpleNamespace(hits=[hit])))
        observed.append({"trace": trace, "outcome": result})
    assert observed[0]["trace"] == observed[1]["trace"]
    assert observed[0]["outcome"].get("error") == observed[1]["outcome"].get("error")
    assert observed[0]["outcome"].get("result") == observed[1]["outcome"].get("result")
    PROOFS.append({"kind": "bounded", "field": field, "value_type": type(bad_value).__name__, "old": observed[0], "new": observed[1]})


def test_catalog_has_no_duplicate_allocation():
    tree = ast.parse((ROOT / "backend/core/error_catalog/hsbc.py").read_text())
    node = next(n.value for n in tree.body if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "CODES" for t in n.targets))
    keys = [ast.literal_eval(k) for k in node.keys]
    assert len(keys) == len(set(keys))
    ids = [f"{prefix}-{reason:03d}" for prefix in PREFIX.values() for reason in range(1, 13)]
    assert len(ids) == len(set(ids))
    assert set(ids) <= CODES.keys()


@pytest.mark.parametrize("role", ["detail", "unposted"])
@pytest.mark.parametrize("case,packet,ordinal", [
    ("missing", None, 4),
    ("bytes-type", {"bytes": True}, 5),
    ("bytes-negative", {"bytes": -1}, 6),
    ("bytes-overflow", {"bytes": 501, "exceeded": True}, 7),
    ("body-type", {"bytes": 1, "body": []}, 8),
    ("success-false", {"bytes": 1, "body": {"success": False}}, 9),
    ("error-present", {"bytes": 1, "body": {"success": True, "error": "synthetic"}}, 10),
    ("native-error", "raise", 11),
    ("success", {"bytes": 1, "body": {"success": True, "payload": []}}, None),
])
def test_fetch_json_python_metadata_does_not_change_return_or_budget(original, monkeypatch, capsys, role, case, packet, ordinal):
    observed = []
    ids = ["stale"]
    path = "cards/synthetic-card" + ("/transactions/unposted?pageSize=200" if role == "unposted" else "")
    for module in (original, current):
        calls = []
        def fetch(_page, **kwargs):
            calls.append({k: v for k, v in kwargs.items() if k not in {"diagnostics", "diagnostic_role"}})
            if packet == "raise":
                raise TimeoutError("synthetic-sensitive-error")
            return deepcopy(packet)
        with monkeypatch.context() as patch:
            patch.setattr(module.HsbcCrawler, "_fetch_api_page", fetch)
            budget = [500]
            extra = {"diagnostics": ids} if module is current else {}
            value = module.HsbcCrawler._fetch_json(SimpleNamespace(), path, "Bearer synthetic", byte_budget=budget, **extra)
        observed.append({"value": value, "budget": budget, "calls": calls, "stderr": capsys.readouterr().err})
    assert observed[0]["value"] == observed[1]["value"]
    assert observed[0]["budget"] == observed[1]["budget"]
    assert observed[0]["calls"] == observed[1]["calls"]
    prefix = "HSBC-06-640" if role == "detail" else "HSBC-06-650"
    assert ids == ([f"{prefix}-{ordinal:03d}"] if ordinal else [])
    if packet == "raise":
        assert observed[0]["stderr"] == "[fetch] HSBC API request failed\n"
        assert observed[1]["stderr"] == f"[fetch] HSBC API request failed [{prefix}-011]\n"
    else:
        assert observed[0]["stderr"] == observed[1]["stderr"] == ""
    PROOFS.append({"kind": "python-json", "role": role, "case": case, "diagnostics": ids, "old": observed[0], "new": observed[1]})


@pytest.mark.parametrize("role", ["detail", "unposted"])
@pytest.mark.parametrize("case,ordinal", [("path", 1), ("token", 2), ("budget", 3)])
def test_fetch_json_early_guards_remain_no_request(original, role, case, ordinal):
    observed = []
    ids = ["stale"]
    for module in (original, current):
        path = "cards/synthetic-card" + ("/transactions/unposted?pageSize=200" if role == "unposted" else "")
        if case == "path":
            path += "/invalid"
        budget = [0 if case == "budget" else 500]
        page = NodePage()
        extra = {"diagnostics": ids} if module is current else {}
        value = module.HsbcCrawler._fetch_json(page, path, "invalid" if case == "token" else "Bearer synthetic", byte_budget=budget, **extra)
        observed.append({"value": value, "budget": budget, "calls": page.calls})
    assert observed[0] == observed[1]
    assert observed[1]["calls"] == []
    assert ids == [f"HSBC-06-{'640' if role == 'detail' else '650'}-{ordinal:03d}"]
    PROOFS.append({"kind": "python-json-guard", "role": role, "case": case, "diagnostics": ids, "old": observed[0], "new": observed[1]})


@pytest.mark.parametrize("role", list(PREFIX))
@pytest.mark.parametrize("text", ["{", '{"value":NaN}', '{"x":1,"x":2}', '{"value":1e309}'])
def test_actual_stream_json_failure_preserves_strict_parser(original, role, text):
    outputs = []
    ids = []
    for module in (original, current):
        page = NodePage({"chunks": [list(text.encode())]})
        kwargs = dict(url="https://synthetic.invalid/cards", token="Bearer synthetic", timeout_ms=100, max_bytes=500)
        if module is current:
            kwargs["diagnostics"] = ids
        if role == "attest":
            value = module.HsbcCrawler._attest_api_page(page, expected_snapshot={}, **kwargs)
        else:
            if module is current:
                kwargs["diagnostic_role"] = role
            value = module.HsbcCrawler._fetch_api_page(page, **kwargs)
        outputs.append({"value": value, "calls": page.calls})
    assert outputs[0] == outputs[1]
    assert outputs[1]["value"] is None
    prefix = {"inventory": "HSBC-04-600", "posted": "HSBC-06-600", "detail": "HSBC-06-610", "unposted": "HSBC-06-620", "attest": "HSBC-06-630"}[role]
    assert ids == [f"{prefix}-004"]
    PROOFS.append({"kind": "strict-json", "role": role, "text": text, "diagnostics": ids, "old": outputs[0], "new": outputs[1]})


def test_overflow_fixture_supports_original_and_current_private_metadata(original, monkeypatch):
    from tests import test_hsbc_history_sync as history
    observations = []
    for module in (original, current):
        with monkeypatch.context() as patch:
            patch.setattr(history, "HsbcCrawler", module.HsbcCrawler)
            result = outcome(lambda: history.test_hsbc_detail_stream_overflow_aborts_before_posted_history(patch))
            observations.append(result)
    assert observations == [{"result": None, "error": None}] * 2
    PROOFS.append({"kind": "existing-overflow-test", "old": observations[0], "new": observations[1]})


@pytest.mark.parametrize("role", list(PREFIX))
@pytest.mark.parametrize("case,packet,ordinal", [
    ("evaluate-exception", "raise", 1),
    ("non-object", None, 2),
    ("text-type", {"text": 1}, 3),
    ("invalid-json", {"text": "{"}, 4),
])
def test_python_helper_boundary_metadata_preserves_public_sentinel(original, role, case, packet, ordinal):
    ids = ["stale"]
    observations = []
    for module in (original, current):
        calls = []
        native_error = TimeoutError("synthetic-native")
        def evaluate(_script, args):
            calls.append(args)
            if packet == "raise":
                raise native_error
            return deepcopy(packet)
        kwargs = dict(url="https://synthetic.invalid/cards", token="Bearer synthetic", timeout_ms=100, max_bytes=500)
        if module is current:
            kwargs["diagnostics"] = ids
        if role == "attest":
            result = module.HsbcCrawler._attest_api_page(SimpleNamespace(evaluate=evaluate), expected_snapshot={}, **kwargs)
        else:
            if module is current:
                kwargs["diagnostic_role"] = role
            result = module.HsbcCrawler._fetch_api_page(SimpleNamespace(evaluate=evaluate), **kwargs)
        observations.append({"result": result, "calls": calls, "native_id": exception_error_id(native_error, bank="hsbc")})
    assert observations[0]["result"] is observations[1]["result"] is None
    assert observations[0]["calls"] == observations[1]["calls"]
    prefix = {"inventory": "HSBC-04-600", "posted": "HSBC-06-600", "detail": "HSBC-06-610", "unposted": "HSBC-06-620", "attest": "HSBC-06-630"}[role]
    assert ids == [f"{prefix}-{ordinal:03d}"]
    if packet == "raise":
        assert observations[1]["native_id"] == ids[0]
    PROOFS.append({"kind": "python-helper", "role": role, "case": case, "diagnostics": ids, "old": observations[0], "new": observations[1]})


@pytest.mark.parametrize("body,ordinal", [
    ([], 5), ({"success": False}, 6),
    ({"success": True, "error": "synthetic"}, 7),
    ({"success": True, "payload": []}, 8),
    ({"success": True, "payload": {"pageInfo": None, "content": None}}, None),
])
def test_attestation_python_semantics_stay_closed(original, body, ordinal):
    ids = ["stale"]
    observations = []
    for module in (original, current):
        page = NodePage({"chunks": [list(json.dumps(body).encode())]})
        extra = {"diagnostics": ids} if module is current else {}
        value = module.HsbcCrawler._attest_api_page(page, url="https://synthetic.invalid/cards", token="Bearer synthetic", timeout_ms=100, max_bytes=500, expected_snapshot={"pageInfo": None, "content": None}, **extra)
        observations.append({"value": value, "calls": page.calls})
    assert observations[0] == observations[1]
    assert ids == ([f"HSBC-06-630-{ordinal:03d}"] if ordinal else [])
    if not ordinal:
        assert observations[1]["value"]["matched"] is True
    PROOFS.append({"kind": "attest-semantics", "body": body, "diagnostics": ids, "old": observations[0], "new": observations[1]})


@pytest.mark.parametrize("invalid", [
    None, {"token": "invalid"}, {"url": "http://card.hsbc.com.tw/ibk-bff/api/v1/cards"},
    {"url": "https://synthetic.invalid/ibk-bff/api/v1/cards"},
    {"url": "https://card.hsbc.com.tw:444/ibk-bff/api/v1/cards"},
    {"url": "https://synthetic@card.hsbc.com.tw/ibk-bff/api/v1/cards"},
    {"url": "https://synthetic:synthetic@card.hsbc.com.tw/ibk-bff/api/v1/cards"},
    {"url": "https://card.hsbc.com.tw/other"}, {"redirected": True},
])
def test_invalid_token_candidate_then_valid_stays_quiet(original, invalid, capsys):
    observations = []
    for module in (original, current):
        trace = []
        valid = collector().auth_token_events[0]
        rejected = TracedDict({**valid, **invalid}, trace, "invalid") if invalid is not None else None
        events = TracedList([rejected, TracedDict(valid, trace, "valid")], trace)
        value = module.HsbcCrawler._history_token(SimpleNamespace(auth_token_events=events))
        observations.append({"value": value, "trace": trace, "stderr": capsys.readouterr().err})
    assert observations[0] == observations[1]
    assert observations[1]["value"] == "Bearer synthetic"
    assert observations[1]["stderr"] == ""
    PROOFS.append({"kind": "quiet-candidate", "invalid": invalid, "old": observations[0], "new": observations[1]})


@pytest.mark.parametrize("fault", [None, "scheme", "hostname", "port", "username", "password", "path", "iterator", "sequence-compare"])
def test_token_native_operand_order_and_exception_identity(original, monkeypatch, fault):
    observations = []
    for module in (original, current):
        trace = []
        native = ValueError("synthetic-native")
        valid = collector().auth_token_events[0]
        parse = module.urlparse
        class Source:
            def __init__(self, value):
                self.value = value
            def __getattr__(self, name):
                trace.append(("source", name))
                if fault == name:
                    raise native
                return getattr(self.value, name)
        class Sequence:
            def __gt__(self, other):
                trace.append(("sequence", "compare"))
                if fault == "sequence-compare":
                    raise native
                return False
        class Events(TracedList):
            def __iter__(self):
                for value in super().__iter__():
                    if fault == "iterator":
                        raise native
                    yield value
        events = Events([TracedDict({**valid, "sequence": Sequence()}, trace, "first"), TracedDict({**valid, "sequence": Sequence()}, trace, "second")], trace)
        with monkeypatch.context() as patch:
            patch.setattr(module, "urlparse", lambda raw: Source(parse(raw)))
            try:
                value = module.HsbcCrawler._history_token(SimpleNamespace(auth_token_events=events))
                result = {"value": value}
            except ValueError as error:
                assert error is native
                result = {"error": [type(error).__name__, str(error)]}
        observations.append({"result": result, "trace": trace})
    assert observations[0] == observations[1]
    PROOFS.append({"kind": "token-operands", "fault": fault, "old": observations[0], "new": observations[1]})
