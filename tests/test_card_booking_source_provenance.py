"""Source status and date provenance through real collectors and persistence."""
import pytest

from backend.core.store import BankStore
from backend.core.persist import persist_collected


@pytest.mark.parametrize("status", ["未入帳", "已入帳", "入帳中", None, "", "unknown"])
def test_esun_requires_positive_booked_status_in_real_persistence(status, tmp_path, monkeypatch):
    from backend.banks.esun import EsunCrawler
    from tests.test_esun_history_sync import _bound_coverage, _bound_result

    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    # Real parser for recognized statuses; unknowns exercise the writer boundary.
    row = EsunCrawler._parse_card_transactions(
        "2026/06/18\nSynthetic merchant\nTWD 100\nTWD 100\n9000-XXXX-XXXX-1234\n"
        + (status if status in ("未入帳", "已入帳", "入帳中") else "已入帳")
    )[0]
    row["status"] = status
    store = BankStore("esun", user_id=7, source_account_id=91)
    try:
        persist_collected("esun", {
            "twd_txn_results": [_bound_result()], "history_coverage": _bound_coverage(),
            "card_transactions": [row], "card_transactions_ok": True,
        }, store)
        booked = status == "已入帳"
        assert store.conn.execute("SELECT COUNT(*) AS n FROM card_billed_txns").fetchone()["n"] == int(booked)
        assert store.conn.execute("SELECT COUNT(*) AS n FROM card_pending_txns").fetchone()["n"] == int(not booked)
        table = "card_billed_txns" if booked else "card_pending_txns"
        assert store.conn.execute(f"SELECT post_date FROM {table}").fetchone()["post_date"] is None
    finally:
        store.close()


@pytest.mark.parametrize("date_fields,booked", [
    ({"postingDate": "2026-06-23"}, True),
    ({"convertDate": "2026-06-23"}, False),
    ({"beginValueDate": "2026-06-23"}, False),
    ({"postingDate": "2026-06-23junk"}, False),
    ({"postingDate": "2026-06-23T00:00:00+08:60"}, False),
    ({"postingDate": "2026-06-23T00:00:00+0860"}, False),
])
def test_cathay_native_provenance_survives_real_persistence(date_fields, booked, tmp_path, monkeypatch):
    from backend.banks.cathay import CathayCrawler

    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    crawler = CathayCrawler.__new__(CathayCrawler)
    crawler.mask_card = lambda value: "****1234"
    rows = crawler._parse_consume({"consumeList": [{
        "cardNo": "9000000000001234", "consumeDate": "2026-06-18",
        "merchantName": "Synthetic merchant", "twdAmount": 100, **date_fields,
    }]})
    store = BankStore("cathay", user_id=7, source_account_id=91)
    try:
        persist_collected("cathay", {"credit_card": {"unbilled_detail": rows}}, store)
        assert store.conn.execute("SELECT COUNT(*) AS n FROM card_billed_txns").fetchone()["n"] == int(booked)
        assert store.conn.execute("SELECT COUNT(*) AS n FROM card_pending_txns").fetchone()["n"] == int(not booked)
        table = "card_billed_txns" if booked else "card_pending_txns"
        assert store.conn.execute(f"SELECT post_date FROM {table}").fetchone()["post_date"] == (
            "2026-06-23" if booked else None
        )
    finally:
        store.close()
