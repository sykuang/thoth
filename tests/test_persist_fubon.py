"""End-to-end fubon persist_fubon regression test.

歷史背景:
- commit b21e721 修了 fubon server-mode silent regression
  (sync_runner 走 persist_generic_dump → persist_fubon, 0 cards → 3 cards 完整入庫)
- 這份 test 用 fake fixture 鎖死 persist_fubon 對真實結構資料的解析行為
- 確保:
    A. persist_fubon 對 cards_page_text 能正確 parse 3 張卡 (regex pattern 別漂移)
    B. amount_page_text 正卡人信用額度 80,000 被抓進 credit_limit
    C. billing_summary 抓得到本期帳單結帳日 → cards.statement_close_date
    D. billed_page_text 「自動扣繳」會被分類成 txn_type='payment'
    E. server-mode 跟 CLI 跑同份 data 得到完全一致的 delta / stats

Fixture:
- tests/fixtures/fubon_collected_fake.json
- 結構直接從使用者真實 collected.json 複製, 但 PII 全清:
    * 卡末四 1763/2099/3368 → 1111/2222/3333
    * 卡名 ＪＵ卡紅../Costco../momo.. → Fake VirtualCard A/B/C
    * BIN 356969/524108 → 000001/000002
    * 姓名 測＊試君 → Test*Holder, 身分證 → B999999999
- 任何「絕不再用使用者真資料進 fixture」鐵律的 future fixture 都該照這 pattern
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.core.persist import persist_fubon
from backend.core.store import BankStore

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "fubon_collected_fake.json"


@pytest.fixture
def fubon_data():
    fubon_data = json.loads(Path(FIXTURE).read_text())["data"]
    # --- 富邦 TWD deposit transactions fixture (CDSQU001) ---
    # PII-safe synthetic account/amounts. Mirrors real text shape:
    # 帳務日期\t交易時間\t摘要\t支出金額\t存入金額\t即時餘額\t附註
    fubon_data["deposit_txn_results"] = [
        {
            "account_no": "90000000267053",
            "selected_text": "90000000267053 測試分行",
            "text": (
                "存款/外匯/轉帳 > 存款交易查詢 > 臺外幣交易明細查詢\n"
                "查詢結果\t\t \n"
                "臺幣活期存款 外幣活期存款\n"
                "帳務日期\t交易時間\t摘要\t支出金額\t存入金額\t即時餘額\t附註\n"
                "2026/06/21\t2026/06/21 00:00:00\t利息\t\t5.00\t5.00\t\n"
                "2026/06/30\t2026/06/30 19:21:08\t測試轉入\t\t7,473.00\t7,478.00\t********70019999\n"
            ),
        }
    ]
    return fubon_data


@pytest.fixture
def fubon_store(tmp_path, monkeypatch):
    """Isolated BankStore that writes to tmp_path instead of backend/data/"""
    from backend.core import store as store_mod
    monkeypatch.setattr(store_mod, "DATA_ROOT", tmp_path)
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))  # 雙保險：env 也設,即使 store.py 退化也不會 leak 進真 fubon.sqlite
    s = BankStore("fubon")
    yield s
    s.close()


def test_fubon_persist_parses_three_cards(fubon_data, fubon_store):
    """A: cards_page_text 必須解析出 3 張卡 (regex pattern stability)."""
    persist_fubon(fubon_data, fubon_store)
    stats = fubon_store.stats()

    assert stats["cards"] == 3, (
        f"應抓 3 張卡, 抓到 {stats['cards']}. "
        f"persist_fubon._parse_fubon_credit_card 的 cards regex (line ~1953) 漂移?"
    )


def test_fubon_persist_extracts_credit_limit_80k(fubon_data, fubon_store):
    """B: 正卡人信用額度 80,000 必須 parse 成功, 三張卡全套用."""
    persist_fubon(fubon_data, fubon_store)

    rows = fubon_store.conn.execute(
        "SELECT card_no, credit_limit FROM cards"
    ).fetchall()

    assert len(rows) == 3
    for card_no, limit in rows:
        assert limit == 80000.0, (
            f"card {card_no} credit_limit={limit}, 應 80,000. "
            f"persist_fubon 對 amount_page_text 的「正卡人信用額度」抽取失效?"
        )


def test_fubon_persist_captures_statement_date(fubon_data, fubon_store):
    """C: 本期帳單結帳日必須抽出來寫進 statement_close_date."""
    persist_fubon(fubon_data, fubon_store)

    rows = fubon_store.conn.execute(
        "SELECT card_no, statement_close_date FROM cards"
    ).fetchall()

    for card_no, stmt in rows:
        assert stmt is not None, f"card {card_no} statement_close_date 是 None"
        # 格式 YYYY-MM-DD
        assert len(stmt) == 10 and stmt[4] == "-" and stmt[7] == "-", (
            f"card {card_no} stmt={stmt}, 預期 YYYY-MM-DD"
        )


def test_fubon_persist_normalizes_payment_due_date(fubon_data, fubon_store):
    """富邦 collector/parser 的 YYYY/MM/DD 繳款截止日入庫前必須正規化成 ISO dash."""
    fubon_data["amount_page_text"] = fubon_data["amount_page_text"].replace(
        "2026/05/16\t0\t0\t無需繳款\t12.62%\t5.62%",
        "2026/06/16\t7,473\t747\t2026/07/02\t12.62%\t5.62%",
    )

    persist_fubon(fubon_data, fubon_store)

    rows = fubon_store.conn.execute(
        "SELECT card_no, payment_due_date FROM cards ORDER BY card_no"
    ).fetchall()

    assert len(rows) == 3
    for card_no, due in rows:
        assert due == "2026-07-02", (
            f"card {card_no} payment_due_date={due!r}, "
            "富邦 payment_due_date 應在 persist/collector 層正規化成 YYYY-MM-DD"
        )


def test_fubon_persist_classifies_payment_txn(fubon_data, fubon_store):
    """D: 「自動扣繳」必須被識別成 txn_type='payment' (本期繳清 → 不算 spending)."""
    persist_fubon(fubon_data, fubon_store)

    rows = fubon_store.conn.execute(
        "SELECT description, amount, txn_type FROM card_billed_txns "
        "WHERE description LIKE '%扣繳%' OR description LIKE '%自動%'"
    ).fetchall()

    assert rows, "沒抓到「自動扣繳」筆 (billed_page_text parser 漂移?)"
    for desc, amt, txn_type in rows:
        assert txn_type == "payment", (
            f"desc={desc} amt={amt} txn_type={txn_type}, 「自動扣繳」應為 payment 不是 spending"
        )


def test_fubon_persist_writes_four_daily_metrics(fubon_data, fubon_store):
    """E: persist_fubon 應寫 4 個 daily_metric: card_billing_summary / card_limits / card_points / endpoints."""
    persist_fubon(fubon_data, fubon_store)

    cats = {
        r[0] for r in fubon_store.conn.execute(
            "SELECT DISTINCT category FROM daily_metrics"
        )
    }

    expected = {
        "fubon_card_billing_summary",
        "fubon_card_limits",
        "fubon_card_points",
        "fubon_endpoints",
    }

    missing = expected - cats
    assert not missing, f"daily_metrics 缺類別: {missing}, 實際: {cats}"


def test_fubon_persist_delta_structured_scope(fubon_data, fubon_store):
    """F: delta 必須回 scope='structured' (不是 'dump_only' — generic_dump 的招牌標記)."""
    delta = persist_fubon(fubon_data, fubon_store)

    assert delta["scope"] == "structured", (
        f"delta.scope={delta['scope']}, 應為 'structured'. "
        f"'dump_only' 表 persist 退化回 persist_generic_dump."
    )
    assert delta["bank"] == "fubon"
    assert delta["card_billed_new"] >= 1, "billed 至少要寫 1 筆 (本期自動扣繳)"


def test_fubon_persist_writes_deposit_transactions(fubon_data, fubon_store):
    """G: CDSQU001 存款交易查詢結果必須寫入 twd_transactions."""
    delta = persist_fubon(fubon_data, fubon_store)

    assert delta["twd_txn_new"] == 2
    rows = [
        tuple(row)
        for row in fubon_store.conn.execute(
            "SELECT account_no, txn_datetime, description, raw_description, "
            "expend, income, balance, memo "
            "FROM twd_transactions ORDER BY txn_datetime"
        ).fetchall()
    ]
    assert rows == [
        ("90000000267053", "2026-06-21 00:00:00", "利息", "利息", None, 5.0, 5.0, None),
        ("90000000267053", "2026-06-30 19:21:08", "測試轉入 - ********70019999",
         "測試轉入", None, 7473.0, 7478.0, "********70019999"),
    ]


def test_fubon_persist_no_pii_leak_in_fixture():
    """⚠️ 鐵律: fixture 不可帶使用者真資料."""
    import re
    txt = FIXTURE.read_text()
    pii_patterns = [
        (r"測", "姓"),
        (r"昀君", "名"),
        (r"\b1763\b", "真實末四"),
        (r"\b2099\b", "真實末四"),
        (r"\b3368\b", "真實末四"),
        (r"A1265185\d{2}", "身分證"),
        (r"ＪＵ卡紅", "真實卡名"),
        (r"Costco聯名", "真實卡名"),
        (r"momo卡-kiwi", "真實卡名"),
    ]
    leaks = []
    for pat, name in pii_patterns:
        if re.search(pat, txt):
            leaks.append(f"{name}: pattern {pat!r}")
    assert not leaks, (
        f"🚨 fubon_collected_fake.json fixture 帶使用者真資料: {leaks}. "
        f"絕不再用使用者真資料進 fixture 鐵律."
    )
