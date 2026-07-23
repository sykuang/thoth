"""Phase 5.1 — Categorizer pure-function tests.

Phase 5.1 — categorizer 純函式測試。

驗證：
  - safe_match 正常 pattern 命中
  - safe_match 對 ReDoS pattern 撐住 timeout 回 False
  - categorize 按 rule list 第一個 match 勝（priority 排序由 caller 負責）
  - 空字串 / 無 rules → None
"""
from __future__ import annotations

import time


def test_safe_match_normal_pattern_returns_true():
    from backend.server.categorizer import safe_match
    assert safe_match(r"北捷|台鐵|高鐵", "北捷儲值") is True
    assert safe_match(r"麥當勞", "今天去麥當勞吃早餐") is True


def test_safe_match_no_match_returns_false():
    from backend.server.categorizer import safe_match
    assert safe_match(r"北捷", "早餐店") is False


def test_safe_match_empty_text_returns_false():
    from backend.server.categorizer import safe_match
    assert safe_match(r"任何", "") is False


def test_safe_match_invalid_regex_returns_false():
    """壞 regex 應 fallback 回 False，不該炸到外面。"""
    from backend.server.categorizer import safe_match
    assert safe_match(r"(unclosed", "anything") is False


def test_safe_match_redos_pattern_times_out():
    """ReDoS pattern: (a+)+$ 對 'aaaa…X' 會 catastrophic backtrack。
    safe_match 應在 timeout 內回 False，而非掛死 server。
    """
    from backend.server.categorizer import safe_match
    pattern = r"^(a+)+$"
    text = "a" * 32 + "X"  # 結尾 X 觸發 backtrack
    start = time.monotonic()
    result = safe_match(pattern, text, timeout=1)
    elapsed = time.monotonic() - start
    # 不論回 True/False，必須在 ~timeout 秒內結束（容忍 1s 抖動）
    assert elapsed < 3.0, f"safe_match 卡了 {elapsed:.2f}s，timeout 沒生效"
    assert result is False


# ============================================================
# Phase 8.3 (2026-06-15) — categorize_with_excluded
# ============================================================

from backend.server.categorizer import categorize_with_excluded


def test_categorize_with_excluded_returns_triple():
    rules = [
        {"pattern": "還款", "category": "還款", "subcategory": None,
         "priority": 90, "auto_excluded": 1},
    ]
    assert categorize_with_excluded("信用卡還款", rules) == ("還款", None, True)


def test_categorize_with_excluded_default_false_when_no_flag():
    rules = [
        {"pattern": "餐廳", "category": "飲食", "subcategory": "餐廳", "priority": 100},
    ]
    assert categorize_with_excluded("某餐廳消費", rules) == ("飲食", "餐廳", False)


def test_categorize_with_excluded_no_match():
    rules = [{"pattern": "nope", "category": "X", "auto_excluded": 1}]
    assert categorize_with_excluded("沒對到", rules) == (None, None, False)


def test_categorize_with_excluded_empty():
    assert categorize_with_excluded("", []) == (None, None, False)
    assert categorize_with_excluded(None, [{"pattern": "x", "category": "y"}]) == (None, None, False)
