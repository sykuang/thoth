"""db_facade PoC contract tests — Plan B prototype validation.

Plan B (2026-06-19): 3 ops cards use case PoC. Verify:
  1. typed Pydantic result (not raw Row / dict)
  2. bank string in, conn lifecycle internal
  3. transaction context manager commits/rollbacks
  4. domain errors (CardNotFound) instead of leaking sqlite/HTTPException
  5. zero raw SQL escapes to test layer (test only touches db_facade)
"""

from __future__ import annotations


import pytest

from backend.server.db_facade import (
    BankNotAvailable,
    BilledTxnRow,
    CardDetail,
    CardNotFound,
    CardSummary,
    Database,
    PaymentRow,
    PendingTxnRow,
    SetCardExcludedResult,
    SetCardNicknameResult,
)


# ============================================================
# fixtures — minimal in-memory bank con (reuse open_bank_conn via monkeypatch)
# ============================================================


@pytest.fixture
def mem_bank_con():
    """In-memory SQLite con seeded with cards + bill txns. Monkeypatch
    open_bank_conn so db_facade gets this con back."""
    import sqlite3
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    # cards table (full schema incl. nickname_overwrite)
    con.execute("""
        CREATE TABLE cards (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            card_no TEXT NOT NULL,
            name TEXT,
            nickname_overwrite TEXT,
            association TEXT,
            type TEXT,
            is_cube INTEGER,
            excluded INTEGER DEFAULT 0,
            active INTEGER DEFAULT 1,
            credit_limit REAL,
            used_credit REAL,
            statement_close_date TEXT,
            payment_due_date TEXT,
            updated_at TEXT
        )
    """)
    con.execute("""
        CREATE TABLE card_billed_txns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            card_no TEXT,
            bill_date TEXT,
            consume_date TEXT,
            post_date TEXT,
            amount REAL,
            description TEXT,
            currency TEXT,
            txn_type TEXT,
            flow_type TEXT
        )
    """)
    con.execute("""
        CREATE TABLE card_pending_txns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            card_no TEXT,
            consume_date TEXT,
            amount REAL,
            description TEXT,
            currency TEXT
        )
    """)
    # seed
    con.executemany(
        "INSERT INTO cards (user_id, card_no, name, association, type, "
        "credit_limit, used_credit, statement_close_date, payment_due_date, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (1, "CARD-A", "聯邦旅人卡", "VISA", "credit", 300000, 41065,
             "2026-06-30", "2026-07-15", "2026-06-19T10:00:00Z"),
            (1, "CARD-B", "Cashback 卡", "MasterCard", "credit", 200000, 5000,
             "2026-06-25", "2026-07-10", "2026-06-19T10:00:00Z"),
            # cross-tenant — must not leak
            (2, "OTHER", "別人的卡", "VISA", "credit", 100000, 0, None, None, None),
        ],
    )
    con.executemany(
        "INSERT INTO card_billed_txns (user_id, card_no, bill_date, consume_date, post_date, amount, description, currency, txn_type, flow_type) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (1, "CARD-A", "2026-06-15", "2026-06-10", "2026-06-11", 1000.0, "蝦皮購物", "TWD", "consume", "expense"),
            (1, "CARD-A", "2026-06-15", "2026-06-12", "2026-06-13", 500.0, "全家", "TWD", "consume", "expense"),
            (1, "CARD-A", "2026-06-15", "2026-06-14", "2026-06-15", -800.0, "自動扣繳", "TWD", "payment", "transfer"),  # last payment
        ],
    )
    con.executemany(
        "INSERT INTO card_pending_txns (user_id, card_no, consume_date, amount, description, currency) VALUES (?, ?, ?, ?, ?, ?)",
        [
            (1, "CARD-A", "2026-06-18", 150.0, "7-11", "TWD"),
            (1, "CARD-A", "2026-06-17", 250.0, "麥當勞", "TWD"),
            (1, "CARD-B", "2026-06-18", 80.0, "全聯", "TWD"),
        ],
    )
    con.commit()
    yield con
    con.close()


@pytest.fixture
def db(mem_bank_con, monkeypatch):
    """Database instance with open_bank_conn monkeypatched to return our mem con.

    IMPORTANT: monkeypatch returns the SAME con each call but never closes it
    (caller calls con.close() inside methods — we wrap to no-op for testing).
    """
    class _ConWrapper:
        def __init__(self, real):
            self._real = real
        def __getattr__(self, k):
            return getattr(self._real, k)
        def close(self):
            pass  # don't actually close the test con
        def commit(self):
            self._real.commit()
        def rollback(self):
            self._real.rollback()
        def execute(self, *a, **kw):
            return self._real.execute(*a, **kw)

    wrapper = _ConWrapper(mem_bank_con)

    def _fake_open(_bank: str):
        return wrapper

    monkeypatch.setattr("backend.server.db.open_bank_conn", _fake_open)
    # bank_data.has_table / .columns 走 sqlite_master / PRAGMA — 對 in-memory 通用
    return Database()


# ============================================================
# Decision 1: typed Pydantic result
# ============================================================


def test_list_cards_returns_typed_pydantic_models(db):
    """caller 拿到的不是 dict 不是 Row — 是 CardSummary BaseModel."""
    cards = db.list_cards(bank="hsbc", user_id=1)
    assert all(isinstance(c, CardSummary) for c in cards)
    # 不會洩漏 sqlite3.Row / dict
    assert not any(isinstance(c, dict) for c in cards)


def test_list_cards_returns_business_only_fields(db):
    """CardSummary.model_dump() 應該是純 dict, 沒有 con/cur/row 物件."""
    cards = db.list_cards(bank="hsbc", user_id=1)
    for c in cards:
        d = c.model_dump()
        for v in d.values():
            assert v is None or isinstance(v, str | int | float | bool), \
                f"非 primitive: {v!r}"


def test_list_cards_excludes_cross_tenant(db):
    """user 1 看不到 user 2 的卡 (跨 user 防護內建)."""
    cards = db.list_cards(bank="hsbc", user_id=1)
    nos = {c.card_no for c in cards}
    assert "OTHER" not in nos
    assert {"CARD-A", "CARD-B"}.issubset(nos)


def test_list_cards_requires_native_remaining_due_but_keeps_other_summary(db):
    cards = {c.card_no: c for c in db.list_cards(bank="hsbc", user_id=1)}
    assert cards["CARD-A"].bill_due_amount is None
    # CARD-A: pending 150+250 = 400
    assert cards["CARD-A"].unbilled_amount == 400.0
    # CARD-A: payment effective date uses post_date 6/15, abs(-800) = 800
    assert cards["CARD-A"].last_payment_date == "2026-06-15"
    assert cards["CARD-A"].last_payment_amount == 800.0
    assert cards["CARD-B"].bill_due_amount is None
    assert cards["CARD-B"].unbilled_amount == 80.0
    assert cards["CARD-B"].last_payment_date is None


def test_get_card_returns_card_summary(db):
    """單張 get_card 同樣回 CardSummary, 不是 dict."""
    c = db.get_card(bank="hsbc", user_id=1, card_no="CARD-A")
    assert isinstance(c, CardSummary)
    assert c.card_no == "CARD-A"
    assert c.name == "聯邦旅人卡"
    assert c.bill_due_amount is None


def test_get_card_returns_none_for_missing(db):
    """找不到 → None, 沒 raise."""
    c = db.get_card(bank="hsbc", user_id=1, card_no="NOSUCHCARD")
    assert c is None


def test_get_card_returns_none_for_cross_tenant(db):
    """user 1 查 user 2 的卡 → None (cross-tenant 防護)."""
    c = db.get_card(bank="hsbc", user_id=1, card_no="OTHER")
    assert c is None


# ============================================================
# Decision 2: bank string in, conn lifecycle internal
# ============================================================


def test_caller_never_sees_connection_or_cursor(db):
    """list_cards / get_card 簽名不接 con / cur, 內部自己處理."""
    import inspect
    for method_name in ["list_cards", "get_card"]:
        sig = inspect.signature(getattr(db, method_name))
        params = list(sig.parameters.keys())
        assert "con" not in params
        assert "cur" not in params
        assert "conn" not in params
        assert "bank" in params, f"{method_name} 必須接 bank 字串"


# ============================================================
# Decision 3: transaction context manager
# ============================================================


def test_transaction_commits_on_exit(db, mem_bank_con):
    """正常退出 → commit, 變動入 DB."""
    with db.transaction(bank="hsbc") as tx:
        result = tx.set_card_excluded(user_id=1, card_no="CARD-A", excluded=True)
        assert isinstance(result, SetCardExcludedResult)
        assert result.excluded is True
        assert result.card_no == "CARD-A"

    # verify persistent
    row = mem_bank_con.execute(
        "SELECT excluded FROM cards WHERE card_no = ? AND user_id = ?",
        ("CARD-A", 1),
    ).fetchone()
    assert row["excluded"] == 1


def test_transaction_rolls_back_on_exception(db, mem_bank_con):
    """raise 之後 → rollback, DB 沒被改."""
    with pytest.raises(RuntimeError), db.transaction(bank="hsbc") as tx:
        tx.set_card_excluded(user_id=1, card_no="CARD-A", excluded=True)
        raise RuntimeError("test rollback")

    # original value preserved
    row = mem_bank_con.execute(
        "SELECT excluded FROM cards WHERE card_no = ? AND user_id = ?",
        ("CARD-A", 1),
    ).fetchone()
    assert row["excluded"] == 0


def test_transaction_set_card_excluded_returns_typed_result(db):
    with db.transaction(bank="hsbc") as tx:
        r = tx.set_card_excluded(user_id=1, card_no="CARD-A", excluded=True)
    assert isinstance(r, SetCardExcludedResult)
    assert r.bank == "hsbc"
    assert r.card_no == "CARD-A"
    assert r.excluded is True
    assert r.updated_at.endswith("Z")


# ============================================================
# Decision 4: domain exceptions (CardNotFound, not HTTPException)
# ============================================================


def test_set_card_excluded_unknown_card_raises_card_not_found(db):
    """卡不存在 → 拋 CardNotFound (domain exception), 不是 HTTPException."""
    with pytest.raises(CardNotFound) as excinfo, db.transaction(bank="hsbc") as tx:
        tx.set_card_excluded(user_id=1, card_no="NOSUCHCARD", excluded=True)
    assert excinfo.value.bank == "hsbc"
    assert excinfo.value.card_no == "NOSUCHCARD"


def test_set_card_excluded_cross_tenant_raises_card_not_found(db):
    """user 1 改 user 2 的卡 → CardNotFound (不能洩漏卡確實存在的事實)."""
    with pytest.raises(CardNotFound), db.transaction(bank="hsbc") as tx:
        tx.set_card_excluded(user_id=1, card_no="OTHER", excluded=True)


# ============================================================
# Architecture invariant: test layer touches no raw SQL types
# ============================================================


def test_db_facade_module_has_no_route_or_router_dep():
    """db_facade 不該 import fastapi / 拋 HTTPException / 直接 import sqlite3/psycopg.

    只看 import 跟實際 call site, 不誤判 docstring 提到 keyword.
    """
    import ast
    from backend.server import db_facade
    tree = ast.parse(__import__("inspect").getsource(db_facade))

    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            imported.add(mod)
            for alias in node.names:
                imported.add(f"{mod}.{alias.name}")

    forbidden = {"fastapi", "sqlite3", "psycopg", "psycopg2"}
    bad = forbidden & imported
    # also catch fastapi.* / psycopg.* prefix matches
    bad |= {i for i in imported if any(i == f or i.startswith(f + ".") for f in forbidden)}
    assert not bad, f"db_facade import 禁忌 module: {bad}"

    # check no raise HTTPException(...) call site
    for node in ast.walk(tree):
        if isinstance(node, ast.Raise) and node.exc is not None:
            exc = node.exc
            name = None
            if isinstance(exc, ast.Call) and isinstance(exc.func, ast.Name):
                name = exc.func.id
            elif isinstance(exc, ast.Name):
                name = exc.id
            assert name != "HTTPException", \
                "db_facade 禁 raise HTTPException — 用 domain exception"


def test_pydantic_models_locked_to_extra_forbid():
    """所有 Pydantic models 都要 extra='forbid' — 除了跨表 raw row wrappers.

    白名單 (extra='allow' OK):
      TxnRow / TxnUpdateResult — 三表 schema 不同(hsbc 有 flow_type, cathay
        有 acquirer_descriptor, dbs 有 isDisplayImg), caller transform 端
        需要拿完整 raw row, 不能 forbid.
    """
    import inspect

    from pydantic import BaseModel as _BaseModel

    from backend.server import db_facade
    ALLOW_EXTRA = {"TxnRow", "TxnUpdateResult"}
    models = [
        obj for _, obj in inspect.getmembers(db_facade)
        if inspect.isclass(obj) and issubclass(obj, _BaseModel) and obj is not _BaseModel
    ]
    assert models, "db_facade 應該至少有 1 個 Pydantic model"
    for cls in models:
        expected = "allow" if cls.__name__ in ALLOW_EXTRA else "forbid"
        assert cls.model_config.get("extra") == expected, \
            f"{cls.__name__}: 預期 extra='{expected}', 實際 '{cls.model_config.get('extra')}'"


# ============================================================
# B1: get_card_detail (含 billed/pending/payments)
# ============================================================


def test_get_card_detail_returns_typed_card_detail(db):
    """get_card_detail 回 CardDetail (含 typed billed_txns / pending_txns / payments)."""
    detail = db.get_card_detail(
        bank="hsbc", user_id=1, card_no="CARD-A", cycle_start="2026-06-01",
    )
    assert isinstance(detail, CardDetail)
    assert isinstance(detail.card, CardSummary)
    assert detail.card.card_no == "CARD-A"
    # 本期 (consume_date >= 2026-06-01) 全部 3 筆 (含 payment)
    assert len(detail.billed_txns) == 3
    assert all(isinstance(t, BilledTxnRow) for t in detail.billed_txns)
    # pending 2 筆
    assert len(detail.pending_txns) == 2
    assert all(isinstance(t, PendingTxnRow) for t in detail.pending_txns)
    # payments 只取 txn_type='payment' 的 1 筆
    assert len(detail.payments) == 1
    assert all(isinstance(p, PaymentRow) for p in detail.payments)
    assert detail.payments[0].date == "2026-06-15"
    assert detail.payments[0].amount == 800.0  # abs(-800)


def test_get_card_detail_filters_by_cycle_start(db):
    """沒有 cycle_end 時，cycle_start 之後的 billed_txns 才算查詢期間。"""
    # cycle_start 設超晚 → 沒 billed
    detail = db.get_card_detail(
        bank="hsbc", user_id=1, card_no="CARD-A", cycle_start="2026-12-01",
    )
    assert detail is not None
    assert detail.billed_txns == []
    # pending 不受 cycle_start 影響 (intentional — 整批顯示)
    assert len(detail.pending_txns) == 2


def test_get_card_detail_returns_none_for_missing_card(db):
    detail = db.get_card_detail(
        bank="hsbc", user_id=1, card_no="NOSUCHCARD", cycle_start="2026-06-01",
    )
    assert detail is None


def test_get_card_detail_excludes_cross_tenant(db):
    """user 1 查 user 2 的卡 → None."""
    detail = db.get_card_detail(
        bank="hsbc", user_id=1, card_no="OTHER", cycle_start="2026-06-01",
    )
    assert detail is None


def test_get_card_detail_billed_txn_has_business_fields(db):
    """BilledTxnRow 欄位都是 caller-friendly primitive."""
    detail = db.get_card_detail(
        bank="hsbc", user_id=1, card_no="CARD-A", cycle_start="2026-06-01",
    )
    # 找 consume 那筆
    consume_rows = [t for t in detail.billed_txns if t.txn_type == "consume"]
    assert len(consume_rows) == 2
    row = consume_rows[0]
    # 純 primitive, 沒 row/cur
    d = row.model_dump()
    for v in d.values():
        assert v is None or isinstance(v, str | int | float | bool)


# ============================================================
# B1: list_excluded_card_nos_all_banks
# ============================================================


def test_list_excluded_card_nos_all_banks_returns_per_bank_set(db, mem_bank_con):
    """先把 CARD-A excluded, 然後驗證 list 出來."""
    mem_bank_con.execute(
        "UPDATE cards SET excluded = 1 WHERE card_no = ? AND user_id = ?",
        ("CARD-A", 1),
    )
    mem_bank_con.commit()
    out = db.list_excluded_card_nos_all_banks(user_id=1, banks=["hsbc"])
    assert out == {"hsbc": {"CARD-A"}}


def test_list_excluded_card_nos_all_banks_skips_banks_with_no_excluded(db, mem_bank_con):
    """沒 excluded 的 bank 不出現在回傳 dict."""
    out = db.list_excluded_card_nos_all_banks(user_id=1, banks=["hsbc"])
    # 沒人標 excluded → empty
    assert out == {}


def test_list_excluded_card_nos_all_banks_excludes_cross_tenant(db, mem_bank_con):
    """user 1 list 不包 user 2 的 excluded 卡."""
    mem_bank_con.execute(
        "UPDATE cards SET excluded = 1 WHERE user_id = ?", (2,),
    )
    mem_bank_con.commit()
    out = db.list_excluded_card_nos_all_banks(user_id=1, banks=["hsbc"])
    assert out == {}  # user 2 的 OTHER 不該在 user 1 list 裡


# ============================================================
# B1: set_card_nickname
# ============================================================


def test_transaction_set_card_nickname_returns_typed_result(db, mem_bank_con):
    with db.transaction(bank="hsbc") as tx:
        r = tx.set_card_nickname(user_id=1, card_no="CARD-A", nickname_overwrite="主力卡")
    assert isinstance(r, SetCardNicknameResult)
    assert r.bank == "hsbc"
    assert r.card_no == "CARD-A"
    assert r.nickname_overwrite == "主力卡"
    assert r.updated_at.endswith("Z")
    # verify persistent
    row = mem_bank_con.execute(
        "SELECT nickname_overwrite FROM cards WHERE card_no = ? AND user_id = ?",
        ("CARD-A", 1),
    ).fetchone()
    assert row["nickname_overwrite"] == "主力卡"


def test_set_card_nickname_empty_string_clears_to_null(db, mem_bank_con):
    """'' / None → SQL 寫 NULL (恢復顯示 cards.name)."""
    # 先設一個值
    with db.transaction(bank="hsbc") as tx:
        tx.set_card_nickname(user_id=1, card_no="CARD-A", nickname_overwrite="主力卡")
    # 再用空字串清掉
    with db.transaction(bank="hsbc") as tx:
        r = tx.set_card_nickname(user_id=1, card_no="CARD-A", nickname_overwrite="")
    assert r.nickname_overwrite is None
    row = mem_bank_con.execute(
        "SELECT nickname_overwrite FROM cards WHERE card_no = ? AND user_id = ?",
        ("CARD-A", 1),
    ).fetchone()
    assert row["nickname_overwrite"] is None


def test_set_card_nickname_none_clears_to_null(db, mem_bank_con):
    with db.transaction(bank="hsbc") as tx:
        tx.set_card_nickname(user_id=1, card_no="CARD-A", nickname_overwrite="X")
    with db.transaction(bank="hsbc") as tx:
        r = tx.set_card_nickname(user_id=1, card_no="CARD-A", nickname_overwrite=None)
    assert r.nickname_overwrite is None


def test_set_card_nickname_unknown_card_raises_card_not_found(db):
    with pytest.raises(CardNotFound), db.transaction(bank="hsbc") as tx:
        tx.set_card_nickname(user_id=1, card_no="NOSUCHCARD", nickname_overwrite="X")


def test_set_card_nickname_cross_tenant_raises_card_not_found(db):
    with pytest.raises(CardNotFound), db.transaction(bank="hsbc") as tx:
        tx.set_card_nickname(user_id=1, card_no="OTHER", nickname_overwrite="X")


def test_set_card_nickname_adds_column_if_missing(db, monkeypatch):
    """老 db 沒 nickname_overwrite 欄 → ALTER TABLE 自動補."""
    import sqlite3
    legacy_con = sqlite3.connect(":memory:")
    legacy_con.row_factory = sqlite3.Row
    # 老 schema 沒 nickname_overwrite
    legacy_con.execute("""
        CREATE TABLE cards (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            card_no TEXT NOT NULL,
            name TEXT,
            updated_at TEXT
        )
    """)
    legacy_con.execute(
        "INSERT INTO cards (user_id, card_no, name) VALUES (?, ?, ?)",
        (1, "CARD-A", "Old card"),
    )
    legacy_con.commit()

    class _W:
        def __init__(self, r):
            self._r = r
        def __getattr__(self, k):
            return getattr(self._r, k)
        def close(self):
            pass
        def commit(self):
            self._r.commit()
        def rollback(self):
            self._r.rollback()
        def execute(self, *a, **kw):
            return self._r.execute(*a, **kw)

    monkeypatch.setattr(
        "backend.server.db.open_bank_conn", lambda _b: _W(legacy_con),
    )
    new_db = Database()
    with new_db.transaction(bank="hsbc") as tx:
        r = tx.set_card_nickname(user_id=1, card_no="CARD-A", nickname_overwrite="新暱稱")
    assert r.nickname_overwrite == "新暱稱"
    # 欄已加
    cols = [r[1] for r in legacy_con.execute("PRAGMA table_info(cards)").fetchall()]
    assert "nickname_overwrite" in cols


# ============================================================
# B1: transaction error pathway
# ============================================================


def test_transaction_raises_bank_not_available_when_no_conn(monkeypatch):
    """db.open_bank_conn 回 None → transaction 拋 BankNotAvailable."""
    monkeypatch.setattr(
        "backend.server.db.open_bank_conn", lambda _b: None,
    )
    new_db = Database()
    with pytest.raises(BankNotAvailable) as exc, new_db.transaction(bank="ghost"):
        pass
    assert exc.value.bank == "ghost"
