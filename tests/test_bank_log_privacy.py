from __future__ import annotations

import inspect
import re

from backend.banks import ctbc, fubon, linebank, scsb, ubot


def test_touched_bank_logs_never_interpolate_raw_exceptions() -> None:
    raw_exception_log = re.compile(r"_log\(f[^\n]*\{e(?:!r)?(?:[:}])")

    for module in (ctbc, fubon, linebank, scsb, ubot):
        assert not raw_exception_log.search(inspect.getsource(module)), module.__name__


def test_touched_bank_logs_never_dump_raw_urls_or_endpoint_collections() -> None:
    forbidden = {
        ctbc: ("url={nav_result", "{card_resources}", "url={result.get", "resource: {data.get"),
        linebank: ("transaction url={txn_url}", "endpoint: {out['_all_endpoints']"),
        scsb: ("url={out['overview_url']}", "已跳到 {page.url}", "url={result['url']}",
               "最終 url: {data.get", "Overview url: {data.get", "endpoint: {data.get"),
        ubot: ("url={result.get", "endpoint: {data.get"),
    }

    for module, markers in forbidden.items():
        source = inspect.getsource(module)
        for marker in markers:
            assert marker not in source, (module.__name__, marker)


def test_touched_bank_logs_never_dump_account_amounts() -> None:
    forbidden = {
        ctbc: ("台幣存款餘額:", "額度={cc.get", "應繳={cc.get"),
        ubot: ("本期應繳={cs.get", "總額={dt.get"),
    }

    for module, markers in forbidden.items():
        source = inspect.getsource(module)
        for marker in markers:
            assert marker not in source, (module.__name__, marker)
