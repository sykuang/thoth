"""Offline regression: distinct CTBC inventory predicates need distinct diagnostics."""
from copy import deepcopy
from typing import Any

from backend.banks.ctbc import CtbcCrawler, _CTBC_EBMW_PATH
from backend.core.base import ApiHit, ResponseCollector, _collect_failure_diagnostics


def test_ctbc_inventory_rejections_remain_distinguishable():
    """The actual validator and actual safe sink must not collapse different causes."""
    valid: dict[str, Any] = dict(
        url=f"https://www.ctbcbank.com{_CTBC_EBMW_PATH}",
        method="POST", status=200,
        req_body={"resource": "/twrbc-deposit/qu001/010"},
        resp_json={"code": "0000", "rsData": {"twdAcctSummaryResponse": {
            "demDepBalSummaryResponse": {"infoList": [{"accountId": "synthetic-account"}]},
        }}},
    )
    cases: dict[str, dict[str, Any] | None] = {"missing_response": None}
    for name, changes in (
        ("wrong_method", {"method": "GET"}),
        ("bad_http_status", {"status": 503}),
        ("wrong_origin", {"url": "https://synthetic.invalid/"}),
        ("non_object_response", {"resp_json": []}),
        ("native_failure", {"resp_json": {"code": "synthetic-failure", "rsData": {}}}),
        ("missing_rsdata", {"resp_json": {"code": "0000"}}),
        ("missing_inventory_structure", {"resp_json": {"code": "0000", "rsData": {}}}),
    ):
        cases[name] = {**deepcopy(valid), **changes}
    for name, rows in (
        ("rows_not_list", {}), ("row_not_object", [None]),
        ("identity_not_string", [{"accountId": None}]),
        ("identity_empty", [{"accountId": ""}]),
        ("identity_not_canonical", [{"accountId": " synthetic-account"}]),
        ("identity_duplicate", [{"accountId": "synthetic-account"}] * 2),
    ):
        value = deepcopy(valid)
        value["resp_json"]["rsData"]["twdAcctSummaryResponse"]["demDepBalSummaryResponse"]["infoList"] = rows
        cases[name] = value
    crawler = object.__new__(CtbcCrawler)
    crawler.name = "ctbc"  # Match real constructor state without loading credentials.
    observed = {}
    for name, value in cases.items():
        collector = ResponseCollector()
        if value is not None:
            collector.hits.append(ApiHit(**value))
        try:
            CtbcCrawler._validated_twd_inventory(collector)
        except RuntimeError as error:
            diagnostic = _collect_failure_diagnostics(crawler, error)
            observed[name] = tuple(sorted(diagnostic.items()))
        else:
            raise AssertionError(f"synthetic negative accepted: {name}")
    assert len(set(observed.values())) == len(cases), observed
    assert "synthetic-account" not in repr(observed)
    assert "synthetic-failure" not in repr(observed)
