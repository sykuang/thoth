"""Natural Decimal failures through real offline producers and terminal cause traversal."""
import ast
from decimal import Overflow
from pathlib import Path
import sys

import pytest

from backend.core.error_codes import code_details, exception_error_id
from backend.core.money import native_money
from backend.core.persist._common import _num, _num_real, _num_to_float
from backend.core.sync_errors import safe_failure_message
from backend.server.sync_runner import _PersistenceError
from tests.test_ordered_persistence_codes import synthetic_store


NATIVE_REASON = "原生金額轉換或算術運算失敗"


@pytest.mark.parametrize("bank,field,expected,action", [
    ("linebank", "acctBal", "LINEBANK-08-830-010", "連線銀行 帳戶餘額摘要驗證"),
    ("linebank", "txAmt", "LINEBANK-08-832-010", "連線銀行 存款交易金額驗證"),
    ("ctbc", "balance", "CTBC-08-830-010", "中信 臺幣帳戶餘額驗證"),
    ("cathay", "credit_limit", "CATHAY-08-831-010", "國泰 整戶信用額度驗證"),
    ("cathay", "current", "CATHAY-08-832-010", "國泰 整戶本期已動用額度驗證"),
    ("store", "expend", "CTBC-08-862-010", "中信 存款交易 支出金額驗證"),
    ("store", "income", "CTBC-08-863-010", "中信 存款交易 存入金額驗證"),
    ("store", "balance", "CTBC-08-864-010", "中信 存款交易 交易後餘額驗證"),
])
def test_natural_overflow_producer_to_typed_parent(bank, field, expected, action):
    value = "1e1000000"
    store = synthetic_store(None)
    store.bank = "ctbc" if bank == "store" else bank
    store.list_cards = lambda: []
    if bank == "linebank":
        if field == "acctBal":
            data = {"api_responses": {"payables": [{"resp": {"content": {"dpstAcctList": [{"acctNbr": "fixture", field: value}]}}}]}}
        else:
            data = {"api_responses": {"transactions": [{"resp": {"content": {"acctNbr": "fixture", "txLst": [{"txDt": "20260101", field: value}]}}}]}}
    elif bank == "ctbc":
        data = {"twd_deposit": {"demDepBalSummaryResponse": {"infoList": [{"accountId": "fixture", field: value}]}}}
    elif bank == "cathay":
        data = {"credit_card": {"cards": [{"number": "fixture"}], "quota": {field: value}}}
    else:
        data = [{"account_no": "fixture", "datetime": "2026-01-01", field: value}]
    if bank == "store":
        produce = lambda: store.upsert_twd_txns(data)
    else:
        from backend.core import persist
        produce = lambda: getattr(persist, "persist_" + bank)(data, store)
    _assert_natural_terminal(produce, store, expected, action)


@pytest.mark.parametrize("field,expected,action", [
    ("transactionAmount", "DBS-08-830-010", "星展 臺幣交易金額驗證"),
    ("debitAmount", "DBS-08-831-010", "星展 臺幣交易支出驗證"),
    ("creditAmount", "DBS-08-832-010", "星展 臺幣交易存入驗證"),
    ("balance", "DBS-08-833-010", "星展 臺幣交易餘額驗證"),
])
@pytest.mark.parametrize("nested", [False, True])
def test_dbs_natural_scalar_and_dictionary_producer(field, expected, action, nested):
    from backend.core.persist.dbs import persist_dbs
    value = {"amount": "1e1000000"} if nested else "1e1000000"
    row = {"postingDate": "2026-01-01", "transactionAmount": "1", field: value}
    data = {"api_responses": {"inquiry": [{"url": "transactions-history/inquiry", "req_body": {"globalAccountId": "fixture"}, "resp": {"transactions": [row]}}]}}
    store = synthetic_store(None)
    store.bank = "dbs"
    _assert_natural_terminal(lambda: persist_dbs(data, store), store, expected, action)


def _assert_natural_terminal(produce, store, expected, action):
    # Observe the actual exception at the first Python frame; never replace Decimal.
    observations = []
    def trace(frame, event, arg):
        if event == "exception" and type(arg[1]) is Overflow:
            error = arg[1]
            observations.append((error, str(error), error.__cause__, error.__context__, error.__suppress_context__, frame.f_code.co_name))
        return trace
    old_trace = sys.gettrace()
    try:
        sys.settrace(trace)
        with pytest.raises(_PersistenceError) as terminal:
            try:
                produce()
            except Overflow as error:
                raise _PersistenceError("persistence_failed") from error
    finally:
        sys.settrace(old_trace)
    native = terminal.value.__cause__
    first, message, cause, context, suppressed, frame = observations[0]
    assert native is first and type(native) is Overflow
    assert str(native) == message == "[<class 'decimal.Overflow'>]"
    assert (native.__cause__, native.__context__, native.__suppress_context__) == (cause, context, suppressed) == (None, None, False)
    assert frame in {"native_money", "_num_to_float"}
    assert terminal.value.__cause__ is native
    assert terminal.value.__context__ is native
    assert terminal.value.__suppress_context__ is True
    assert store.conn.events == []
    assert exception_error_id(terminal.value, bank=store.bank) == expected
    assert code_details(expected) == (action, NATIVE_REASON)
    assert safe_failure_message(terminal.value, store.bank, persistence=True) == f"[{expected}] {action}：{NATIVE_REASON} | sync_failed:Exception: phase=persistence"


@pytest.mark.parametrize("helper,expected", [
    (native_money, None), (_num, "SYS-08-012-001"),
    (_num_real, "SYS-08-013-001"), (_num_to_float, None),
])
@pytest.mark.parametrize("nine", [False, True])
def test_legacy_defaults_and_nine_entry_native_fallback_unchanged(helper, expected, nine):
    codes = tuple(f"SYS-08-001-{i:03d}" for i in range(1, 10))
    kwargs = {"_codes": codes} if nine else {}
    with pytest.raises(Overflow) as caught:
        helper("1e1000000", "TWD", **kwargs) if helper is native_money else helper("1e1000000", **kwargs)
    assert exception_error_id(caught.value) == expected


@pytest.mark.parametrize("value", [None, "", "-", True, "bad", "NaN", "Infinity", "9007199254740992"])
def test_float_preflight_original_unavailable_policy(value):
    codes = ("SYS-08-001-001",) * 9 + ("CATHAY-08-831-010",)
    assert _num_to_float(value) is None
    assert _num_to_float(value, _codes=codes) is None


@pytest.mark.parametrize("helper_name,expected", [
    ("rakuten", "RAKUTEN-08-830-010"),
    ("dbs", "DBS-08-830-010"),
])
def test_source_owned_wrapper_preconversion_failure_is_same_object(helper_name, expected):
    # Deliberately adversarial Python protocols, not natural JSON reachability.
    from backend.core.persist.rakuten import _money
    from backend.core.persist.dbs import _dbs_money
    error = RuntimeError("synthetic conversion failure")
    events = []
    class BadString:
        def __str__(self):
            events.append("str")
            raise error
    class BadDict(dict):
        def get(self, key):
            events.append(key)
            raise error
    codes = ("SYS-08-001-001",) * 9 + (expected,)
    function, value = (_money, BadString()) if helper_name == "rakuten" else (_dbs_money, BadDict())
    with pytest.raises(RuntimeError) as caught:
        function(value, _codes=codes)
    assert caught.value is error
    assert (error.__cause__, error.__context__, error.__suppress_context__) == (None, None, False)
    assert len(events) == 1
    assert exception_error_id(error) == expected


def test_sinopac_native_arithmetic_before_argument_tuple_keeps_aggregate_role():
    from decimal import Inexact, localcontext
    from backend.core.persist.sinopac import _persist_sinopac
    store = synthetic_store(None)
    store.bank = "sinopac"
    data = {"bank_balance": [{"SubInfo": [
        {"AcctValue": "fixture1", "AvailBalance": "9007199254740991"},
        {"AcctValue": "fixture2", "AvailBalance": "9007199254740991"},
    ]}]}
    # Exercise an existing caller context, never change the production context.
    with localcontext() as context:
        context.prec = 16
        context.traps[Inexact] = True
        with pytest.raises(Inexact) as caught:
            _persist_sinopac(data, store)
    assert store.conn.events == []
    assert exception_error_id(caught.value) == "SINOPAC-08-831-010"
    assert code_details("SINOPAC-08-831-010")[1] == NATIVE_REASON


@pytest.mark.parametrize("action,expected", [
    ("SINOPAC-08-015-041", "SINOPAC-08-831-010"),
    ("SINOPAC-08-015-042", "SINOPAC-08-832-010"),
    ("SINOPAC-08-015-044", "SINOPAC-08-836-010"),
    ("SINOPAC-08-015-045", "SINOPAC-08-837-010"),
])
def test_aggregate_argument_native_boundary_is_source_owned(action, expected):
    from backend.core.persist import sinopac
    source = Path(sinopac.__file__).read_text()
    # Static coverage only: arguments execute before the helper receives _codes.
    assert f"with error_step('{expected}'):" in source


def test_catalog_preserves_all_3693_existing_ids_and_chinese():
    import hashlib
    import json
    from backend.core.error_catalog.persist import CODES
    added = {key: value for key, value in CODES.items() if value[1] == NATIVE_REASON}
    old = {key: value for key, value in CODES.items() if key not in added}
    assert len(added) == 336
    assert len(CODES) == 4029 <= 4096
    assert len(old) == 3693
    assert hashlib.sha256(json.dumps(old, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest() == "315a87274876c52ff6dfa441b40f7913043bbf45eb743e9d1fbb5441438b1fc3"


@pytest.mark.parametrize("family", ["InvalidOperation", "Inexact", "Rounded", "Underflow"])
def test_native_decimal_context_failure_is_not_an_explicit_financial_rejection(family):
    import decimal
    from backend.core.persist.cathay import persist_cathay
    store = synthetic_store(None)
    store.bank = "cathay"
    store.list_cards = lambda: []
    value = "123.123456"
    with decimal.localcontext() as context:
        if family == "InvalidOperation":
            context.prec = 8
        elif family == "Underflow":
            context.prec = 3
            context.Emin = -2
            value = "0.000001"
        else:
            context.prec = 8
        context.traps[getattr(decimal, family)] = True
        with pytest.raises(getattr(decimal, family)) as caught:
            persist_cathay({"credit_card": {"cards": [{"number": "fixture"}], "quota": {"credit_limit": value}}}, store)
    assert type(caught.value) is getattr(decimal, family)
    assert exception_error_id(caught.value) == "CATHAY-08-831-010"
    assert code_details("CATHAY-08-831-010") == ("國泰 整戶信用額度驗證", NATIVE_REASON)
    assert store.conn.events == []


def test_all_source_owned_monetary_tuples_have_native_fallback():
    root = Path(__file__).resolve().parents[1] / "backend/core"
    tuples = []
    for path in [*sorted((root / "persist").glob("*.py")), root / "store.py"]:
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.keyword) and node.arg == "_codes" and not isinstance(node.value, ast.Name):
                tuples.extend(n for n in ast.walk(node.value) if isinstance(n, ast.Tuple))
    assert len(tuples) == 132
    for codes in tuples:
        assert len(codes.elts) == 10
        final = codes.elts[-1]
        if isinstance(final, ast.Call):
            assert final.func.id == "bank_error_id"
            suffix = ast.literal_eval(final.args[1])
            from backend.core.error_codes import BANKS
            ids = [bank.upper() + "-" + suffix for bank in BANKS]
        else:
            ids = [ast.literal_eval(final)]
        for error_id in ids:
            assert error_id.endswith("-010")
            assert code_details(error_id)[1] == NATIVE_REASON
