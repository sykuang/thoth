from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from urllib.parse import urlencode

import pytest

from backend.banks.sinopac import SinopacCrawler
from backend.core.base import ApiHit, ResponseCollector
from backend.core.persist.sinopac import _persist_sinopac as persist_sinopac
from backend.core.store import BankStore


FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "sinopac_loan_api_contract.json").read_text()
)


def test_sinopac_loan_fixture_uses_canonical_synthetic_values():
    account = FIXTURE["account_response"][0]["SubInfo"][0]
    detail = FIXTURE["info_response"][0]["SubInfo"][0]

    assert account["AcctValue"] == "0123456789012"
    assert "測試" in account["AcctText"]
    assert detail["LoanAcctCName"] == "測試貸款"


class _LoanPage:
    def __init__(self, collector: ResponseCollector):
        self.collector = collector
        self.urls: list[str] = []
        self.query_args: list[dict] = []

    def goto(self, url: str, **_kwargs) -> None:
        self.urls.append(url)
        self.collector.hits.append(ApiHit(
            url="https://mma.sinopac.com/ws/bank/loan/ws_loanaccount.ashx",
            method="POST",
            status=200,
            resp_json=deepcopy(FIXTURE["account_response"]),
        ))

    def go_back(self, **_kwargs):
        self.url = "https://mma.sinopac.com/mma/bank/easy_index_loan/mma_loandetail.aspx"

    frames = []
    main_frame = None

    def locator(self, selector):
        from types import SimpleNamespace
        key = {"#AcctValue": "account", "#AcctValueFormat": "formatted"}.get(selector)
        return SimpleNamespace(count=lambda: int(key is not None),
                               input_value=lambda: self.query_args[-1][key])

    def wait_for_timeout(self, _milliseconds: int) -> None:
        return None

    def evaluate(self, _script: str, args: dict):
        self.query_args.append(args)
        self.collector.hits.append(ApiHit(
            url="https://mma.sinopac.com/ws/bank/loan/ws_loaninfo.ashx",
            method="POST",
            status=200,
            req_body=urlencode({
                "AcctValue": args["account"],
                "AcctValueFormat": args["formatted"],
            }),
            resp_json=FIXTURE["info_response"],
        ))
        return True


class _IncompleteLoanResponsePage(_LoanPage):
    def evaluate(self, script: str, args: dict):
        result = super().evaluate(script, args)
        self.collector.hits[-1].resp_json = [{
            "SubInfo": [{}],
            "Header": "系統錯誤",
            "Message": "請稍後再試",
        }]
        return result


class _FailedLoanResponsePage(_LoanPage):
    def evaluate(self, script: str, args: dict):
        result = super().evaluate(script, args)
        self.collector.hits[-1].status = 500
        return result


class _DelayedPreviousAccountPage(_LoanPage):
    def goto(self, url: str, **kwargs) -> None:
        super().goto(url, **kwargs)
        self.collector.hits[-1].resp_json[0]["SubInfo"].append({
            "AcctText": "999999999999【測試分行】",
            "AcctValue": "999999999999",
            "AcctValueFormat": "999-999-999999",
        })

    def evaluate(self, _script: str, args: dict):
        self.query_args.append(args)
        request_account = (
            args["account"] if args["account"] == "0123456789012" else "0123456789012"
        )
        self.collector.hits.append(ApiHit(
            url="https://mma.sinopac.com/ws/bank/loan/ws_loaninfo.ashx",
            method="POST",
            status=200,
            req_body=urlencode({
                "AcctValue": request_account,
                "AcctValueFormat": "012-345-6789012",
            }),
            resp_json=FIXTURE["info_response"],
        ))
        return True


def test_sinopac_collects_each_loan_account_from_live_api_contract(monkeypatch):
    monkeypatch.setattr("backend.banks.sinopac.SinopacCreds.load", lambda: object())
    # This fixture tests the terms API; native repayment navigation has separate browser tests.
    monkeypatch.setattr(SinopacCrawler, "_collect_loan_repayments", lambda *args: _repayment())
    collector = ResponseCollector("sinopac.com")
    page = _LoanPage(collector)

    loan = SinopacCrawler()._collect_loans(page, collector)

    assert page.urls == [
        "https://mma.sinopac.com/mma/bank/easy_index_loan/mma_detail.aspx"
    ]
    assert page.query_args == [{
        "account": "0123456789012",
        "formatted": "012-345-6789012",
    }]
    assert loan["fetch_ok"] is True
    assert loan["details"] == [{
        "account": "0123456789012",
        "records": FIXTURE["info_response"][0]["SubInfo"],
        "repayments": [_repayment()],
    }]


def test_sinopac_rejects_incomplete_loan_business_response(monkeypatch):
    monkeypatch.setattr("backend.banks.sinopac.SinopacCreds.load", lambda: object())
    collector = ResponseCollector("sinopac.com")

    with pytest.raises(RuntimeError, match="^sinopac-loan-response-records$"):
        SinopacCrawler()._collect_loans(_IncompleteLoanResponsePage(collector), collector)


def test_sinopac_rejects_failed_loan_api_response(monkeypatch):
    monkeypatch.setattr("backend.banks.sinopac.SinopacCreds.load", lambda: object())
    collector = ResponseCollector("sinopac.com")

    with pytest.raises(RuntimeError, match="^sinopac-loan-response-http$"):
        SinopacCrawler()._collect_loans(_FailedLoanResponsePage(collector), collector)


def test_sinopac_rejects_delayed_response_from_previous_loan_account(monkeypatch):
    monkeypatch.setattr("backend.banks.sinopac.SinopacCreds.load", lambda: object())
    monkeypatch.setattr(SinopacCrawler, "_collect_loan_repayments", lambda *args: _repayment())
    collector = ResponseCollector("sinopac.com")

    with pytest.raises(RuntimeError, match="^sinopac-loan-response-missing$"):
        SinopacCrawler()._collect_loans(_DelayedPreviousAccountPage(collector), collector)


def _repayment():
    # Synthetic values; source field meanings come from the native loan renderer.
    values = ["2026/08/06", "2026/08/06", "10,300", "300", "890,000", "0",
              "", "", "", "", "10,000"]
    return {
        "sub_account": "99-0001", "currency": "TWD",
        "receipt": {"account": "0123456789012", "sub_account": "99-0001",
                    "currency": "TWD", "start": "2026-08-01", "end": "2026-09-07",
                    "status": "complete", "period": "native_default", "pages": 1, "rows": 1},
        "records": [dict(zip((f"DataValue{i}" for i in range(1, 12)), values, strict=True))],
    }


def _loan_with_repayments():
    return {"loan": {"fetch_ok": True, "details": [{
        "account": "0123456789012",
        "records": deepcopy(FIXTURE["info_response"][0]["SubInfo"]),
        "repayments": [_repayment()],
    }]}}


def test_native_repayment_persists_as_separate_durable_bank_fact(tmp_path, monkeypatch):
    from backend.core import bank_pg, store as store_mod
    monkeypatch.setattr(bank_pg, "DB_BACKEND", "sqlite")
    monkeypatch.setattr(store_mod, "DATA_ROOT", tmp_path)
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    store = BankStore("sinopac", user_id=1, source_account_id=7)
    try:
        delta = persist_sinopac(_loan_with_repayments(), store)
        assert delta.get("loan_repayments") == 1
        store.commit()
    finally:
        store.close()
    store = BankStore("sinopac", user_id=1, source_account_id=7)
    try:
        row = dict(store.conn.execute("SELECT * FROM loan_repayments").fetchone())
        assert {k: row[k] for k in (
            "user_id", "source_account_id", "account_no", "sub_account", "currency",
            "due_date", "paid_on", "principal", "interest", "penalty", "paid_total",
            "principal_balance", "status", "query_start", "query_end",
        )} == {
            "user_id": 1, "source_account_id": 7, "account_no": "0123456789012",
            "sub_account": "99-0001", "currency": "TWD", "due_date": "2026-08-06",
            "paid_on": "2026-08-06", "principal": "10000", "interest": "300",
            "penalty": "0", "paid_total": "10300", "principal_balance": "890000",
            "status": "", "query_start": "2026-08-01", "query_end": "2026-09-07",
        }
        assert json.loads(row["raw_json"])["DataValue7"] == ""
        assert store.conn.execute("SELECT COUNT(*) FROM twd_transactions").fetchone()[0] == 0
        assert store.conn.execute("SELECT raw_balance FROM accounts").fetchone()[0] == -900000
        assert store.conn.execute("SELECT loan_balance FROM balance_history").fetchone()[0] == 900000
    finally:
        store.close()


def test_cli_repayments_use_isolated_unscoped_source(tmp_path, monkeypatch):
    from backend.core import bank_pg, store as store_mod
    monkeypatch.setattr(bank_pg, "DB_BACKEND", "sqlite")
    monkeypatch.setattr(store_mod, "DATA_ROOT", tmp_path)
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    for source in (None, 7):
        store = BankStore("sinopac", user_id=1, source_account_id=source)
        try:
            persist_sinopac(_loan_with_repayments(), store)
            store.commit()
        finally:
            store.close()
    store = BankStore("sinopac", user_id=1)
    try:
        assert [r[0] for r in store.conn.execute(
            "SELECT source_account_id FROM loan_repayments ORDER BY source_account_id"
        ).fetchall()] == [0, 7]
    finally:
        store.close()


def test_repayment_window_bounds_payment_date_not_original_due_date():
    from backend.core.persist.sinopac import _parse_sinopac_repayment
    repayment = _repayment()
    repayment["records"][0]["DataValue1"] = "2026/07/06"
    rows = _parse_sinopac_repayment("0123456789012", repayment)
    assert rows[0]["due_date"] == "2026-07-06"
    assert rows[0]["paid_on"] == "2026-08-06"
    repayment["records"][0]["DataValue2"] = "2026/07/06"
    with pytest.raises(ValueError, match="SinoPac repayment"):
        _parse_sinopac_repayment("0123456789012", repayment)


@pytest.mark.parametrize("mutation", [
    lambda p: p["receipt"].update(status="explicit_empty"),
    lambda p: p["receipt"].update(period="full"),
    lambda p: p["receipt"].update(pages=True),
    lambda p: p["receipt"].update(rows=2),
    lambda p: p["receipt"].update(account="999999999999"),
    lambda p: p["receipt"].update(sub_account="99-0002"),
    lambda p: p["receipt"].update(currency="USD"),
    lambda p: p["receipt"].update(start="2026-8-01"),
    lambda p: p["receipt"].update(end="2026-02-30"),
    lambda p: p["records"][0].update(DataValue2="2026/07/06"),
    lambda p: p["records"][0].update(DataValue1="2026/02/30"),
    lambda p: p["records"][0].update(DataValue11="NaN"),
    lambda p: p["records"][0].update(DataValue3="10,301"),
    lambda p: p["records"][0].update(DataValue4="30,0"),
    lambda p: p["records"][0].update(DataValue6="-1"),
    lambda p: p["records"][0].update(DataValue7=None),
    lambda p: p.update(records=[]),
])
def test_invalid_repayment_never_mutates_bank_tables(tmp_path, monkeypatch, mutation):
    from backend.core import bank_pg, store as store_mod
    monkeypatch.setattr(bank_pg, "DB_BACKEND", "sqlite")
    monkeypatch.setattr(store_mod, "DATA_ROOT", tmp_path)
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    store = BankStore("sinopac", user_id=1, source_account_id=7)
    try:
        payload = _loan_with_repayments()
        mutation(payload["loan"]["details"][0]["repayments"][0])
        before = store.conn.total_changes
        with pytest.raises(ValueError, match="SinoPac repayment"):
            persist_sinopac(payload, store)
        assert store.conn.total_changes == before
    finally:
        store.close()


@pytest.mark.parametrize("mutation", [
    lambda loan: loan.update(fetch_ok=False),
    lambda loan: loan["details"][0].update(repayments=[]),
    lambda loan: loan["details"][0]["repayments"].append(_repayment()),
    lambda loan: loan["details"][0]["records"][0].update(Sub1_Sub2="99-0002"),
    lambda loan: loan["details"].append({"account": "999999999999", "records": deepcopy(FIXTURE["info_response"][0]["SubInfo"])}),
])
def test_repayment_inventory_must_cover_every_loan_subaccount(tmp_path, monkeypatch, mutation):
    from backend.core import bank_pg, store as store_mod
    monkeypatch.setattr(bank_pg, "DB_BACKEND", "sqlite")
    monkeypatch.setattr(store_mod, "DATA_ROOT", tmp_path)
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    store = BankStore("sinopac", user_id=1, source_account_id=7)
    try:
        payload = _loan_with_repayments()
        mutation(payload["loan"])
        before = store.conn.total_changes
        with pytest.raises(ValueError, match="SinoPac repayment"):
            persist_sinopac(payload, store)
        assert store.conn.total_changes == before
    finally:
        store.close()


def test_persist_sinopac_loan_updates_account_and_liability_snapshot(tmp_path, monkeypatch):
    from backend.core import store as store_mod

    monkeypatch.setattr(store_mod, "DATA_ROOT", tmp_path, raising=True)
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    store = BankStore("sinopac_loan_test", user_id=1)
    try:
        delta = persist_sinopac({
            "loan": {
                "details": [{
                    "account": "0123456789012",
                    "records": FIXTURE["info_response"][0]["SubInfo"],
                }],
                "fetch_ok": True,
            }
        }, store)

        account = store.conn.execute(
            "SELECT account_no, currency, type, product_type, raw_balance FROM accounts"
        ).fetchone()
        balance = store.conn.execute(
            "SELECT loan_balance FROM balance_history ORDER BY snapshot_date DESC LIMIT 1"
        ).fetchone()
        metric = store.conn.execute(
            "SELECT payload_json FROM daily_metrics WHERE category='loan'"
        ).fetchone()

        assert account is not None
        assert balance is not None
        assert metric is not None
        assert dict(account) == {
            "account_no": "0123456789012",
            "currency": "TWD",
            "type": "信用貸款",
            "product_type": "loan",
            "raw_balance": -900000.0,
        }
        assert balance["loan_balance"] == 900000
        assert json.loads(metric["payload_json"]) == {
            "records": [{
                "loan_kind": "信用貸款",
                "repayment_method": "平均攤還本息",
                "sub_account": "99-0001",
                "currency": "TWD",
                "begin_loan_date": "20260806",
                "loan_date": "20260806",
                "maturity_date": "20330806",
                "original_principal": 1000000.0,
                "principal_balance": 900000.0,
                "interest_rate": "3.00%",
            }],
        }
        assert delta["balance_days"] == 1
    finally:
        store.close()
