"""CTBC collector raw schema validate + multi-account/m1~m5 POST body builder.

2026-06-22 (使用者指示, root cause fix): collector 是 raw 結構守門員. CTBC API
qu002/011 偶有 detail row 缺 actDtTm (prod job#152 NotNullViolation 實證).
Collector 該 skip + log, 不該往下游 persist 送結構不全的 dict.

2026-06-22 (使用者指示, 多帳號 + m1~m5 拓展): collector 主動 POST qu002/011 對
每個 (account, month) 組合, 取代「只抓 SPA auto-fire 的 m0」單帳號限制. 此檔
驗 `_build_qu002_011_post_body` 純函式邏輯; multi-account 主流程要 mock SPA/page
所以走 integration test 較合適.

姊妹層級 test: tests/test_persist_ctbc_twd_txns.py 鎖死 persist 層「raw 假設
完整」的 contract (拿到 detail 不再 sweep structural anomaly).
"""
from __future__ import annotations

import pytest

from backend.banks.ctbc import (
    _build_qu002_011_post_body,
    _filter_valid_ctbc_details,
)


# ============================================================
# _filter_valid_ctbc_details — schema validate at collector layer
# ============================================================

def test_empty_input_returns_empty():
    assert _filter_valid_ctbc_details([]) == ([], 0)
    assert _filter_valid_ctbc_details(None) == ([], 0)  # type: ignore[arg-type]


def test_all_valid_pass_through():
    raw = [
        {"actDtTm": "2026-06-02-14.53.14", "trnDtRaw": "20260602", "memo1": "ok"},
        {"actDtTm": "2026-06-08-00.45.27", "memo1": "second"},
    ]
    filtered, skipped = _filter_valid_ctbc_details(raw)
    assert len(filtered) == 2
    assert skipped == 0
    # 不變形 — 原 dict 直接 forward
    assert filtered[0] is raw[0]
    assert filtered[1] is raw[1]


def test_missing_actDtTm_skipped():
    """Prod job#152 scenario: detail row 缺 actDtTm 整筆 drop."""
    raw = [
        {"trnDtRaw": "20260602", "memo1": "no_actDtTm",
         "dbAmt": 1, "balanceAmt": "0"},
    ]
    filtered, skipped = _filter_valid_ctbc_details(raw)
    assert filtered == []
    assert skipped == 1


def test_empty_actDtTm_skipped():
    """空字串 / 只有 whitespace 也算缺."""
    raw = [
        {"actDtTm": "", "memo1": "empty"},
        {"actDtTm": "   ", "memo1": "whitespace"},
        {"actDtTm": None, "memo1": "none"},
    ]
    filtered, skipped = _filter_valid_ctbc_details(raw)
    assert filtered == []
    assert skipped == 3


def test_non_dict_entries_skipped():
    """detailList 偶有 non-dict noise (raw protocol drift), defensive skip."""
    raw = [
        {"actDtTm": "2026-06-02-14.53.14", "memo1": "good"},
        "not_a_dict",  # type: ignore[list-item]
        None,
        42,
        {"actDtTm": "2026-06-03-10.00.00", "memo1": "also_good"},
    ]
    filtered, skipped = _filter_valid_ctbc_details(raw)
    assert len(filtered) == 2
    assert skipped == 3
    assert filtered[0]["memo1"] == "good"
    assert filtered[1]["memo1"] == "also_good"


def test_mixed_valid_and_invalid_preserves_order():
    """同 batch 內 valid + invalid 混雜, valid 順序保留 (給 dedup_key occurrence 用)."""
    raw = [
        {"actDtTm": "2026-06-01-10.00.00", "memo1": "first"},
        {"trnDtRaw": "20260602", "memo1": "drop_me"},  # 缺 actDtTm
        {"actDtTm": "2026-06-03-12.00.00", "memo1": "third"},
        {"actDtTm": "", "memo1": "drop_me_too"},
        {"actDtTm": "2026-06-04-15.00.00", "memo1": "fifth"},
    ]
    filtered, skipped = _filter_valid_ctbc_details(raw)
    assert [d["memo1"] for d in filtered] == ["first", "third", "fifth"]
    assert skipped == 2


# ============================================================
# _build_qu002_011_post_body — multi-account + m1~m5 POST body builder
# ============================================================

# 模擬 SPA 自己 fire 的 template body shape (collector 從 ApiHit.req_body 撈)
SAMPLE_TEMPLATE_BODY = {
    "resource": "/twrbc-deposit/qu002/011",
    "rqData": {
        "accountId": "0000900000317011",  # info_list[0] 帳號
        "type": "m0",                     # 預設本月
        "ctry": "TW",                     # SPA 可能還帶其他欄位, 原樣保留
    },
    "encryptFlag": True,                  # SPA 可能帶, 我們不動
}


def test_build_body_swaps_account_and_type():
    """Happy path: rqData.accountId / type 換掉, 其他欄位原樣."""
    body = _build_qu002_011_post_body("0000900000297063", "m3", SAMPLE_TEMPLATE_BODY)
    assert body["rqData"]["accountId"] == "0000900000297063"
    assert body["rqData"]["type"] == "m3"
    # 原樣保留
    assert body["rqData"]["ctry"] == "TW"
    assert body["resource"] == "/twrbc-deposit/qu002/011"
    assert body["encryptFlag"] is True


def test_build_body_does_not_mutate_template():
    """純函式不可改 input — caller 拿 template 多次套不同 (acct, month)."""
    original_acct = SAMPLE_TEMPLATE_BODY["rqData"]["accountId"]
    original_type = SAMPLE_TEMPLATE_BODY["rqData"]["type"]
    _ = _build_qu002_011_post_body("NEW_ACCT", "m5", SAMPLE_TEMPLATE_BODY)
    assert SAMPLE_TEMPLATE_BODY["rqData"]["accountId"] == original_acct
    assert SAMPLE_TEMPLATE_BODY["rqData"]["type"] == original_type


def test_build_body_all_six_months_valid():
    for m in ("m0", "m1", "m2", "m3", "m4", "m5"):
        body = _build_qu002_011_post_body("A", m, SAMPLE_TEMPLATE_BODY)
        assert body["rqData"]["type"] == m


def test_build_body_rejects_invalid_month():
    with pytest.raises(ValueError, match="month_type"):
        _build_qu002_011_post_body("A", "m6", SAMPLE_TEMPLATE_BODY)
    with pytest.raises(ValueError, match="month_type"):
        _build_qu002_011_post_body("A", "", SAMPLE_TEMPLATE_BODY)
    with pytest.raises(ValueError, match="month_type"):
        _build_qu002_011_post_body("A", "all", SAMPLE_TEMPLATE_BODY)


def test_build_body_rejects_non_dict_template():
    with pytest.raises(ValueError, match="template_body"):
        _build_qu002_011_post_body("A", "m0", None)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="template_body"):
        _build_qu002_011_post_body("A", "m0", "not a dict")  # type: ignore[arg-type]


def test_build_body_handles_empty_rqData():
    """SPA 偶爾把 rqData 設 {} 或缺欄, 仍能套出可用 body."""
    template = {"resource": "/twrbc-deposit/qu002/011", "rqData": {}}
    body = _build_qu002_011_post_body("ACCT_X", "m2", template)
    assert body["rqData"] == {"accountId": "ACCT_X", "type": "m2"}


def test_build_body_handles_missing_rqData_key():
    """更極端: template 連 rqData key 都沒有 (defensive)."""
    template = {"resource": "/twrbc-deposit/qu002/011"}
    body = _build_qu002_011_post_body("ACCT_Y", "m4", template)
    assert body["rqData"] == {"accountId": "ACCT_Y", "type": "m4"}
    assert body["resource"] == "/twrbc-deposit/qu002/011"

