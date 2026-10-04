from backend.core.store import BankStore
from backend.core.twd_dedup_repair import repair


def _insert(store, **row):
    base = dict(user_id=7, account_no="0118979071948", txn_datetime="2026-03-28 03:38:04",
                description="ＡＴＭ跨行轉", expend=None, income=65714, balance=65715,
                currency="TWD", first_seen="2026-07-01T00:00:00", dedup_key="k")
    base.update(row)
    cols = ", ".join(base)
    store.conn.execute(f"INSERT INTO twd_transactions ({cols}) VALUES ({', '.join('?' * len(base))})",
                       tuple(base.values()))
    return store.conn.execute("SELECT max(id) FROM twd_transactions").fetchone()[0]


def _rows(store):
    return {r["id"]: dict(r) for r in store.conn.execute(
        "SELECT id, dedup_key, currency, category, auto_excluded FROM twd_transactions")}


def test_repair_merges_cross_sync_duplicates_only(tmp_path, monkeypatch):
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    store = BankStore("esun", user_id=7)
    try:
        old = _insert(store, income=65714.0, balance=65715.0, dedup_key="old~0",
                      category="轉帳", auto_excluded=1)
        _insert(store, first_seen="2026-09-07T00:53:10", dedup_key="new~0")
        # same-sync identical rows are real repeats and must survive
        twin_a = _insert(store, txn_datetime="2026-04-01 10:00:00", income=80, balance=80,
                         dedup_key="t~0")
        twin_b = _insert(store, txn_datetime="2026-04-01 10:00:00", income=80, balance=80,
                         dedup_key="t~1")
        # currency moved: keep the newer non-TWD row, inherit user edits
        legacy = _insert(store, account_no="1", txn_datetime="2026-08-21T01:00:00", income=46,
                         balance=1201494, dedup_key="1|x~0", category="利息", auto_excluded=1)
        moved = _insert(store, account_no="1", txn_datetime="2026-08-21T01:00:00", income=46,
                        balance=1201494, currency="JPY", first_seen="2026-10-03T04:24:04",
                        dedup_key="1:JPY|x~0")

        assert len(repair(store.conn, 7, apply=False)) == 2
        assert len(_rows(store)) == 6  # dry-run writes nothing

        repair(store.conn, 7, apply=True)
        rows = _rows(store)
        assert set(rows) == {old, twin_a, twin_b, moved}
        assert rows[old]["dedup_key"] == "new~0"
        assert (rows[old]["category"], rows[old]["auto_excluded"]) == ("轉帳", 1)
        assert (rows[moved]["currency"], rows[moved]["category"], rows[moved]["auto_excluded"]) == ("JPY", "利息", 1)
        assert legacy not in rows
        assert repair(store.conn, 7, apply=False) == []
    finally:
        store.close()
