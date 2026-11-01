"""Offline diagnostic leaves; no bank, credentials or production store access."""
import ast
from pathlib import Path
from types import SimpleNamespace

from decimal import Decimal

import pytest

from backend.core.error_codes import code_details, exception_error_id
from backend.core.money import native_money


@pytest.mark.parametrize("value,currency,reason", [
    (None, "TWD", "必填金額未提供"),
    (True, "TWD", "金額不可使用布林值"),
    (1, "??", "幣別不是三碼大寫 ISO 代碼"),
    ("oops", "TWD", "金額無法解析為十進位數"),
    ("NaN", "TWD", "金額不是有限數值"),
    ("9007199254740992", "TWD", "金額絕對值超過安全整數範圍"),
    ("1.1", "TWD", "臺幣金額含小數"),
    ("0.0000001", "USD", "原幣金額精度超過六位小數"),
])
def test_shared_money_failure_keeps_type_message_and_leaf(value, currency, reason):
    with pytest.raises(ValueError, match="^invalid native monetary value$") as raised:
        native_money(value, currency)
    error_id = exception_error_id(raised.value, bank="esun")
    assert error_id is not None
    assert error_id.startswith("SYS-08-")
    assert code_details(error_id)[1] == reason


def test_shared_money_unavailable_and_success_values_unchanged():
    assert native_money(None, "TWD", optional=True) is None
    assert native_money("-", "TWD", optional=True) is None
    assert native_money("1,234", "TWD") == 1234
    assert native_money("-1.125", "USD", absolute=True) == 1.125
    assert native_money("1.125", "USD", preserve_decimal=True) == Decimal("1.125")

@pytest.mark.parametrize("module,name,cases", [
    ("esun", "_esun_twd_integer", [(True, {"non_negative": True}), (1.2, {"non_negative": True}), ("01", {"non_negative": True}), ("2147483648", {"non_negative": True}), ("-1", {"non_negative": True})]),
    ("fubon", "_strict_fubon_money", [("NaN", {}), ("1.1", {}), ("2147483648", {}), ("-1", {"non_negative": True})]),
    ("ctbc", "_ctbc_native_identity", []),
])
def test_bank_money_reasons_are_distinct(module, name, cases):
    from importlib import import_module
    function = getattr(import_module("backend.core.persist." + module), name)
    codes = []
    for value, kwargs in cases:
        if module == "fubon":
            kwargs = {"optional": False, "non_negative": False, **kwargs}
        with pytest.raises(ValueError) as raised:
            function(value, **kwargs)
        error_id = exception_error_id(raised.value, bank=module)
        assert error_id and error_id.startswith(module.upper() + "-08-")
        codes.append(error_id)
    assert len(set(codes)) == len(codes)


class BoundaryFailure(RuntimeError):
    pass


class SyntheticConnection:
    total_changes = 0

    def __init__(self, fail):
        self.fail = fail
        self.events = []
        self.error = BoundaryFailure("synthetic private driver text")

    def execute(self, sql, params=()):
        self.events.append("execute")
        if self.fail == "execute":
            raise self.error
        return SimpleNamespace(rowcount=1, fetchone=lambda: (0,), fetchall=lambda: [])

    def commit(self):
        self.events.append("commit")
        if self.fail == "commit":
            raise self.error

    def rollback(self):
        self.events.append("rollback")
        if self.fail == "rollback":
            raise self.error

    def close(self):
        self.events.append("close")
        if self.fail == "close":
            raise self.error


def synthetic_store(fail):
    from backend.core.store import BankStore
    store = BankStore.__new__(BankStore)
    store.bank = "esun"
    store.user_id = 1
    store.source_account_id = 1
    store.conn = SyntheticConnection(fail)
    return store


@pytest.mark.parametrize("method,args", [
    ("upsert_accounts", ([{"account_no": "fixture", "raw_balance": 1}],)),
    ("upsert_balance_history", ([{"snapshotDate": "2026-01-01", "twdBalance": 1}],)),
    ("upsert_cards", ([{"number": "fixture", "used_credit": 1}],)),
    ("update_card_bill_facts", ([{"number": "fixture", "bill_due_amount": 1}],)),
    ("put_daily_metric", ("fixture", {"fixture": 1})),
    ("log_sync", ({"fixture": 1},)),
    ("clear_sync_log", ()),
    ("delete_daily_metrics", (["fixture"],)),
    ("record_auto_debit_payment", ("2026-01-01", 1)),
])
def test_write_and_commit_boundaries_are_separate_native_exceptions(method, args):
    ids = []
    for boundary in ("execute", "commit"):
        store = synthetic_store(boundary)
        with pytest.raises(BoundaryFailure) as raised:
            getattr(store, method)(*args)
        assert raised.value is store.conn.error
        error_id = exception_error_id(raised.value, bank="esun")
        assert error_id and code_details(error_id)
        assert "private" not in str(code_details(error_id))
        ids.append(error_id)
    assert ids[0] != ids[1]


@pytest.mark.parametrize("method,args", [
    ("begin_transaction", ()), ("commit", ()), ("rollback", ()),
    ("close", ()), ("_record_transaction_cursor", ("twd_transactions", "fixture", "2026-01-01")),
    ("_transaction_cursor_dates", ("twd_transactions",)), ("stats", ()),
])
def test_native_transaction_and_cursor_boundaries(method, args):
    fail = {"begin_transaction": "execute", "_record_transaction_cursor": "execute", "_transaction_cursor_dates": "execute", "stats": "execute"}.get(method, method)
    store = synthetic_store(fail)
    with pytest.raises(BoundaryFailure) as raised:
        getattr(store, method)(*args)
    assert raised.value is store.conn.error
    assert exception_error_id(raised.value, bank="esun")


def test_optional_missing_cursor_is_still_nonfatal():
    store = synthetic_store("execute")
    store.source_account_id = None
    assert store._record_transaction_cursor("twd_transactions", "fixture", "2026-01-01") is None
    assert store._transaction_cursor_dates("twd_transactions") == {}
    assert store.conn.events == []


def test_all_owned_explicit_failure_leaves_are_registered():
    root = Path(__file__).resolve().parents[1] / "backend/core"
    paths = sorted((root / "persist").glob("*.py")) + [root / name for name in ("money.py", "card_bills.py", "card_status.py", "store.py", "bank_pg.py")]
    missing = []
    for path in paths:
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Raise) and node.exc is not None:
                if not (isinstance(node.exc, ast.Call) and isinstance(node.exc.func, ast.Name) and node.exc.func.id == "with_error_code"):
                    missing.append((path.name, node.lineno))
    assert missing == []

@pytest.mark.parametrize("boundary", ["begin_transaction", "persist", "bill", "cursor", "commit", "rollback"])
def test_dispatch_attributes_exact_boundary_and_retains_order(monkeypatch, boundary):
    from backend.core import persist
    from backend.core.error_codes import with_error_code
    events = []
    primary = with_error_code(BoundaryFailure("primary"), "ESUN-08-001-001")
    cleanup = BoundaryFailure("cleanup")

    def operation(name):
        def call(*args, **kwargs):
            events.append(name)
            if name == boundary or (boundary == "rollback" and name == "persist"):
                raise primary if name == "persist" and boundary == "rollback" else cleanup
            return {} if name == "persist" else 0
        return call

    monkeypatch.setattr(persist, "_validate_history_coverage_before_persist", lambda _: None)
    monkeypatch.setattr(persist, "validate_card_bill_facts", lambda *a, **k: None)
    monkeypatch.setitem(persist.PERSISTERS, "esun", operation("persist"))
    monkeypatch.setattr(persist, "apply_card_bill_facts", operation("bill"))
    store = SimpleNamespace(begin_transaction=operation("begin_transaction"), commit=operation("commit"), record_history_coverage_cursors=operation("cursor"), conn=SimpleNamespace(rollback=operation("rollback")))
    with pytest.raises(BoundaryFailure) as raised:
        persist.persist_collected("esun", {"history_coverage": {"mode": "full"}}, store)
    code = exception_error_id(raised.value, bank="esun")
    assert code and code.startswith("ESUN-08-")
    if boundary == "rollback":
        assert raised.value is cleanup
        assert raised.value.__context__ is primary
        # The terminal rollback is registered; handled primary context is incidental.
        assert code == "ESUN-08-800-011"
        assert events == ["begin_transaction", "persist", "rollback"]
    else:
        assert raised.value is cleanup
        assert events[-1] == "rollback"


@pytest.mark.parametrize("kind", ["UndefinedTable", "UndefinedColumn"])
def test_postgres_schema_translation_preserves_native_cause_and_reason(kind):
    import sqlite3
    from backend.core.bank_pg import Connection
    connection = Connection.__new__(Connection)
    native = type(kind, (Exception,), {})("synthetic private driver text")
    events = []
    def fail(*args):
        events.append("execute")
        raise native
    connection._conn = SimpleNamespace(execute=fail, rollback=lambda: events.append("rollback"))
    with pytest.raises(sqlite3.OperationalError) as raised:
        connection.execute("SELECT 1")
    assert raised.value.__cause__ is native
    assert events == ["execute", "rollback"]
    details = code_details(exception_error_id(raised.value, bank="esun"))
    assert details and details[1] == {"UndefinedTable": "原生資料庫回報資料表不存在", "UndefinedColumn": "原生資料庫回報欄位不存在"}[kind]


def test_native_occurrence_proof_and_alias_reasons_are_distinct():
    store = synthetic_store("execute")
    rows = [
        {"native_identity": "fixture", "card_no": "fixture"},
        {"native_identity": "fixture", "card_no": "fixture", "txn_type": "purchase", "date": "2026-01-01", "consume_date": "2026-01-02", "currency": "TWD", "amount": 1},
    ]
    ids = []
    for row in rows:
        with pytest.raises(ValueError, match="incomplete native card occurrence") as raised:
            store._validate_native_card_occurrences([row])
        ids.append(exception_error_id(raised.value, bank="esun"))
    assert all(ids) and ids[0] != ids[1]

def test_fatal_guard_independent_disjunctions_have_separate_decisions():
    root = Path(__file__).resolve().parents[1] / "backend/core"
    remaining = []

    def rejection_or(test):
        if isinstance(test, ast.BoolOp):
            return isinstance(test.op, ast.Or) or any(rejection_or(value) for value in test.values)
        if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
            return rejection_or(test.operand)
        if isinstance(test, ast.Call) and isinstance(test.func, ast.Name) and test.func.id in {"any", "all"}:
            return any(rejection_or(arg.elt) for arg in test.args if isinstance(arg, ast.GeneratorExp))
        # `value or default` inside a call/comparison is data selection, not rejection.
        return False

    for path in [*sorted((root / "persist").glob("*.py")), root / "store.py"]:
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.If) and len(node.body) == 1 and isinstance(node.body[0], ast.Raise):
                if rejection_or(node.test):
                    remaining.append((path.name, ast.unparse(node.test)))
    assert remaining == []

@pytest.mark.parametrize("method,args", [("put_daily_metric", ("fixture", {"bad": float("nan")})), ("log_sync", ({"bad": object()},))])
def test_serialization_and_sql_write_have_separate_attribution(method, args):
    store = synthetic_store("execute")
    with pytest.raises((TypeError, ValueError)) as encoded:
        getattr(store, method)(*args)
    assert store.conn.events == []
    clean = ("fixture", {"ok": 1}) if method == "put_daily_metric" else ({"ok": 1},)
    with pytest.raises(BoundaryFailure) as written:
        getattr(store, method)(*clean)
    assert exception_error_id(encoded.value, bank="esun") != exception_error_id(written.value, bank="esun")


def test_store_native_database_calls_are_attributed_at_operation():
    from backend.core import store
    tree = ast.parse(Path(store.__file__).read_text())
    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    missing = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in {"execute", "fetchall", "fetchone", "commit", "rollback", "close"}:
            parent = parents.get(node)
            while parent and not isinstance(parent, (ast.With, ast.FunctionDef)):
                parent = parents.get(parent)
            if not isinstance(parent, ast.With):
                missing.append(ast.unparse(node.func))
    assert missing == []

@pytest.mark.parametrize("field,value", [("date", "bad"), ("summary", None), ("summary", " "), ("expense", ""), ("expense", "x"), ("deposit", "x"), ("balance", "x"), ("expense", "-1"), ("deposit", "-1")])
def test_scsb_twd_predicate_leaf(field, value):
    from backend.core.persist.scsb import persist_scsb
    row = {"account_no": "fixture", "date": "2026/01/01", "summary": "fixture", "expense": "1", "deposit": "", "balance": "1"}
    if field == "deposit":
        row["expense"] = ""
    row[field] = value
    with pytest.raises(ValueError) as raised:
        persist_scsb({"twd_inquiry": {"accounts": [{"account_no": "fixture", "record_count": 1}], "records": [row]}}, object())
    error_id = exception_error_id(raised.value, bank="scsb")
    assert error_id and error_id.startswith("SCSB-08-810-")


@pytest.mark.parametrize("field,value", [("date", "bad"), ("card_no", "bad"), ("scope", "wrong"), ("desc", None), ("desc", " "), ("amount", None), ("amount", True), ("amount", float("inf")), ("currency", "USD")])
def test_scsb_card_predicate_leaf(field, value):
    from backend.core.persist.scsb import persist_scsb
    row = {"date": "2026-01-01", "card_no": "****1234", "scope": "unbilled", "desc": "fixture", "amount": 1, "currency": "TWD"}
    row[field] = value
    with pytest.raises(ValueError) as raised:
        persist_scsb({"card_inquiry": {"leaves": {"unbilled": {"rows": [row]}}}}, object())
    error_id = exception_error_id(raised.value, bank="scsb")
    assert error_id and error_id.startswith("SCSB-08-811-")


@pytest.mark.parametrize("amount", [True, "bad", "100000001", "1.1"])
def test_hsbc_inventory_money_predicate_leaf(monkeypatch, amount):
    from backend.core.persist import hsbc
    monkeypatch.setattr(hsbc, "validate_history_coverage", lambda *a, **k: None)
    store = SimpleNamespace(latest_card_transaction_dates=lambda: {})
    card = {"maskedCardNumber": "1234-****-****-5678", "id": "fixture", "cardStatusDisplay": "ACTIVATED", "outstandingBalance": amount}
    with pytest.raises(ValueError) as raised:
        hsbc._validate_hsbc_history({"history_coverage": {"mode": "full", "domains": [{"domain": "card_billed_transactions"}]}, "cards": [card]}, store)
    error_id = exception_error_id(raised.value, bank="hsbc")
    assert error_id and error_id.startswith("HSBC-08-810-")

def test_catalog_descriptions_explain_actions_and_reasons_in_chinese():
    import re
    from backend.core.error_catalog.persist import CODES

    for error_id, (action, reason) in CODES.items():
        for description in (action, reason):
            assert len(re.findall(r"[\u4e00-\u9fff]", description)) >= 4, (error_id, description)
            assert not re.search(r"__init__|persist_\w+|->|\bfailed\b|\brequired coverage absent\b", description), (error_id, description)
            assert set(re.findall(r"[A-Za-z_]+", description)) <= {"API", "ISO", "JSON", "PostgreSQL", "SQLite", "SQL", "TWD", "FX"}, (error_id, description)


@pytest.mark.parametrize("action,reason", [
    ("__init__.persist_collected", "required coverage absent"),
    ("esun._native_money", "金額轉換失敗"),
    ("資料寫入 persist_esun", "參數驗證失敗"),
    ("銀行資料寫入", "required coverage absent"),
    ("銀行資料寫入", "解析資料失敗 module.function"),
])
def test_catalog_chinese_guard_rejects_placeholder_mutations(monkeypatch, action, reason):
    from backend.core.error_catalog.persist import CODES

    monkeypatch.setitem(CODES, "ESUN-08-800-002", (action, reason))
    with pytest.raises(AssertionError):
        test_catalog_descriptions_explain_actions_and_reasons_in_chinese()


def test_catalog_fixed_roles_match_source_not_intermediate_names():
    from backend.core.error_catalog.persist import CODES

    assert "本期應繳" in CODES["CTBC-08-007-003"][1]
    assert "來源帳號交易游標" in CODES["SYS-08-127-002"][1]
    assert "存款交易支出存入與餘額" in CODES["SYS-08-135-004"][1]
    assert "臺外幣" in CODES["SCSB-08-005-001"][0]
    for details in CODES.values():
        assert not any(placeholder in details[1] for placeholder in ("指定資料表或相容性結果", "解析後數值或日期", "轉換數值或變更筆數", "帳戶或交易餘額", "原生交易或帳戶識別"))


def test_catalog_is_literal_registered_and_bank_qualified():
    import re
    from backend.core.error_catalog import persist as catalog
    from backend.core.error_codes import BANKS
    source = ast.parse(Path(catalog.__file__).read_text())
    declaration = next(node for node in source.body if isinstance(node, ast.Assign))
    assert ast.literal_eval(declaration.value) == catalog.CODES
    assert len(catalog.CODES) <= 4096
    for error_id, details in catalog.CODES.items():
        assert re.fullmatch(r"(?:SYS|CATHAY|CTBC|DBS|ESUN|FUBON|HSBC|LINEBANK|RAKUTEN|SCB|SCSB|SINOPAC|TAISHIN|UBOT)-08-\d{3}-\d{3}", error_id)
        assert int(error_id.split("-")[2]) < 900
        assert code_details(error_id) == details
    root = Path(__file__).resolve().parents[1] / "backend/core"
    for path in [*sorted((root / "persist").glob("*.py")), *[root / name for name in ("money.py", "card_bills.py", "card_status.py", "store.py", "bank_pg.py")]]:
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and re.fullmatch(r"[A-Z]+-08-\d{3}-\d{3}", node.value):
                assert code_details(node.value), (path.name, node.value)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "bank_error_id":
                suffixes = ast.literal_eval(node.args[1].value).values() if isinstance(node.args[1], ast.Subscript) else [ast.literal_eval(node.args[1])]
                for suffix in suffixes:
                    for bank in BANKS:
                        assert code_details(bank.upper() + "-" + suffix, bank=bank)

def test_native_sql_execution_and_result_reads_do_not_share_a_boundary():
    root = Path(__file__).resolve().parents[1] / "backend/core"
    chains = []
    for name in ("store.py", "bank_pg.py"):
        for node in ast.walk(ast.parse((root / name).read_text())):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in {"fetchone", "fetchall"}:
                receiver = node.func.value
                if isinstance(receiver, ast.Call) and isinstance(receiver.func, ast.Attribute) and receiver.func.attr == "execute":
                    chains.append((name, node.lineno))
    assert chains == []


@pytest.mark.parametrize("method,args", [("stats", ()), ("_transaction_cursor_dates", ("twd_transactions",)), ("card_dedup_rows", ((),))])
def test_sql_execute_and_result_read_keep_distinct_native_boundaries(method, args):
    ids = []
    for boundary in ("execute", "fetchall", "fetchone"):
        store = synthetic_store(None)
        events = []
        error = BoundaryFailure("synthetic private driver failure")

        def fetch(name):
            events.append(name)
            if boundary != "execute":
                raise error
            return []

        def execute(sql, params=()):
            events.append("execute")
            if boundary == "execute":
                raise error
            return SimpleNamespace(fetchall=lambda: fetch("fetchall"), fetchone=lambda: fetch("fetchone"))

        store.conn.execute = execute
        with pytest.raises(BoundaryFailure) as raised:
            getattr(store, method)(*args)
        assert raised.value is error
        assert events == (["execute"] if boundary == "execute" else ["execute", "fetchone" if method == "stats" else "fetchall"])
        ids.append(exception_error_id(error, bank="esun"))
    assert ids[0] != ids[1]
    assert ids[1] == ids[2]


@pytest.mark.parametrize("bank,field,expected", [
    ("cathay", "credit_limit", "CATHAY-08-003-012"),
    ("cathay", "current", "CATHAY-08-003-013"),
    ("ctbc", "quota", "CTBC-08-007-003"),
    ("ctbc", "availBal", "CTBC-08-007-004"),
])
def test_real_persister_money_fields_keep_caller_role_at_terminal_sink(bank, field, expected):
    from backend.core import persist
    from backend.core.sync_errors import safe_failure_message

    if bank == "cathay":
        data = {"credit_card": {"cards": [{"number": "fixture"}], "quota": {field: "0.0000001"}}}
    else:
        data = {"summary": {"creditCardSummary": {field: "0.0000001"}}}
    store = synthetic_store(None)
    store.list_cards = lambda: []
    with pytest.raises(ValueError) as raised:
        getattr(persist, "persist_" + bank)(data, store)
    message = safe_failure_message(raised.value, bank, persistence=True)
    assert "0.0000001" not in message
    assert exception_error_id(raised.value, bank=bank) == expected
    assert f"[{expected}]" in message


@pytest.mark.parametrize("bank", ["cathay", "ctbc"])
def test_store_card_money_fields_have_fixed_bank_qualified_boundaries(monkeypatch, bank):
    from backend.core import store as module
    from backend.core.sync_errors import safe_failure_message

    for field, suffix in (("credit_limit", "001"), ("used_credit", "002"), ("bill_due_amount", "003"), ("last_payment_amount", "004")):
        store = synthetic_store(None)
        store.bank = bank
        error = BoundaryFailure("synthetic private money failure")
        calls = []

        def money(value, *args, **kwargs):
            calls.append(value)
            if value is not None:
                raise error
            return None

        monkeypatch.setattr(module, "native_money", money)
        with pytest.raises(BoundaryFailure) as raised:
            store.upsert_cards([{"number": "fixture", field: "invalid"}])
        expected = f"{bank.upper()}-08-802-{suffix}"
        assert raised.value is error
        assert calls.count("invalid") == 1
        assert exception_error_id(error, bank=bank) == expected
        assert f"[{expected}]" in safe_failure_message(error, bank, persistence=True)
        assert store.conn.events == []


@pytest.mark.parametrize("bank", ["cathay", "ctbc"])
def test_real_store_card_money_field_role_is_not_collapsed(bank):
    from backend.core.sync_errors import safe_failure_message

    ids = []
    for field in ("credit_limit", "used_credit", "bill_due_amount", "last_payment_amount"):
        store = synthetic_store(None)
        store.bank = bank
        with pytest.raises(ValueError) as raised:
            store.upsert_cards([{"number": "fixture", field: "1.1"}])
        error_id = exception_error_id(raised.value, bank=bank)
        assert error_id is not None
        assert f"[{error_id}]" in safe_failure_message(raised.value, bank, persistence=True)
        assert store.conn.events == []
        ids.append(error_id)
    assert ids == {
        "cathay": ["CATHAY-08-873-007", "CATHAY-08-874-007", "CATHAY-08-875-007", "CATHAY-08-876-007"],
        "ctbc": ["CTBC-08-873-007", "CTBC-08-874-007", "CTBC-08-875-007", "CTBC-08-876-007"],
    }[bank]
    assert len(set(ids)) == 4


@pytest.mark.parametrize("domain,expected", [("twd", "SCSB-08-810-001"), ("card", "SCSB-08-811-001")])
def test_real_persister_invalid_date_keeps_domain_at_terminal_sink(domain, expected):
    from backend.core.persist.scsb import persist_scsb
    from backend.core.sync_errors import safe_failure_message

    if domain == "twd":
        row = {"account_no": "fixture", "date": "bad", "summary": "fixture", "expense": "1", "deposit": "", "balance": "1"}
        data = {"twd_inquiry": {"accounts": [{"account_no": "fixture", "record_count": 1}], "records": [row]}}
    else:
        row = {"date": "bad", "card_no": "****1234", "scope": "unbilled", "desc": "fixture", "amount": 1, "currency": "TWD"}
        data = {"card_inquiry": {"leaves": {"unbilled": {"rows": [row]}}}}
    with pytest.raises(ValueError) as raised:
        persist_scsb(data, synthetic_store(None))
    assert exception_error_id(raised.value, bank="scsb") == expected
    assert f"[{expected}]" in safe_failure_message(raised.value, "scsb", persistence=True)


def test_second_bank_real_persister_invalid_date_reaches_terminal_sink():
    from backend.core.persist.esun import persist_esun
    from backend.core.sync_errors import safe_failure_message

    row = {"account_no": "9999999999999", "datetime": "bad", "account_date": None, "desc": "fixture", "expend": 1, "income": None, "balance": 1}
    coverage = {"mode": "full", "domains": [{
        "domain": "twd_transactions",
        "expected": [{"identity": "9999999999999", "start": "2026-08-01", "end": "2026-08-31"}],
        "windows": [{"identity": "9999999999999", "start": "2026-08-01", "end": "2026-08-31", "status": "complete", "pages": 1}],
    }]}
    with pytest.raises(ValueError) as raised:
        persist_esun({"twd_txns": [row], "history_coverage": coverage}, synthetic_store(None))
    assert exception_error_id(raised.value, bank="esun") == "ESUN-08-002-009"
    assert "[ESUN-08-002-009]" in safe_failure_message(raised.value, "esun", persistence=True)


def test_generic_postgres_driver_failure_keeps_store_action():
    from backend.core.bank_pg import Connection
    ids = []
    for method, args in [("upsert_accounts", ([{"account_no": "fixture", "raw_balance": 1}],)), ("put_daily_metric", ("fixture", {"ok": 1}))]:
        error = BoundaryFailure("synthetic driver failure")
        def fail(*args):
            raise error
        adapter = Connection.__new__(Connection)
        adapter._conn = SimpleNamespace(execute=fail)
        adapter.total_changes = 0
        store = synthetic_store("execute")
        store.conn = adapter
        with pytest.raises(BoundaryFailure) as raised:
            getattr(store, method)(*args)
        assert raised.value is error
        ids.append(exception_error_id(raised.value, bank="esun"))
    assert all(ids) and ids[0] != ids[1]


@pytest.mark.parametrize("field,expected", [("acctBal", "LINEBANK-08-830-002"), ("txAmt", "LINEBANK-08-832-002")])
def test_linebank_real_money_role_selected_before_shared_leaf(field, expected):
    from backend.core.persist.linebank import persist_linebank
    from backend.core.sync_errors import safe_failure_message
    if field == "acctBal":
        data = {"api_responses": {"payables": [{"resp": {"content": {"dpstAcctList": [{"acctNbr": "fixture", "acctBal": True}]}}}]}}
    else:
        data = {"api_responses": {"transactions": [{"resp": {"content": {"acctNbr": "fixture", "txLst": [{"txDt": "20260101", "txAmt": True}]}}}]}}
    store = synthetic_store(None)
    store.bank = "linebank"
    with pytest.raises(ValueError, match="^invalid native monetary value$") as raised:
        persist_linebank(data, store)
    assert exception_error_id(raised.value, bank="linebank") == expected
    assert f"[{expected}]" in safe_failure_message(raised.value, "linebank", persistence=True)
    assert store.conn.events == []


@pytest.mark.parametrize("field,expected", [("expend", "CTBC-08-862-002"), ("income", "CTBC-08-863-002"), ("balance", "CTBC-08-864-002")])
def test_ctbc_history_fields_through_real_store_and_parent_sink(field, expected):
    from backend.core.persist.ctbc import persist_ctbc
    from backend.server.sync_runner import _PersistenceError
    from backend.core.sync_errors import safe_failure_message
    raw = {"actDtTm": "2026-01-01-12.00.00.000000", "memo1": "fixture", {"expend": "dbAmt", "income": "crAmt", "balance": "balanceAmt"}[field]: True}
    store = synthetic_store(None)
    store.bank = "ctbc"
    with pytest.raises(_PersistenceError) as terminal:
        try:
            persist_ctbc({"twd_history": [{"account_no": "fixture", "months": {"m0": [raw]}}]}, store)
        except ValueError as error:
            raise _PersistenceError("persistence_failed") from error
    assert exception_error_id(terminal.value, bank="ctbc") == expected
    assert f"[{expected}]" in safe_failure_message(terminal.value, "ctbc", persistence=True)
    assert store.conn.events == []


@pytest.mark.parametrize("kind,field,expected", [
    ("billed", "amount", "CATHAY-08-841-008"),
    ("billed", "consume_amount", "CATHAY-08-842-008"),
    ("due", "currentPaymentAmount", "CATHAY-08-833-008"),
])
def test_cathay_real_billed_and_due_money_context(kind, field, expected):
    from backend.core.persist.cathay import persist_cathay
    from backend.server.sync_runner import _PersistenceError
    from backend.core.sync_errors import safe_failure_message
    if kind == "due":
        cc = {"cards": [{"number": "fixture"}], "bill_summary": {"currencies": [{field: "0.0000001"}]}}
    else:
        cc = {"billed_detail": {"TWD": [{"date": "2026-01-01", "post_date": "2026-01-02", "desc": "fixture", "amount": 1, field: "0.0000001"}]}}
    store = synthetic_store(None)
    store.bank = "cathay"
    store.list_cards = lambda: []
    store.upsert_twd_txns = lambda *a, **k: 0
    with pytest.raises(_PersistenceError) as terminal:
        try:
            persist_cathay({"credit_card": cc}, store)
        except ValueError as error:
            raise _PersistenceError("persistence_failed") from error
    assert exception_error_id(terminal.value, bank="cathay") == expected
    assert f"[{expected}]" in safe_failure_message(terminal.value, "cathay", persistence=True)


@pytest.mark.parametrize("value,expected", [
    (True, "CTBC-08-872-002"), ("bad", "CTBC-08-872-004"),
    ("NaN", "CTBC-08-872-005"), ("9007199254740992", "CTBC-08-872-006"),
    ("1.1", "CTBC-08-872-007"),
])
def test_real_store_current_balance_reason_reaches_parent_terminal(value, expected):
    from backend.server.sync_runner import _PersistenceError
    from backend.core.sync_errors import safe_failure_message
    # CTBC normalizes balance with the real-valued helper first; a fractional
    # TWD value reaches BankStore.upsert_accounts and must retain that field.
    store = synthetic_store(None)
    store.bank = "ctbc"
    with pytest.raises(_PersistenceError) as terminal:
        try:
            store.upsert_accounts([{"account_no": "fixture", "raw_balance": value, "currency": "TWD"}])
        except ValueError as error:
            raise _PersistenceError("persistence_failed") from error
    assert exception_error_id(terminal.value, bank="ctbc") == expected
    assert f"[{expected}]" in safe_failure_message(terminal.value, "ctbc", persistence=True)
    assert store.conn.events == []


@pytest.mark.parametrize("domain,expected", [("account", "UBOT-08-830-002"), ("billed", "UBOT-08-839-002"), ("pending", "UBOT-08-841-002")])
def test_ubot_real_monetary_siblings_reach_terminal(domain, expected):
    from datetime import date
    from backend.core.persist.ubot import _persist_ubot
    from backend.core.sync_errors import safe_failure_message
    if domain == "account":
        data = {"deposit_twd": {"NTList": [{"Account": "fixture", "AccountBal": True}]}}
    else:
        row = {"txAmt": 1, "oriAmt": True, "txDesc": "fixture", "Currency": "USD"}
        data = {"card_billed": [{"CardList": [row]}]} if domain == "billed" else {"card_unbilled": {"CardList": [row]}}
    store = synthetic_store(None)
    store.bank = "ubot"
    with pytest.raises(ValueError) as raised:
        _persist_ubot(data, store, as_of=date(2026, 1, 1))
    assert exception_error_id(raised.value, bank="ubot") == expected
    assert f"[{expected}]" in safe_failure_message(raised.value, "ubot", persistence=True)
    assert store.conn.events == []


def test_sinopac_existing_replacement_keeps_selected_native_field():
    from backend.core.persist.sinopac import _persist_sinopac
    from backend.core.sync_errors import safe_failure_message
    store = synthetic_store(None)
    store.bank = "sinopac"
    with pytest.raises(ValueError, match="^invalid SinoPac native amount$") as raised:
        _persist_sinopac({"bank_balance": [{"SubInfo": [{"AcctValue": "fixture", "AvailBalance": True}]}]}, store)
    assert raised.value.__cause__ is None
    assert raised.value.__suppress_context__ is True
    assert exception_error_id(raised.value, bank="sinopac") == "SINOPAC-08-830-002"
    assert "[SINOPAC-08-830-002]" in safe_failure_message(raised.value, "sinopac", persistence=True)
    assert store.conn.events == []


@pytest.mark.parametrize("value,expected", [(True, "CTBC-08-830-002"), ("1.1", "CTBC-08-872-007")])
def test_ctbc_account_balance_traverses_real_persister_and_store(value, expected):
    from backend.core.persist.ctbc import persist_ctbc
    from backend.core.sync_errors import safe_failure_message
    store = synthetic_store(None)
    store.bank = "ctbc"
    data = {"twd_deposit": {"demDepBalSummaryResponse": {"infoList": [{"accountId": "fixture", "balance": value}]}}}
    with pytest.raises(ValueError) as raised:
        persist_ctbc(data, store)
    assert exception_error_id(raised.value, bank="ctbc") == expected
    assert f"[{expected}]" in safe_failure_message(raised.value, "ctbc", persistence=True)
    assert store.conn.events == []


def test_owned_money_callers_forward_private_metadata_at_original_native_rejection():
    root = Path(__file__).resolve().parents[1] / "backend/core"
    helpers = {"native_money", "_num", "_num_real", "_num_to_float", "_finite_amount", "_dbs_money", "_native_amount", "_money"}
    for path in [*sorted((root / "persist").glob("*.py")), root / "store.py"]:
        for call in ast.walk(ast.parse(path.read_text())):
            if isinstance(call, ast.Call) and isinstance(call.func, ast.Name) and call.func.id in helpers:
                assert any(keyword.arg == "_codes" for keyword in call.keywords), (path.name, call.lineno)


def test_private_money_metadata_does_not_change_business_signature():
    import inspect
    from backend.core.persist._common import _num, _num_real, _num_to_float
    from backend.core.persist.cathay import persist_cathay
    from backend.core.persist.ctbc import persist_ctbc
    signature = inspect.signature(native_money)
    assert list(signature.parameters) == ["value", "currency", "optional", "absolute", "preserve_decimal", "_codes"]
    assert [signature.parameters[name].default for name in ("optional", "absolute", "preserve_decimal", "_codes")] == [False, False, False, None]
    assert signature.parameters["_codes"].kind is inspect.Parameter.KEYWORD_ONLY
    for helper in (_num, _num_real, _num_to_float):
        assert list(inspect.signature(helper).parameters) == ["s", "_codes"]
        assert inspect.signature(helper).parameters["_codes"].kind is inspect.Parameter.KEYWORD_ONLY
    for public in (persist_cathay, persist_ctbc):
        assert list(inspect.signature(public).parameters) == ["data", "store", "rules"]
