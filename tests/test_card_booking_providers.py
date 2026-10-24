"""Provider-shaped source evidence, independent of shared promotion behavior."""
from datetime import date
import json
from pathlib import Path

import pytest

from backend.core.persist import PERSISTERS
from backend.core.store import BankStore


def _payload(bank, posted):
    if bank == "hsbc":
        from tests.test_hsbc_history_sync import _persist_payload, _posted_row

        data = _persist_payload()
        entry = next(iter(data["card_detail"].values()))
        row = _posted_row(date(2026, 8, 30))
        row["description"] = "source-booking-probe"
        if not posted:
            row["postedDate"] = "0002-11-30T00:00"
        entry["unposted"] = [row]
        return data
    if bank == "ctbc":
        return {"card_api_dump": {"/twrbc-card/qu006/011": {"allItems": [{
            "purchaseDt": "20260618", "postingDt": "20260623" if posted else "00000000",
            "cardNoSuffixFour": "1234_0", "description": "source-booking-probe",
            "purchaseAmt": 100, "origCurCode": "901", "origCurDesc": "TWD",
            "origCurAmt": 100, "txCode": "40",
        }]}}, "summary": {}, "twd_deposit": {}}
    if bank == "ubot":
        from tests.test_persist_ubot_last_payment import _base_data

        data = _base_data()
        data["card_billed"] = []
        data["card_unbilled"] = {"CardSum": "0", "DispStmtAmt": "0", "CardList": [{
            "cardNo": "9000000000001234", "seq": "00001", "effectiveDate": "20260609",
            "postingDate": "20260610" if posted else "00000000", "txCode": "40",
            "txAmt": "100", "oriCode": "", "oriAmt": "0.00",
            "txDesc": "source-booking-probe", "typeName": "Synthetic card",
        }]}
        return data
    data = json.loads((Path(__file__).parent / "fixtures/fubon_collected_fake.json").read_text())["data"]
    data["pending_click_ok"] = True
    data["pending_page_url"] = "https://ebank.example/cccqu004/home"
    day = "115/05/06" if posted else "---"
    data["pending_page_text"] = (
        "消費日期\t消費說明\t入帳日期\t外幣折算日/幣別\t外幣金額/消費地\t臺幣金額\n"
        f"115/05/05\tsource-booking-probe\t{day}\t　\t　\t100\n"
    )
    return data


@pytest.mark.parametrize("bank", ["hsbc", "ctbc", "ubot", "fubon"])
@pytest.mark.parametrize("posted", [True, False], ids=["native-posting-date", "no-posting-evidence"])
def test_pending_provider_passes_only_native_booking_evidence(bank, posted, tmp_path, monkeypatch):
    from backend.core import store as module

    monkeypatch.setattr(module, "DATA_ROOT", tmp_path)
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    if bank == "ubot":
        from backend.core.persist import ubot
        monkeypatch.setattr(ubot, "_today", lambda: date(2026, 9, 1))
    captured = []
    store = BankStore(bank, user_id=1, source_account_id=7)
    try:
        def capture(scope, rows, **kwargs):
            captured.extend(rows)
            return len(rows)

        monkeypatch.setattr(store, "refresh_card_pending", capture)
        from backend.core import persist
        getattr(persist, PERSISTERS[bank])(_payload(bank, posted), store)
    finally:
        store.close()
    rows = [row for row in captured if row.get("desc") == "source-booking-probe"]
    assert len(rows) == 1
    assert rows[0].get("booking_status") == ("booked" if posted else None)


@pytest.mark.parametrize("bank", ["hsbc", "ctbc", "ubot", "fubon"])
@pytest.mark.parametrize("posted", [True, False], ids=["native-posting-date", "no-posting-evidence"])
def test_provider_booking_routes_through_real_persistence(bank, posted, tmp_path, monkeypatch):
    from backend.core import persist, store as module

    monkeypatch.setattr(module, "DATA_ROOT", tmp_path)
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    if bank == "ubot":
        from backend.core.persist import ubot
        monkeypatch.setattr(ubot, "_today", lambda: date(2026, 9, 1))
    store = BankStore(bank, user_id=1, source_account_id=7)
    try:
        payload = _payload(bank, posted)
        if bank == "fubon":
            from tests.test_fubon_history_sync import _payload as history_payload
            payload.update(history_payload())
        # UBOT's legacy fixture already proves provider-specific coverage; the
        # production orchestrator additionally requires the v1 envelope.
        if "history_coverage" in payload:
            payload["history_coverage"]["version"] = 1
        persist.persist_collected(bank, payload, store)
        counts = {
            table: store.conn.execute(
                f"SELECT COUNT(*) AS n FROM {table} WHERE description = ?",
                ("source-booking-probe",),
            ).fetchone()["n"]
            for table in ("card_billed_txns", "card_pending_txns")
        }
        assert counts == {"card_billed_txns": int(posted), "card_pending_txns": int(not posted)}
    finally:
        store.close()
