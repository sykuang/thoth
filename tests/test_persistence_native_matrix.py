"""Finite native matrix: helper replay is NOT public JSON/caller reachability.

Store methods use plain synthetic rows and the real methods. Tuple replays use
real Decimal operations but substitute the operand at an isolated helper call;
they establish metadata plumbing only, never production dispatcher coverage.
"""
import ast
from collections import Counter
from decimal import InvalidOperation, Overflow, localcontext
from importlib import import_module
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.core.error_codes import BANKS, bank_error_id, code_details, exception_error_id
from backend.core.sync_errors import safe_failure_message
from backend.server.sync_runner import _PersistenceError
from tests.test_ordered_persistence_codes import synthetic_store


ROOT = Path(__file__).resolve().parents[1]
REASON = "原生金額轉換或算術運算失敗"
LABELS = dict(cathay="國泰", ctbc="中信", dbs="星展", esun="玉山", fubon="富邦", hsbc="滙豐", linebank="連線銀行", rakuten="樂天", scb="渣打", scsb="上海商銀", sinopac="永豐", taishin="台新", ubot="聯邦")


class PersisterStore:
    """Inert persistence dependency, recording original preceding operations."""
    def __init__(self, bank, cards=()):
        self.bank = bank
        self.cards = list(cards)
        self.events = []
        self.conn = self

    def list_cards(self):
        self.events.append("list_cards")
        return self.cards

    def latest_twd_transaction_dates(self):
        return {}

    def latest_card_transaction_dates(self):
        return {}

    def __getattr__(self, name):
        if name.startswith(("upsert_", "refresh_", "put_", "log_")) or name in {"commit", "rollback"}:
            def call(*args, **kwargs):
                self.events.append(name)
                return 0
            return call
        raise AssertionError(f"unexpected dependency: {name}")


def _plain_bank_cases():
    value = "1e1000000"
    cases = []
    def add(bank, number, data, cards=()):
        cases.append(pytest.param(bank, number, data, cards, id=f"{bank}-{number}"))
    add("cathay", 830, {"loan": {"accounts": [{"accountNo": "fixture", "balance": value}]}})
    for number, field in [(831, "credit_limit"), (832, "current")]:
        add("cathay", number, {"credit_card": {"cards": [{"number": "fixture"}], "quota": {field: value}}})
    add("cathay", 833, {"credit_card": {"cards": [{"number": "fixture"}], "bill_summary": {"currencies": [{"currentPaymentAmount": value}]}}})
    add("cathay", 834, {"credit_card": {"cards": [{"number": "fixture"}], "latest_bill": {"twd": {"billAmount": value}}}})
    add("cathay", 835, {"credit_card": {"cards": [{"number": "fixture"}], "billed_detail": {"TWD": [{"post_date": "2026-01-01", "desc": "自動扣繳", "amount": value}]}}})
    add("cathay", 836, {"credit_card": {"cards": [{"number": "fixture"}]}, "twd_transactions": [{"transactions": [{"expend": value}]}]})
    add("cathay", 837, {}, [{"last_payment_date": "2026-01-01", "last_payment_amount": value}])
    for number, field in [(838, "expend"), (839, "income"), (840, "balance")]:
        add("cathay", number, {"twd_transactions": [{"account": "fixture", "transactions": [{field: value}]}]})
    for number, field in [(841, "amount"), (842, "consume_amount")]:
        add("cathay", number, {"credit_card": {"billed_detail": {"TWD": [{"date": "2026-01-01", "amount": 1, field: value}]}}})
    for number, field, key in [(843, "amount", "unbilled_detail"), (844, "consume_amount", "unbilled_detail"), (845, "amount", "current_detail"), (846, "consume_amount", "current_detail")]:
        add("cathay", number, {"credit_card": {key: {"consume": [{"amount": 1, field: value}]}}})
    add("ctbc", 830, {"twd_deposit": {"demDepBalSummaryResponse": {"infoList": [{"accountId": "fixture", "balance": value}]}}})
    for number, field in [(831, "quota"), (832, "availBal"), (833, "unpaidStmt")]:
        add("ctbc", number, {"summary": {"creditCardSummary": {field: "1000"}}})
    add("ctbc", 834, {"card_api_dump": {"/twrbc-card/qu038/011": {"billDataTWD": [{"amt": value}]}}})
    add("ctbc", 835, {"card_api_dump": {"/twrbc-card/qu006/011": {"allItems": [{"cardNoSuffixFour": "1234_0", "description": "fixture", "purchaseAmt": 1, "purchaseDt": "20260101", "origCurAmt": value}]}}})
    for number, field in [(830, "transactionAmount"), (831, "debitAmount"), (832, "creditAmount"), (833, "balance")]:
        add("dbs", number, {"api_responses": {"inquiry": [{"url": "transactions-history/inquiry", "req_body": {"globalAccountId": "fixture"}, "resp": {"transactions": [{"postingDate": "2026-01-01", "transactionAmount": 1, field: value}]}}]}})
    add("dbs", 834, {"api_responses": {"liabilities": [{"resp": {"creditCard": {"paymentDetails": {"amount": value}}}}]}})
    add("dbs", 835, {"dbs_card_fee_page": {"bill_due_amount": value}, "api_responses": {"liabilities": [{"resp": {}}]}})
    add("dbs", 837, {"api_responses": {"assets": [{"resp": {"casa": {"accounts": [{"accountId": "fixture", "availableBalance": {"balance": value}}]}}}]}})
    add("dbs", 838, {"api_responses": {"liabilities": [{"resp": {"loan": {"accountId": "fixture", "balance": value}}}]}})
    add("linebank", 830, {"api_responses": {"payables": [{"resp": {"content": {"dpstAcctList": [{"acctNbr": "fixture", "acctBal": value}]}}}]}})
    for number, field in [(832, "txAmt"), (833, "afTxBal")]:
        add("linebank", number, {"api_responses": {"transactions": [{"resp": {"content": {"acctNbr": "fixture", "txLst": [{"txDt": "20260101", field: value}]}}}]}})
    add("scb", 830, {"accounts": [{"account_no": "fixture", "balance": value}]})
    add("esun", 830, {"accounts": [{"account_no": "fixture", "balance": value}]})
    add("esun", 831, {"card_summary": {"credit_limit_twd": value}})
    add("esun", 832, {"card_quota": {"used_credit_twd": value}})
    add("esun", 833, {"card_quota": {"credit_limit_twd": value}})
    add("esun", 834, {"card_transactions": [{"status": "已入帳", "billed_amount": value}]})
    add("esun", 835, {"card_bills": [{"due_amount": value}]})
    add("esun", 836, {"card_bills": [{"paid_amount": value}]})
    add("esun", 837, {"card_pay_history": {"records": [{"paid_amount": value}]}})
    for number, key in [(830, "NTList"), (831, "FTList"), (832, "LoanList")]:
        add("ubot", number, {"deposit_twd": {key: [{"Account": "fixture", "AccountBal": value}]}})
    for number, field in [(833, "Deposit"), (834, "Loan")]:
        add("ubot", number, {"deposit_twd": {"TotalData": {field: value}}})
    for number, field in [(835, "Expenditure"), (836, "Income"), (837, "Balance")]:
        add("ubot", number, {"twd_txns": [{"Account": "fixture", "NTDetailList": [{field: value}]}]})
    for number, field in [(838, "txAmt"), (839, "oriAmt")]:
        add("ubot", number, {"card_billed": [{"CardList": [{"txAmt": 1, "Summary": "fixture", field: value}]}]})
    for number, field in [(840, "txAmt"), (841, "oriAmt")]:
        add("ubot", number, {"card_unbilled": {"CardList": [{"txAmt": 1, "Summary": "fixture", field: value}]}})
    for number, field in [(842, "crLmt"), (843, "avalCrLmt"), (845, "unsettleAmt"), (846, "payAmt"), (847, "lastPayAmt")]:
        add("ubot", number, {"card_limit": {"CardList": [{field: value}]}})
    add("ubot", 844, {"card_summary": {"CardList": [{"avalCrLmt": value}]}})
    add("ubot", 848, {"card_pay_history": {"DateList": [{"payAmt": value}]}})
    add("sinopac", 830, {"bank_balance": [{"SubInfo": [{"AcctValue": "fixture", "AvailBalance": value}]}]})
    for number, field in [(833, "LoanAmt"), (834, "LoanBalance")]:
        add("sinopac", number, {"loan": {"fetch_ok": True, "details": [{"account": "fixture", "records": [{field: value}]}]}})
    add("sinopac", 838, {"card_statements": [{"summary": {"current_due": value}}]})
    add("sinopac", 839, {"card_statements": [{"records": [{"amount": value}]}]})
    for number, field in [(840, "信用額度"), (841, "以刷卡未請款金額"), (842, "本期應繳"), (844, "最近繳款金額")]:
        add("sinopac", number, {"card_summary": [{"SubInfo": [[{"DataText": field, "DataValue": value}]]}]})
    add("sinopac", 845, {"account_transactions": [{"account": "fixture", "currency": "TWD", "records": [{"DataText4": "1", "DataText5": value}]}]})
    add("sinopac", 847, {"card_unbilled": {"latest_tx": {"Result": {"Items": [{"AuthAmount": value}]}}}})
    from tests.taishin_fixtures import with_taishin_history
    taishin = [
        (830, {"api_responses": {"query": {"OUTPUTDATA": {"SavingAccount": [{"accountNo": "fixture", "balance": value}]}}}}),
        (831, {"api_responses": {"query": {"OUTPUTDATA": {"SavingAccount": [{"accountNo": "fixture", "balance": "1000"}]}}}}),
        (832, {"api_responses": {"qryRealTime": {"value": {"crlimit": value}}}}),
        (833, {"credit_card_parsed": {"billed_txns": [{"amount": value}]}}),
        (834, {"api_responses": {"doXTPA": {"value": {"001": {"OUT-CRLIMIT-PERM": value}}}}}),
        (835, {"api_responses": {"doXTPA": {"value": {"001": {"OUT-AVAIL-CREDIT": value}}}}}),
        (836, {"credit_card_parsed": {"summary": {"remaining": value}}}),
        (837, {"credit_card_parsed": {"summary": {"bill_amount": value}}}),
        (838, {"credit_card_parsed": {"summary": {"paid": value}}}),
        (840, {"credit_card_parsed": {"cards": [{"number": "fixture"}], "top_summary": {"unpaid": value}}}),
    ]
    from datetime import date
    for number, payload in taishin:
        add("taishin", number, with_taishin_history(payload, as_of=date(2026, 10, 28)))
    for number, text in [
        (835, "正卡人信用額度\t1000"),
        (836, "正卡人可用額度\t1000"),
        (837, "本期帳單結帳日\t應繳總金額\t最低應繳金額\t繳款截止日\tX\n2026/01/01\t1000\t0\t2026/01/15"),
        (838, "繳款狀態 自動扣繳帳號\n已繳\t2026/01/01\t1000\t0\tfixture"),
    ]:
        add("fubon", number, {"amount_page_text": text})
    add("rakuten", 830, {"twd_txn_results": [{"account_no": "fixture", "accounts": [{"balance": "1000"}], "txDetails": []}]})
    for number, field in [(831, "amt"), (832, "balance")]:
        add("rakuten", number, {"twd_txn_results": [{"account_no": "fixture", "accounts": [{"balance": "1"}], "txDetails": [{"sysDate": "2026/01/01", "sysTime": "12:00:00", "txDesc": "fixture", "amt": "1", "amtSign": True, "balance": "1", "nickNameOrAcct": None, "memo": "", field: "1000"}]}]})
    return cases


@pytest.mark.parametrize("bank,number,data,cards", _plain_bank_cases())
def test_plain_json_bank_persister_source_roles(bank, number, data, cards, record_property):
    from copy import deepcopy
    from backend.core import persist
    store = PersisterStore(bank, cards)
    configured = bank in {"fubon", "rakuten"} or bank == "ctbc" and number in {831, 832, 833}
    precision_only = bank == "taishin" and number == 831
    from decimal import InvalidOperation
    family = InvalidOperation if precision_only else Overflow
    with localcontext() as context:
        if configured:
            context.Emax = 2
        if precision_only:
            context.prec = 6
        with pytest.raises(family) as caught:
            if bank in {"ubot", "sinopac", "rakuten"}:
                from datetime import date
                module = import_module("backend.core.persist." + bank)
                getattr(module, "_persist_" + bank)(deepcopy(data), store, rules=None, as_of=date(2026, 10, 28))
            else:
                getattr(persist, "persist_" + bank)(deepcopy(data), store)
    expected = f"{bank.upper()}-08-{number}-010"
    assert exception_error_id(caught.value) == expected
    assert caught.value.__cause__ is caught.value.__context__ is None
    _typed_sink(caught.value, bank, expected, code_details(expected)[0])
    kind = "private_persister_json_preflight_not_claimed" if bank in {"ubot", "sinopac", "rakuten"} else "natural_json_public_persister"
    record_property("native_evidence", json.dumps(dict(id=expected, kind=kind, configured_context=configured or precision_only, family=family.__name__, dependencies=store.events)))


@pytest.mark.parametrize("number,key", [(830, "Credit Limit"), (831, "Last Statement Amount"), (832, "Last Payment Amount"), (833, None)])
def test_hsbc_natural_configured_context(number, key, record_property):
    from decimal import InvalidOperation
    from backend.core.persist.hsbc import persist_hsbc
    from tests.test_hsbc_history_sync import _persist_payload
    data = _persist_payload()
    entry = next(iter(data["card_detail"].values()))
    for card in data["cards"]:
        card["outstandingBalance"] = "0"
    if key:
        entry["detail"]["details"] = [{"key": key, "value": "100 TWD"}]
    else:
        data["cards"][0]["outstandingBalance"] = "100"
    with localcontext() as context:
        context.prec = 6
        with pytest.raises(InvalidOperation) as caught:
            persist_hsbc(data, PersisterStore("hsbc"))
    expected = f"HSBC-08-{number}-010"
    assert exception_error_id(caught.value) == expected
    _typed_sink(caught.value, "hsbc", expected, code_details(expected)[0])
    record_property("native_evidence", json.dumps(dict(id=expected, kind="natural_json_public_persister", configured_context=True, family="InvalidOperation")))


@pytest.mark.parametrize("bank,number", [("scsb", n) for n in range(830, 833)] + [("fubon", n) for n in range(830, 835)])
def test_natural_text_parsers_configured_context(bank, number, record_property):
    module = import_module("backend.core.persist." + bank)
    with localcontext() as context:
        context.Emax = 2
        with pytest.raises(Overflow) as caught:
            if bank == "scsb":
                text = {
                    830: "交易日\t交易時間\t卡號末4碼\t商店名稱\t交易金額\t交易結果\n2026/01/01\t12:00\t1234\tfixture\t1000\t成功",
                    831: "Transaction Date\tLast 4 Card No\tMerchant\tAmount\n2026/01/01\t1234\tfixture\t1000",
                    832: "Transaction Date\n2026/01/01 12:00 1234 fixture NT$1000",
                }[number]
                module._scsb_parse_card_rows(text, "unbilled")
            elif number == 830:
                module._parse_fubon_deposit_accounts("01234567890123\t活儲\t臺幣\t1000")
            else:
                cells = ["2026/01/01", "2026/01/01 12:00:00", "fixture", "1", "1", "1"]
                if number == 834:
                    text = "2026/01/01\tfixture\t1000"
                else:
                    cells[number - 828] = "1000"
                    text = "\t".join(cells)
                module._parse_fubon_deposit_txn_results([{"account_no": "fixture", "text": text}])
    expected = f"{bank.upper()}-08-{number}-010"
    assert exception_error_id(caught.value) == expected
    _typed_sink(caught.value, bank, expected, code_details(expected)[0])
    record_property("native_evidence", json.dumps(dict(id=expected, kind="private_parser_plain_source_text", configured_context=True)))


@pytest.mark.parametrize("bank,shadow,first", [("dbs", 836, 834), ("linebank", 831, 830), ("sinopac", 835, 834), ("sinopac", 843, 842), ("sinopac", 846, 839), ("ubot", 849, 833), ("taishin", 839, 833)])
def test_plain_json_duplicate_operand_first_failure_control(bank, shadow, first, record_property):
    import sys
    from copy import deepcopy
    from datetime import date
    from backend.core import persist
    _, _, data, cards = next(case.values for case in _plain_bank_cases() if case.values[:2] == (bank, first))
    selected = []
    def trace(frame, event, arg):
        if event == "call" and frame.f_code.co_filename.startswith(str(ROOT / "backend/core")):
            codes = frame.f_locals.get("_codes")
            if isinstance(codes, tuple) and len(codes) == 10:
                selected.append(codes[-1])
        return trace
    old = sys.gettrace()
    try:
        sys.settrace(trace)
        with pytest.raises(Overflow) as caught:
            if bank in {"ubot", "sinopac"}:
                module = import_module("backend.core.persist." + bank)
                getattr(module, "_persist_" + bank)(deepcopy(data), PersisterStore(bank, cards), rules=None, as_of=date(2026, 10, 28))
            else:
                getattr(persist, "persist_" + bank)(deepcopy(data), PersisterStore(bank, cards))
    finally:
        sys.settrace(old)
    expected, later = f"{bank.upper()}-08-{first}-010", f"{bank.upper()}-08-{shadow}-010"
    assert exception_error_id(caught.value) == expected
    assert expected in selected and later not in selected
    record_property("shadow_control", json.dumps(dict(target=later, first=expected, entered=selected, constraint="stable plain JSON operand / unchanged Decimal context; same conversion repeated or stricter prior USD conversion before TWD")))


STORE_ROLES = [
    (862, "upsert_twd_txns", "expend", "存款交易 支出金額"),
    (863, "upsert_twd_txns", "income", "存款交易 存入金額"),
    (864, "upsert_twd_txns", "balance", "存款交易 交易後餘額"),
    (865, "upsert_card_billed", "amount", "已入帳信用卡交易金額"),
    (866, "upsert_card_billed", "consume_amount", "已入帳信用卡原幣金額"),
    (868, "_refresh_card_pending", "amount", "未出帳信用卡交易金額"),
    (869, "_refresh_card_pending", "consume_amount", "未出帳信用卡原幣金額"),
    (870, "upsert_balance_history", "twdBalance", "每日臺幣存款餘額"),
    (871, "upsert_balance_history", "loanBalance", "每日貸款本金餘額"),
    (872, "upsert_accounts", "raw_balance", "帳戶目前原幣餘額"),
    (873, "upsert_cards", "credit_limit", "信用卡狀態 信用額度"),
    (874, "upsert_cards", "used_credit", "信用卡狀態 已使用額度"),
    (875, "upsert_cards", "bill_due_amount", "信用卡狀態 本期應繳金額"),
    (876, "upsert_cards", "last_payment_amount", "信用卡狀態 最近繳款金額"),
    (877, "update_card_bill_facts", "bill_due_amount", "權威帳單本期應繳金額"),
    (878, "update_card_bill_facts", "last_payment_amount", "權威帳單最近繳款金額"),
]


def _typed_sink(error, bank, expected, action):
    # A test-owned typed carrier, NOT the production dispatcher/executor.
    try:
        raise _PersistenceError("persistence_failed") from error
    except _PersistenceError as terminal:
        assert terminal.__cause__ is error
        assert exception_error_id(terminal, bank=bank) == expected
        assert code_details(expected) == (action, REASON)
        assert safe_failure_message(terminal, bank, persistence=True) == f"[{expected}] {action}：{REASON} | sync_failed:Exception: phase=persistence"


@pytest.mark.parametrize("bank", sorted(BANKS))
@pytest.mark.parametrize("number,method,field,label", STORE_ROLES, ids=[str(r[0]) for r in STORE_ROLES])
def test_plain_store_native_overflow_all_qualifications(bank, number, method, field, label, record_property):
    store = synthetic_store(None)
    store.bank = bank
    row = {"account_no": "fixture", "number": "fixture", "datetime": "2026-01-01", "amount": 1, field: "1e1000000"}
    with localcontext() as context:
        context.clear_flags()
        with pytest.raises(Overflow) as caught:
            if method == "_refresh_card_pending":
                store._refresh_card_pending("unbilled", [row])
            else:
                getattr(store, method)([row])
        assert {k.__name__ for k, v in context.flags.items() if v} == {"Overflow", "Inexact", "Rounded"}
    expected = f"{bank.upper()}-08-{number}-010"
    _typed_sink(caught.value, bank, expected, f"{LABELS[bank]} {label}驗證")
    assert caught.value.__cause__ is caught.value.__context__ is None
    assert caught.value.__suppress_context__ is False
    # Billed validation has exactly two original read-only native-row scans.
    assert store.conn.events == (["execute", "execute"] if method == "upsert_card_billed" else [])
    record_property("native_evidence", json.dumps(dict(id=expected, kind="natural_json_store_method", calls=store.conn.events)))


@pytest.mark.parametrize("number,field,label", [(860, "amount", "未入帳比對入帳金額"), (861, "consume_amount", "未入帳比對原幣金額")])
def test_plain_native_identity_overflow(number, field, label, record_property):
    from backend.core.store import _pending_billed_identity
    row = dict(native_identity="fixture", card_no="fixture", txn_type="purchase", date="2026-01-01", currency="TWD", amount=1)
    row[field] = "1e1000000"
    with pytest.raises(Overflow) as caught:
        _pending_billed_identity(row)
    expected = f"SYS-08-{number}-010"
    _typed_sink(caught.value, "ctbc", expected, f"銀行 {label}驗證")
    record_property("native_evidence", json.dumps(dict(id=expected, kind="natural_json_private_identity")))


@pytest.mark.parametrize("bank", sorted(BANKS))
def test_plain_stored_overlay_native_exact_overflow(bank, record_property):
    # Synthetic persisted rows, not JSON received from the crawler. No DB opens.
    store = synthetic_store(None)
    store.bank = bank
    target = dict(id=1, card_no="fixture", consume_date="2026-01-01", amount=1, currency="TWD", description="fixture")
    donor = {**target, "amount": "1e1000000"}
    calls = []
    def execute(sql, params=()):
        calls.append(sql)
        assert sql.startswith("SELECT ")
        return SimpleNamespace(fetchall=lambda: [donor], fetchone=lambda: target)
    store.conn.execute = execute
    with pytest.raises(Overflow) as caught:
        store._promote_card_pending_overlays("unbilled", [{"card_no": "fixture"}], [], [1], set())
    expected = f"{bank.upper()}-08-867-010"
    _typed_sink(caught.value, bank, expected, f"{LABELS[bank]} 未入帳精確比對金額驗證")
    assert len(calls) == 2
    record_property("native_evidence", json.dumps(dict(id=expected, kind="natural_synthetic_stored_row", reads=len(calls))))


def _tuple_cases():
    """Inventory metadata expressions only; do not generate production IDs."""
    cases = []
    for path in [*sorted((ROOT / "backend/core/persist").glob("*.py")), ROOT / "backend/core/store.py"]:
        tree = ast.parse(path.read_text())
        for call in ast.walk(tree):
            if not isinstance(call, ast.Call):
                continue
            for kw in call.keywords:
                if kw.arg != "_codes" or isinstance(kw.value, ast.Name):
                    continue
                for codes in ast.walk(kw.value):
                    if not isinstance(codes, ast.Tuple):
                        continue
                    final = codes.elts[-1]
                    banks = sorted(BANKS) if isinstance(final, ast.Call) else [None]
                    for bank in banks:
                        namespace = dict(bank_error_id=bank_error_id, self=SimpleNamespace(bank=bank))
                        value = eval(compile(ast.Expression(codes), str(path), "eval"), namespace)
                        cases.append(pytest.param(path, call.func.id, value, id=value[-1]))
    return cases


@pytest.mark.parametrize("path,helper,codes", _tuple_cases())
def test_isolated_source_tuple_real_decimal_replay(path, helper, codes, record_property):
    # Operand substitution at the helper boundary; NOT natural source reachability.
    module = import_module(".".join(path.relative_to(ROOT).with_suffix("").parts))
    function = getattr(module, helper)
    # Rakuten removes exponent syntax; long numeric text exercises real abs Overflow.
    value = "1" + "0" * 1000000 if helper == "_money" else "1e1000000"
    with pytest.raises(Overflow) as caught:
        if helper in {"native_money", "_native_amount"}:
            function(value, "TWD", _codes=codes)
        else:
            function(value, _codes=codes)
    expected = codes[-1]
    bank = expected.partition("-")[0].lower()
    bank = "ctbc" if bank == "sys" else bank
    _typed_sink(caught.value, bank, expected, code_details(expected)[0])
    record_property("native_evidence", json.dumps(dict(id=expected, kind="isolated_helper_operand_substitution_not_caller", helper=helper)))


@pytest.mark.parametrize("number", [831, 832, 836, 837])
def test_sinopac_four_real_aggregate_operations(number, record_property):
    from backend.core.persist.sinopac import _persist_sinopac
    store = synthetic_store(None)
    store.bank = "sinopac"
    value = "9007199254740991"
    if number in {831, 832}:
        currency = "TWD" if number == 831 else "USD"
        data = {"bank_balance": [{"SubInfo": [dict(AcctValue=f"fixture{i}", Curr=currency, AvailBalance=value) for i in range(2)]}]}
    else:
        record = dict(Currency="TWD", LoanBalance=value)
        details = [dict(account="fixture1", records=[record, record])] if number == 836 else [dict(account=f"fixture{i}", records=[record]) for i in range(2)]
        data = {"loan": {"fetch_ok": True, "details": details}}
    with localcontext() as context:
        # Individual exponent 15 fits; addition produces exponent 16. Keep
        # precision 28 so earlier USD quantize operations remain exact.
        context.prec = 28
        context.Emax = 15
        context.traps[Overflow] = True
        with pytest.raises(Overflow) as caught:
            _persist_sinopac(data, store)
    expected = f"SINOPAC-08-{number}-010"
    assert exception_error_id(caught.value) == expected
    assert store.conn.events == []
    record_property("native_evidence", json.dumps(dict(id=expected, kind="private_persister_json_configured_decimal_context", family="Overflow")))


def test_matrix_is_exact_and_unique():
    cases = _tuple_cases()
    ids = [case.id for case in cases]
    assert len(cases) == len(set(ids)) == 336
    assert Counter(str(case.values[0].relative_to(ROOT)) for case in cases)["backend/core/store.py"] == 223


def test_native_fallback_removal_is_detected():
    from backend.core.persist._common import _num
    codes = ("SYS-08-001-001",) * 9 + ("LINEBANK-08-830-010",)
    for selected, expected in [(codes, "LINEBANK-08-830-010"), (codes[:9], "SYS-08-012-001")]:
        with pytest.raises(Overflow) as caught:
            _num("1e1000000", _codes=selected)
        assert exception_error_id(caught.value) == expected
    assert expected != codes[-1]


@pytest.mark.parametrize("field,expected,action", [
    ("acctBal", "LINEBANK-08-830-010", "連線銀行 帳戶餘額摘要驗證"),
    ("txAmt", "LINEBANK-08-832-010", "連線銀行 存款交易金額驗證"),
    pytest.param("acctBal", "LINEBANK-08-831-010", "連線銀行 帳戶原幣餘額驗證", id="configured-precision6-acctBal1000-831"),
])
def test_linebank_natural_overflow_real_executor(field, expected, action, monkeypatch, capsys, record_property):
    import sys
    from backend.banks import linebank
    from backend.core import persist, store as store_module
    from backend.server import card_events, rules_repo, sync_jobs_repo, sync_runner as runner
    from tests.test_persistence_executor_provenance import MemoryStore

    assert linebank.LinebankCrawler.HISTORY_COVERAGE_REQUIRED is False
    crawler = object.__new__(linebank.LinebankCrawler)
    crawler.transaction_cursors = {}
    memory = MemoryStore()
    configured = expected == "LINEBANK-08-831-010"
    value = "1000" if configured else "1e1000000"
    family = InvalidOperation if configured else Overflow
    if field == "acctBal":
        data = {"api_responses": {"payables": [{"resp": {"content": {"dpstAcctList": [{"acctNbr": "fixture", field: value}]}}}]}}
    else:
        data = {"api_responses": {"transactions": [{"resp": {"content": {"acctNbr": "fixture", "txLst": [{"txDt": "20260101", field: value}]}}}]}}
    jobs = {71: dict(user_id=1, bank="linebank", account_id=None, batch_id=None, history_mode="full", status="queued", error_msg=None)}
    ingress = Counter()
    def run(**kwargs):
        assert kwargs == dict(login_url=linebank.BASE, headless=True)
        ingress["run"] += 1
        return {"data": data}
    def factory():
        ingress["factory"] += 1
        return crawler
    def bank_store(*args, **kwargs):
        assert args == ("linebank",) and kwargs == dict(user_id=1, source_account_id=None)
        ingress["store"] += 1
        return memory
    def claim(job_id):
        assert jobs[job_id]["status"] == "queued"
        jobs[job_id]["status"] = "running"
        ingress["claim"] += 1
        return True
    def failed(job_id, message):
        assert jobs[job_id]["status"] == "running"
        jobs[job_id].update(status="failed", error_msg=message)
        ingress["failed"] += 1
    monkeypatch.setattr(crawler, "run", run)
    monkeypatch.setattr(runner, "_load_crawler", lambda bank: (linebank, factory) if bank == "linebank" else pytest.fail("foreign bank"))
    monkeypatch.setattr(store_module, "BankStore", bank_store)
    monkeypatch.setattr(rules_repo, "list_rules", lambda **kwargs: [])
    monkeypatch.setattr(card_events, "snapshot_cards", lambda **kwargs: [])
    monkeypatch.setattr(sync_jobs_repo, "get", lambda job_id: dict(jobs[job_id]))
    monkeypatch.setattr(sync_jobs_repo, "claim_queued", claim)
    monkeypatch.setattr(sync_jobs_repo, "mark_failed", failed)
    monkeypatch.setattr(sync_jobs_repo, "mark_done", lambda *a: pytest.fail("must not mark done"))
    notifications = []
    monkeypatch.setattr(runner, "_send_sync_notification", lambda **kwargs: notifications.append(kwargs))
    targets = {fn.__code__: name for name, fn in dict(exec=runner._exec_sync, dispatch=runner._dispatch_crawler_and_persist, persist_collected=persist.persist_collected, public_persister=persist.persist_linebank, sink=runner.safe_failure_message).items()}
    calls = Counter()
    natives = []
    escaped = []
    prior_balances = []
    cursor_calls = []
    from backend.core.base import BankCrawler
    from backend.core.persist._common import _num
    def trace(frame, event, arg):
        if event == "call" and frame.f_code in targets:
            calls[targets[frame.f_code]] += 1
        if event == "call" and frame.f_code is BankCrawler.configure_transaction_cursor.__code__:
            cursor_calls.append(frame.f_locals["domain"])
        if event == "return" and frame.f_code is _num.__code__:
            prior_balances.append(arg)
        if event == "exception":
            if type(arg[1]) is family:
                natives.append(arg[1])
            if frame.f_code is runner._exec_sync.__code__:
                escaped.append(arg[1])
        return trace
    previous = sys.gettrace()
    try:
        sys.settrace(trace)
        with localcontext() as context:
            if configured:
                context.prec = 6
            context.clear_flags()
            before = (context.prec, context.Emin, context.Emax, dict(context.traps))
            assert context.traps[family] is True
            assert runner._exec_sync(71) is True
            assert before == (context.prec, context.Emin, context.Emax, dict(context.traps))
            flags = sorted(key.__name__ for key, active in context.flags.items() if active)
            assert flags == (["InvalidOperation"] if configured else ["Inexact", "Overflow", "Rounded"])
    finally:
        sys.settrace(previous)
    assert len(escaped) == 1 and type(escaped[0]) is runner._PersistenceError
    assert type(escaped[0].__cause__) is family
    assert all(error is escaped[0].__cause__ for error in natives) and natives
    assert escaped[0].__cause__.__cause__ is escaped[0].__cause__.__context__ is None
    assert escaped[0].__cause__.__suppress_context__ is False
    if configured:
        assert prior_balances == [1000] and type(prior_balances[0]) is int
    assert exception_error_id(escaped[0]) == expected
    message = f"[{expected}] {action}：{REASON} | sync_failed:Exception: phase=persistence"
    stored = runner.get_job(71)
    assert stored["status"] == "failed" and stored["error_msg"] == message
    assert memory.calls == dict(close=1)
    assert ingress == dict(factory=1, store=1, run=1, claim=1, failed=1)
    assert calls == dict(exec=1, dispatch=1, persist_collected=1, public_persister=1, sink=1)
    assert cursor_calls == ["twd_transactions", "account_transactions", "card_billed_transactions"]
    assert crawler.transaction_cursors == {name: {} for name in ("twd_transactions", "account_transactions", "card_billed_transactions")}
    output = capsys.readouterr()
    assert output.err == f"[sync] {message}; details withheld\n" and output.out == ""
    assert len(notifications) == 1 and notifications[0]["ok"] is False
    assert "1e1000000" not in stored["error_msg"]
    kind = "natural_json_configured_context_real_executor" if configured else "natural_json_real_executor"
    record_property("native_evidence", json.dumps(dict(id=expected, kind=kind, calls=calls, error_msg=stored["error_msg"], family=family.__name__, precision=before[0], Emin=before[1], Emax=before[2], traps={key.__name__: active for key, active in before[3].items()}, flags=flags, prior_balances=prior_balances, cursor_calls=cursor_calls, store_calls=memory.calls), ensure_ascii=False))


@pytest.mark.parametrize("method,base_columns", [
    ("twd_dedup_rows", ("id", "account_no", "txn_datetime", "expend", "income", "balance", "currency", "first_seen", "dedup_key")),
    ("card_dedup_rows", ("id", "card_no", "consume_date", "post_date", "description", "amount", "consume_amount", "first_seen")),
])
def test_dedup_tuple_contract_native_sql_unchanged(method, base_columns):
    from backend.core.twd_dedup_repair import USER_COLUMNS
    assert type(USER_COLUMNS) is tuple
    store = synthetic_store(None)
    events = []
    def execute(sql, params):
        events.append((sql, params))
        return SimpleNamespace(fetchall=lambda: [{"id": 71}])
    store.conn.execute = execute
    assert getattr(store, method)(USER_COLUMNS) == [{"id": 71}]
    table = "twd_transactions" if method == "twd_dedup_rows" else "card_billed_txns"
    suffix = " AND native_identity IS NULL" if method == "card_dedup_rows" else ""
    assert events == [(f"SELECT {', '.join(base_columns + USER_COLUMNS)} FROM {table} WHERE user_id = ?{suffix}", (1,))]


@pytest.mark.parametrize("helper", ["native_money", "_num", "_num_real", "_num_to_float"])
@pytest.mark.parametrize("profile,value,currency", [
    ("default", "1e1000000", "TWD"),
    ("default", "-1e1000000", "USD"),
    ("default", "bad", "USD"),
    ("default", "1.125", "USD"),
    ("inexact", "123.123456", "USD"),
    ("invalid", "123.123456", "USD"),
    ("underflow", "0.000001", "USD"),
    ("rounded", "123.123456", "USD"),
])
def test_original_current_private_decimal_context_replay(helper, profile, value, currency, record_property):
    import decimal
    import hashlib
    import subprocess
    import types
    from backend.core import money
    from backend.core.persist import _common

    commit = "16f2d4b130f44973d346b420f3d486ac839f71ac"
    frozen = {}
    hashes = {}
    for key, path in [("money", "backend/core/money.py"), ("common", "backend/core/persist/_common.py")]:
        source = subprocess.check_output(["git", "show", f"{commit}:{path}"], cwd=ROOT, text=True)
        module = types.ModuleType(f"frozen_monetary_{key}")
        exec(compile(source, f"<{commit}:{path}>", "exec"), module.__dict__)
        frozen[key] = module
        hashes[path] = hashlib.sha256(source.encode()).hexdigest()
    frozen["common"].native_money = frozen["money"].native_money
    original = getattr(frozen["money" if helper == "native_money" else "common"], helper)
    current = getattr(money if helper == "native_money" else _common, helper)

    selected_codes = next(case.values[2] for case in _tuple_cases() if case.values[1] == helper)

    def observe(function, *, contextual=False):
        with localcontext() as context:
            context.clear_flags()
            if profile in {"inexact", "rounded", "invalid"}:
                context.prec = 8
            if profile == "underflow":
                context.prec, context.Emin = 3, -2
            family = {"inexact": decimal.Inexact, "rounded": decimal.Rounded, "invalid": decimal.InvalidOperation, "underflow": decimal.Underflow}.get(profile)
            if family:
                context.traps[family] = True
            before = (context.prec, context.Emin, context.Emax, dict(context.traps))
            try:
                kwargs = dict(_codes=selected_codes) if contextual else {}
                result = function(value, currency, **kwargs) if helper == "native_money" else function(value, **kwargs)
                outcome = ("return", type(result).__name__, repr(result))
            except Exception as error:
                outcome = ("raise", type(error).__module__, type(error).__name__, str(error), type(error.__cause__).__name__, type(error.__context__).__name__, error.__suppress_context__)
            assert before == (context.prec, context.Emin, context.Emax, dict(context.traps))
            return outcome, sorted(key.__name__ for key, set_flag in context.flags.items() if set_flag)
    expected, actual = observe(original), observe(current)
    assert expected == actual == observe(current, contextual=True)
    if helper == "native_money" and profile == "default" and value == "1e1000000":
        # Meaningful mutation control: copy_abs changes both the escaping family
        # and sticky flags; do not make that financial-semantic change in source.
        mutated = types.ModuleType("frozen_copy_abs_negative_control")
        source = subprocess.check_output(["git", "show", f"{commit}:backend/core/money.py"], cwd=ROOT, text=True)
        assert source.count("abs(amount)") == 1
        exec(compile(source.replace("abs(amount)", "amount.copy_abs()"), "<copy-abs-negative-control>", "exec"), mutated.__dict__)
        assert observe(mutated.native_money) != expected
    record_property("decimal_replay", json.dumps(dict(helper=helper, profile=profile, value=value, currency=currency, baseline=commit, baseline_hashes=hashes, observation=actual)))
