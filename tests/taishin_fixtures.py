from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime
from zoneinfo import ZoneInfo

from backend.banks.taishin import TaishinCrawler


TAISHIN_TEST_ACCOUNT = "01234567890123"


def with_taishin_history(
    data: dict,
    *,
    account: str = TAISHIN_TEST_ACCOUNT,
    as_of: date | None = None,
) -> dict:
    as_of = as_of or datetime.now(ZoneInfo("Asia/Taipei")).date()
    start = TaishinCrawler._subtract_months(as_of, 12)
    payload = deepcopy(data)
    receipt = {
        "identity": account,
        "start": start.isoformat(),
        "end": as_of.isoformat(),
        "status": "explicit_empty",
        "pages": 1,
    }
    payload["twd_txn_results"] = [{
        **receipt,
        "period": "12_months",
        "rows": [],
        "snapshot": {
            "evidence_fresh": True,
            "mutation_count": 1,
            "quiet_ms": 2500,
            "route_bound": True,
            "result_scope_bound": True,
            "selected_identity": account,
            "selected_period": "12_months",
            "selected_sort": "forward",
            "busy_count": 0,
            "dialog_count": 0,
            "error_count": 0,
            "table_count": 0,
            "headers": [],
            "rows": [],
            "total_count": 0,
            "more_button_count": 0,
            "no_more_count": 0,
            "pager_count": 0,
            "no_result_count": 1,
        },
        "api_row_count": 0,
        "api_rows": [],
        "transport": {
            "url": "https://my.taishinbank.com.tw/TIBNetBank/svc/web1/rb0102/query",
            "method": "POST",
            "status": 200,
            "content_type": "application/json",
            "redirected": False,
            "main_frame_request": False,
            "request_frame_url": (
                "https://my.taishinbank.com.tw/TIBNetBank/svc/rwd/"
                "index.html#/RB0102/0100"
            ),
            "request_body": {
                "account": account,
                "start": start.strftime("%Y%m%d"),
                "end": as_of.strftime("%Y%m%d"),
            },
            "response_result": "NORMAL",
            "body_size": 100,
            "request_sequence": 1,
        },
        "binding_digest": "4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945",
        "request_count": 1,
        "response_count": 1,
    }]
    payload["history_coverage"] = {
        "version": 1,
        "mode": "full",
        "domains": [{
            "domain": "twd_transactions",
            "expected": [{
                "identity": account,
                "start": start.isoformat(),
                "end": as_of.isoformat(),
            }],
            "windows": [receipt],
        }],
    }
    return payload
