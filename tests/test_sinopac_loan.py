from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from urllib.parse import urlencode

import pytest

from backend.banks.sinopac import SinopacCrawler
from backend.core.base import ApiHit, ResponseCollector
from backend.core.persist import persist_sinopac
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
    }]


def test_sinopac_rejects_incomplete_loan_business_response(monkeypatch):
    monkeypatch.setattr("backend.banks.sinopac.SinopacCreds.load", lambda: object())
    collector = ResponseCollector("sinopac.com")

    with pytest.raises(RuntimeError, match="必要欄位"):
        SinopacCrawler()._collect_loans(_IncompleteLoanResponsePage(collector), collector)


def test_sinopac_rejects_failed_loan_api_response(monkeypatch):
    monkeypatch.setattr("backend.banks.sinopac.SinopacCreds.load", lambda: object())
    collector = ResponseCollector("sinopac.com")

    with pytest.raises(RuntimeError, match="HTTP"):
        SinopacCrawler()._collect_loans(_FailedLoanResponsePage(collector), collector)


def test_sinopac_rejects_delayed_response_from_previous_loan_account(monkeypatch):
    monkeypatch.setattr("backend.banks.sinopac.SinopacCreds.load", lambda: object())
    collector = ResponseCollector("sinopac.com")

    with pytest.raises(RuntimeError, match="對應 API 回應"):
        SinopacCrawler()._collect_loans(_DelayedPreviousAccountPage(collector), collector)


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
