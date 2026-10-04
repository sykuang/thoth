from backend.core.store import BankStore

BASE = {"date": "2026-07-19", "post_date": "2026-07-22", "desc": "優步－南松山炒飯麵",
        "amount": 327, "currency": "TWD", "bill_date": "2026-07-01"}


def _rows(store):
    return [dict(r) for r in store.conn.execute(
        "SELECT id, card_no, bill_date, amount FROM card_billed_txns ORDER BY id").fetchall()]


def test_card_billed_reuses_row_across_sync_format_drift():
    store = BankStore("card_cross_sync_test", user_id=7)
    try:
        store.conn.execute("DELETE FROM card_billed_txns")
        store.upsert_card_billed([{**BASE, "card_no": "****2869"}])
        # Statement row without card number, float money, later bill_date.
        store.upsert_card_billed([{**BASE, "card_no": "", "amount": 327.0}])
        store.upsert_card_billed([{**BASE, "card_no": "****2869", "bill_date": "2026-08-01"}])
        assert [(r["card_no"], r["bill_date"]) for r in _rows(store)] == [("****2869", "2026-07-01")]

        # Blank row first, carded row later: fill the card number in place.
        store.conn.execute("DELETE FROM card_billed_txns")
        store.upsert_card_billed([{**BASE, "card_no": ""}])
        store.upsert_card_billed([{**BASE, "card_no": "****2869"}])
        assert [r["card_no"] for r in _rows(store)] == ["****2869"]

        # Genuine repeats in one sync stay separate, and a resync keeps the pair.
        store.conn.execute("DELETE FROM card_billed_txns")
        pair = [{**BASE, "card_no": "****2869"}, {**BASE, "card_no": "****2869"}]
        store.upsert_card_billed(pair)
        store.upsert_card_billed([{**t, "amount": 327.0} for t in pair])
        assert len(_rows(store)) == 2

        # Different cards are different purchases.
        store.upsert_card_billed([{**BASE, "card_no": "****1111"}])
        assert len(_rows(store)) == 3
    finally:
        store.close()
