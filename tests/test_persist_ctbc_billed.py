"""驗證 persist_ctbc 處理三段信用卡 API 的 mechanism。

CTBC 真實 endpoints (2026-06-13 live login 攔到):
  /twrbc-card/qu041/010 = 即時消費 (pending, allItems)
  /twrbc-card/qu002/010 = 帳單明細 (billed, billData.TWD.{月}.bills[]) + cardDataList
  /twrbc-card/qu006/011 = 未出帳單 (unbilled, allItems)

case 1: cards 從 qu002 cardDataList 抓出 4 張
case 2: billed 從 qu002 billData 抓出有外幣 occCurCode
case 3: pending 從 qu041 allItems 抓出（外幣放 desc tail workaround）
"""
from __future__ import annotations

from pathlib import Path

import pytest

from backend.core.persist import persist_ctbc
from backend.core.store import BankStore


@pytest.fixture
def store(tmp_path: Path, monkeypatch):
    # 2026-06-14: monkeypatch.setenv 比 setattr 安全（避免 leak 到真實 DB）
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    s = BankStore("ctbc_test")
    yield s
    s.close()


FIXTURE_BILLED = {
    "card_api_dump": {
        "/twrbc-card/qu002/010": {
            "cardDataList": [
                {"cardNoSuffixFour": "7036_1", "cardName": "中華航空聯名卡",
                 "positiveOrAttached": "正卡", "cardNo": "9000-56**-****-7036"},
                {"cardNoSuffixFour": "7051_0", "cardName": "中油聯名卡",
                 "positiveOrAttached": "正卡", "cardNo": "9000-55**-****-7051"},
            ],
            "billData": {
                "TWD": {
                    "2026/05": {
                        "summary": {"billAmt": 15943, "pmtAmt": 8600},
                        "bills": [
                            # 純台幣 (postingDt 比 purchaseDt 晚 3 天 = 信用卡常見)
                            {"purchaseDt": "050526", "postingDt": "050826",
                             "cardNo": "7036_1",
                             "merchantChiName": "健身工廠信義廠",
                             "ntAmt": 1288, "foreignAmt": "", "occCurCode": "",
                             "origCurCode": "N"},
                            # 外幣 EUR (postingDt 比 purchaseDt 晚很久 — 外幣 forex clearing 慢)
                            {"purchaseDt": "051226", "postingDt": "052026",
                             "cardNo": "7036_1",
                             "merchantChiName": "GetYourGuide Tickets",
                             "ntAmt": 7262, "foreignAmt": "196.2", "occCurCode": "EUR",
                             "origCurCode": "I"},  # origCurCode 是 'I' 垃圾欄
                            # 本行扣繳（負值, postingDt = '000000' 視為缺值 fallback consume_date）
                            {"purchaseDt": "050526", "postingDt": "000000",
                             "cardNo": "0000",
                             "merchantChiName": "本行扣繳",
                             "ntAmt": -8600, "foreignAmt": "", "occCurCode": "",
                             "origCurCode": "N"},
                        ],
                    },
                    "2026/04": {"summary": {"billAmt": 8600, "pmtAmt": 20270}, "bills": []},
                },
            },
        },
    },
    "summary": {},
    "twd_deposit": {},
}


def test_ctbc_billed_extracts_4_cards_from_cardDataList(store):
    delta = persist_ctbc(FIXTURE_BILLED, store)
    assert delta["cards"] == 2  # fixture 給 2 張
    conn = store.conn
    cards = list(conn.execute("SELECT card_no, name FROM cards ORDER BY card_no"))
    assert len(cards) == 2
    names = {r[0]: r[1] for r in cards}
    assert "****7036" in names
    assert names["****7036"] == "中華航空聯名卡"
    assert "****7051" in names
    assert names["****7051"] == "中油聯名卡"


def test_ctbc_billed_writes_3_bills_with_foreign_currency(store):
    delta = persist_ctbc(FIXTURE_BILLED, store)
    assert delta["card_billed_new"] == 3
    conn = store.conn
    rows = list(conn.execute(
        "SELECT card_no, description, amount, currency, consume_currency, consume_amount "
        "FROM card_billed_txns ORDER BY description"
    ))
    assert len(rows) == 3
    # GetYourGuide 應有 EUR 196.2（用 occCurCode 不是 origCurCode）
    gyg = next(r for r in rows if "GetYourGuide" in r[1])
    assert gyg[2] == 7262   # ntAmt = TWD 入帳值
    assert gyg[4] == "EUR"  # consume_currency
    assert abs(gyg[5] - 196.2) < 0.01
    # 健身工廠純台幣不應有 consume_*
    gym = next(r for r in rows if "健身工廠" in r[1])
    assert gym[4] is None
    assert gym[5] is None
    # 本行扣繳 ntAmt=-8600 負值
    debit = next(r for r in rows if "本行扣繳" in r[1])
    assert debit[2] == -8600


def test_ctbc_billed_date_format_mmdd_yy(store):
    """purchaseDt='050526' → 2026-05-05 (MMDDYY)"""
    persist_ctbc(FIXTURE_BILLED, store)
    conn = store.conn
    rows = list(conn.execute("SELECT consume_date FROM card_billed_txns WHERE description LIKE '健身工廠%'"))
    assert rows[0][0] == "2026-05-05"


def test_ctbc_billed_post_date_from_postingDt_field(store):
    """2026-06-19 regression: postingDt 是 CTBC 真實「入帳日」欄位 (MMDDYY)。

    之前誤判「CTBC 無單獨入帳日」把 post_date 直接 copy consume_date，
    UI 顯示「入帳日 = 消費日」永遠一樣。修正後：
    - 健身工廠: purchaseDt=050526 → consume=2026-05-05, postingDt=050826 → post=2026-08-05
    - GetYourGuide: purchaseDt=051226 → consume=2026-05-12, postingDt=052026 → post=2026-05-20
    - 本行扣繳: postingDt=000000 (缺值) → fallback consume_date 2026-05-05
    """
    persist_ctbc(FIXTURE_BILLED, store)
    conn = store.conn
    rows = list(conn.execute(
        "SELECT description, consume_date, post_date FROM card_billed_txns ORDER BY description"
    ))
    by_desc = {r[0]: (r[1], r[2]) for r in rows}

    # 健身工廠: post_date 跟 consume_date 不同 (postingDt='050826' = MMDDYY 05/08/26 = 2026-05-08, 晚 3 天)
    gym_consume, gym_post = by_desc["健身工廠信義廠"]
    assert gym_consume == "2026-05-05"
    assert gym_post == "2026-05-08"
    assert gym_consume != gym_post  # 確認真有區別

    # GetYourGuide: 外幣 clearing 晚 8 天 (postingDt='052026' = MMDDYY 05/20/26 = 2026-05-20)
    gyg_consume, gyg_post = by_desc["GetYourGuide Tickets"]
    assert gyg_consume == "2026-05-12"
    assert gyg_post == "2026-05-20"
    assert gyg_consume != gyg_post

    # 本行扣繳: postingDt='000000' fallback consume_date (避免 NOT NULL 違反)
    debit_consume, debit_post = by_desc["本行扣繳"]
    assert debit_consume == "2026-05-05"
    assert debit_post == "2026-05-05"  # fallback


def test_ctbc_unbilled_qu006_writes_real_unbilled_rows(store):
    """CTBC qu006/011 未出帳單是 MoneyBook 也會顯示的真實 unbilled source.

    Regression (2026-06-25): 兩筆「９１ＡＰＰ＊ＩＳＰＯ＋」已從 qu041 的暫無資訊
    授權 placeholder 進到 qu006/011, MoneyBook 顯示但 thoth 仍整包忽略, 導致漏消費.
    正解: 仍忽略 qu041 即時授權 placeholder, 但 parse qu006/011 allItems.
    """
    fixture_pending = {
        "card_api_dump": {
            "/twrbc-card/qu041/010": {
                "allItems": [
                    {"txnDate": "20260618", "cardNoSuffixFour": "7036",
                     "merchName": "暫無資訊", "txnAmt": 5866,
                     "origCurCode": "", "origTxnAmt": None},
                ],
            },
            "/twrbc-card/qu006/011": {
                "allItems": [
                    {"purchaseDt": "20260618", "postingDt": "20260623",
                     "cardNoSuffixFour": "7036_0",
                     "description": "９１ＡＰＰ＊ＩＳＰＯ＋   TAIPEI CITY  TW",
                     "purchaseAmt": 4631, "origCurCode": "901", "origCurDesc": "TWD",
                     "origCurAmt": 4631, "txCode": "40"},
                    {"purchaseDt": "20260618", "postingDt": "20260623",
                     "cardNoSuffixFour": "7036_0",
                     "description": "９１ＡＰＰ＊ＩＳＰＯ＋   TAIPEI CITY  TW",
                     "purchaseAmt": 5866, "origCurCode": "901", "origCurDesc": "TWD",
                     "origCurAmt": 5866, "txCode": "40"},
                ],
            },
        },
        "summary": {}, "twd_deposit": {},
    }
    delta = persist_ctbc(fixture_pending, store)
    assert delta["card_unbilled"] == 2
    conn = store.conn
    rows = list(conn.execute(
        "SELECT scope, card_no, consume_date, description, amount, currency, "
        "consume_currency, consume_amount, txn_type "
        "FROM card_pending_txns ORDER BY amount"
    ))
    assert len(rows) == 2, f"qu006 unbilled rows should be persisted, got: {rows}"
    assert [r[0] for r in rows] == ["unbilled", "unbilled"]
    assert [r[1] for r in rows] == ["****7036", "****7036"]
    assert [r[2] for r in rows] == ["2026-06-18", "2026-06-18"]
    assert [r[3] for r in rows] == ["９１ＡＰＰ＊ＩＳＰＯ＋   TAIPEI CITY  TW"] * 2
    assert [r[4] for r in rows] == [4631, 5866]
    assert [r[5] for r in rows] == ["TWD", "TWD"]
    assert [r[6] for r in rows] == [None, None]
    assert [r[7] for r in rows] == [None, None]
    assert [r[8] for r in rows] == ["spending", "spending"]


def test_ctbc_persist_sweeps_existing_pending_rows(store):
    """既有 prod 已有殘留 pending row, 升級到此版後第一次 sync 應該自動 sweep 清空.

    機制: persist_ctbc 仍 call store.refresh_card_pending("unbilled", [], ...) 空 batch,
    refresh_card_pending 先 DELETE 整 scope 再 INSERT (這次 INSERT 0 筆) → 殘留全清.
    """
    # 預塞 3 筆殘留 (模擬升級前的歷史 row)
    conn = store.conn
    for i in range(3):
        conn.execute(
            """INSERT INTO card_pending_txns
               (user_id, scope, card_no, consume_date, description, amount, currency,
                refreshed_at)
               VALUES (1, 'unbilled', ?, ?, ?, ?, 'TWD', '2026-06-19T00:00:00')""",
            (f"****344{i}", f"2026-06-0{i+1}", f"舊殘留-{i}", 100 + i),
        )
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM card_pending_txns").fetchone()[0] == 3

    # 跑一次 persist (qu041 還是有資料, 但新版不該寫)
    fixture = {
        "card_api_dump": {
            "/twrbc-card/qu041/010": {
                "allItems": [
                    {"txnDate": "20260611", "cardNoSuffixFour": "7036",
                     "merchName": "中華航空", "txnAmt": 8292,
                     "origCurCode": "", "origTxnAmt": None},
                ],
            },
        },
        "summary": {}, "twd_deposit": {},
    }
    persist_ctbc(fixture, store)
    # 殘留全清, 新 batch 也沒寫進去
    assert conn.execute("SELECT COUNT(*) FROM card_pending_txns").fetchone()[0] == 0


def test_ctbc_billed_months_summary_in_daily_metric(store):
    delta = persist_ctbc(FIXTURE_BILLED, store)
    assert delta["bill_months"] == 2  # 2026/05 + 2026/04
    conn = store.conn
    rows = list(conn.execute(
        "SELECT payload_json FROM daily_metrics WHERE category='ctbc_bill_months_summary'"
    ))
    assert len(rows) == 1
    import json
    summary = json.loads(rows[0][0])
    assert "TWD/2026/05" in summary
    assert summary["TWD/2026/05"]["billAmt"] == 15943


def test_ctbc_same_last4_pk_collision_resolved(store):
    """🚨 2026-06-14 PK 衝突修：同 last4 多張卡（正卡+附卡）應分別 insert，
    不能互相覆蓋。使用者 7036 有中華航空正卡 + 中華航空附卡兩張。
    """
    fixture_dup_last4 = {
        "card_api_dump": {
            "/twrbc-card/qu002/010": {
                "cardDataList": [
                    {"cardNoSuffixFour": "7036_1", "cardName": "中華航空聯名卡",
                     "positiveOrAttached": "正卡", "cardNo": "9000-56**-****-7036"},
                    {"cardNoSuffixFour": "7036_2", "cardName": "中華航空聯名卡",
                     "positiveOrAttached": "附卡", "cardNo": "9000-56**-****-7036"},
                    # 另一張不同 last4 的單張卡（應保持純 "****7051" PK）
                    {"cardNoSuffixFour": "7051_0", "cardName": "中油聯名卡",
                     "positiveOrAttached": "正卡", "cardNo": "9000-55**-****-7051"},
                ],
                "billData": {"TWD": {}},
            },
        },
        "summary": {}, "twd_deposit": {},
    }
    delta = persist_ctbc(fixture_dup_last4, store)
    # 應該有 3 張卡（不再因 PK 衝突剩 2 張）
    assert delta["cards"] == 3
    conn = store.conn
    cards = list(conn.execute("SELECT card_no, name FROM cards ORDER BY card_no"))
    pk_list = [c[0] for c in cards]
    name_map = {c[0]: c[1] for c in cards}
    # 同 last4 2 張 → 加 _1 / _2 suffix
    assert "****7036_1" in pk_list, f"正卡應有 _1 suffix: {pk_list}"
    assert "****7036_2" in pk_list, f"附卡應有 _2 suffix: {pk_list}"
    # 單張不同 last4 → 保持純 PK（向後相容）
    assert "****7051" in pk_list, f"單張應為純 ****7051: {pk_list}"
    # name 加上「正卡/附卡」幫忙辨識
    assert "正卡" in name_map["****7036_1"]
    assert "附卡" in name_map["****7036_2"]
    # 單張不加正/附卡 suffix（沒衝突不需區分）
    assert "正卡" not in name_map["****7051"]
