"""驗證 ESun 信用卡額度查詢 (card_quota) parser + persist 路徑。

2026-06-18 B 路線：used_credit 改為直接抓「信用卡 > 信用卡帳單/明細 > 信用卡額度查詢」
頁的原生欄位，因為原本 sum 已入帳的推算邏輯會少算未入帳 + 上期未繳。

第三輪修正 (2026-06-18 vision 確認):
玉山頁面實際表格結構:
  信用狀態 | 已用額度 | 可用餘額
  歸戶     |   -807   | 400,807    ← 可能是負數 (溢繳)
parser 從「歸戶」row 抓兩個數字, credit_limit = used + available 算出來。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from backend.banks.esun import EsunCrawler
from backend.core.persist import persist_esun
from backend.core.store import BankStore


# ---------- parser unit tests ----------

def test_parse_card_quota_real_yushan_text_with_negative_used():
    """使用者 2026-06-18 真實截圖場景: used=-807, available=400,807, limit=400,000。

    textContent 是 widget render 後的 main DOM, 帶有 ESun 表格 header 和 row。
    """
    text = """信用卡額度查詢
查詢時間：2026/06/18 18:08:16
信用狀態
已用額度
可用餘額
歸戶
-807
400,807
指定額度
已用額度
"""
    out = EsunCrawler._parse_card_quota(text)
    assert out["used_credit_twd"] == -807
    assert out["available_credit_twd"] == 400807
    assert out["credit_limit_twd"] == 400000  # used + available


def test_parse_card_quota_normal_positive_used():
    """正常狀況: 已用 87,654 / 可用 312,346 / 額度 400,000。"""
    text = """信用狀態
已用額度
可用餘額
歸戶
87,654
312,346
"""
    out = EsunCrawler._parse_card_quota(text)
    assert out["used_credit_twd"] == 87654
    assert out["available_credit_twd"] == 312346
    assert out["credit_limit_twd"] == 400000


def test_parse_card_quota_zero_used_just_after_payment():
    """使用者剛繳完: used=0 / available=400,000 / limit=400,000。
    used=0 不能誤判成「沒抓到」走 fallback。"""
    text = """歸戶
0
400,000
"""
    out = EsunCrawler._parse_card_quota(text)
    assert out["used_credit_twd"] == 0
    assert out["available_credit_twd"] == 400000
    assert out["credit_limit_twd"] == 400000


def test_parse_card_quota_empty_text_keeps_sample_only():
    """頁面沒抓到任何 keyword → 不命中, 但 raw_text_sample 還在好讓使用者 audit。"""
    text = "Language\nENGLISH\n登出\n"
    out = EsunCrawler._parse_card_quota(text)
    assert "credit_limit_twd" not in out
    assert "used_credit_twd" not in out
    assert "raw_text_sample" in out


def test_parse_card_quota_inline_tabs_no_newlines():
    """如果玉山改用 \\t 或空白分隔, 不靠換行也要 match。"""
    text = "信用狀態 已用額度 可用餘額 歸戶 -807 400,807"
    out = EsunCrawler._parse_card_quota(text)
    assert out["used_credit_twd"] == -807
    assert out["available_credit_twd"] == 400807


# ---------- persist integration tests ----------

@pytest.fixture
def store(tmp_path: Path, monkeypatch):
    from backend.core import store as store_mod
    monkeypatch.setattr(store_mod, "DATA_ROOT", tmp_path, raising=True)
    s = BankStore("esun_quota_test")
    yield s
    s.close()


def _base_data_with_card():
    """共用 fixture: 1 張卡 + 1 筆已入帳 2,085 交易。"""
    return {
        "accounts": [],
        "card_summary": {
            "credit_limit_twd": 400000,
            "payment_due_date_roc": "115/06/29",
        },
        "card_transactions": [
            {
                "consume_date": "2026/06/08",
                "merchant": "優步－大埔鐵板燒",
                "consume_currency": "TWD",
                "consume_amount": 2085.0,
                "billed_currency": "TWD",
                "billed_amount": 2085.0,
                "card_no": "9064-XXXX-XXXX-7032",
                "card_last4": "7032",
                "status": "已入帳",
            },
        ],
    }


def test_persist_uses_raw_quota_used_when_present(store):
    """B 路線主場景: card_quota.used_credit_twd=87654 → cards.used_credit=87654
    (不是 sum 已入帳的 2085)。"""
    data = _base_data_with_card()
    data["card_quota"] = {
        "credit_limit_twd": 400000,
        "used_credit_twd": 87654,
        "available_credit_twd": 312346,
    }
    persist_esun(data, store)
    row = store.conn.execute(
        "SELECT card_no, credit_limit, used_credit FROM cards"
    ).fetchone()
    assert row["used_credit"] == 87654.0
    assert row["credit_limit"] == 400000.0


def test_persist_writes_negative_used_credit_honestly(store):
    """使用者溢繳場景: quota.used=-807 → cards.used_credit=-807 (忠實寫入，禁強制 max(0))。

    顯示誠實鐵令: frontend 自己 handle 負數 (顯示「溢繳」)。
    """
    data = _base_data_with_card()
    data["card_quota"] = {
        "credit_limit_twd": 400000,
        "used_credit_twd": -807,
        "available_credit_twd": 400807,
    }
    persist_esun(data, store)
    row = store.conn.execute("SELECT used_credit FROM cards").fetchone()
    assert row["used_credit"] == -807.0


def test_persist_zero_used_not_misread_as_missing_quota(store):
    """used=0 (剛繳完) 不能被 `if quota_used` 誤判走 fallback, 必須用 `is not None`。"""
    data = _base_data_with_card()
    data["card_quota"] = {
        "credit_limit_twd": 400000,
        "used_credit_twd": 0,  # ← 0 是 falsy, 但是合法值
        "available_credit_twd": 400000,
    }
    persist_esun(data, store)
    row = store.conn.execute("SELECT used_credit FROM cards").fetchone()
    # ❌ 退化 bug: 走 fallback 會寫成 2085 (sum 已入帳)
    # ✅ 正確: 寫 0.0
    assert row["used_credit"] == 0.0


def test_persist_quota_limit_overrides_card_summary_limit(store):
    """quota 頁的 credit_limit 比 card_summary (帳單頁) 新, 應該 override。"""
    data = _base_data_with_card()
    data["card_summary"]["credit_limit_twd"] = 300000  # 舊值
    data["card_quota"] = {
        "credit_limit_twd": 500000,  # 新值, 使用者臨櫃調高
        "used_credit_twd": 50000,
    }
    persist_esun(data, store)
    row = store.conn.execute(
        "SELECT credit_limit, used_credit FROM cards"
    ).fetchone()
    assert row["credit_limit"] == 500000.0
    assert row["used_credit"] == 50000.0


def test_persist_falls_back_to_sum_billed_when_no_quota(store):
    """quota 完全沒抓到 → 退回舊路徑 sum(已入帳) = 2085 (確保不退化)。"""
    data = _base_data_with_card()
    # no card_quota key
    persist_esun(data, store)
    row = store.conn.execute("SELECT used_credit FROM cards").fetchone()
    assert row["used_credit"] == 2085.0


def test_persist_writes_card_quota_daily_metric(store):
    """card_quota 入 daily_metrics 給未來 debug / audit。"""
    data = _base_data_with_card()
    data["card_quota"] = {
        "credit_limit_twd": 400000,
        "used_credit_twd": 87654,
        "raw_text_sample": "歸戶信用額度...",
    }
    persist_esun(data, store)
    row = store.conn.execute(
        "SELECT payload_json FROM daily_metrics WHERE category='esun_card_quota'"
    ).fetchone()
    assert row is not None
    import json
    payload = json.loads(row["payload_json"])
    assert payload["used_credit_twd"] == 87654
