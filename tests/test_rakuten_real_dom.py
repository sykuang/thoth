"""樂天真實 DOM 回歸測試。

fixture 來自 2026-07-28 真實帳號 probe（帳號已 redact）。存在理由：先前整套
crawler 只有臣妾手寫的合成 fixture，因此四個假設全錯卻 CI 全綠——
`page.goto(TWD_URL)` 掉 session、`combo-item` 是臆造 tag、`body.length>=300`
魔術門檻、選項容器實為 `a.dropdown-item`。這支測試綁死真實 DOM 形狀。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from backend.banks.rakuten import _account_number, _month_labels, _six_month_labels

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "rakuten_twd_real_dom.json").read_text(encoding="utf-8"),
)


def _option_texts(html: str) -> list[str]:
    """模擬 `.dropdown-menu a.dropdown-item` 這條 selector 取到的文字。"""
    menu = re.search(r'<div class="dropdown-menu".*?</div>', html, re.S)
    assert menu, "真實 DOM 必須有 .dropdown-menu 容器"
    return [
        re.sub(r"<[^>]+>", "", m).strip()
        for m in re.findall(r'<a class="dropdown-item".*?</a>', menu.group(0), re.S)
    ]


def test_dropdown_options_are_anchor_dropdown_item_not_combo_item() -> None:
    html = FIXTURE["month_dropdown_html"]
    # combo-item 只是第一個選項的 attribute value，不是 tag——舊 selector 據此臆造而全數落空
    assert "combo-item" not in re.sub(r'type="combo-item"', "", html)
    assert _option_texts(html) == FIXTURE["month_options"]


def test_real_month_labels_parse_to_exactly_six() -> None:
    labels = [FIXTURE["month_selected"], *FIXTURE["month_options"]]
    assert _six_month_labels(labels) == _month_labels(labels)
    assert len(_six_month_labels(labels)) == 6


def test_real_account_label_yields_account_number() -> None:
    expected = _account_number(FIXTURE["account_options"][-1])
    assert expected is not None
    assert _account_number(FIXTURE["account_selected"]) == expected


def test_real_table_head_matches_row_column_order() -> None:
    """`_row_from_dom` 的 index 假設必須對上真實表頭順序。"""
    heads = [
        re.sub(r"<[^>]+>", " ", m).split()
        for m in re.findall(r"<th[^>]*>(.*?)</th>", FIXTURE["table_head_html"], re.S)
    ]
    flat = [" ".join(h) for h in heads]
    assert flat[0] == "交易時間"
    assert flat[1] == "交易說明 對方帳號或暱稱"  # cells[1] 是兩行合併
    assert flat[2] == "轉入"   # cells[2] income
    assert flat[3] == "轉出"   # cells[3] expend
    assert flat[4] == "帳戶餘額"  # cells[4] balance
    assert flat[5] == "備註"     # cells[5] memo
