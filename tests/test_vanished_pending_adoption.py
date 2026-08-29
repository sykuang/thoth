"""pending→billed 消失比對 (vanished-pending adoption)。

背景：四欄 key (card_no, consume_date, amount, description) 搬 overlay 只在銀行
入帳後 description 不變時有效。銀行把「暫無資訊」改寫成正式商戶名時，使用者手動
設的分類/備註/拆帳會留在 pending、billed 是白紙，且 pending 沒被 prune → UI 雙顯。

配對規則：
- 外幣用 `(card_no, consume_currency, consume_amount)`，但只接受唯一 1:1；
- 台幣只接受四欄 exact key，不使用 `(card_no, date, amount)` fuzzy identity。
候選只限同一 transaction 內剛 INSERT／touch 的 billed；歧義時不搬 overlay，
但可信 pending snapshot 已無該筆時仍刪除舊 pending。
"""
from __future__ import annotations

import pytest

from backend.core.store import BankStore, _pending_billed_identity, _rescale_splits

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


@pytest.mark.parametrize(("card_no", "date"), [
    ("", "2026-07-01"), ("   ", "2026-07-01"), ("****1234", ""),
])
def test_fx_identity_requires_nonblank_card_and_date(card_no, date):
    assert _pending_billed_identity({
        "card_no": card_no, "date": date, "amount": 1000, "currency": "TWD",
        "consume_currency": "USD", "consume_amount": 31.5,
    }) is None


@pytest.mark.parametrize("override", [
    {"card_no": "   "}, {"date": "   "}, {"desc": "   "},
])
def test_twd_exact_merge_requires_nonblank_identity_fields(store, override):
    txn = {**PEND, **override}
    store.refresh_card_pending("unbilled", [txn], rules=[])
    store.conn.execute("UPDATE card_pending_txns SET category='購物'")
    store.conn.commit()
    store.upsert_card_billed([{**txn, "bill_date": "2026-07-20"}], rules=[])

    store.refresh_card_pending("unbilled", [], rules=[], fetch_ok=True)

    assert _billed(store)[0]["category"] is None
    assert _pending_count(store) == 0


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


def test_historical_exact_billed_does_not_adopt_disappeared_pending_overlay(store):
    """Historical exact billed 不算本輪 transition；pending 消失時不搬 overlay。"""
    posted = _seed_existing_billed_then_pending(store)
    store._new_billed_ids.clear()
    store._current_billed_ids.clear()
    assert store.upsert_card_billed([posted], rules=[]) == 0
    store.refresh_card_pending("unbilled", [], rules=[], fetch_ok=True)

    billed = _billed(store)[0]
    assert billed["category"] is None
    assert billed["description_overwrite"] is None
    assert _pending_count(store) == 0


def test_existing_edited_billed_conflict_keeps_billed_and_drops_vanished_pending(store):
    """兩邊 overlay 衝突時不猜；可信清單已無 pending 就保留 billed、刪 pending。"""
    posted = _seed_existing_billed_then_pending(store, billed_category="餐飲")
    # 模擬下一次 production sync 的 fresh BankStore run-state。
    store._new_billed_ids.clear()
    store._current_billed_ids.clear()
    assert store.upsert_card_billed([posted], rules=[]) == 0
    store.refresh_card_pending("unbilled", [], rules=[], fetch_ok=True)

    assert _billed(store)[0]["category"] == "餐飲"
    assert _pending_count(store) == 0


def test_pending_refresh_invalid_split_preserves_other_overlay_fields(store):
    """split 無法重算時只丟 split；可信銀行 row 更新且其他人工 overlay 保留。"""
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
        "SELECT amount, description, category, splits_overwrite FROM card_pending_txns"
    ).fetchone()
    assert row["amount"] == 1
    assert row["description"] == "UPDATED"
    assert row["category"] == "旅遊"
    assert row["splits_overwrite"] is None


def test_pending_fx_fallback_ambiguity_rebuilds_without_guessing_overlay(store):
    """同 FX identity 多筆時不猜 overlay，但可信清單的 rows 仍須重建。"""
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
        ("NEW A", None), ("NEW B", None),
    ]


def test_pending_fx_multi_renamed_occurrences_drop_ambiguous_overlays(store):
    """多個未配對 FX incoming 沒有一對一證據；即使 overlay 等價也不靠順序分派。"""
    base = {**PEND, "amount": 3200, "consume_currency": "USD", "consume_amount": 100.2}
    store.refresh_card_pending(
        "unbilled", [{**base, "desc": "OLD A"}, {**base, "desc": "OLD B"}], rules=[])
    store.conn.execute(
        "UPDATE card_pending_txns SET category='旅遊', description_overwrite='東京行'"
    )
    store.conn.commit()

    store.refresh_card_pending(
        "unbilled", [
            {**base, "desc": "NEW A"},
            {**base, "desc": "NEW B"},
            {**base, "desc": "NEW C"},
        ], rules=[], fetch_ok=True)

    rows = store.conn.execute(
        "SELECT description, category, description_overwrite "
        "FROM card_pending_txns ORDER BY description"
    ).fetchall()
    assert [(r["description"], r["category"], r["description_overwrite"]) for r in rows] == [
        ("NEW A", None, None),
        ("NEW B", None, None),
        ("NEW C", None, None),
    ]


def test_pending_fx_exact_mapping_is_reserved_before_renamed_fallback(store):
    """Renamed row 排前面時，也要先保留整批 exact evidence，避免 input-order metadata loss。"""
    base = {**PEND, "amount": 3200, "consume_currency": "USD", "consume_amount": 100.2}
    store.refresh_card_pending(
        "unbilled", [{**base, "desc": "EXACT"}, {**base, "desc": "OLD"}], rules=[])
    ids = store.conn.execute(
        "SELECT id, description FROM card_pending_txns"
    ).fetchall()
    by_desc = {row["description"]: row["id"] for row in ids}
    store.conn.execute(
        "UPDATE card_pending_txns SET category='餐飲' WHERE id=?", (by_desc["EXACT"],))
    store.conn.execute(
        "UPDATE card_pending_txns SET category='旅遊' WHERE id=?", (by_desc["OLD"],))
    store.conn.commit()

    store.refresh_card_pending(
        "unbilled", [{**base, "desc": "RENAMED"}, {**base, "desc": "EXACT"}],
        rules=[], fetch_ok=True)

    rows = store.conn.execute(
        "SELECT description, category FROM card_pending_txns ORDER BY description"
    ).fetchall()
    assert [(row["description"], row["category"]) for row in rows] == [
        ("EXACT", "餐飲"), ("RENAMED", "旅遊"),
    ]


def test_exact_billed_merge_reserves_overlay_before_fx_fallback(store):
    """被 exact billed 吞掉的 FX pending metadata 不可再參與 renamed fallback。"""
    base = {**PEND, "amount": 3200, "consume_currency": "USD", "consume_amount": 100.2}
    store.refresh_card_pending(
        "unbilled", [{**base, "desc": "EXACT"}, {**base, "desc": "OLD"}], rules=[])
    rows = store.conn.execute(
        "SELECT id, description FROM card_pending_txns"
    ).fetchall()
    by_desc = {row["description"]: row["id"] for row in rows}
    store.conn.execute(
        "UPDATE card_pending_txns SET category='餐飲' WHERE id=?", (by_desc["EXACT"],))
    store.conn.execute(
        "UPDATE card_pending_txns SET category='旅遊' WHERE id=?", (by_desc["OLD"],))
    store.conn.commit()
    store.upsert_card_billed([
        {**base, "desc": "EXACT", "bill_date": "2026-07-20"}
    ], rules=[])

    store.refresh_card_pending(
        "unbilled", [{**base, "desc": "EXACT"}, {**base, "desc": "RENAMED"}],
        rules=[], fetch_ok=True)

    assert _billed(store)[0]["category"] == "餐飲"
    pending = store.conn.execute(
        "SELECT description, category FROM card_pending_txns"
    ).fetchone()
    assert pending is not None
    assert (pending["description"], pending["category"]) == ("RENAMED", "旅遊")


def test_upsert_billed_count_uses_returned_ids_not_total_changes(store):
    """RETURNING id 直接計新增數；pending purge 不可把 billed_new 灌大。"""
    _seed_edited_pending(store)
    assert store.upsert_card_billed([{**PEND, "bill_date": "2026-07-20"}], rules=[]) == 1
    assert store.upsert_card_billed([{**PEND, "bill_date": "2026-07-20"}], rules=[]) == 0
    store.refresh_card_pending("unbilled", [PEND], rules=[], fetch_ok=True)


def test_desc_rewritten_twd_pending_is_deleted_without_guessing_overlay(store):
    """TWD 入帳改名且 pending 消失：刪 pending，但不靠弱 identity 猜 overlay。"""
    _seed_edited_pending(store)
    store.upsert_card_billed(
        [{**PEND, "desc": "AMAZON.CO.JP TOKYO JP", "bill_date": "2026-07-20"}],
        rules=[])
    store.refresh_card_pending("unbilled", [], rules=[], fetch_ok=True)

    b = _billed(store)
    assert len(b) == 1
    assert b[0]["description"] == "AMAZON.CO.JP TOKYO JP"  # 銀行版本不被竄改
    assert b[0]["category"] is None
    assert b[0]["subcategory"] is None
    assert b[0]["description_overwrite"] is None
    assert b[0]["auto_excluded"] == 0
    assert _pending_count(store) == 0, "殘留 pending 會在 UI 上與 billed 雙顯"


def test_twd_renamed_pending_still_present_is_not_treated_as_vanished(store):
    """同卡／日／額的 TWD 改名 row 仍在 trusted snapshot 時，不可搬 overlay 到 billed。"""
    _seed_edited_pending(store)
    store.upsert_card_billed(
        [{**PEND, "desc": "BILLED NAME", "bill_date": "2026-07-20"}], rules=[])

    renamed = {**PEND, "desc": "PENDING RENAMED"}
    assert store.refresh_card_pending(
        "unbilled", [renamed], rules=[], fetch_ok=True
    ) == 1
    assert _billed(store)[0]["category"] is None
    row = store.conn.execute(
        "SELECT description FROM card_pending_txns"
    ).fetchone()
    assert row is not None and row["description"] == "PENDING RENAMED"


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


def test_fetch_failure_new_exact_billed_preserves_pending_membership(store):
    """Exact billed 新增也不能在 pending fetch 失敗時先刪來源 membership。"""
    _seed_edited_pending(store)
    store.upsert_card_billed([{**PEND, "bill_date": "2026-07-20"}], rules=[])

    assert store.refresh_card_pending(
        "unbilled", [], rules=[], fetch_ok=False
    ) == 1
    assert _pending_count(store) == 1


def test_fetch_not_ok_preserves_fx_and_unrelated_pending_membership(store):
    """Failed fetch 不做 FX adoption，也不清同 scope 的其他 pending。"""
    pending = {**PEND, "amount": 3200, "consume_currency": "USD", "consume_amount": 100.2}
    other = {**PEND, "card_no": "****9999", "amount": 50, "desc": "另一筆未入帳"}
    store.refresh_card_pending("unbilled", [pending, other], rules=[])
    store.conn.execute(
        "UPDATE card_pending_txns SET category='購物' WHERE card_no='****1234'"
    )
    store.conn.commit()
    store.upsert_card_billed([{
        **pending, "amount": 3267, "desc": "正式商戶", "bill_date": "2026-07-20",
    }], rules=[])

    assert store.refresh_card_pending(
        "unbilled", [], rules=[], fetch_ok=False
    ) == 2
    assert _billed(store)[0]["category"] is None
    remaining = store.conn.execute(
        "SELECT card_no, category FROM card_pending_txns ORDER BY card_no"
    ).fetchall()
    assert [(r["card_no"], r["category"]) for r in remaining] == [
        ("****1234", "購物"), ("****9999", None),
    ]


def test_ambiguous_vanished_rows_are_deleted_by_authoritative_pending_snapshot(store):
    """不能唯一合併時不猜 metadata；可信清單已無該筆就刪除舊 pending。"""
    _seed_edited_pending(store, (PEND, {**PEND, "desc": "同日同額另一筆"}))
    store.upsert_card_billed(
        [{**PEND, "desc": "改寫A", "bill_date": "2026-07-20"}], rules=[])

    store.refresh_card_pending("unbilled", [], rules=[], fetch_ok=True)

    assert _billed(store)[0]["category"] is None
    assert _pending_count(store) == 0


def test_multiple_vanished_same_card_amount_are_not_merged_but_are_removed(store):
    """同卡同額消失多筆時不搬 overlay；scope 仍依可信清單重建。"""
    _seed_edited_pending(store, (PEND, {**PEND, "desc": "同日同額另一筆"}))
    store.upsert_card_billed(
        [{**PEND, "desc": "改寫A", "bill_date": "2026-07-20"}], rules=[])
    store.refresh_card_pending(
        "unbilled", [{**PEND, "date": "2026-07-09", "desc": "還在的別筆",
                      "amount": 77}], rules=[], fetch_ok=True)

    assert _billed(store)[0]["category"] is None
    remaining = store.conn.execute(
        "SELECT description FROM card_pending_txns WHERE scope='unbilled'"
    ).fetchall()
    assert [row["description"] for row in remaining] == ["還在的別筆"]


def test_twd_renamed_identities_are_deleted_without_overlay_guessing(store):
    """TWD 改名沒有穩定 ID；可信消失可刪 rows，但任何 identity 都不搬 overlay。"""
    safe = {**PEND, "desc": "SAFE PENDING"}
    ambiguous = {**PEND, "card_no": "****9999", "amount": 2000, "desc": "OLD A"}
    _seed_edited_pending(store, (safe, ambiguous, {**ambiguous, "desc": "OLD B"}))
    store.upsert_card_billed([
        {**safe, "desc": "SAFE SETTLED", "bill_date": "2026-07-20"},
        {**ambiguous, "desc": "AMBIGUOUS SETTLED", "bill_date": "2026-07-20"},
    ], rules=[])

    store.refresh_card_pending("unbilled", [], rules=[], fetch_ok=True)

    rows = {row["description"]: row for row in _billed(store)}
    assert rows["SAFE SETTLED"]["category"] is None
    assert rows["AMBIGUOUS SETTLED"]["category"] is None
    assert _pending_count(store) == 0


def test_ambiguous_billed_candidates_skip_merge_but_drop_vanished_pending(store):
    """候選 billed 同卡同額有兩筆時不搬 overlay，但可信消失仍刪 pending。"""
    _seed_edited_pending(store)
    store.upsert_card_billed([
        {**PEND, "desc": "候選一", "bill_date": "2026-07-20"},
        {**PEND, "desc": "候選二", "bill_date": "2026-07-20"},
    ], rules=[])
    store.refresh_card_pending("unbilled", [], rules=[], fetch_ok=True)

    assert all(r["category"] is None for r in _billed(store))
    assert _pending_count(store) == 0


def test_auto_categorized_renamed_twd_billed_keeps_rule_category(store):
    """TWD 改名 pending 不可安全搬；新 billed 保留自己的 rule category。"""
    _seed_edited_pending(store)
    rules = [{"pattern": "正式商戶", "category": "餐飲", "subcategory": "其他"}]
    store.upsert_card_billed(
        [{**PEND, "desc": "正式商戶", "bill_date": "2026-07-20"}], rules=rules)
    store.refresh_card_pending("unbilled", [], rules=rules, fetch_ok=True)

    row = _billed(store)[0]
    assert row["category"] == "餐飲"
    assert row["subcategory"] == "其他"
    assert row["description_overwrite"] is None
    assert row["auto_excluded"] == 0


def test_same_identity_unedited_sibling_skips_merge_but_drops_vanished_rows(store):
    """未編輯 sibling 也使 identity 歧義；不搬 overlay但依可信清單刪除。"""
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
    assert _pending_count(store) == 0


def test_two_prior_exact_occurrences_do_not_merge_into_one_billed(store):
    """先前有兩筆同 key occurrence 時不具唯一性；可信來源剩一筆就保留一筆。"""
    _seed_edited_pending(store, (PEND, PEND))
    store.upsert_card_billed([{**PEND, "bill_date": "2026-07-20"}], rules=[])

    reported = store.refresh_card_pending("unbilled", [PEND], rules=[], fetch_ok=True)

    assert reported == 1
    assert _billed(store)[0]["category"] is None
    rows = store.conn.execute(
        "SELECT category, description_overwrite FROM card_pending_txns"
    ).fetchall()
    assert [(row["category"], row["description_overwrite"]) for row in rows] == [
        ("購物", "老婆的生日禮物"),
    ]


def test_historical_billed_does_not_consume_new_distinct_exact_pending(store):
    """無本輪 transition 時，historical billed 不得吞掉銀行新回的一筆 exact occurrence。"""
    store.upsert_card_billed([{**PEND, "bill_date": "2026-07-20"}], rules=[])
    store.commit()
    store._new_billed_ids.clear()
    store._current_billed_ids.clear()
    store.upsert_card_billed([{**PEND, "bill_date": "2026-07-20"}], rules=[])

    assert store.refresh_card_pending(
        "unbilled", [PEND], rules=[], fetch_ok=True
    ) == 1
    assert _pending_count(store) == 1


def test_legacy_fetch_none_does_not_exact_merge(store):
    """未聲明 fetch 成功的 legacy refresh 不得消耗 exact pending。"""
    _seed_edited_pending(store)
    store.upsert_card_billed([{**PEND, "bill_date": "2026-07-20"}], rules=[])

    assert store.refresh_card_pending("unbilled", [PEND], rules=[]) == 1
    assert _pending_count(store) == 1
    assert _billed(store)[0]["category"] is None


def test_multiple_new_exact_billed_rows_do_not_adopt_or_merge(store):
    """B>1 不是唯一 transition；不搬 overlay，也不 suppress pending。"""
    _seed_edited_pending(store)
    store.upsert_card_billed([
        {**PEND, "bill_date": "2026-07-20", "post_date": "2026-07-10"},
        {**PEND, "bill_date": "2026-08-20", "post_date": "2026-08-10"},
    ], rules=[])

    assert store.refresh_card_pending(
        "unbilled", [PEND], rules=[], fetch_ok=True
    ) == 1
    assert all(row["category"] is None for row in _billed(store))
    assert _pending_count(store) == 1


def test_existing_plus_new_exact_billed_rows_do_not_pass_one_to_one_gate(store):
    """本輪 touch 一筆 historical 並新增一筆同 key 時，實際 B=2，不能 merge。"""
    _seed_edited_pending(store)
    first = {**PEND, "bill_date": "2026-07-20", "post_date": "2026-07-10"}
    second = {**PEND, "bill_date": "2026-08-20", "post_date": "2026-08-10"}
    store.upsert_card_billed([first], rules=[])
    store.commit()
    store._new_billed_ids.clear()
    store._current_billed_ids.clear()
    store.upsert_card_billed([first, second], rules=[])

    assert store.refresh_card_pending(
        "unbilled", [PEND], rules=[], fetch_ok=True
    ) == 1
    assert all(row["category"] is None for row in _billed(store))
    assert _pending_count(store) == 1


def test_ambiguous_exact_occurrences_do_not_merge(store):
    """兩筆衝突 overlay 配一筆 billed 不具唯一性；兩筆 pending 依可信清單保留。"""
    store.refresh_card_pending("unbilled", [PEND, PEND], rules=[])
    ids = [row["id"] for row in store.conn.execute(
        "SELECT id FROM card_pending_txns ORDER BY id"
    ).fetchall()]
    store.conn.execute("UPDATE card_pending_txns SET category='餐飲' WHERE id=?", (ids[0],))
    store.conn.execute("UPDATE card_pending_txns SET category='旅遊' WHERE id=?", (ids[1],))
    store.conn.commit()
    store.upsert_card_billed([{**PEND, "bill_date": "2026-07-20"}], rules=[])

    assert store.refresh_card_pending(
        "unbilled", [PEND, PEND], rules=[], fetch_ok=True
    ) == 2

    rows = store.conn.execute("SELECT category FROM card_pending_txns").fetchall()
    assert {row["category"] for row in rows} == {"餐飲", "旅遊"}

    store._new_billed_ids.clear()
    store._current_billed_ids.clear()
    store.upsert_card_billed([{**PEND, "bill_date": "2026-07-20"}], rules=[])
    assert store.refresh_card_pending(
        "unbilled", [PEND, PEND], rules=[], fetch_ok=True
    ) == 2


def test_refresh_exact_key_conflicting_occurrence_rebuilds_authoritative_count(store):
    """同 exact key 兩筆減為一筆時不猜 overlay；可信清單的一筆數量仍是權威。"""
    store.refresh_card_pending("unbilled", [PEND, PEND], rules=[])
    ids = [row["id"] for row in store.conn.execute(
        "SELECT id FROM card_pending_txns ORDER BY id"
    ).fetchall()]
    assert len(ids) == 2
    store.conn.execute("UPDATE card_pending_txns SET category='餐飲' WHERE id=?", (ids[0],))
    store.conn.execute("UPDATE card_pending_txns SET category='旅遊' WHERE id=?", (ids[1],))
    store.conn.commit()
    store.upsert_card_billed([{**PEND, "bill_date": "2026-07-20"}], rules=[])

    reported = store.refresh_card_pending("unbilled", [PEND], rules=[], fetch_ok=True)

    assert reported == 1
    billed = store.conn.execute("SELECT COUNT(*) AS n FROM card_billed_txns").fetchone()
    assert billed is not None and billed["n"] == 1
    rows = store.conn.execute(
        "SELECT category FROM card_pending_txns"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["category"] is None

    # 下一個 production-shaped sync 只 touch existing billed；同一已保留 occurrence
    # 不可被 historical billed 再消耗一次。
    store._new_billed_ids.clear()
    store._current_billed_ids.clear()
    store.upsert_card_billed([{**PEND, "bill_date": "2026-07-20"}], rules=[])
    assert store.refresh_card_pending(
        "unbilled", [PEND], rules=[], fetch_ok=True
    ) == 1
    assert _pending_count(store) == 1


def test_next_sync_existing_billed_does_not_guess_twd_renamed_overlay(store):
    """首輪 fetch 失敗保留 pending；次輪可信消失只刪除，不做 TWD fuzzy adoption。"""
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
    assert _billed(store)[0]["category"] is None


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
    assert store.refresh_card_pending(
        "unbilled", [PEND], rules=[], fetch_ok=True, commit=False
    ) == 1
    assert store.refresh_card_pending(
        "current", [PEND], rules=[], fetch_ok=True
    ) == 1

    assert _billed(store)[0]["category"] is None
    rows = store.conn.execute(
        "SELECT scope, category FROM card_pending_txns ORDER BY scope"
    ).fetchall()
    assert [(r["scope"], r["category"]) for r in rows] == [
        ("current", "餐飲"), ("unbilled", "旅遊"),
    ]


def test_vanished_conflicting_cross_scope_overlays_never_become_unique_by_order(store):
    """第一個 scope 消失後，第二個 scope 也不可把原本衝突的 overlay 搬到 billed。"""
    store.refresh_card_pending("unbilled", [PEND], rules=[])
    store.refresh_card_pending("current", [PEND], rules=[])
    store.conn.execute(
        "UPDATE card_pending_txns SET category='旅遊' WHERE scope='unbilled'")
    store.conn.execute(
        "UPDATE card_pending_txns SET category='餐飲' WHERE scope='current'")
    store.conn.commit()
    store.upsert_card_billed([
        {**PEND, "desc": "RENAMED SHOP", "bill_date": "2026-07-20"}
    ], rules=[])

    assert store.refresh_card_pending(
        "unbilled", [], rules=[], fetch_ok=True, commit=False
    ) == 0
    assert store.refresh_card_pending(
        "current", [], rules=[], fetch_ok=True
    ) == 0
    assert _billed(store)[0]["category"] is None


def test_different_desc_cross_scope_conflict_cannot_overwrite_exact_merge(store):
    """同 local identity 不同 desc 的跨 scope conflict，不可在後一個 scope 覆寫 billed。"""
    other = {**PEND, "desc": "OTHER PENDING"}
    store.refresh_card_pending("unbilled", [PEND], rules=[])
    store.refresh_card_pending("current", [other], rules=[])
    store.conn.execute(
        "UPDATE card_pending_txns SET category='旅遊' WHERE scope='unbilled'")
    store.conn.execute(
        "UPDATE card_pending_txns SET category='餐飲' WHERE scope='current'")
    store.conn.commit()
    store.upsert_card_billed([{**PEND, "bill_date": "2026-07-20"}], rules=[])

    assert store.refresh_card_pending(
        "unbilled", [PEND], rules=[], fetch_ok=True, commit=False
    ) == 0
    assert store.refresh_card_pending(
        "current", [], rules=[], fetch_ok=True
    ) == 0
    assert _billed(store)[0]["category"] == "旅遊"


def test_fx_adoption_uses_incoming_raw_key_and_does_not_reinsert_renamed_row(store):
    """FX overlap merge 後 ledger 必須 suppress trusted incoming renamed row。"""
    old = {**PEND, "amount": 3200, "consume_currency": "USD", "consume_amount": 100.2}
    store.refresh_card_pending("unbilled", [old], rules=[])
    store.conn.execute("UPDATE card_pending_txns SET category='旅遊'")
    store.conn.commit()
    store.upsert_card_billed([{
        **old, "amount": 3267, "desc": "SETTLED", "bill_date": "2026-07-20",
    }], rules=[])
    incoming = {**old, "desc": "RENAMED PENDING"}

    assert store.refresh_card_pending(
        "unbilled", [incoming], rules=[], fetch_ok=True
    ) == 0
    assert _pending_count(store) == 0
    assert _billed(store)[0]["category"] == "旅遊"


def test_fx_multi_incoming_identity_does_not_adopt_to_one_billed(store):
    """一個 old overlay + 一個 billed + 兩個 incoming FX occurrences 不可 merge。"""
    old = {**PEND, "amount": 3200, "consume_currency": "USD", "consume_amount": 100.2}
    store.refresh_card_pending("unbilled", [old], rules=[])
    store.conn.execute("UPDATE card_pending_txns SET category='旅遊'")
    store.conn.commit()
    store.upsert_card_billed([{
        **old, "amount": 3267, "desc": "SETTLED", "bill_date": "2026-07-20",
    }], rules=[])

    assert store.refresh_card_pending("unbilled", [
        {**old, "desc": "RENAMED A"}, {**old, "desc": "RENAMED B"},
    ], rules=[], fetch_ok=True) == 2
    assert _billed(store)[0]["category"] is None
    rows = store.conn.execute(
        "SELECT category FROM card_pending_txns ORDER BY id"
    ).fetchall()
    assert [row["category"] for row in rows] == [None, None]


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
    assert store.refresh_card_pending(
        "unbilled", [pending], rules=[], fetch_ok=True, commit=False
    ) == 0
    assert store.refresh_card_pending(
        "current", [pending], rules=[], fetch_ok=True
    ) == 0

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


def test_unchanged_exact_pending_transfers_overlay_when_snapshot_is_empty(store):
    """Strict exact 1:1 也涵蓋 incoming=0：先搬 overlay，再依可信空清單刪 pending。"""
    _seed_edited_pending(store)
    store.upsert_card_billed([{**PEND, "bill_date": "2026-07-20"}], rules=[])

    assert store.refresh_card_pending(
        "unbilled", [], rules=[], fetch_ok=True
    ) == 0
    billed = _billed(store)[0]
    assert billed["category"] == "購物"
    assert billed["description_overwrite"] == "老婆的生日禮物"
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


def test_subcategory_only_twd_renamed_overlay_is_not_guessed(store):
    """即使只有 subcategory，TWD 改名後仍無穩定 ID，不可 fuzzy 搬移。"""
    store.refresh_card_pending("unbilled", [PEND], rules=[])
    store.conn.execute("UPDATE card_pending_txns SET subcategory='電商'")
    store.conn.commit()
    store.upsert_card_billed(
        [{**PEND, "desc": "正式商戶", "bill_date": "2026-07-20"}], rules=[])
    store.refresh_card_pending("unbilled", [], rules=[], fetch_ok=True)

    assert _billed(store)[0]["subcategory"] is None
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


def test_non_integer_settled_amount_drops_only_invalid_split(store):
    """母筆非整數時不截斷 split；其餘 overlay 搬移，可信消失刪 pending。"""
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
    assert row["category"] == "旅遊"
    assert row["splits_overwrite"] is None
    assert _pending_count(store) == 0


def test_fractional_split_component_drops_only_invalid_split(store):
    """既有 split 子項非整數時只丟 split；其餘 overlay 搬移並刪 vanished pending。"""
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

    row = store.conn.execute(
        "SELECT category, splits_overwrite FROM card_billed_txns"
    ).fetchone()
    assert row["category"] == "旅遊"
    assert row["splits_overwrite"] is None
    assert _pending_count(store) == 0


def test_historical_same_identity_does_not_enable_twd_fuzzy_adoption(store):
    """不論歷史候選，TWD 改名本身就不足以搬 overlay。"""
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
    assert rows[1]["category"] is None


def test_pending_without_user_edits_not_adopted(store):
    """沒有 overlay 可搬的 pending 不應觸發任何 UPDATE（避免無謂寫入）。"""
    store.refresh_card_pending("unbilled", [PEND], rules=[])
    store.upsert_card_billed(
        [{**PEND, "desc": "改寫", "bill_date": "2026-07-20"}], rules=[])
    store.refresh_card_pending("unbilled", [], rules=[], fetch_ok=True)

    b = _billed(store)
    assert b[0]["category"] is None
    assert b[0]["description_overwrite"] is None
