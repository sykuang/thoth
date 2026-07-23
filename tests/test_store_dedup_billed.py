"""驗證 BankStore.dedup_billed_stale_rows() — 清理 card_billed_txns 因 norm
規則演進造成的「同消費多 row」歷史包袱。

背景：dedup_key 包含 consume_amount，norm 改變（None vs 358.0）會讓
同一筆消費產生不同 dedup_key → ON CONFLICT 不會攔下 → 雙列。

策略：對 (card_no, consume_date, amount, description) 四欄全等的群組，
保留 first_seen 最舊那筆（最早爬到的，最可信），DELETE 其他。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from backend.core import store as store_mod
from backend.core.store import BankStore


@pytest.fixture
def tmp_store(tmp_path: Path, monkeypatch) -> BankStore:
    monkeypatch.setattr(store_mod, "DATA_ROOT", tmp_path)
    return BankStore("testbank")


def _insert_raw_billed(conn, **kwargs) -> int:
    """繞過 upsert_card_billed 直接寫一筆（測試 stale row 場景）。"""
    cur = conn.execute(
        """INSERT INTO card_billed_txns
           (card_no, bill_date, currency, consume_date, post_date, description,
            amount, consume_country, consume_currency, consume_amount, first_seen,
            dedup_key, category)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (kwargs["card_no"], kwargs.get("bill_date"), kwargs.get("currency", "TWD"),
         kwargs["consume_date"], kwargs.get("post_date") or kwargs["consume_date"],
         kwargs["description"], kwargs["amount"], None,
         kwargs.get("consume_currency"), kwargs.get("consume_amount"),
         kwargs["first_seen"], kwargs["dedup_key"], None),
    )
    return cur.lastrowid


# === 1. 經典 case：esun 純台幣 norm 演進產生雙列 → 兩者資訊量相同則保留最舊 ===
def test_dedup_keeps_oldest_when_info_equal(tmp_store: BankStore) -> None:
    """模擬 esun ****7032 / 中油 1727 / 2026-06-08 因 norm 改變產生 2 筆 row。
    兩筆 consume_currency 都是 None，consume_amount 一個 1727.0 一個 None，
    分數 1 vs 0 → 留分數高（舊那筆 consume_amount=1727.0）。"""
    # 舊 row (consume_amount=1727.0) 分數 1
    old_id = _insert_raw_billed(
        tmp_store.conn, card_no="****7032", consume_date="2026-06-08",
        description="中油", amount=1727, consume_amount=1727.0,
        first_seen="2026-06-13T15:33:58", dedup_key="key_v1",
    )
    # 新 row (consume_amount=None) 分數 0
    _insert_raw_billed(
        tmp_store.conn, card_no="****7032", consume_date="2026-06-08",
        description="中油", amount=1727, consume_amount=None,
        first_seen="2026-06-13T15:34:27", dedup_key="key_v2",
    )
    tmp_store.conn.commit()

    n = tmp_store.dedup_billed_stale_rows()
    assert n == 1, f"應只清 1 筆，實際清 {n}"

    rows = tmp_store.conn.execute(
        "SELECT id, consume_amount FROM card_billed_txns"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["id"] == old_id, "兩者資訊量比較：舊那筆 consume_amount=1727.0 較完整"


# === 2. 三筆 row → 清 2 留 1 ===
def test_dedup_three_rows_keep_one(tmp_store: BankStore) -> None:
    ids = []
    for i, fs in enumerate(["2026-06-10T01", "2026-06-11T02", "2026-06-12T03"]):
        ids.append(_insert_raw_billed(
            tmp_store.conn, card_no="****7016", consume_date="2026-06-08",
            description="蝦皮", amount=500, consume_amount=None,
            first_seen=fs, dedup_key=f"key_v{i}",
        ))
    tmp_store.conn.commit()

    n = tmp_store.dedup_billed_stale_rows()
    assert n == 2

    rows = tmp_store.conn.execute("SELECT id FROM card_billed_txns").fetchall()
    assert len(rows) == 1
    assert rows[0]["id"] == ids[0], "保留最早寫入那筆"


# === 3. 同卡同日同金額不同 desc → 不合併（合理 case 7-11 跨店買 100） ===
def test_dedup_different_desc_keeps_both(tmp_store: BankStore) -> None:
    _insert_raw_billed(
        tmp_store.conn, card_no="****7016", consume_date="2026-06-08",
        description="7-11 內湖店", amount=100,
        first_seen="2026-06-10T01", dedup_key="k1",
    )
    _insert_raw_billed(
        tmp_store.conn, card_no="****7016", consume_date="2026-06-08",
        description="7-11 信義店", amount=100,
        first_seen="2026-06-10T02", dedup_key="k2",
    )
    tmp_store.conn.commit()

    n = tmp_store.dedup_billed_stale_rows()
    assert n == 0, "不同 desc 視為不同筆消費，不清"
    assert tmp_store.conn.execute(
        "SELECT COUNT(*) FROM card_billed_txns").fetchone()[0] == 2


# === 4. 不同卡 → 不合併 ===
def test_dedup_different_card_keeps_both(tmp_store: BankStore) -> None:
    _insert_raw_billed(
        tmp_store.conn, card_no="****7015", consume_date="2026-06-08",
        description="中油", amount=1727,
        first_seen="2026-06-10T01", dedup_key="k1",
    )
    _insert_raw_billed(
        tmp_store.conn, card_no="****7026", consume_date="2026-06-08",
        description="中油", amount=1727,
        first_seen="2026-06-10T02", dedup_key="k2",
    )
    tmp_store.conn.commit()

    assert tmp_store.dedup_billed_stale_rows() == 0
    assert tmp_store.conn.execute(
        "SELECT COUNT(*) FROM card_billed_txns").fetchone()[0] == 2


# === 5. NULL 欄位 → 保守不清 ===
def test_dedup_null_fields_kept(tmp_store: BankStore) -> None:
    """任一關鍵欄 (card_no/consume_date/amount/description) 為 NULL 都不敢清。"""
    _insert_raw_billed(
        tmp_store.conn, card_no=None, consume_date="2026-06-08",
        description="未知卡", amount=100,
        first_seen="2026-06-10T01", dedup_key="k1",
    )
    _insert_raw_billed(
        tmp_store.conn, card_no=None, consume_date="2026-06-08",
        description="未知卡", amount=100,
        first_seen="2026-06-10T02", dedup_key="k2",
    )
    tmp_store.conn.commit()

    n = tmp_store.dedup_billed_stale_rows()
    assert n == 0, "card_no=NULL 不參與合併（保守策略避免 wildcard 誤殺）"
    assert tmp_store.conn.execute(
        "SELECT COUNT(*) FROM card_billed_txns").fetchone()[0] == 2


# === 6. 空表 → safe ===
def test_dedup_empty_table(tmp_store: BankStore) -> None:
    assert tmp_store.dedup_billed_stale_rows() == 0


# === 7. 全唯一 → 0 清 ===
def test_dedup_all_unique_no_change(tmp_store: BankStore) -> None:
    _insert_raw_billed(
        tmp_store.conn, card_no="****7016", consume_date="2026-06-08",
        description="中油", amount=1727,
        first_seen="2026-06-10T01", dedup_key="k1",
    )
    _insert_raw_billed(
        tmp_store.conn, card_no="****7016", consume_date="2026-06-09",
        description="優步", amount=358,
        first_seen="2026-06-10T02", dedup_key="k2",
    )
    tmp_store.conn.commit()

    assert tmp_store.dedup_billed_stale_rows() == 0
    assert tmp_store.conn.execute(
        "SELECT COUNT(*) FROM card_billed_txns").fetchone()[0] == 2


# === 8. 關鍵 case：CTBC EUR 外幣 — 留資訊量高那筆即使 first_seen 較新 ===
def test_dedup_keeps_richer_row_even_if_newer(tmp_store: BankStore) -> None:
    """模擬 ctbc ****7036 / GetYourGuide Tickets 7262 / 2026-05-12。
    舊 row consume_currency=None / consume_amount=None  → 分數 0
    新 row consume_currency='EUR' / consume_amount=196.2 → 分數 2
    必須留新那筆（資訊完整），不能留最舊（會丟外幣金額）。"""
    # 舊 row 分數 0
    _insert_raw_billed(
        tmp_store.conn, card_no="****7036", consume_date="2026-05-12",
        description="GetYourGuide Tickets", amount=7262,
        consume_currency=None, consume_amount=None,
        first_seen="2026-06-13T14:47:13", dedup_key="key_v1",
    )
    # 新 row 分數 2
    new_id = _insert_raw_billed(
        tmp_store.conn, card_no="****7036", consume_date="2026-05-12",
        description="GetYourGuide Tickets", amount=7262,
        consume_currency="EUR", consume_amount=196.2,
        first_seen="2026-06-13T14:47:54", dedup_key="key_v2",
    )
    tmp_store.conn.commit()

    n = tmp_store.dedup_billed_stale_rows()
    assert n == 1

    rows = tmp_store.conn.execute(
        "SELECT id, consume_currency, consume_amount FROM card_billed_txns"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["id"] == new_id, "必須留新那筆（資訊完整 EUR/196.2）"
    assert rows[0]["consume_currency"] == "EUR"
    assert rows[0]["consume_amount"] == 196.2


# === 9. 平手 case：三筆都同分 → 留最舊 ===
def test_dedup_tie_breaker_oldest(tmp_store: BankStore) -> None:
    """三筆都 consume_*=None (分數 0)，平手時看 first_seen 取最舊。"""
    ids = []
    for fs in ["2026-06-12T03", "2026-06-10T01", "2026-06-11T02"]:
        ids.append(_insert_raw_billed(
            tmp_store.conn, card_no="****7016", consume_date="2026-06-08",
            description="蝦皮", amount=500, consume_amount=None, consume_currency=None,
            first_seen=fs, dedup_key=f"k_{fs}",
        ))
    tmp_store.conn.commit()

    n = tmp_store.dedup_billed_stale_rows()
    assert n == 2

    rows = tmp_store.conn.execute(
        "SELECT id, first_seen FROM card_billed_txns").fetchall()
    assert len(rows) == 1
    assert rows[0]["first_seen"] == "2026-06-10T01", "平手取 first_seen 最舊"


# === 10. 關鍵 case：HSBC 分期付款不同 post_date → 不可合併（同 desc 同 0 元但是 6 期分期） ===
def test_dedup_keeps_installment_payments_with_different_post_date(tmp_store: BankStore) -> None:
    """HSBC 雄獅旅行社 6 期分期 0 元的「剩餘本金」row：
    consume_date 都同 2026-01-27，但 post_date 每期不同 (02-02 / 03-02 / 04-02 / 05-02)。
    雖然 (card_no, consume_date, amount, desc) 四欄全等，但 post_date 不同
    → 是真的多期分期記錄，不可合併。"""
    for post_date in ["2026-02-02", "2026-03-02", "2026-04-02", "2026-05-02"]:
        _insert_raw_billed(
            tmp_store.conn, card_no="****7034", consume_date="2026-01-27",
            post_date=post_date, description="雄獅旅行社ＴＡ剩餘本金", amount=0,
            consume_currency="TWD", consume_amount=None,
            first_seen="2026-06-10T04:27:39", dedup_key=f"k_{post_date}",
        )
    tmp_store.conn.commit()

    n = tmp_store.dedup_billed_stale_rows()
    assert n == 0, "不同 post_date 視為不同期分期，不可合併"
    assert tmp_store.conn.execute(
        "SELECT COUNT(*) FROM card_billed_txns").fetchone()[0] == 4


# === 11. post_date 也相同才合併 ===
def test_dedup_combines_when_post_date_also_equal(tmp_store: BankStore) -> None:
    """5 欄全等才合併：同 post_date 的兩筆是真 stale。"""
    _insert_raw_billed(
        tmp_store.conn, card_no="****7034", consume_date="2026-01-27",
        post_date="2026-02-02", description="雄獅", amount=0,
        consume_currency=None, consume_amount=None,
        first_seen="2026-06-10T01", dedup_key="k1",
    )
    _insert_raw_billed(
        tmp_store.conn, card_no="****7034", consume_date="2026-01-27",
        post_date="2026-02-02", description="雄獅", amount=0,
        consume_currency="TWD", consume_amount=None,  # 資訊較完整
        first_seen="2026-06-10T02", dedup_key="k2",
    )
    tmp_store.conn.commit()

    n = tmp_store.dedup_billed_stale_rows()
    assert n == 1
    rows = tmp_store.conn.execute(
        "SELECT consume_currency FROM card_billed_txns").fetchall()
    assert len(rows) == 1
    assert rows[0]["consume_currency"] == "TWD", "留資訊較完整那筆"
