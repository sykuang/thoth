"""pending→billed 消失比對 (vanished-pending adoption)。

背景：四欄 key (card_no, consume_date, amount, description) 搬 overlay 只在銀行
入帳後 description 不變時有效。銀行把「暫無資訊」改寫成正式商戶名時，使用者手動
設的分類/備註/拆帳會留在 pending、billed 是白紙，且 pending 沒被 prune → UI 雙顯。

改用行為與穩定 identity 當證據：
- 外幣用 `(card_no, consume_currency, consume_amount)`，可跨結匯與 overlap；
- 台幣用 `(card_no, consume_date, amount)`，且必須由可信 pending 清單證明已消失。
候選只限同一 transaction 內剛 INSERT 的 billed；歧義／split 無法精確重算即
fail-closed 保留 pending，避免錯搬或靜默遺失。
"""
from __future__ import annotations

import pytest

from backend.core.store import BankStore, _rescale_splits

PEND = {"card_no": "****1234", "date": "2026-07-01", "desc": "AMAZON JP",
        "amount": 1000, "currency": "TWD"}


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    monkeypatch.delenv("DB_BACKEND", raising=False)
    st = BankStore("testbank", user_id=1)
    yield st
    st.close()


def _seed_edited_pending(st, pendings=(PEND,)):
    """種 pending 並在每筆上做使用者編輯。"""
    st.refresh_card_pending("unbilled", list(pendings), rules=[])
    for r in st.conn.execute("SELECT id FROM card_pending_txns").fetchall():
        st.conn.execute(
            "UPDATE card_pending_txns SET category=?, subcategory=?, "
            "description_overwrite=?, auto_excluded=1 WHERE id=?",
            ("購物", "電商", "老婆的生日禮物", r["id"]))
    st.conn.commit()


def _billed(st):
    return [dict(r) for r in st.conn.execute(
        "SELECT id, description, amount, category, subcategory, txn_type, flow_type, "
        "description_overwrite, auto_excluded FROM card_billed_txns").fetchall()]


def _pending_count(st):
    return st.conn.execute(
        "SELECT COUNT(*) c FROM card_pending_txns").fetchone()["c"]


def test_rescale_rejects_invalid_splits_even_when_total_already_matches():
    assert _rescale_splits('[{"amount":100}]', 100) is None
    assert _rescale_splits('[{"amount":0},{"amount":100}]', 100) is None
    assert _rescale_splits('[{"amount":1},{"amount":1}]', 0) is None


def _seed_existing_billed_then_pending(store, billed_category=None):
    posted = {**PEND, "bill_date": "2026-07-20"}
    store.upsert_card_billed([posted], rules=[])
    store.conn.execute("UPDATE card_billed_txns SET category=?", (billed_category,))
    store.conn.commit()
    store.refresh_card_pending("unbilled", [{**PEND, "desc": "TEMP", "amount": 999}], rules=[])
    store.conn.execute(
        "UPDATE card_pending_txns SET description=?, amount=?, category='旅遊', "
        "description_overwrite='KEEP' WHERE scope='unbilled'",
        (PEND["desc"], PEND["amount"]),
    )
    store.conn.commit()
    return posted


def test_existing_blank_billed_conflict_adopts_late_pending_edit(store):
    """前輪 billed 已存在時，晚到的 pending 人工 edit 不能因 conflict+purge 遺失。"""
    posted = _seed_existing_billed_then_pending(store)
    assert store.upsert_card_billed([posted], rules=[]) == 0

    billed = _billed(store)[0]
    assert billed["category"] == "旅遊"
    assert billed["description_overwrite"] == "KEEP"
    assert _pending_count(store) == 0


def test_existing_edited_billed_conflict_preserves_both_overlays(store):
    """Existing billed 也可能已人工改過；provenance 不足時寧可雙顯，不覆蓋任一方。"""
    posted = _seed_existing_billed_then_pending(store, billed_category="餐飲")
    assert store.upsert_card_billed([posted], rules=[]) == 0
    store.refresh_card_pending("unbilled", [], rules=[], fetch_ok=True)

    assert _billed(store)[0]["category"] == "餐飲"
    pending = store.conn.execute(
        "SELECT category, description_overwrite FROM card_pending_txns"
    ).fetchone()
    assert pending["category"] == "旅遊"
    assert pending["description_overwrite"] == "KEEP"


def test_pending_refresh_invalid_split_rescale_rolls_back_scope(store):
    """pending→pending 新母筆太小時不可清掉原 split；整個 scope 原樣保留。"""
    pending = {**PEND, "amount": 1200, "consume_currency": "USD", "consume_amount": 10}
    store.refresh_card_pending("unbilled", [pending], rules=[])
    splits = [
        {"amount": 600, "category": "餐飲"},
        {"amount": 600, "category": "旅遊"},
    ]
    store.conn.execute(
        "UPDATE card_pending_txns SET category='旅遊', splits_overwrite=?",
        (__import__("json").dumps(splits),),
    )
    store.conn.commit()
    store.refresh_card_pending(
        "unbilled", [{**pending, "amount": 1, "desc": "UPDATED"}],
        rules=[], fetch_ok=True)

    row = store.conn.execute(
        "SELECT amount, description, splits_overwrite FROM card_pending_txns"
    ).fetchone()
    assert row["amount"] == 1200
    assert row["description"] == PEND["desc"]
    assert __import__("json").loads(row["splits_overwrite"]) == splits


def test_pending_fx_fallback_ambiguity_rolls_back_scope(store):
    """兩筆同 FX identity 且 description 都改寫時，順序不是證據；不可搬 overlay。"""
    base = {**PEND, "amount": 3200, "consume_currency": "USD", "consume_amount": 100.2}
    store.refresh_card_pending(
        "unbilled", [{**base, "desc": "OLD A"}, {**base, "desc": "OLD B"}], rules=[])
    store.conn.execute(
        "UPDATE card_pending_txns SET category='旅遊' WHERE description='OLD A'"
    )
    store.conn.commit()
    store.refresh_card_pending(
        "unbilled", [{**base, "desc": "NEW A"}, {**base, "desc": "NEW B"}],
        rules=[], fetch_ok=True)

    rows = store.conn.execute(
        "SELECT description, category FROM card_pending_txns ORDER BY description"
    ).fetchall()
    assert [(r["description"], r["category"]) for r in rows] == [
        ("OLD A", "旅遊"), ("OLD B", None),
    ]


def test_upsert_billed_count_uses_returned_ids_not_total_changes(store):
    """RETURNING id 直接計新增數；pending purge 不可把 billed_new 灌大。"""
    _seed_edited_pending(store)
    assert store.upsert_card_billed([{**PEND, "bill_date": "2026-07-20"}], rules=[]) == 1
    assert store.upsert_card_billed([{**PEND, "bill_date": "2026-07-20"}], rules=[]) == 0
    store.refresh_card_pending("unbilled", [PEND], rules=[], fetch_ok=True)


def test_desc_rewritten_overlay_adopted_and_no_double_display(store):
    """主線：銀行入帳時改寫 description → 分類仍搬到 billed 且不雙顯。"""
    _seed_edited_pending(store)
    store.upsert_card_billed(
        [{**PEND, "desc": "AMAZON.CO.JP TOKYO JP", "bill_date": "2026-07-20"}],
        rules=[])
    store.refresh_card_pending("unbilled", [], rules=[], fetch_ok=True)

    b = _billed(store)
    assert len(b) == 1
    assert b[0]["description"] == "AMAZON.CO.JP TOKYO JP"  # 銀行版本不被竄改
    assert b[0]["category"] == "購物"
    assert b[0]["subcategory"] == "電商"
    assert b[0]["description_overwrite"] == "老婆的生日禮物"
    assert b[0]["auto_excluded"] == 1
    assert _pending_count(store) == 0, "殘留 pending 會在 UI 上與 billed 雙顯"


def test_fetch_not_ok_preserves_existing_pending_overlay(store):
    """守門1：爬蟲失敗必 fail-closed，舊 pending 與使用者編輯都不動。"""
    _seed_edited_pending(store)
    store.upsert_card_billed(
        [{"card_no": "****1234", "date": "2026-06-01", "desc": "別筆交易",
          "amount": 1000, "bill_date": "2026-06-20"}], rules=[])
    store.refresh_card_pending("unbilled", [], rules=[], fetch_ok=False)

    assert _billed(store)[0]["category"] is None
    row = store.conn.execute(
        "SELECT category, description_overwrite FROM card_pending_txns").fetchone()
    assert row["category"] == "購物"
    assert row["description_overwrite"] == "老婆的生日禮物"


def test_fetch_not_ok_still_adopts_matching_new_billed_without_clearing_scope(store):
    """Billed 新增與 pending fetch 失敗同時發生：搬匹配筆，但保留其他舊 pending。"""
    pending = {**PEND, "amount": 3200, "consume_currency": "USD", "consume_amount": 100.2}
    store.refresh_card_pending("unbilled", [pending], rules=[])
    store.conn.execute("UPDATE card_pending_txns SET category='購物'")
    store.conn.commit()
    other = {**PEND, "card_no": "****9999", "amount": 50, "desc": "另一筆未入帳"}
    store.refresh_card_pending("other", [other], rules=[])
    # 同 scope 再加一筆不相關舊 pending，證明不會 whole-scope DELETE。
    store.conn.execute(
        "INSERT INTO card_pending_txns "
        "(user_id, scope, card_no, consume_date, description, amount, currency, refreshed_at) "
        "VALUES (1, 'unbilled', '****9999', '2026-07-02', '另一筆未入帳', 50, 'TWD', 'now')"
    )
    store.conn.commit()
    store.upsert_card_billed([{
        **pending, "amount": 3267, "desc": "正式商戶", "bill_date": "2026-07-20",
    }], rules=[])
    store.refresh_card_pending("unbilled", [], rules=[], fetch_ok=False)

    assert _billed(store)[0]["category"] == "購物"
    remaining = store.conn.execute(
        "SELECT card_no FROM card_pending_txns WHERE scope='unbilled'"
    ).fetchall()
    assert [r["card_no"] for r in remaining] == ["****9999"]


def test_multiple_vanished_same_card_amount_skipped(store):
    """守門3a：同卡同額消失多筆 → 無法判定誰對誰，寧可漏搬。"""
    _seed_edited_pending(store, (PEND, {**PEND, "desc": "同日同額另一筆"}))
    store.upsert_card_billed(
        [{**PEND, "desc": "改寫A", "bill_date": "2026-07-20"}], rules=[])
    store.refresh_card_pending(
        "unbilled", [{**PEND, "date": "2026-07-09", "desc": "還在的別筆",
                      "amount": 77}], rules=[], fetch_ok=True)

    assert _billed(store)[0]["category"] is None
    assert _pending_count(store) == 2, "歧義時必須保留原 pending overlay，不可只跳過搬遷後仍清空 scope"


def test_ambiguous_billed_candidates_skipped(store):
    """守門3b：候選 billed 同卡同額有兩筆 → 放棄配對。"""
    _seed_edited_pending(store)
    store.upsert_card_billed([
        {**PEND, "desc": "候選一", "bill_date": "2026-07-20"},
        {**PEND, "desc": "候選二", "bill_date": "2026-07-20"},
    ], rules=[])
    store.refresh_card_pending("unbilled", [], rules=[], fetch_ok=True)

    assert all(r["category"] is None for r in _billed(store))
    assert _pending_count(store) == 1


def test_auto_categorized_new_billed_is_overridden_by_pending_user_overlay(store):
    """本 sync 新 billed 的 rule category 不是人工編輯；pending 人工 overlay 必須優先。"""
    _seed_edited_pending(store)
    rules = [{"pattern": "正式商戶", "category": "餐飲", "subcategory": "其他"}]
    store.upsert_card_billed(
        [{**PEND, "desc": "正式商戶", "bill_date": "2026-07-20"}], rules=rules)
    store.refresh_card_pending("unbilled", [], rules=rules, fetch_ok=True)

    row = _billed(store)[0]
    assert row["category"] == "購物"
    assert row["subcategory"] == "電商"
    assert row["description_overwrite"] == "老婆的生日禮物"


def test_same_identity_unedited_sibling_blocks_adoption(store):
    """唯一性要先算所有 pending；未編輯 sibling 也會使 identity 歧義。"""
    store.refresh_card_pending(
        "unbilled", [PEND, {**PEND, "desc": "同日同額另一筆"}], rules=[])
    first = store.conn.execute(
        "SELECT id FROM card_pending_txns WHERE description=?", (PEND["desc"],)
    ).fetchone()
    store.conn.execute(
        "UPDATE card_pending_txns SET category='購物', description_overwrite='KEEP' WHERE id=?",
        (first["id"],),
    )
    store.conn.commit()
    store.upsert_card_billed(
        [{**PEND, "desc": "正式商戶", "bill_date": "2026-07-20"}], rules=[])
    store.refresh_card_pending("unbilled", [], rules=[], fetch_ok=True)

    assert _billed(store)[0]["category"] is None
    assert _pending_count(store) == 2


def test_refresh_exact_key_conflicting_occurrence_drop_fails_closed(store):
    """同 exact key 由兩筆減為一筆時，衝突 overlay 無法靠順序判斷去留。"""
    store.refresh_card_pending("unbilled", [PEND, PEND], rules=[])
    ids = [row["id"] for row in store.conn.execute(
        "SELECT id FROM card_pending_txns ORDER BY id"
    ).fetchall()]
    assert len(ids) == 2
    store.conn.execute("UPDATE card_pending_txns SET category='餐飲' WHERE id=?", (ids[0],))
    store.conn.execute("UPDATE card_pending_txns SET category='旅遊' WHERE id=?", (ids[1],))
    store.conn.commit()

    reported = store.refresh_card_pending("unbilled", [PEND], rules=[], fetch_ok=True)

    assert reported == 2
    assert _pending_count(store) == 2
    assert {row["category"] for row in store.conn.execute(
        "SELECT category FROM card_pending_txns"
    ).fetchall()} == {"餐飲", "旅遊"}


def test_next_sync_existing_billed_can_adopt_vanished_pending_overlay(store):
    """首輪 pending fetch 失敗後，次輪 conflict 仍可用本 sync touched billed 接手。"""
    store.refresh_card_pending("unbilled", [PEND], rules=[])
    store.conn.execute("UPDATE card_pending_txns SET category='購物'")
    store.conn.commit()
    billed = {**PEND, "desc": "AMAZON MARKETPLACE", "bill_date": "2026-07-20"}

    store.upsert_card_billed([billed], rules=[])
    store.refresh_card_pending("unbilled", [], rules=[], fetch_ok=False)
    assert _pending_count(store) == 1

    # 模擬下一個 sync request：新 INSERT ledger 清空，但同 payload 會 touch existing billed。
    store._new_billed_ids.clear()
    store._current_billed_ids.clear()
    store.upsert_card_billed([billed], rules=[])
    store.refresh_card_pending("unbilled", [], rules=[], fetch_ok=True)

    assert _pending_count(store) == 0
    assert _billed(store)[0]["category"] == "購物"


def test_exact_key_conflicting_overlays_across_scopes_fail_closed(store):
    """同 exact key 若跨 scope 人工分類衝突，不能第一筆勝出並刪掉另一筆。"""
    store.refresh_card_pending("unbilled", [PEND], rules=[])
    store.refresh_card_pending("current", [PEND], rules=[])
    store.conn.execute(
        "UPDATE card_pending_txns SET category='旅遊' WHERE scope='unbilled'")
    store.conn.execute(
        "UPDATE card_pending_txns SET category='餐飲' WHERE scope='current'")
    store.conn.commit()

    store.upsert_card_billed([{**PEND, "bill_date": "2026-07-20"}], rules=[])

    assert _billed(store)[0]["category"] is None
    rows = store.conn.execute(
        "SELECT scope, category FROM card_pending_txns ORDER BY scope"
    ).fetchall()
    assert [(r["scope"], r["category"]) for r in rows] == [
        ("current", "餐飲"), ("unbilled", "旅遊"),
    ]


def test_later_scope_extra_identical_occurrence_is_preserved(store):
    """接手 1 筆後，較晚 scope 回 2 筆完全相同 raw rows，只能扣 1 筆。"""
    txn = {**PEND, "amount": 3200, "consume_currency": "USD", "consume_amount": 100.2}
    store.refresh_card_pending("unbilled", [txn], rules=[])
    store.conn.execute("UPDATE card_pending_txns SET category='旅遊'")
    store.conn.commit()
    store.upsert_card_billed([{
        **txn, "amount": 3267, "desc": "正式商戶", "bill_date": "2026-07-20",
    }], rules=[])
    store.refresh_card_pending("unbilled", [txn], rules=[], fetch_ok=True, commit=False)
    store.refresh_card_pending("current", [txn, txn], rules=[], fetch_ok=True)

    rows = store.conn.execute(
        "SELECT scope, description FROM card_pending_txns"
    ).fetchall()
    assert [(r["scope"], r["description"]) for r in rows] == [
        ("current", PEND["desc"]),
    ]


def test_later_scope_distinct_occurrence_with_same_identity_is_preserved(store):
    """較晚 scope 同 FX identity 但 raw merchant 不同，不能被全域 identity ledger 吃掉。"""
    a = {**PEND, "amount": 3200, "consume_currency": "USD", "consume_amount": 100.2}
    b = {**a, "desc": "DIFFERENT SHOP"}
    store.refresh_card_pending("unbilled", [a], rules=[])
    store.conn.execute("UPDATE card_pending_txns SET category='旅遊'")
    store.conn.commit()
    store.upsert_card_billed([{
        **a, "amount": 3267, "desc": "正式商戶", "bill_date": "2026-07-20",
    }], rules=[])
    store.refresh_card_pending("unbilled", [a], rules=[], fetch_ok=True, commit=False)
    store.refresh_card_pending("current", [b], rules=[], fetch_ok=True)

    rows = store.conn.execute(
        "SELECT scope, description FROM card_pending_txns ORDER BY scope"
    ).fetchall()
    assert [(r["scope"], r["description"]) for r in rows] == [
        ("current", "DIFFERENT SHOP"),
    ]
    assert _billed(store)[0]["category"] == "旅遊"


def test_cross_scope_duplicate_is_adopted_once_without_pending_reinsert(store):
    """同一交易同時在 unbilled/current：接手一次並刪兩 scope，後續 refresh 不插回。"""
    pending = {**PEND, "amount": 3200, "consume_currency": "USD", "consume_amount": 100.2}
    store.refresh_card_pending("unbilled", [pending], rules=[])
    store.refresh_card_pending("current", [pending], rules=[])
    store.conn.execute(
        "UPDATE card_pending_txns SET category='旅遊', description_overwrite='KEEP'"
    )
    store.conn.commit()
    store.upsert_card_billed([{
        **pending, "amount": 3267, "desc": "正式商戶", "bill_date": "2026-07-20",
    }], rules=[])
    store.refresh_card_pending("unbilled", [pending], rules=[], fetch_ok=True, commit=False)
    store.refresh_card_pending("current", [pending], rules=[], fetch_ok=True)

    assert _billed(store)[0]["category"] == "旅遊"
    assert _pending_count(store) == 0


def test_refund_flow_type_survives_adoption(store):
    """Adoption 重算 flow 時必須保留 billed txn_type=refund。"""
    _seed_edited_pending(store)
    store.upsert_card_billed([{
        **PEND, "desc": "退款正式入帳", "bill_date": "2026-07-20", "txn_type": "refund",
    }], rules=[])
    store.refresh_card_pending("unbilled", [], rules=[], fetch_ok=True)

    row = _billed(store)[0]
    assert row["txn_type"] == "refund"
    assert row["flow_type"] == "income"


def test_billed_insert_rolls_back_if_transition_crashes_before_refresh(tmp_path, monkeypatch):
    """upsert 與 refresh 間中斷時 billed 不可先落盤；重啟後仍保有 pending overlay。"""
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    monkeypatch.delenv("DB_BACKEND", raising=False)
    first = BankStore("atomicbank", user_id=1)
    _seed_edited_pending(first)
    first.upsert_card_billed(
        [{**PEND, "desc": "正式商戶", "bill_date": "2026-07-20"}], rules=[])
    first.close()  # 模擬 refresh 前 process crash：未 commit transaction 應 rollback

    reopened = BankStore("atomicbank", user_id=1)
    try:
        billed_count = reopened.conn.execute(
            "SELECT COUNT(*) FROM card_billed_txns").fetchone()
        assert billed_count is not None and billed_count[0] == 0
        row = reopened.conn.execute(
            "SELECT category, description_overwrite FROM card_pending_txns").fetchone()
        assert row is not None
        assert row["category"] == "購物"
        assert row["description_overwrite"] == "老婆的生日禮物"
    finally:
        reopened.close()


def test_unchanged_desc_path_still_works(store):
    """回歸：description 不變時原四欄搬遷路徑仍正常。"""
    _seed_edited_pending(store)
    store.upsert_card_billed([{**PEND, "bill_date": "2026-07-20"}], rules=[])
    store.refresh_card_pending("unbilled", [PEND], rules=[], fetch_ok=True)

    b = _billed(store)
    assert b[0]["category"] == "購物"
    assert b[0]["description_overwrite"] == "老婆的生日禮物"
    assert _pending_count(store) == 0


def test_vanished_without_billed_is_noop(store):
    """授權取消：pending 消失但沒有對應 billed → 不該發生任何事。"""
    _seed_edited_pending(store)
    store.refresh_card_pending(
        "unbilled", [{"card_no": "****9999", "date": "2026-07-05",
                      "desc": "別的", "amount": 50, "currency": "TWD"}],
        rules=[], fetch_ok=True)

    assert _billed(store) == []


def test_foreign_currency_matches_original_amount_and_rescales_splits(store):
    """外幣結匯：以原幣 identity 配對；TWD 金額改變時 split 依比例精確重算。"""
    pending = {
        **PEND, "amount": 3200, "consume_currency": "USD", "consume_amount": 100.2,
    }
    store.refresh_card_pending("unbilled", [pending], rules=[])
    splits = [
        {"amount": 2000, "category": "旅遊", "subcategory": None,
         "note": "機票", "auto_excluded": False},
        {"amount": 1200, "category": "代墊", "subcategory": None,
         "note": "同行者", "auto_excluded": True},
    ]
    store.conn.execute(
        "UPDATE card_pending_txns SET category=?, description_overwrite=?, "
        "splits_overwrite=?",
        ("旅遊", "東京行", __import__("json").dumps(splits, ensure_ascii=False)),
    )
    store.conn.commit()

    store.upsert_card_billed([{
        **pending, "desc": "AMAZON.CO.JP", "amount": 3267,
        "consume_amount": 100.20, "bill_date": "2026-07-20",
    }], rules=[])
    store.refresh_card_pending("unbilled", [], rules=[], fetch_ok=True)

    row = store.conn.execute(
        "SELECT amount, category, description_overwrite, splits_overwrite "
        "FROM card_billed_txns").fetchone()
    moved = __import__("json").loads(row["splits_overwrite"])
    assert row["amount"] == 3267
    assert row["category"] == "旅遊"
    assert row["description_overwrite"] == "東京行"
    assert [s["amount"] for s in moved] == [2042, 1225]
    assert sum(s["amount"] for s in moved) == 3267
    assert moved[0]["note"] == "機票"
    assert moved[1]["auto_excluded"] is True
    assert _pending_count(store) == 0


def test_foreign_pending_refresh_keeps_overlay_when_twd_estimate_changes(store):
    """入帳前 TWD 估算先變，也要以原幣 identity 保住編輯與拆帳。"""
    pending = {
        **PEND, "amount": 3200, "consume_currency": "USD", "consume_amount": 100.2,
    }
    store.refresh_card_pending("unbilled", [pending], rules=[])
    splits = [
        {"amount": 1600, "category": "旅遊", "note": None, "auto_excluded": False},
        {"amount": 1600, "category": "代墊", "note": None, "auto_excluded": True},
    ]
    store.conn.execute(
        "UPDATE card_pending_txns SET category='旅遊', description_overwrite='東京', "
        "splits_overwrite=?", (__import__("json").dumps(splits),))
    store.conn.commit()

    changed = {**pending, "amount": 3267, "desc": "AMAZON PENDING UPDATED"}
    store.refresh_card_pending("unbilled", [changed], rules=[], fetch_ok=True)

    row = store.conn.execute(
        "SELECT amount, category, description_overwrite, splits_overwrite "
        "FROM card_pending_txns").fetchone()
    moved = __import__("json").loads(row["splits_overwrite"])
    assert row["amount"] == 3267
    assert row["category"] == "旅遊"
    assert row["description_overwrite"] == "東京"
    assert [s["amount"] for s in moved] == [1634, 1633]
    assert sum(s["amount"] for s in moved) == 3267


def test_billed_adopts_even_while_bank_still_returns_pending(store):
    """銀行 pending/billed 重疊 1–3 天：新 billed 立即接手，pending 不可插回雙顯。"""
    pending = {
        **PEND, "amount": 3200, "consume_currency": "USD", "consume_amount": 100.2,
    }
    store.refresh_card_pending("unbilled", [pending], rules=[])
    store.conn.execute("UPDATE card_pending_txns SET category='旅遊'")
    store.conn.commit()
    store.upsert_card_billed([{
        **pending, "desc": "正式商戶", "amount": 3267, "bill_date": "2026-07-20",
    }], rules=[])
    # 同次 pending API 還回舊授權資料。
    store.refresh_card_pending("unbilled", [pending], rules=[], fetch_ok=True)

    assert _billed(store)[0]["category"] == "旅遊"
    assert _pending_count(store) == 0


def test_foreign_same_original_amount_on_different_date_never_matches(store):
    """同卡同幣同原幣額會跨月重複；consume_date 不同不可搬 overlay。"""
    pending = {
        **PEND, "date": "2026-01-01", "amount": 3200,
        "consume_currency": "USD", "consume_amount": 100.2,
    }
    store.refresh_card_pending("unbilled", [pending], rules=[])
    store.conn.execute("UPDATE card_pending_txns SET category='旅遊'")
    store.conn.commit()
    store.upsert_card_billed([{
        **pending, "date": "2026-07-01", "amount": 3267,
        "desc": "半年後同額訂閱", "bill_date": "2026-07-20",
    }], rules=[])
    store.refresh_card_pending("unbilled", [], rules=[], fetch_ok=True)

    assert _billed(store)[0]["category"] is None


def test_exact_and_fx_fallback_share_one_occurrence_ledger(store):
    """Exact 已消耗 A 後，FX fallback 不得再把 A 的 overlay 搬給同 identity 的 B。"""
    a = {**PEND, "desc": "EXACT A", "amount": 3200,
         "consume_currency": "USD", "consume_amount": 100.2}
    b = {**a, "desc": "OLD B"}
    store.refresh_card_pending("unbilled", [a, b], rules=[])
    a_id = store.conn.execute(
        "SELECT id FROM card_pending_txns WHERE description='EXACT A'"
    ).fetchone()["id"]
    store.conn.execute(
        "UPDATE card_pending_txns SET category='旅遊', description_overwrite='ONLY A' WHERE id=?",
        (a_id,),
    )
    store.conn.commit()
    store.refresh_card_pending(
        "unbilled", [a, {**b, "desc": "NEW B"}], rules=[], fetch_ok=True)

    rows = store.conn.execute(
        "SELECT description, category, description_overwrite FROM card_pending_txns "
        "ORDER BY description"
    ).fetchall()
    by_desc = {r["description"]: r for r in rows}
    assert by_desc["EXACT A"]["description_overwrite"] == "ONLY A"
    assert by_desc["NEW B"]["category"] is None
    assert by_desc["NEW B"]["description_overwrite"] is None


def test_subcategory_only_overlay_is_adopted(store):
    """只有 subcategory 也算人工 overlay，不可被誤判成空白。"""
    store.refresh_card_pending("unbilled", [PEND], rules=[])
    store.conn.execute("UPDATE card_pending_txns SET subcategory='電商'")
    store.conn.commit()
    store.upsert_card_billed(
        [{**PEND, "desc": "正式商戶", "bill_date": "2026-07-20"}], rules=[])
    store.refresh_card_pending("unbilled", [], rules=[], fetch_ok=True)

    assert _billed(store)[0]["subcategory"] == "電商"
    assert _pending_count(store) == 0


def test_foreign_currency_different_original_amount_never_matches(store):
    """同卡但原幣金額不同，即使 TWD 金額撞到也不可誤搬。"""
    pending = {
        **PEND, "amount": 3200, "consume_currency": "USD", "consume_amount": 100.2,
    }
    store.refresh_card_pending("unbilled", [pending], rules=[])
    store.conn.execute("UPDATE card_pending_txns SET category='旅遊'")
    store.conn.commit()
    store.upsert_card_billed([{
        **pending, "desc": "OTHER", "amount": 3200,
        "consume_amount": 99.9, "bill_date": "2026-07-20",
    }], rules=[])
    store.refresh_card_pending("unbilled", [], rules=[], fetch_ok=True)

    assert _billed(store)[0]["category"] is None


def test_foreign_main_currency_without_original_fields_never_falls_back_to_local(store):
    """只知 USD 主幣別、缺 consume_* 時資料不足，不可拿 amount 當 TWD identity。"""
    pending = {**PEND, "currency": "USD", "amount": 100}
    store.refresh_card_pending("unbilled", [pending], rules=[])
    store.conn.execute("UPDATE card_pending_txns SET category='旅遊'")
    store.conn.commit()
    store.upsert_card_billed([{
        **PEND, "currency": "TWD", "amount": 100,
        "desc": "不相干台幣交易", "bill_date": "2026-07-20",
    }], rules=[])
    store.refresh_card_pending("unbilled", [], rules=[], fetch_ok=True)

    assert _billed(store)[0]["category"] is None


def test_non_integer_settled_amount_drops_splits_instead_of_truncating(store):
    """split 只允許整數；母筆非整數時不可 int() 截斷後寫入錯誤總和。"""
    pending = {
        **PEND, "amount": 3200, "consume_currency": "USD", "consume_amount": 100.2,
    }
    store.refresh_card_pending("unbilled", [pending], rules=[])
    splits = [
        {"amount": 1600, "category": "旅遊", "note": None, "auto_excluded": False},
        {"amount": 1600, "category": "代墊", "note": None, "auto_excluded": True},
    ]
    store.conn.execute(
        "UPDATE card_pending_txns SET category='旅遊', splits_overwrite=?",
        (__import__("json").dumps(splits),),
    )
    store.conn.commit()
    store.upsert_card_billed([{
        **pending, "amount": 3267.5, "desc": "正式商戶", "bill_date": "2026-07-20",
    }], rules=[])
    store.refresh_card_pending("unbilled", [], rules=[], fetch_ok=True)

    row = store.conn.execute(
        "SELECT category, splits_overwrite FROM card_billed_txns").fetchone()
    assert row["category"] is None
    assert row["splits_overwrite"] is None
    assert _pending_count(store) == 1, "split 無法精確重算時須 fail-closed 保留 pending"


def test_fractional_split_component_blocks_adoption(store):
    """既有 split 子項若非整數不可 int() 截斷；保留 pending 等人工確認。"""
    pending = {
        **PEND, "amount": 3200, "consume_currency": "USD", "consume_amount": 100.2,
    }
    store.refresh_card_pending("unbilled", [pending], rules=[])
    splits = [
        {"amount": 1599.5, "category": "旅遊", "auto_excluded": False},
        {"amount": 1600.5, "category": "代墊", "auto_excluded": True},
    ]
    store.conn.execute(
        "UPDATE card_pending_txns SET category='旅遊', splits_overwrite=?",
        (__import__("json").dumps(splits),),
    )
    store.conn.commit()
    store.upsert_card_billed([{
        **pending, "amount": 3267, "desc": "正式商戶", "bill_date": "2026-07-20",
    }], rules=[])
    store.refresh_card_pending("unbilled", [], rules=[], fetch_ok=True)

    assert _billed(store)[0]["category"] is None
    assert _pending_count(store) == 1


def test_historical_same_identity_not_a_candidate(store):
    """候選只限本次新增 billed；歷史同卡同額交易不可讓新配對變模糊。"""
    store.upsert_card_billed([{
        **PEND, "desc": "一年前交易", "date": "2025-07-01", "bill_date": "2025-07-20",
    }], rules=[])
    # 模擬跨 sync run：舊 id 不屬於這次。
    store._new_billed_ids.clear()
    _seed_edited_pending(store)
    store.upsert_card_billed([{
        **PEND, "desc": "本次正式商戶", "bill_date": "2026-07-20",
    }], rules=[])
    store.refresh_card_pending("unbilled", [], rules=[], fetch_ok=True)

    rows = _billed(store)
    assert rows[0]["category"] is None
    assert rows[1]["category"] == "購物"


def test_pending_without_user_edits_not_adopted(store):
    """沒有 overlay 可搬的 pending 不應觸發任何 UPDATE（避免無謂寫入）。"""
    store.refresh_card_pending("unbilled", [PEND], rules=[])
    store.upsert_card_billed(
        [{**PEND, "desc": "改寫", "bill_date": "2026-07-20"}], rules=[])
    store.refresh_card_pending("unbilled", [], rules=[], fetch_ok=True)

    b = _billed(store)
    assert b[0]["category"] is None
    assert b[0]["description_overwrite"] is None
