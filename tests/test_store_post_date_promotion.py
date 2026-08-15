from pathlib import Path
from collections.abc import Iterator
import inspect

import pytest

from backend.core import store as store_mod
from backend.core.store import BankStore


def test_post_date_transition_sql_uses_typed_null_safe_comparison() -> None:
    source = inspect.getsource(BankStore.upsert_card_billed)
    assert "? IS NULL" not in source
    assert source.count("IS NOT DISTINCT FROM ?") == 4


@pytest.fixture
def store(tmp_path: Path, monkeypatch) -> Iterator[BankStore]:
    monkeypatch.setattr(store_mod, "DATA_ROOT", tmp_path)
    value = BankStore("esun")
    yield value
    value.close()


def test_billed_row_promotes_from_missing_to_real_post_date_without_duplicate(store: BankStore) -> None:
    base = {
        "card_no": "****7032",
        "bill_date": None,
        "currency": "TWD",
        "date": "2026-07-09",
        "post_date": None,
        "desc": "測試商店",
        "amount": 30,
    }
    assert store.upsert_card_billed([base], rules=[]) == 1
    original = store.conn.execute("SELECT id FROM card_billed_txns").fetchone()
    assert original is not None
    store.conn.execute("UPDATE card_billed_txns SET category='餐飲' WHERE id=?", (original["id"],))
    store.conn.commit()

    posted = {**base, "bill_date": "2026-07-01", "post_date": "2026-07-15"}
    assert store.upsert_card_billed([posted], rules=[]) == 0

    rows = store.conn.execute(
        "SELECT id, bill_date, consume_date, post_date, category FROM card_billed_txns"
    ).fetchall()
    assert [tuple(row) for row in rows] == [
        (original["id"], "2026-07-01", "2026-07-09", "2026-07-15", "餐飲"),
    ]


def test_duplicate_billed_rows_do_not_guess_post_date_identity(store: BankStore) -> None:
    base = {
        "card_no": "****7032",
        "bill_date": None,
        "currency": "TWD",
        "date": "2026-07-09",
        "post_date": None,
        "desc": "同店同額",
        "amount": 30,
    }
    assert store.upsert_card_billed([base, base], rules=[]) == 2
    ids = [row["id"] for row in store.conn.execute(
        "SELECT id FROM card_billed_txns ORDER BY id"
    ).fetchall()]
    store.conn.execute("UPDATE card_billed_txns SET category='第一筆' WHERE id=?", (ids[0],))
    store.conn.execute("UPDATE card_billed_txns SET category='第二筆' WHERE id=?", (ids[1],))
    store.conn.commit()

    posted = {**base, "bill_date": "2026-07-01", "post_date": "2026-07-15"}
    assert store.upsert_card_billed([posted, posted], rules=[]) == 0

    rows = store.conn.execute(
        "SELECT id, post_date, category FROM card_billed_txns ORDER BY id"
    ).fetchall()
    assert [tuple(row) for row in rows] == [
        (ids[0], None, "第一筆"),
        (ids[1], None, "第二筆"),
    ]


def test_bank_level_statement_promotes_one_unique_card_candidate(store: BankStore) -> None:
    base = {
        "card_no": "****7032",
        "bill_date": None,
        "currency": "TWD",
        "date": "2026-05-09",
        "post_date": None,
        "desc": "唯一候選",
        "amount": 40,
    }
    store.upsert_card_billed([base], rules=[])

    posted = {**base, "card_no": "", "post_date": "2026-05-15"}
    assert store.upsert_card_billed([posted], rules=[]) == 0
    row = store.conn.execute(
        "SELECT card_no, post_date FROM card_billed_txns"
    ).fetchone()
    assert row is not None
    assert tuple(row) == ("****7032", "2026-05-15")


def test_bank_level_statement_skips_ambiguous_card_candidates(store: BankStore) -> None:
    rows = [{
        "card_no": card_no,
        "bill_date": None,
        "currency": "TWD",
        "date": "2026-04-09",
        "post_date": None,
        "desc": "兩張卡同店同額",
        "amount": 50,
    } for card_no in ("****1111", "****2222")]
    store.upsert_card_billed(rows, rules=[])

    posted = {**rows[0], "card_no": "", "post_date": "2026-04-15"}
    assert store.upsert_card_billed([posted], rules=[]) == 0
    actual = store.conn.execute(
        "SELECT card_no, post_date FROM card_billed_txns ORDER BY id"
    ).fetchall()
    assert [tuple(row) for row in actual] == [
        ("****1111", None),
        ("****2222", None),
    ]


def test_store_normalizes_empty_post_date_to_sql_null(store: BankStore) -> None:
    billed = {
        "card_no": "****7032", "bill_date": None, "currency": "TWD",
        "date": "2026-03-01", "post_date": "", "desc": "空白日期", "amount": 60,
    }
    store.upsert_card_billed([billed], rules=[])
    store.refresh_card_pending(
        "realtime", [{**billed, "date": "2026-03-02"}], rules=[], fetch_ok=True,
    )

    billed_row = store.conn.execute(
        "SELECT post_date IS NULL FROM card_billed_txns"
    ).fetchone()
    pending_row = store.conn.execute(
        "SELECT post_date IS NULL FROM card_pending_txns"
    ).fetchone()
    assert billed_row is not None and billed_row[0] == 1
    assert pending_row is not None and pending_row[0] == 1


def test_repeat_sync_clears_legacy_fake_post_date_by_dedup_provenance(store: BankStore) -> None:
    row = {
        "card_no": "****7032", "bill_date": None, "currency": "TWD",
        "date": "2026-02-01", "post_date": None, "desc": "舊假值", "amount": 70,
    }
    store.upsert_card_billed([row], rules=[])
    store.conn.execute(
        "UPDATE card_billed_txns SET post_date=consume_date WHERE description='舊假值'"
    )
    store.conn.commit()

    assert store.upsert_card_billed([row], rules=[]) == 0
    actual = store.conn.execute(
        "SELECT COUNT(*), post_date FROM card_billed_txns WHERE description='舊假值'"
    ).fetchone()
    assert actual is not None
    assert tuple(actual) == (1, None)


def test_card_known_replay_promotes_unique_blank_card_row(store: BankStore) -> None:
    blank = {
        "card_no": "", "bill_date": "2026-01-01", "currency": "TWD",
        "date": "2026-01-09", "post_date": "2026-01-15",
        "desc": "歷史空卡號", "amount": 80,
    }
    store.upsert_card_billed([blank], rules=[])

    known = {**blank, "card_no": "****7032"}
    assert store.upsert_card_billed([known], rules=[]) == 0
    rows = store.conn.execute(
        "SELECT card_no, consume_date, post_date FROM card_billed_txns"
    ).fetchall()
    assert [tuple(row) for row in rows] == [
        ("****7032", "2026-01-09", "2026-01-15"),
    ]
