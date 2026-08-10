from __future__ import annotations

import ast
import inspect
import textwrap

from backend.banks.esun import EsunCrawler


class _FakeFrame:
    url = "https://ebank.example/card-query"

    def evaluate(self, _script: str, _period: str) -> dict:
        return {
            "monthClicked": True,
            "periodSelected": False,
            "queryClicked": True,
            "log": ["clicked text only", "clicked 查詢"],
        }


class _FakePage:
    frames = [_FakeFrame()]

    def screenshot(self, **_kwargs) -> None:
        pass


def test_esun_card_period_submit_rejects_unselected_radio(tmp_path) -> None:
    crawler = EsunCrawler.__new__(EsunCrawler)

    result = crawler._submit_card_txn_query(_FakePage(), tmp_path, "最近二個月")

    assert result["strategy"] is None


def test_each_esun_card_period_reloads_query_form_before_submit() -> None:
    """結果 widget 會取代表單；後續期間必須先重新開查詢表單。"""
    tree = ast.parse(textwrap.dedent(inspect.getsource(EsunCrawler.collect)))
    def iterated_tuple(node: ast.For) -> ast.Tuple | None:
        value = node.iter
        if (
            isinstance(value, ast.Call)
            and isinstance(value.func, ast.Name)
            and value.func.id == "enumerate"
            and value.args
        ):
            value = value.args[0]
        return value if isinstance(value, ast.Tuple) else None

    period_loop = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.For)
        and (items := iterated_tuple(node)) is not None
        and [elt.value for elt in items.elts if isinstance(elt, ast.Constant)]
        == ["最近一個月", "最近二個月"]
    )
    calls = [node for node in ast.walk(period_loop) if isinstance(node, ast.Call)]

    def attr_name(call: ast.Call) -> str | None:
        return call.func.attr if isinstance(call.func, ast.Attribute) else None

    reload_lines = [
        call.lineno
        for call in calls
        if attr_name(call) == "_navigate_menu"
        and any(
            isinstance(arg, ast.Constant) and arg.value == "信用卡消費明細查詢"
            for arg in call.args
        )
    ]
    submit_lines = [call.lineno for call in calls if attr_name(call) == "_submit_card_txn_query"]

    assert reload_lines, "每個查詢期間開始前都必須重新載入被結果頁取代的表單"
    assert submit_lines
    assert min(reload_lines) < min(submit_lines)
