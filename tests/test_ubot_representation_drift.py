"""Synthetic UBOT ingress regressions; no login, collection or bank data."""
from datetime import date

import pytest

from backend.core.persist.ubot import _persist_ubot
from backend.core.store import BankStore
from tests.test_ubot_observed_shape import PLAIN, CONTROL


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("DB_BACKEND", "sqlite")
    value = BankStore("ubot", user_id=7, source_account_id=91)
    try:
        yield value
    finally:
        value.close()


def raw(**changes):
    return {"cardNo": "****1234", "effectiveDate": "2026/07/01",
            "effectDate": "2026/07/01", "txDesc": PLAIN,
            "txAmt": "100", "Currency": "TWD", "oriAmt": "0",
            "txCode": "05", **changes}


def ingress(store, *, pending=None, statement=None):
    payload = {}
    if pending is not None:
        payload["card_unbilled"] = {"CardList": pending}
    if statement is not None:
        payload["card_billed"] = [{"CardHeader": {"stmtDate": "2026/08/03"},
                                   "CardList": statement}]
    result = _persist_ubot(payload, store, as_of=date(2026, 8, 4))
    store.commit()
    return result


def rows(store, table="card_billed_txns"):
    return [dict(row) for row in store.conn.execute(f"SELECT * FROM {table} ORDER BY id")]


@pytest.mark.parametrize("drift", ["original", "description", "both"])
@pytest.mark.parametrize("count", [1, 2])
def test_actual_ubot_seed_booked_formal_stale_reopen(store, drift, count):
    source = raw()
    ingress(store, pending=[source] * count)
    assert len(rows(store, "card_pending_txns")) == count
    if count == 1:
        store.conn.execute("UPDATE card_pending_txns SET category='Manual', description_overwrite='KEEP'")
        store.commit()
    booked = {**source, "postDate": "2026/07/03"}
    ingress(store, pending=[booked] * count)
    before = rows(store)
    assert len(before) == count
    assert all(row["consume_amount"] == 0 for row in before)
    if count == 1:
        assert (before[0]["category"], before[0]["description_overwrite"]) == ("Manual", "KEEP")
    formal = {**booked,
              **({"oriAmt": ""} if drift in ("original", "both") else {}),
              **({"txDesc": CONTROL} if drift in ("description", "both") else {})}
    ingress(store, pending=[], statement=[formal] * count)
    actual = rows(store)
    assert [row["id"] for row in actual] == [row["id"] for row in before], "Representation drift duplicated a bank occurrence"
    assert actual == [{**row, "bill_date": "2026-08-03",
                       "consume_amount": None if drift in ("original", "both") else 0,
                       "description": CONTROL if drift in ("description", "both") else PLAIN}
                      for row in before]
    ingress(store, pending=[booked] * count)
    ingress(store, pending=[], statement=[formal] * count)
    assert rows(store) == actual
    store.close()
    reopened = BankStore("ubot", user_id=7, source_account_id=91)
    try:
        assert rows(reopened) == actual
        assert rows(reopened, "card_pending_txns") == []
    finally:
        reopened.close()


def test_unique_manual_donor_survives_actual_control_byte_booking(store):
    ingress(store, pending=[raw()])
    store.conn.execute("UPDATE card_pending_txns SET category='Manual', description_overwrite='KEEP'")
    store.commit()
    ingress(store, pending=[raw(txDesc=CONTROL, postDate="2026/07/03")])
    actual = rows(store)
    assert len(actual) == 1
    assert (actual[0]["category"], actual[0]["description_overwrite"]) == ("Manual", "KEEP")
    assert actual[0]["description"] == CONTROL


@pytest.mark.parametrize("reverse", [False, True])
def test_actual_mixed_unique_and_ambiguous_drift_donors(store, reverse):
    unique = raw()
    repeated = raw(cardNo="****5678", txAmt="200")
    ingress(store, pending=[unique, repeated, repeated])
    for index, donor in enumerate(rows(store, "card_pending_txns")):
        store.conn.execute("UPDATE card_pending_txns SET category='Manual', description_overwrite=? WHERE id=?",
                           (f"KEEP-{index}", donor["id"]))
    store.commit()
    booked = [{**t, "txDesc": CONTROL, "postDate": "2026/07/03"}
              for t in (unique, repeated, repeated)]
    if reverse:
        booked.reverse()
    ingress(store, pending=booked)
    actual = rows(store)
    assert len(actual) == 3
    kept = next(r for r in actual if r["card_no"] == unique["cardNo"])
    assert (kept["category"], kept["description_overwrite"]) == ("Manual", "KEEP-0")
    assert all(r["description_overwrite"] is None for r in actual if r["card_no"] != unique["cardNo"])


@pytest.mark.parametrize("value,expected", [(None, None), ("", None), ("-", None), ("0", 0)])
def test_actual_original_amount_keeps_bank_absence_and_zero(store, value, expected):
    source = raw(postDate="2026/07/03", oriAmt=value)
    ingress(store, pending=[source])
    assert rows(store)[0]["consume_amount"] == expected


def test_actual_original_amount_invalid_still_raises(store):
    with pytest.raises(ValueError):
        _persist_ubot({"card_unbilled": {"CardList": [raw(oriAmt="invalid")]}},
                      store, as_of=date(2026, 8, 4))
    assert rows(store) == []
    assert rows(store, "card_pending_txns") == []


def normalized(**changes):
    return {"card_no": "****1234", "date": "2026-07-01", "post_date": "2026-07-03",
            "desc": PLAIN, "amount": 100, "currency": "TWD", "consume_currency": "TWD",
            "consume_amount": 0, "txn_type": "spending", **changes}


def test_actual_three_observations_and_two_to_three_occurrences(store):
    plain = raw(postDate="2026/07/03")
    control = {**plain, "txDesc": CONTROL}
    for observed in (plain, control, plain):
        ingress(store, pending=[observed] * 2)
        assert len(rows(store)) == 2
    original_ids = [r["id"] for r in rows(store)]
    formal = {**plain, "oriAmt": ""}
    ingress(store, pending=[], statement=[formal] * 2)
    assert [r["id"] for r in rows(store)] == original_ids
    ingress(store, pending=[], statement=[formal] * 3)
    assert len(rows(store)) == 3
    ingress(store, pending=[], statement=[formal] * 3)
    assert len(rows(store)) == 3


@pytest.mark.parametrize("reverse", [False, True])
def test_mixed_exact_drift_occurrences_reserve_edits(store, reverse):
    base = normalized()
    store.upsert_card_billed([base])
    store.conn.execute("UPDATE card_billed_txns SET description_overwrite='KEEP'")
    store.commit()
    before = rows(store)[0]
    source = [base, {**base, "desc": CONTROL}]
    if reverse:
        source.reverse()
    store.upsert_card_billed(source)
    assert len(rows(store)) == 2
    assert rows(store)[0] == before
    assert rows(store)[1]["description_overwrite"] is None
    store.upsert_card_billed(source)
    assert len(rows(store)) == 2


@pytest.mark.parametrize("reverse", [False, True])
def test_edited_undated_two_to_three_preserves_unattributed_edits(store, reverse):
    base = normalized(post_date=None)
    store.upsert_card_billed([base, base])
    store.conn.execute("UPDATE card_billed_txns SET description_overwrite='UNPAIRED'")
    store.commit()
    before = rows(store)
    statement = {**base, "post_date": "2026-07-03", "bill_date": "2026-08-03"}
    independent = normalized(desc="Independent", amount=200)
    incoming = [statement, statement, statement, independent]
    if reverse:
        incoming.reverse()
    store.upsert_card_billed(incoming)
    assert len([r for r in rows(store) if r["description"] == PLAIN]) == 3
    assert all(r["post_date"] == "2026-07-03" and r["bill_date"] == "2026-08-03"
               for r in rows(store) if r["description"] == PLAIN)
    assert [r["description_overwrite"] for r in rows(store)[:2]] == [
        r["description_overwrite"] for r in before]
    assert any(r["description"] == "Independent" for r in rows(store))


@pytest.mark.parametrize("reverse", [False, True])
def test_same_batch_unique_donor_and_ambiguous_donors_are_component_local(store, reverse):
    unique = normalized(native_identity='["ubot",7,91,"1234","unique"]')
    ambiguous = normalized(card_no="****5678", desc="Ambiguous", native_identity='["ubot",7,91,"5678","repeat"]')
    store.refresh_card_pending("unbilled", [unique, ambiguous, ambiguous], fetch_ok=True)
    donors = rows(store, "card_pending_txns")
    for index, donor in enumerate(donors):
        store.conn.execute("UPDATE card_pending_txns SET category=?,description_overwrite=? WHERE id=?",
                           ("Manual", f"KEEP-{index}", donor["id"]))
    store.commit()
    statement = [{**t, "bill_date": "2026-08-03"} for t in (unique, ambiguous, ambiguous)]
    booked = [{**t, "booking_status": "booked"} for t in (unique, ambiguous, ambiguous)]
    if reverse:
        statement.reverse()
        booked.reverse()
    rules = [{"pattern": ".*", "category": "Automatic", "subcategory": "Default"}]
    store.persist_card_batch(statement, booked, scope="unbilled", fetch_ok=True, rules=rules)
    actual = rows(store)
    assert len(actual) == 3
    kept = next(r for r in actual if r["card_no"] == unique["card_no"])
    assert (kept["category"], kept["description_overwrite"]) == ("Manual", "KEEP-0")
    assert all(r["description_overwrite"] is None for r in actual if r["card_no"] != unique["card_no"])
    assert rows(store, "card_pending_txns") == []


@pytest.mark.parametrize("change", [
    {"desc": "SYNTHETIC TAX 1200 USD 05/12"}, {"consume_amount": 100},
    {"consume_currency": "USD"}, {"currency": "USD"}, {"txn_type": "refund"},
    {"post_date": "2026-07-04"}, {"date": "2026-07-02"}, {"card_no": "****5678"},
    {"native_identity": "different-reference"}, {"amount": -100},
])
def test_drift_never_merges_distinct_evidence(store, change):
    base = normalized()
    store.upsert_card_billed([base])
    before = rows(store)[0]
    incoming = {**base, "desc": CONTROL, "bill_date": "2026-08-03", **change}
    if change.get("native_identity"):
        with pytest.raises(ValueError, match="ambiguous native card identity"):
            store.upsert_card_billed([incoming])
        assert rows(store) == [before]
    else:
        store.upsert_card_billed([incoming])
        assert len(rows(store)) == 2
        assert rows(store)[0] == before


def test_nonubot_zero_missing_remain_distinct(store):
    other = BankStore("esun", user_id=7, source_account_id=91)
    try:
        other.upsert_card_billed([normalized()])
        other.upsert_card_billed([normalized(consume_amount=None, bill_date="2026-08-03")])
        assert len(rows(other)) == 2
    finally:
        other.close()


@pytest.mark.parametrize("reverse", [False, True])
def test_blank_drift_does_not_choose_between_known_card_owners(store, reverse):
    store.upsert_card_billed([normalized(card_no="")])
    store.conn.execute("UPDATE card_billed_txns SET description_overwrite='UNOWNED'")
    store.commit()
    before = rows(store)[0]
    incoming = [normalized(desc=CONTROL), normalized(card_no="****5678", desc=CONTROL)]
    if reverse:
        incoming.reverse()
    store.upsert_card_billed(incoming)
    assert rows(store)[0] == before
    assert len(rows(store)) == 3


def test_zero_missing_without_statement_is_not_money_equality(store):
    store.upsert_card_billed([normalized()])
    store.upsert_card_billed([normalized(consume_amount=None)])
    assert len(rows(store)) == 2


def test_formal_first_stale_booked_does_not_downgrade_or_fake_source(store, monkeypatch):
    formal = raw(postDate="2026/07/03", oriAmt="")
    ingress(store, pending=[], statement=[formal])
    before = rows(store)
    stale = {**formal, "oriAmt": "0", "txDesc": CONTROL}
    observations = []
    original = store.upsert_card_billed

    def observe(*args, **kwargs):
        result = original(*args, **kwargs)
        observations.extend(s for group in store._billed_source_rows.values() for s in group)
        return result

    monkeypatch.setattr(store, "upsert_card_billed", observe)
    ingress(store, pending=[stale])
    assert rows(store) == before
    assert observations
    assert all(s["consume_amount"] == 0 and s["description"] == CONTROL for s in observations)


@pytest.mark.parametrize("reverse", [False, True])
def test_exact_reservations_span_owning_batch_partitions(store, reverse):
    base = normalized()
    store.upsert_card_billed([base])
    store.conn.execute("UPDATE card_billed_txns SET description_overwrite='EXACT'")
    store.commit()
    before = rows(store)[0]
    billed = [normalized(desc=CONTROL), normalized(desc="Independent", amount=200)]
    if reverse:
        billed.reverse()
    store.persist_card_batch(billed, [{**base, "booking_status": "booked"}],
                             scope="unbilled", fetch_ok=True)
    assert len(rows(store)) == 3
    assert rows(store)[0] == before


def test_preexisting_representations_are_not_cleanup_authority(store):
    store.upsert_card_billed([normalized(), normalized(desc=CONTROL),
                              normalized(consume_amount=None, bill_date="2026-08-03")])
    assert len(rows(store)) == 3
    before = rows(store)
    store.upsert_card_billed([normalized()])
    assert rows(store) == before


@pytest.mark.parametrize("reverse", [False, True])
def test_edited_undated_dates_publish_before_later_purchase(store, reverse):
    base = normalized(post_date=None)
    store.upsert_card_billed([base, base])
    store.conn.execute("UPDATE card_billed_txns SET description_overwrite='KEEP'")
    store.commit()
    july3 = normalized(bill_date="2026-08-03")
    store.upsert_card_billed([july3, july3])
    refreshed = rows(store)
    july4 = {**july3, "post_date": "2026-07-04"}
    store.upsert_card_billed([july4])
    assert len(rows(store)) == 3
    source = [july3, july3, july4]
    if reverse:
        source.reverse()
    store.upsert_card_billed(source)
    assert len(rows(store)) == 3
    assert sorted((r["post_date"] for r in rows(store)), key=str) == ["2026-07-03", "2026-07-03", "2026-07-04"]
    assert len(refreshed) == 2
    assert all(r["post_date"] == "2026-07-03" and r["bill_date"] == "2026-08-03" for r in refreshed)
    assert all(r["description_overwrite"] == "KEEP" for r in refreshed)
    actual = rows(store)
    store.upsert_card_billed(source)
    assert rows(store) == actual


@pytest.mark.parametrize("reverse", [False, True])
def test_undated_exact_occurrences_reserved_before_posting(store, reverse):
    base = normalized(post_date=None)
    store.upsert_card_billed([base, base])
    store.conn.execute("UPDATE card_billed_txns SET description_overwrite='EXACT'")
    store.commit()
    before = rows(store)
    source = [normalized(), base, base]
    if reverse:
        source.reverse()
    store.upsert_card_billed(source)
    assert len(rows(store)) == 3
    assert rows(store)[:2] == before
    assert rows(store)[2]["post_date"] == "2026-07-03"
    actual = rows(store)
    store.upsert_card_billed(source)
    assert rows(store) == actual


@pytest.mark.parametrize("reverse", [False, True])
def test_mixed_undated_ambiguous_dates_keep_unique_manual(store, reverse):
    base = normalized(post_date=None)
    unique = normalized(post_date=None, amount=200, desc="Unique")
    store.upsert_card_billed([base, base, unique])
    for i, row in enumerate(rows(store)):
        store.conn.execute("UPDATE card_billed_txns SET category='Manual', description_overwrite=? WHERE id=?",
                           (f"KEEP-{i}", row["id"]))
    store.commit()
    unique_id = rows(store)[2]["id"]
    source = [normalized(bill_date="2026-08-03"),
              normalized(post_date="2026-07-04", bill_date="2026-08-03"),
              {**unique, "post_date": "2026-07-03", "bill_date": "2026-08-03"}]
    if reverse:
        source.reverse()
    store.upsert_card_billed(source)
    actual = rows(store)
    assert len(actual) == 3
    assert sorted((r["post_date"] for r in actual if r["amount"] == 100), key=str) == ["2026-07-03", "2026-07-04"]
    kept = next(r for r in actual if r["id"] == unique_id)
    assert (kept["category"], kept["description_overwrite"]) == ("Manual", "KEEP-2")
    assert all(r["description_overwrite"] is None for r in actual if r["amount"] == 100)
    store.upsert_card_billed(source)
    assert rows(store) == actual


@pytest.mark.parametrize("barrier", [False, True])
def test_actual_ubot_collection_reserves_exact_booked_partition(store, barrier, monkeypatch):
    import sqlite3
    from backend.core.card_bills import CardBillWriteBarrier
    from backend.server import db
    from backend.server.db_facade import db_api

    plain = raw(postDate="2026/07/03")
    ingress(store, pending=[plain])
    store.conn.execute("UPDATE card_billed_txns SET description_overwrite='EXACT'")
    store.commit()
    before = rows(store)[0]
    owner = CardBillWriteBarrier(store) if barrier else store
    data = {"card_billed": [{"CardHeader": {"stmtDate": "2026/08/03"},
                             "CardList": [{**plain, "txDesc": CONTROL}]}],
            "card_unbilled": {"CardList": [plain]}}
    delta = _persist_ubot(data, owner, as_of=date(2026, 8, 4))
    store.commit()
    assert len(rows(store)) == 2
    assert rows(store)[0] == before
    assert rows(store)[1]["description"] == CONTROL
    assert rows(store)[1]["description_overwrite"] is None
    assert delta["card_billed_new"] == 1
    assert delta["card_unbilled"] == 0

    def open_conn(bank):
        assert bank == "ubot"
        conn = sqlite3.connect(store.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    monkeypatch.setattr(db, "open_bank_conn", open_conn)
    detail = db_api.get_card_detail(bank="ubot", user_id=7, card_no="****1234", cycle_start="2026-07-01")
    assert detail is not None
    assert sorted(t.description for t in detail.posted_txns) == sorted([PLAIN, CONTROL])
    actual = rows(store)
    _persist_ubot(data, owner, as_of=date(2026, 8, 4))
    store.commit()
    assert rows(store) == actual


@pytest.mark.parametrize("native_side", ["donor", "source"])
@pytest.mark.parametrize("description", ["SHOP", " SHOP "])
def test_native_one_sided_promotion_keeps_meaningful_whitespace(store, native_side, description):
    reference = '["ubot",7,91,"1234","whitespace"]'
    donor = normalized(post_date=None, desc=description,
                       **({"native_identity": reference} if native_side == "donor" else {}))
    store.refresh_card_pending("unbilled", [donor], fetch_ok=True)
    store.conn.execute("UPDATE card_pending_txns SET category='Manual', description_overwrite='KEEP'")
    store.commit()
    booked = normalized(desc="SHOP", booking_status="booked",
                        **({"native_identity": reference} if native_side == "source" else {}))
    store.refresh_card_pending("unbilled", [booked], fetch_ok=True)
    actual = rows(store)
    assert len(actual) == 1
    expected = ("Manual", "KEEP") if description == "SHOP" else (None, None)
    assert (actual[0]["category"], actual[0]["description_overwrite"]) == expected
    assert rows(store, "card_pending_txns") == []


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("bill_date", [None, "2026-08-03"])
@pytest.mark.parametrize("route", ["direct", "collection", "split-collection", "public"])
def test_single_edited_undated_donor_cannot_choose_between_dates(store, reverse, bill_date, route, monkeypatch):
    unique = normalized(post_date=None, amount=200, desc="Unique", bill_date=bill_date)
    store.upsert_card_billed([normalized(post_date=None, bill_date=bill_date), unique])
    store.conn.execute("UPDATE card_billed_txns SET category='Manual', description_overwrite='KEEP'")
    store.commit()
    unique_id = rows(store)[1]["id"]
    source = [normalized(), normalized(post_date="2026-07-04"),
              {**unique, "post_date": "2026-07-03"}]
    if reverse:
        source.reverse()
    def persist():
        if route == "direct":
            store.upsert_card_billed(source)
            return
        statement = [raw(postDate=t["post_date"].replace("-", "/"),
                         txAmt=str(t["amount"]), txDesc=t["desc"]) for t in source]
        if route == "collection":
            ingress(store, statement=statement)
            return
        data = {"card_billed": [{"CardHeader": {"stmtDate": "2026/08/03"}, "CardList": [t]}
                                for t in statement]}
        if route == "public":
            from backend.core.persist import persist_collected, ubot
            from tests.test_ubot_history_sync import _persist_payload

            monkeypatch.setattr(ubot, "_today", lambda: date(2026, 9, 1))
            persist_collected("ubot", {**_persist_payload(), **data}, store)
        else:
            _persist_ubot(data, store, as_of=date(2026, 8, 4))
    persist()
    actual = rows(store)
    assert len(actual) == 3
    assert all(r["description_overwrite"] is None for r in actual if r["amount"] == 100)
    assert {r["post_date"] for r in actual if r["amount"] == 100} == {"2026-07-03", "2026-07-04"}
    kept = next(r for r in actual if r["id"] == unique_id)
    assert (kept["category"], kept["description_overwrite"]) == ("Manual", "KEEP")
    persist()
    assert rows(store) == actual


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("route", ["direct", "collection", "split-collection", "public"])
def test_exact_owned_source_leaves_unique_undated_manual_donor(store, reverse, route, monkeypatch):
    undated = normalized(post_date=None, bill_date="2026-08-03")
    exact = normalized(bill_date="2026-08-03")
    independent = normalized(post_date=None, amount=200, desc="Independent")
    store.upsert_card_billed([undated, exact, independent])
    original = rows(store)
    for donor in (original[0], original[2]):
        store.conn.execute("UPDATE card_billed_txns SET category='Manual', description_overwrite='KEEP' WHERE id=?",
                           (donor["id"],))
    store.commit()
    incoming = [exact, {**exact, "post_date": "2026-07-04"},
                {**independent, "post_date": "2026-07-05", "bill_date": "2026-08-03"}]
    if reverse:
        incoming.reverse()
    if route == "direct":
        store.upsert_card_billed(incoming)
    else:
        statement = [raw(postDate=t["post_date"].replace("-", "/"),
                         txAmt=str(t["amount"]), txDesc=t["desc"]) for t in incoming]
        if route == "collection":
            ingress(store, statement=statement)
        else:
            data = {"card_billed": [{"CardHeader": {"stmtDate": "2026/08/03"}, "CardList": [t]}
                                    for t in statement]}
            if route == "public":
                from backend.core.persist import persist_collected, ubot
                from tests.test_ubot_history_sync import _persist_payload

                monkeypatch.setattr(ubot, "_today", lambda: date(2026, 9, 1))
                persist_collected("ubot", {**_persist_payload(), **data}, store)
            else:
                _persist_ubot(data, store, as_of=date(2026, 8, 4))
    actual = rows(store)
    assert len(actual) == 3
    donor = next(r for r in actual if r["id"] == original[0]["id"])
    assert (donor["category"], donor["description_overwrite"]) == ("Manual", "KEEP")
    assert (donor["post_date"], donor["bill_date"]) == ("2026-07-04", "2026-08-03")
    assert next(r for r in actual if r["id"] == original[1]["id"]) == original[1]
    kept = next(r for r in actual if r["id"] == original[2]["id"])
    assert (kept["category"], kept["description_overwrite"], kept["post_date"]) == ("Manual", "KEEP", "2026-07-05")


@pytest.mark.parametrize("count", [1, 2])
def test_undated_batch_evidence_does_not_double_local_occurrences(store, count):
    store.upsert_card_billed([normalized(post_date=None)] * 2)
    store.conn.execute("UPDATE card_billed_txns SET category='Manual', description_overwrite='KEEP'")
    store.commit()
    source = [normalized()] * count
    store.upsert_card_billed(source, _batch_txns=tuple(source))
    actual = rows(store)
    assert len(actual) == 2
    assert [r["description_overwrite"] for r in actual] == ([None, None] if count == 1 else ["KEEP", "KEEP"])
    assert sum(r["post_date"] == "2026-07-03" for r in actual) == count


@pytest.mark.parametrize("later_scope", ["pending", "billed"])
@pytest.mark.parametrize("later_value", ["bad", "NaN", "sNaN", "1e1000000"])
def test_public_ubot_first_billed_writer_error_precedes_later_mapping(store, monkeypatch, later_scope, later_value):
    from decimal import InvalidOperation, Overflow, localcontext
    from backend.core.error_codes import exception_error_id
    from backend.core.persist import ubot
    from tests.test_ubot_history_sync import _persist_payload, _assert_all_empty

    monkeypatch.setattr(ubot, "_today", lambda: date(2026, 9, 1))
    payload = _persist_payload()
    payload["card_billed"] = [{"CardHeader": {"stmtDate": "2026/08/03"},
                               "CardList": [raw(oriAmt="1.5")]}]
    later = raw(txAmt=later_value, postDate="2026/07/03")
    if later_scope == "pending":
        payload["card_unbilled"] = {"CardList": [later]}
    else:
        payload["card_billed"].append({"CardList": [later]})
    with localcontext() as context:
        context.traps[InvalidOperation] = context.traps[Overflow] = True
        context.clear_flags()
        traps = dict(context.traps)
        with pytest.raises(ValueError) as caught:
            ubot.persist_ubot(payload, store)
        assert exception_error_id(caught.value) == "UBOT-08-866-007"
        assert caught.value.args == ("invalid native monetary value",)
        assert (caught.value.__cause__, caught.value.__context__, caught.value.__suppress_context__) == (None, None, False)
        assert dict(context.traps) == traps
        assert not any(context.flags.values())
    _assert_all_empty(store)
    assert store._billed_source_rows == {}


@pytest.mark.parametrize("later_scope", ["pending", "billed"])
@pytest.mark.parametrize("checkpoint", ["date", "classify", "sql"])
def test_ubot_earlier_writer_precedes_later_native_checkpoint(store, monkeypatch, later_scope, checkpoint):
    from backend.core.persist import ubot
    from backend.core.error_codes import exception_error_id
    from tests.test_ubot_history_sync import _persist_payload, _assert_all_empty

    monkeypatch.setattr(ubot, "_today", lambda: date(2026, 9, 1))
    payload = _persist_payload()
    payload["card_billed"] = [{"CardList": [raw(oriAmt="0" if checkpoint == "sql" else "1.5")]}]
    later = raw(postDate="2026/07/04", effectiveDate="2026/07/04", effectDate="2026/07/04",
                txDesc="Later checkpoint")
    if later_scope == "pending":
        payload["card_unbilled"] = {"CardList": [later]}
    else:
        payload["card_billed"].append({"CardList": [later]})
    events = []
    native_date, native_classify = ubot._ubot_date, ubot.classify.classify_ubot
    sentinel = RuntimeError("later native checkpoint must not run")

    def parse_date(value):
        if value == "2026/07/04":
            events.append("later-date")
            raise sentinel
        return native_date(value)

    def classify(code, desc, amount):
        if desc == "Later checkpoint":
            events.append("later-classify")
            raise sentinel
        return native_classify(code, desc, amount)

    if checkpoint == "date":
        monkeypatch.setattr(ubot, "_ubot_date", parse_date)
    elif checkpoint == "classify":
        monkeypatch.setattr(ubot.classify, "classify_ubot", classify)
    else:
        store.conn.execute("CREATE TRIGGER fail_first_card BEFORE INSERT ON card_billed_txns "
                           "BEGIN SELECT RAISE(FAIL, 'earlier synthetic SQL fault'); END")
        store.commit()
        if later_scope == "pending":
            payload["card_unbilled"] = {"CardList": [raw(txAmt="bad")]}
        else:
            payload["card_billed"][1] = {"CardList": [raw(txAmt="bad")]}
    with pytest.raises(Exception) as caught:
        ubot.persist_ubot(payload, store)
    expected = "SYS-08-142-018" if checkpoint == "sql" else "UBOT-08-866-007"
    assert exception_error_id(caught.value, bank="ubot") == expected
    assert caught.value is not sentinel
    assert events == []
    _assert_all_empty(store)


@pytest.mark.parametrize("missing_post", ["00000000", "not-a-date", "", "2026W271", "2026-W27-1", "2026-W28-5"])
@pytest.mark.parametrize("statement_date", ["20260803", 20260803])
def test_ubot_future_undated_exact_source_keeps_native_date_absence(store, missing_post, statement_date):
    undated = normalized(post_date=None, bill_date="2026-08-03")
    store.upsert_card_billed([undated])
    store.conn.execute("UPDATE card_billed_txns SET category='Manual', description_overwrite='EXACT'")
    store.commit()
    before = rows(store)[0]
    data = {"card_billed": [
        {"CardHeader": {"stmtDate": "2026/08/03"}, "CardList": [raw(postDate="2026/07/03")]},
        {"CardHeader": {"stmtDate": statement_date}, "CardList": [raw(postDate=missing_post)]},
    ]}
    _persist_ubot(data, store, as_of=date(2026, 8, 4))
    assert len(rows(store)) == 2
    assert rows(store)[0] == before
    assert rows(store)[1]["post_date"] == "2026-07-03"
    assert rows(store)[1]["description_overwrite"] is None


@pytest.mark.parametrize("value", [
    "00010701", "09990701", "10000701", "99990701", "2026/07/01", " 2026-07-01 ",
    "２０２６０７０１", "٢٠٢٦٠٧٠١", "2026070１", "20260230", "20261301", "00000000",
    "2026W271", "2026-W27-1", "2026-W28-5", "not-a-date", "", None, 0, 20260701,
    20260701.0, True, [], {},
])
def test_ubot_shadow_dates_match_actual_native_persistence(store, value):
    from backend.core.persist.ubot import _collected_card_evidence, _ubot_date

    data = {"card_billed": [{"CardHeader": {"stmtDate": value},
                             "CardList": [raw(effectDate=value, postDate=value)]}]}
    expected = _ubot_date(value)
    _persist_ubot(data, store, as_of=date(2026, 8, 4))
    actual = rows(store)
    assert len(actual) == 1
    assert (actual[0]["consume_date"], actual[0]["post_date"], actual[0]["bill_date"]) == (expected,) * 3
    evidence = _collected_card_evidence(data)
    assert len(evidence) == 1
    assert (evidence[0]["date"], evidence[0]["post_date"], evidence[0]["bill_date"]) == (expected,) * 3


def _split_public_ingress(store, statement, public, monkeypatch):
    data = {"card_billed": [{"CardHeader": {"stmtDate": "2026/08/03"}, "CardList": [t]}
                            for t in statement]}
    if public:
        from backend.core.persist import persist_collected, ubot
        from tests.test_ubot_history_sync import _persist_payload

        monkeypatch.setattr(ubot, "_today", lambda: date(2026, 9, 1))
        persist_collected("ubot", {**_persist_payload(), **data}, store)
    else:
        _persist_ubot(data, store, as_of=date(2026, 8, 4))


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("public", [False, True])
@pytest.mark.parametrize("change,keep", [
    ({"txCode": "43"}, True), ({"txCode": "20"}, True),
    ({"txCode": "55"}, True), ({"txCode": "60"}, True),
    ({"txCode": "40"}, False), ({"txCode": "41"}, False), ({"txCode": None}, False),
    ({"txCode": "41", "txDesc": "CASHBACK"}, True),
    ({"txCode": "40", "txAmt": "-100"}, True),
    ({"txCode": "41", "txDesc": "Other shop"}, True),
    ({"txCode": "41", "effectDate": "2026/07/02"}, True),
    ({"txCode": "41", "txAmt": "200"}, True),
])
def test_shadow_type_and_other_immutable_fields_limit_competing_dates(store, monkeypatch, change, keep, public, reverse):
    store.upsert_card_billed([normalized(desc="SHOP", post_date=None, bill_date="2026-08-03")])
    store.conn.execute("UPDATE card_billed_txns SET category='Manual', description_overwrite='KEEP'")
    store.commit()
    donor_id = rows(store)[0]["id"]
    incoming = [raw(txCode="40", txDesc="SHOP", postDate="2026/07/03"),
                raw(txCode="41", txDesc="SHOP", postDate="2026/07/04")]
    incoming[1].update(change)
    if reverse:
        incoming.reverse()
    _split_public_ingress(store, incoming, public, monkeypatch)
    actual = rows(store)
    assert len(actual) == 2
    donor = next(r for r in actual if r["id"] == donor_id)
    assert (donor["category"], donor["description_overwrite"]) == (("Manual", "KEEP") if keep else (None, None))
    if keep:
        assert donor["post_date"] == "2026-07-03"
    assert sum(r["description_overwrite"] == "KEEP" for r in actual) == int(keep)


@pytest.mark.parametrize("absence", [None, "", "-", " - ", "\t-\t", "   "])
@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("public", [False, True])
def test_padded_original_absence_reserves_future_exact_undated_source(store, monkeypatch, absence, reverse, public):
    store.upsert_card_billed([normalized(consume_amount=None, post_date=None, bill_date="2026-08-03")])
    store.conn.execute("UPDATE card_billed_txns SET category='Manual', description_overwrite='KEEP'")
    store.commit()
    before = rows(store)[0]
    incoming = [raw(oriAmt=None, postDate="2026/07/03"), raw(oriAmt=absence)]
    if reverse:
        incoming.reverse()
    _split_public_ingress(store, incoming, public, monkeypatch)
    actual = rows(store)
    assert len(actual) == 2
    assert actual[0] == before
    assert (actual[1]["post_date"], actual[1]["category"], actual[1]["description_overwrite"]) == ("2026-07-03", None, None)


@pytest.mark.parametrize("pending", [False, True])
@pytest.mark.parametrize("code", ["43", "20", "55", "60", "40", "41", None])
@pytest.mark.parametrize("desc,amount", [
    ("年費退回", "100"), ("分期", "100"), ("ANNUAL MEMBERSHIP FEE", "100"),
    ("手續費", "100"), ("PAYMENT", "100"), ("CASHBACK", "100"), ("REFUND", "100"),
    ("SHOP", "100"), ("SHOP", "-100"), ("SHOP", "0"), ("SHOP", None),
])
def test_shadow_classification_matches_actual_native_all_branches(store, code, desc, amount, pending):
    from backend.core.persist.ubot import _collected_card_evidence

    body = {"CardList": [raw(txCode=code, txDesc=desc, txAmt=amount, postDate="20260703")]}
    data = {"card_unbilled": body} if pending else {"card_billed": [body]}
    evidence = _collected_card_evidence(data)
    _persist_ubot(data, store, as_of=date(2026, 8, 4))
    actual = rows(store)
    assert len(evidence) == len(actual) == 1
    assert evidence[0].get("txn_type") == actual[0]["txn_type"]


@pytest.mark.parametrize("change", [
    {"oriAmt": " - "}, {"oriAmt": "\t-\t"}, {"oriAmt": "   "},
    {"oriAmt": "0"}, {"oriAmt": 0}, {"oriAmt": " 1,000 "},
    {"oriAmt": "1.234567", "Currency": " usd "},
    {"oriAmt": 1.25, "Currency": "USD"},
    {"txAmt": " 1, 000 "}, {"txAmt": "\t100\t"}, {"txAmt": None},
    {"txDesc": " \tSHOP\x1f\x1e\t ", "Currency": " twd ", "txCode": " 41 "},
    {"Currency": "", "oriAmt": None}, {"Currency": " not-a-currency ", "oriAmt": None},
])
def test_shadow_complete_projection_matches_native_money_and_text(store, change):
    from backend.core.persist.ubot import _collected_card_evidence

    data = {"card_billed": [{"CardHeader": {"stmtDate": "20260803"},
                             "CardList": [raw(postDate="20260703", **change)]}]}
    evidence = _collected_card_evidence(data)
    _persist_ubot(data, store, as_of=date(2026, 8, 4))
    actual = rows(store)[0]
    assert len(evidence) == 1
    for shadow, native in [("card_no", "card_no"), ("date", "consume_date"),
                           ("post_date", "post_date"), ("bill_date", "bill_date"),
                           ("desc", "description"), ("amount", "amount"), ("currency", "currency"),
                           ("consume_currency", "consume_currency"), ("consume_amount", "consume_amount"),
                           ("txn_type", "txn_type")]:
        assert evidence[0].get(shadow) == actual[native], (shadow, change)


@pytest.mark.parametrize("change", [
    {"txAmt": "1.5"}, {"txAmt": True}, {"txAmt": []}, {"txAmt": {}},
    {"txAmt": "NaN"}, {"txAmt": "sNaN"}, {"txAmt": "1e1000000"},
    {"oriAmt": "1.5"}, {"oriAmt": "1.0000001", "Currency": "USD"},
    {"oriAmt": "9007199254740990.1", "Currency": "USD"},
    {"oriAmt": True}, {"oriAmt": []}, {"oriAmt": {}},
    {"oriAmt": "sNaN"}, {"oriAmt": "1e1000000"},
    {"Currency": "BAD!"}, {"txDesc": ["bad"]}, {"txCode": {"bad": 1}},
    {"cardNo": ["bad"]},
])
def test_shadow_invalid_json_fields_do_not_prove_ownership_or_move_native_errors(store, change):
    from decimal import localcontext
    from backend.core.persist.ubot import _collected_card_evidence

    data = {"card_billed": [{"CardList": [raw(postDate="20260703", **change)]}]}
    with localcontext() as context:
        context.clear_flags()
        traps = dict(context.traps)
        assert _collected_card_evidence(data) == ()
        assert not any(context.flags.values())
        assert dict(context.traps) == traps
    with pytest.raises(Exception):
        _persist_ubot(data, store, as_of=date(2026, 8, 4))


@pytest.mark.parametrize("header", [None, [], ["ignored"], "ignored", 123])
def test_shadow_pending_ignores_statement_header_like_native_producer(store, header):
    from backend.core.persist.ubot import _collected_card_evidence

    data = {"card_unbilled": {"CardHeader": header, "CardList": [raw(postDate="20260703")]}}
    evidence = _collected_card_evidence(data)
    _persist_ubot(data, store, as_of=date(2026, 8, 4))
    assert len(rows(store)) == 1
    assert len(evidence) == 1
    assert evidence[0]["bill_date"] is None


@pytest.mark.parametrize("body", [None, {}, {"CardList": None}, {"CardList": []},
    {"CardList": [raw()]}, {"CardList": [raw(postDate="20260703")], "error": "failed"},
    {"CardList": [raw(postDate="20260703")], "Error": "failed"},
    {"CardList": [raw(postDate="20260703")], "errorMessage": "failed"},
])
def test_shadow_pending_fetch_and_booking_gates(body):
    from backend.core.persist.ubot import _collected_card_evidence

    assert _collected_card_evidence({"card_unbilled": body}) == ()


def test_claimed_relisted_source_is_not_a_second_undated_destination(store):
    undated = normalized(post_date=None, bill_date="2026-08-03")
    exact = normalized(bill_date="2026-08-03")
    store.upsert_card_billed([undated, exact])
    original = rows(store)
    store.conn.execute("UPDATE card_billed_txns SET category='Manual', description_overwrite='KEEP' WHERE id=?",
                       (original[0]["id"],))
    store.commit()
    store.upsert_card_billed([{**exact, "bill_date": "2026-09-03"},
                              normalized(post_date="2026-07-04", bill_date="2026-08-03")])
    actual = rows(store)
    assert len(actual) == 2
    assert (actual[0]["category"], actual[0]["description_overwrite"], actual[0]["post_date"]) == (
        "Manual", "KEEP", "2026-07-04")
    assert actual[1] == original[1]


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("public", [False, True])
@pytest.mark.parametrize("posted_count,relist_count", [(1, 1), (2, 2), (1, 2), (2, 3)])
@pytest.mark.parametrize("split_relisting", [False, True])
def test_cross_block_relisting_reserves_only_owned_occurrences(store, monkeypatch, reverse, public, posted_count, relist_count, split_relisting):
    from backend.core.persist import persist_collected, ubot
    from tests.test_ubot_history_sync import _persist_payload

    undated = normalized(post_date=None, bill_date="2026-08-03")
    posted = normalized(bill_date="2026-08-03")
    store.upsert_card_billed([undated, *([posted] * posted_count)])
    original = rows(store)
    store.conn.execute("UPDATE card_billed_txns SET category='Manual', description_overwrite='KEEP' WHERE id=?",
                       (original[0]["id"],))
    store.commit()
    blocks = [
        {"CardHeader": {"stmtDate": "2026/09/03"}, "CardList": [raw(postDate="2026/07/03")] * relist_count},
        {"CardHeader": {"stmtDate": "2026/08/03"}, "CardList": [raw(postDate="2026/07/04")]},
    ]
    if split_relisting:
        blocks = [{**blocks[0], "CardList": [raw(postDate="2026/07/03")]} for _ in range(relist_count)] + blocks[1:]
    if reverse:
        blocks.reverse()
    def persist():
        data = {"card_billed": blocks}
        if public:
            monkeypatch.setattr(ubot, "_today", lambda: date(2026, 9, 1))
            persist_collected("ubot", {**_persist_payload(), **data}, store)
        else:
            _persist_ubot(data, store, as_of=date(2026, 8, 4))
        store.commit()
    persist()
    actual = rows(store)
    assert len(actual) == relist_count + 1
    donor = next(r for r in actual if r["id"] == original[0]["id"])
    keep = posted_count == relist_count
    assert (donor["category"], donor["description_overwrite"]) == (("Manual", "KEEP") if keep else (None, None))
    if keep:
        assert donor["post_date"] == "2026-07-04"
    for saved in original[1:]:
        assert next(r for r in actual if r["id"] == saved["id"]) == saved
    persist()
    assert rows(store) == actual


@pytest.mark.parametrize("value", [None, "", 0, False, 0.0, [], {}])
@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("public", [False, True])
def test_falsy_code_future_exact_undated_source_reserves_manual(store, monkeypatch, value, reverse, public):
    store.upsert_card_billed([normalized(post_date=None, bill_date="2026-08-03")])
    store.conn.execute("UPDATE card_billed_txns SET category='Manual', description_overwrite='KEEP'")
    store.commit()
    before = rows(store)[0]
    incoming = [raw(txCode="40", postDate="2026/07/03"), raw(txCode=value)]
    if reverse:
        incoming.reverse()
    _split_public_ingress(store, incoming, public, monkeypatch)
    actual = rows(store)
    assert len(actual) == 2
    assert actual[0] == before
    assert (actual[1]["post_date"], actual[1]["description_overwrite"]) == ("2026-07-03", None)


@pytest.mark.parametrize("field", ["txCode", "txDesc", "Currency", "cardNo"])
@pytest.mark.parametrize("value", [None, "", 0, False, 0.0, [], {}])
@pytest.mark.parametrize("pending", [False, True])
def test_shadow_falsy_json_shapes_follow_native_field_coercion(store, field, value, pending):
    from backend.core.persist.ubot import _collected_card_evidence

    body = {"CardList": [raw(postDate="20260703", **{field: value})]}
    data = {"card_unbilled": body} if pending else {"card_billed": [body]}
    evidence = _collected_card_evidence(data)
    if field == "cardNo" and isinstance(value, (list, dict)):
        assert evidence == ()
        with pytest.raises(Exception):
            _persist_ubot(data, store, as_of=date(2026, 8, 4))
        return
    _persist_ubot(data, store, as_of=date(2026, 8, 4))
    actual = rows(store)
    assert len(evidence) == len(actual) == 1
    assert evidence[0]["txn_type"] == actual[0]["txn_type"]
    assert evidence[0]["desc"] == actual[0]["description"]
    assert evidence[0]["consume_currency"] == actual[0]["consume_currency"]
    assert evidence[0]["card_no"] == (value if field == "cardNo" else "****1234")


@pytest.mark.parametrize("code", [True, 1, 1.5, ["bad"], {"bad": 1}])
@pytest.mark.parametrize("pending", [False, True])
def test_truthy_bad_code_keeps_original_native_checkpoint(store, code, pending):
    from backend.core.error_codes import exception_error_id
    from backend.core.persist.ubot import _collected_card_evidence

    body = {"CardList": [raw(postDate="20260703", txCode=code)]}
    data = {"card_unbilled": body} if pending else {"card_billed": [body]}
    assert _collected_card_evidence(data) == ()
    with pytest.raises(AttributeError) as caught:
        _persist_ubot(data, store, as_of=date(2026, 8, 4))
    assert exception_error_id(caught.value) == ("UBOT-08-005-030" if pending else "UBOT-08-005-027")
    assert caught.value.args == (f"'{type(code).__name__}' object has no attribute 'strip'",)
    assert rows(store) == []


@pytest.mark.parametrize("public", [False, True])
@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("posted_count,relist_count", [(0, 2), (1, 2), (2, 2), (2, 3)])
def test_identical_source_occurrences_in_separate_blocks_survive_replay(store, monkeypatch, public, reverse, posted_count, relist_count):
    if posted_count:
        store.upsert_card_billed([normalized(bill_date="2026-07-03")] * posted_count)
    original = rows(store)
    source = [raw(postDate="2026/07/03")] * relist_count
    source.append(raw(txAmt="200", txDesc="Independent", postDate="2026/07/04"))
    if reverse:
        source.reverse()
    _split_public_ingress(store, source, public, monkeypatch)
    store.commit()
    actual = rows(store)
    assert len(actual) == relist_count + 1
    for saved in original:
        assert next(r for r in actual if r["id"] == saved["id"]) == saved
    _split_public_ingress(store, source, public, monkeypatch)
    store.commit()
    assert rows(store) == actual
