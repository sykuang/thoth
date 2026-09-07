"""Offline repair regressions against the actual BankStore schema/writer."""
import importlib
import json

import pytest

from backend.core import bank_pg
from backend.core.store import BankStore

LEGACY = "2026/08/3112:34"
CANONICAL = "2026-08-31T12:34:00"


def txn(time=LEGACY, **changes):
    return dict(account_no="00000000000001", datetime=time, account_date="2026-08-31",
                desc="手機轉帳", expend=100, income=None, balance=900,
                counterparty_bank=None, counterparty_acct="fixture peer",
                memo="fixture peer", **changes)


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(bank_pg, "DB_BACKEND", "sqlite")
    result = BankStore("sinopac", user_id=7)
    try:
        yield result
    finally:
        result.close()


def rows(store):
    return [dict(row) for row in store.conn.execute(
        "SELECT * FROM twd_transactions ORDER BY id").fetchall()]


def repair():
    assert importlib.util.find_spec("migrations.repair_sinopac_datetime") is not None, "repair module missing"
    return importlib.import_module("migrations.repair_sinopac_datetime")


def test_real_writer_repro_and_dry_run_plan(store):
    assert [store.upsert_twd_txns([txn(t)]) for t in (LEGACY, CANONICAL, CANONICAL)] == [1, 1, 0]
    before = rows(store)
    report = repair().repair_connection(store.conn, bank="sinopac", user_id=7)
    assert rows(store) == before
    assert report["conflicts"] == []
    assert len(report["actions"]) == 1
    action = report["actions"][0]
    assert action["keep_id"] == before[0]["id"]
    assert action["delete_id"] == before[1]["id"]
    assert action["after"]["dedup_key"] == before[1]["dedup_key"]
    assert set(action["audit"]) == set(before[0])
    assert report == repair().plan_repair(before, bank="sinopac", user_id=7, columns=list(before[0]))


def apply(store, tmp_path, **kwargs):
    module = repair()
    report = module.repair_connection(store.conn, bank="sinopac", user_id=7)
    return module.repair_connection(store.conn, bank="sinopac", user_id=7, execute=True,
                                   expected_digest=report["digest"], backup_path=tmp_path / "backup.json", **kwargs)


def test_apply_keeps_original_id_and_all_metadata_and_resync_noops(store, tmp_path):
    store.upsert_twd_txns([txn()])
    store.upsert_twd_txns([txn(CANONICAL)])
    store.conn.execute("UPDATE twd_transactions SET description_overwrite='my note', tags_overwrite='[]', "
                       "splits_overwrite='[]' WHERE id=1")
    store.conn.commit()
    before = rows(store)
    report = apply(store, tmp_path)
    assert report["applied"] is True
    after = rows(store)
    assert len(after) == 1
    assert after[0] == dict(before[0], txn_datetime=CANONICAL, dedup_key=before[1]["dedup_key"])
    backup = json.loads((tmp_path / "backup.json").read_text())
    assert backup["snapshot"]["rows"] == before
    assert backup["digest"] == report["digest"]
    assert store.upsert_twd_txns([txn(CANONICAL)]) == 0
    assert repair().repair_connection(store.conn, bank="sinopac", user_id=7)["actions"] == []


@pytest.mark.parametrize("column,value", [
    ("account_date", "2026-09-01"), ("description", "different display"),
    ("counterparty_bank", "999"), ("counterparty_acct", "other peer"), ("memo", "other memo"),
    ("category", "manual"), ("subcategory", "custom"), ("flow_type", "transfer"),
    ("income_category", "salary"), ("is_subscription", 1), ("auto_excluded", 1),
    ("legacy_category", "old manual"), ("description_overwrite", "conflicting note"),
    ("tags_overwrite", '["different"]'), ("splits_overwrite", '[{"amount":100}]'),
])
def test_all_nonidentity_differences_fail_closed(store, tmp_path, column, value):
    store.upsert_twd_txns([txn()])
    store.upsert_twd_txns([txn(CANONICAL)])
    if column.endswith("_overwrite"):
        store.conn.execute(f"UPDATE twd_transactions SET {column}='original' WHERE id=1")
    store.conn.execute(f"UPDATE twd_transactions SET {column}=? WHERE id=2", (value,))
    store.conn.commit()
    before = rows(store)
    report = repair().repair_connection(store.conn, bank="sinopac", user_id=7)
    assert report["conflicts"]
    assert column in report["conflicts"][0]["reason"]
    assert set(report["conflicts"][0]["audit"]) == set(before[0])
    with pytest.raises(ValueError, match="conflicts"):
        apply(store, tmp_path)
    assert rows(store) == before
    assert not (tmp_path / "backup.json").exists()
    skipped = apply(store, tmp_path, skip_conflicts=True)
    assert skipped["conflicted_ids"] == [1, 2]
    assert not skipped["actions"]
    assert rows(store) == before


def test_canonical_only_overlays_are_transferred_to_legacy_id(store, tmp_path):
    store.upsert_twd_txns([txn()])
    store.upsert_twd_txns([txn(CANONICAL)])
    store.conn.execute("UPDATE twd_transactions SET description_overwrite='', tags_overwrite='[]', "
                       "splits_overwrite='[]' WHERE id=2")
    store.conn.commit()
    before = rows(store)
    apply(store, tmp_path)
    assert rows(store)[0] == dict(before[1], id=before[0]["id"], first_seen=before[0]["first_seen"])


@pytest.mark.parametrize("time", ["2026/02/3012:34", "2026/8/3112:34", "2026/08/3124:00",
                                  "2026/08/31 12:34", "2026/08/3112:34:00", "garbage", "202608311234"])
def test_malformed_datetime_is_audited_not_repaired(store, time):
    store.upsert_twd_txns([txn(time)])
    report = repair().repair_connection(store.conn, bank="sinopac", user_id=7)
    assert not report["actions"]
    assert report["conflicts"] and "datetime" in report["conflicts"][0]["reason"]
    assert set(report["conflicts"][0]["audit"]) == set(rows(store)[0])


@pytest.mark.parametrize("corruption", ["no-occurrence", "01", "-1", "9", "wrong-base", "wrong-raw"])
def test_legacy_key_must_match_raw_fields_and_contiguous_occurrence(store, corruption):
    store.upsert_twd_txns([txn()])
    key = rows(store)[0]["dedup_key"]
    if corruption == "wrong-base":
        key = "other" + key
    elif corruption == "wrong-raw":
        store.conn.execute("UPDATE twd_transactions SET raw_description='other'")
    elif corruption == "no-occurrence":
        key = key.rsplit("\x1e", 1)[0]
    else:
        key = key.rsplit("\x1e", 1)[0] + "\x1e" + corruption
    store.conn.execute("UPDATE twd_transactions SET dedup_key=?", (key,))
    store.conn.commit()
    report = repair().repair_connection(store.conn, bank="sinopac", user_id=7)
    assert report["conflicts"] and not report["actions"]


@pytest.mark.parametrize("field,value", [("balance", 899), ("expend", 101), ("income", 1), ("desc", "different")])
def test_changed_native_values_at_same_minute_are_ambiguous(store, field, value):
    store.upsert_twd_txns([txn()])
    changed = dict(txn(CANONICAL), **{field: value})
    store.upsert_twd_txns([changed])
    report = repair().repair_connection(store.conn, bank="sinopac", user_id=7)
    assert report["conflicts"] and not report["actions"]


@pytest.mark.parametrize("canonical_count", [0, 1, 2])
def test_same_minute_occurrences_survive_rekey_and_resync(store, tmp_path, canonical_count):
    store.upsert_twd_txns([txn(), txn()])
    store.upsert_twd_txns([txn(CANONICAL)] * canonical_count)
    apply(store, tmp_path)
    after = rows(store)
    assert [r["id"] for r in after] == [1, 2]
    assert [r["dedup_key"].rsplit("\x1e", 1)[1] for r in after] == ["0", "1"]
    assert store.upsert_twd_txns([txn(CANONICAL), txn(CANONICAL)]) == 0


def test_reordered_occurrences_with_different_counterparties_fail_closed(store):
    store.upsert_twd_txns([txn(), dict(txn(), memo="second", counterparty_acct="second")])
    store.upsert_twd_txns([dict(txn(CANONICAL), memo="second", counterparty_acct="second"), txn(CANONICAL)])
    report = repair().repair_connection(store.conn, bank="sinopac", user_id=7)
    assert len(report["conflicts"]) == 2 and not report["actions"]


@pytest.mark.parametrize("extra", ["category_manual", "subcategory_manual", "flow_type_manual", "excluded", "future_field"])
def test_schema_extensions_are_preserved_or_conflict_never_ignored(store, tmp_path, extra):
    store.conn.execute(f"ALTER TABLE twd_transactions ADD COLUMN {extra} INTEGER DEFAULT 0")
    store.upsert_twd_txns([txn()])
    store.conn.execute(f"UPDATE twd_transactions SET {extra}=1")
    store.conn.commit()
    before = rows(store)
    report = repair().repair_connection(store.conn, bank="sinopac", user_id=7)
    assert report["actions"][0]["after"][extra] == 1
    assert extra in report["actions"][0]["audit"]
    store.upsert_twd_txns([txn(CANONICAL)])
    with pytest.raises(ValueError, match="conflicts"):
        apply(store, tmp_path)
    assert rows(store)[0] == before[0]


def test_scope_other_users_accounts_banks_and_unique_legacy(store, tmp_path):
    store.upsert_twd_txns([txn()])
    store.conn.execute("UPDATE twd_transactions SET category='custom', subcategory='sub', flow_type='transfer'")
    store.conn.commit()
    other_user = BankStore("sinopac", user_id=8)
    other_bank = BankStore("ctbc", user_id=7)
    try:
        other_user.upsert_twd_txns([txn(), txn(CANONICAL)])
        other_bank.upsert_twd_txns([txn(), txn(CANONICAL)])
        store.upsert_twd_txns([dict(txn(CANONICAL), account_no="00000000000002")])
        before, bank_before = rows(store), rows(other_bank)
        apply(store, tmp_path)
        after = rows(store)
        assert [r for r in after if r["id"] != 1] == [r for r in before if r["id"] != 1]
        assert after[0]["category"] == "custom" and after[0]["flow_type"] == "transfer"
        assert rows(other_bank) == bank_before
        with pytest.raises(ValueError, match="bank"):
            repair().repair_connection(other_bank.conn, bank="sinopac", user_id=7)
    finally:
        other_user.close()
        other_bank.close()


@pytest.mark.parametrize("user_id", [None, 0, -1, True, "7"])
def test_explicit_scope_validation(store, user_id):
    with pytest.raises(ValueError, match="user_id"):
        repair().repair_connection(store.conn, bank="sinopac", user_id=user_id)


def test_schema_must_include_every_known_column_and_snapshot_is_stable(store):
    store.upsert_twd_txns([txn(), dict(txn(), account_no="other")])
    before = rows(store)
    module = repair()
    report = module.plan_repair(before, bank="sinopac", user_id=7, columns=list(before[0]))
    reordered = module.plan_repair(list(reversed(before)), bank="sinopac", user_id=7, columns=list(reversed(before[0])))
    assert report == reordered
    for missing in before[0]:
        with pytest.raises(ValueError, match="schema"):
            module.plan_repair(before, bank="sinopac", user_id=7, columns=[c for c in before[0] if c != missing])
    with pytest.raises(ValueError, match="duplicate"):
        module.plan_repair(before + before, bank="sinopac", user_id=7, columns=list(before[0]))


@pytest.mark.parametrize("skip_conflicts", [False, True])
def test_apply_requires_backup_and_unchanged_digest(store, tmp_path, skip_conflicts):
    module = repair()
    store.upsert_twd_txns([txn()])
    report = module.repair_connection(store.conn, bank="sinopac", user_id=7)
    with pytest.raises(ValueError, match="requires"):
        module.repair_connection(store.conn, bank="sinopac", user_id=7, execute=True, skip_conflicts=skip_conflicts)
    store.conn.execute("UPDATE twd_transactions SET tags_overwrite='[]'")
    store.conn.commit()
    with pytest.raises(ValueError, match="snapshot changed"):
        module.repair_connection(store.conn, bank="sinopac", user_id=7, execute=True,
                                 expected_digest=report["digest"], backup_path=tmp_path / "backup.json",
                                 skip_conflicts=skip_conflicts)
    assert not (tmp_path / "backup.json").exists()
    (tmp_path / "backup.json").write_text("do not overwrite")
    before = rows(store)
    with pytest.raises(FileExistsError):
        apply(store, tmp_path, skip_conflicts=skip_conflicts)
    assert rows(store) == before
    assert (tmp_path / "backup.json").read_text() == "do not overwrite"


def test_abort_trigger_is_rejected_before_backup_or_mutation(store, tmp_path):
    store.upsert_twd_txns([txn(), txn()])
    store.upsert_twd_txns([txn(CANONICAL), txn(CANONICAL)])
    before = rows(store)
    store.conn.execute("CREATE TRIGGER fail_repair BEFORE UPDATE ON twd_transactions "
                       "WHEN NEW.id=2 BEGIN SELECT RAISE(ABORT, 'injected failure'); END")
    store.conn.commit()
    with pytest.raises(ValueError, match="trigger"):
        apply(store, tmp_path)
    assert rows(store) == before
    assert store.conn.in_transaction is False
    assert not (tmp_path / "backup.json").exists()


def test_silent_trigger_side_effect_is_rejected_before_mutation(store, tmp_path):
    store.upsert_twd_txns([txn()])
    before = rows(store)
    store.conn.execute("CREATE TRIGGER mutate_metadata AFTER UPDATE ON twd_transactions "
                       "BEGIN UPDATE twd_transactions SET category='lost' WHERE id=NEW.id; END")
    store.conn.commit()
    with pytest.raises(ValueError, match="trigger"):
        apply(store, tmp_path)
    assert rows(store) == before


def test_existing_transaction_is_never_committed_or_rolled_back(store, tmp_path):
    store.upsert_twd_txns([txn()])
    store.conn.execute("UPDATE twd_transactions SET category='uncommitted'")
    with pytest.raises(ValueError, match="idle"):
        repair().repair_connection(store.conn, bank="sinopac", user_id=7)
    assert store.conn.in_transaction
    assert rows(store)[0]["category"] == "uncommitted"
    store.conn.rollback()


@pytest.mark.parametrize("hazard", [None, "trigger", "foreign key"])
def test_postgres_adapter_transaction_contract_offline(store, tmp_path, hazard):
    # SQL-shape/ownership test only, NOT a PostgreSQL runtime test.
    from types import SimpleNamespace
    store.upsert_twd_txns([txn(), txn(CANONICAL)])
    sql_log = []

    class Adapter:
        bank = "sinopac"
        schema = "bank_sinopac"
        _conn = SimpleNamespace(info=SimpleNamespace(transaction_status=SimpleNamespace(value=0)))

        def execute(self, sql, params=()):
            sql_log.append(sql)
            if sql == "SELECT current_schema() AS schema":
                return SimpleNamespace(fetchone=lambda: {"schema": self.schema})
            if sql.startswith("LOCK TABLE"):
                assert sql == "LOCK TABLE bank_sinopac.twd_transactions IN EXCLUSIVE MODE"
                return None
            if "pg_catalog.pg_trigger" in sql:
                assert sql_log.index("LOCK TABLE bank_sinopac.twd_transactions IN EXCLUSIVE MODE") < len(sql_log) - 1
                assert "tgrelid = 'bank_sinopac.twd_transactions'::pg_catalog.regclass" in sql
                assert "NOT tgisinternal" in sql
                return SimpleNamespace(fetchone=lambda: {"present": 1} if hazard == "trigger" else None)
            if "pg_catalog.pg_constraint" in sql:
                assert sql_log.index("LOCK TABLE bank_sinopac.twd_transactions IN EXCLUSIVE MODE") < len(sql_log) - 1
                assert "confrelid = 'bank_sinopac.twd_transactions'::pg_catalog.regclass" in sql
                assert "contype = 'f'" in sql
                assert "connamespace" not in sql and "conrelid =" not in sql
                return SimpleNamespace(fetchone=lambda: {"present": 1} if hazard == "foreign key" else None)
            if "information_schema.columns" in sql:
                assert "table_schema = 'bank_sinopac'" in sql
                assert "table_name = 'twd_transactions'" in sql
                return store.conn.execute("PRAGMA main.table_info(twd_transactions)")
            return store.conn.execute(sql.replace("bank_sinopac.twd_transactions", "main.twd_transactions"), params)

        def rollback(self):
            store.conn.rollback()

        def commit(self):
            store.conn.commit()

    module = repair()
    conn = Adapter()
    report = module.repair_connection(conn, bank="sinopac", user_id=7)
    assert not store.conn.in_transaction
    if hazard:
        before = rows(store)
        with pytest.raises(ValueError, match=hazard):
            module.repair_connection(conn, bank="sinopac", user_id=7, execute=True,
                                     expected_digest=report["digest"], backup_path=tmp_path / "backup.json")
        assert rows(store) == before
        assert not store.conn.in_transaction
        assert not (tmp_path / "backup.json").exists()
        return
    module.repair_connection(conn, bank="sinopac", user_id=7, execute=True,
                             expected_digest=report["digest"], backup_path=tmp_path / "backup.json")
    assert len(rows(store)) == 1
    assert any("pg_catalog.pg_trigger" in sql for sql in sql_log)
    assert any("pg_catalog.pg_constraint" in sql for sql in sql_log)
    assert not any(sql.startswith("PRAGMA") for sql in sql_log)
    assert sql_log.index("LOCK TABLE bank_sinopac.twd_transactions IN EXCLUSIVE MODE") < sql_log.index(
        "SELECT * FROM bank_sinopac.twd_transactions WHERE user_id = ? ORDER BY id", sql_log.index("LOCK TABLE bank_sinopac.twd_transactions IN EXCLUSIVE MODE"))
    for sql in sql_log:
        if sql.startswith(("SELECT *", "UPDATE", "DELETE")):
            assert "bank_sinopac.twd_transactions" in sql
    assert not any("IMMEDIATE" in sql or "database_list" in sql for sql in sql_log)


@pytest.mark.parametrize("temporary", [False, True])
@pytest.mark.parametrize("operation", ["UPDATE", "DELETE"])
def test_late_trigger_cannot_delete_other_user(store, tmp_path, temporary, operation):
    store.upsert_twd_txns([txn(), txn(CANONICAL)])
    other = BankStore("sinopac", user_id=8)
    try:
        other.upsert_twd_txns([txn()])
    finally:
        other.close()
    module = repair()
    report = module.repair_connection(store.conn, bank="sinopac", user_id=7)
    store.conn.execute(f"CREATE {'TEMP ' if temporary else ''}TRIGGER cross_user AFTER {operation} "
                       "ON main.twd_transactions BEGIN DELETE FROM twd_transactions WHERE user_id=8; END")
    store.conn.commit()
    before = rows(store)
    sql_log = []
    store.conn.set_trace_callback(sql_log.append)
    try:
        with pytest.raises(ValueError, match="trigger"):
            module.repair_connection(store.conn, bank="sinopac", user_id=7, execute=True,
                                     expected_digest=report["digest"], backup_path=tmp_path / "backup.json")
    finally:
        store.conn.set_trace_callback(None)
    assert rows(store) == before
    assert sql_log[0] == "BEGIN IMMEDIATE"
    assert any("sqlite_master" in sql for sql in sql_log)
    assert not (tmp_path / "backup.json").exists()
    assert not store.conn.in_transaction


@pytest.mark.parametrize("operation", ["ON DELETE CASCADE", "ON DELETE SET NULL", "ON UPDATE CASCADE", "ON DELETE NO ACTION"])
def test_incoming_foreign_key_is_rejected_under_lock(store, tmp_path, operation):
    store.conn.execute("PRAGMA foreign_keys=ON")
    store.upsert_twd_txns([txn(), txn(CANONICAL)])
    module = repair()
    report = module.repair_connection(store.conn, bank="sinopac", user_id=7)
    store.conn.execute('CREATE TABLE "dependent""rows" (owner INTEGER, txn_key TEXT, '
                       'FOREIGN KEY (owner, txn_key) REFERENCES TWD_TRANSACTIONS(user_id, dedup_key) ' + operation + ')')
    # Shadow the child too: main.foreign_key_list must still inspect the real dependency.
    store.conn.execute('CREATE TEMP TABLE "dependent""rows" (unrelated TEXT)')
    store.conn.execute('INSERT INTO main."dependent""rows" VALUES (?, ?)',
                       (7, rows(store)[0 if "UPDATE" in operation else 1]["dedup_key"]))
    store.conn.commit()
    before = rows(store)
    child_before = [tuple(r) for r in store.conn.execute('SELECT * FROM main."dependent""rows"')]
    sql_log = []
    store.conn.set_trace_callback(sql_log.append)
    try:
        with pytest.raises(ValueError, match="incoming foreign key"):
            module.repair_connection(store.conn, bank="sinopac", user_id=7, execute=True,
                                     expected_digest=report["digest"], backup_path=tmp_path / "backup.json")
    finally:
        store.conn.set_trace_callback(None)
    assert sql_log[0] == "BEGIN IMMEDIATE"
    assert any("foreign_key_list" in sql for sql in sql_log)
    assert rows(store) == before
    assert [tuple(r) for r in store.conn.execute('SELECT * FROM main."dependent""rows"')] == child_before
    assert not (tmp_path / "backup.json").exists()
    assert not store.conn.in_transaction


@pytest.mark.parametrize("shadow_schema", ["full", "wrong"])
def test_temp_shadow_cannot_redirect_snapshot_or_writes(store, tmp_path, shadow_schema):
    store.upsert_twd_txns([txn(), txn(CANONICAL)])
    before = rows(store)
    if shadow_schema == "full":
        store.conn.execute("CREATE TEMP TABLE twd_transactions AS SELECT * FROM main.twd_transactions")
        store.conn.execute("UPDATE temp.twd_transactions SET category='shadow'")
    else:
        store.conn.execute("CREATE TEMP TABLE twd_transactions (unrelated TEXT)")
    store.conn.commit()
    shadow_before = [tuple(r) for r in store.conn.execute("SELECT * FROM temp.twd_transactions")]
    report = apply(store, tmp_path)
    assert report["snapshot"]["rows"] == before
    actual = [dict(r) for r in store.conn.execute("SELECT * FROM main.twd_transactions")]
    assert actual == [dict(before[0], txn_datetime=CANONICAL, dedup_key=before[1]["dedup_key"])]
    assert [tuple(r) for r in store.conn.execute("SELECT * FROM temp.twd_transactions")] == shadow_before


def test_skip_conflicts_applies_only_independent_actions(store, tmp_path):
    store.upsert_twd_txns([txn(), txn(CANONICAL)])
    store.conn.execute("UPDATE twd_transactions SET category='manual', tags_overwrite='[  ]' WHERE id=2")
    store.conn.commit()
    store.upsert_twd_txns([dict(txn(), account_no="safe-pair"),
                           dict(txn(CANONICAL), account_no="safe-pair"),
                           dict(txn(), account_no="safe-unique"), txn("invalid")])
    before = rows(store)
    frozen = [r for r in before if r["id"] in {1, 2, 6}]
    with pytest.raises(ValueError, match="conflicts"):
        apply(store, tmp_path)
    report = apply(store, tmp_path, skip_conflicts=True)
    assert report["applied"] is True
    assert report["conflicted_ids"] == [1, 2, 6]
    assert [(a["keep_id"], a["delete_id"]) for a in report["actions"]] == [(3, 4), (5, None)]
    assert [r for r in rows(store) if r["id"] in {1, 2, 6}] == frozen
    assert json.loads((tmp_path / "backup.json").read_text())["snapshot"]["rows"] == before


@pytest.mark.parametrize("reverse", [False, True])
def test_ambiguous_peer_quarantines_overlapping_safe_pair(store, tmp_path, reverse):
    inputs = [txn(), txn(CANONICAL), dict(txn(), expend=101)]
    store.upsert_twd_txns(list(reversed(inputs)) if reverse else inputs)
    before = rows(store)
    report = apply(store, tmp_path, skip_conflicts=True)
    assert report["conflicted_ids"] == [1, 2, 3]
    assert not report["actions"]
    assert rows(store) == before


@pytest.mark.parametrize("target", ["keep_id", "delete_id"])
def test_executor_rejects_planner_action_touching_conflicted_id(store, tmp_path, monkeypatch, target):
    store.upsert_twd_txns([txn(), txn(CANONICAL)])
    before = rows(store)
    module = repair()
    plan = module.plan_repair

    def broken_plan(*args, **kwargs):
        report = plan(*args, **kwargs)
        report["conflicted_ids"] = [report["actions"][0][target]]
        return report

    monkeypatch.setattr(module, "plan_repair", broken_plan)
    with pytest.raises(ValueError, match="action.*conflicted"):
        apply(store, tmp_path, skip_conflicts=True)
    assert rows(store) == before
    assert not (tmp_path / "backup.json").exists()


@pytest.mark.parametrize("phase", ["begin", "second-update", "verification", "commit", "dry-run"])
@pytest.mark.parametrize("rollback_fails", [False, True])
def test_failure_rolls_back_or_closes_and_redacts(store, tmp_path, phase, rollback_fails):
    import sqlite3
    import traceback

    store.upsert_twd_txns([txn(), txn()])
    store.upsert_twd_txns([txn(CANONICAL), txn(CANONICAL)])
    before = rows(store)
    database = store.conn.execute("PRAGMA database_list").fetchone()["file"]
    module = repair()
    reviewed = module.repair_connection(store.conn, bank="sinopac", user_id=7)
    events = []

    class FailingConnection:
        @property
        def in_transaction(self):
            return store.conn.in_transaction

        def execute(self, sql, params=()):
            if sql.startswith("UPDATE"):
                events.append("update")
                if phase == "second-update" and events.count("update") == 2:
                    raise sqlite3.OperationalError("private fixture write details")
            result = store.conn.execute(sql, params)
            if sql.startswith("BEGIN") and phase == "begin":
                raise sqlite3.OperationalError("private fixture begin details")
            if sql.startswith("UPDATE") and phase == "verification":
                store.conn.execute("UPDATE main.twd_transactions SET category='corrupted' WHERE id=1")
            return result

        def commit(self):
            if phase == "commit":
                raise sqlite3.OperationalError("private fixture commit details")
            store.conn.commit()

        def rollback(self):
            events.append("rollback")
            if rollback_fails:
                raise sqlite3.OperationalError("private fixture rollback details")
            store.conn.rollback()

        def close(self):
            events.append("close")
            store.conn.close()

    conn = FailingConnection()
    if phase == "dry-run" and not rollback_fails:
        assert module.repair_connection(conn, bank="sinopac", user_id=7)["digest"] == reviewed["digest"]
    else:
        message = "rollback failed; connection closed" if rollback_fails else (
            "verification" if phase == "verification" else "repair failed; transaction rolled back")
        with pytest.raises((RuntimeError, ValueError), match=message) as caught:
            module.repair_connection(conn, bank="sinopac", user_id=7, execute=phase != "dry-run",
                                     expected_digest=reviewed["digest"], backup_path=tmp_path / "backup.json")
        assert "private fixture" not in "".join(traceback.format_exception(caught.value))
        if rollback_fails:
            assert caught.value.__cause__ is not None
    assert events.count("rollback") == 1
    if rollback_fails:
        assert events[-1] == "close"
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            store.conn.execute("SELECT 1")
    else:
        assert "close" not in events
        assert not store.conn.in_transaction
    with sqlite3.connect(database) as check:
        check.row_factory = sqlite3.Row
        assert [dict(r) for r in check.execute("SELECT * FROM main.twd_transactions ORDER BY id")] == before
    if phase not in {"begin", "dry-run"}:
        assert json.loads((tmp_path / "backup.json").read_text())["snapshot"]["rows"] == before
    else:
        assert not (tmp_path / "backup.json").exists()


def test_postgres_rollback_quarantine_closes_raw_before_pool_release(store, monkeypatch):
    import sqlite3
    import traceback
    from types import SimpleNamespace

    store.upsert_twd_txns([txn()])
    events = []

    class Raw:
        info = SimpleNamespace(transaction_status=SimpleNamespace(value=0))
        closed = False

        def rollback(self):
            events.append("rollback")
            raise RuntimeError("private fixture rollback details")

        def close(self):
            events.append("raw-close")
            store.conn.close()
            self.closed = True

    class Checkout:
        def __exit__(self, *args):
            events.append("release-closed" if raw.closed else "release-live")
            # A live pool context could commit here. Never allow that path.
            assert raw.closed

    raw = Raw()
    conn = bank_pg.Connection.__new__(bank_pg.Connection)
    conn.bank, conn.schema, conn._closed = "sinopac", "bank_sinopac", False
    conn._conn, conn._checkout_cm = raw, Checkout()

    def execute(sql, params=()):
        if sql == "SELECT current_schema() AS schema":
            return SimpleNamespace(fetchone=lambda: {"schema": "bank_sinopac"})
        if "information_schema.columns" in sql:
            return store.conn.execute("PRAGMA main.table_info(twd_transactions)")
        return store.conn.execute(sql.replace("bank_sinopac.twd_transactions", "main.twd_transactions"), params)

    monkeypatch.setattr(conn, "execute", execute)
    with pytest.raises(repair().RollbackFailure, match="rollback failed; connection closed") as caught:
        repair().repair_connection(conn, bank="sinopac", user_id=7)
    assert "private fixture" not in "".join(traceback.format_exception(caught.value))
    assert events == ["rollback", "raw-close", "rollback", "release-closed"]
    assert conn._closed and raw.closed
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        store.conn.execute("SELECT 1")


@pytest.mark.parametrize("postgres", [False, True])
def test_rollback_close_failure_is_explicit_redacted_and_never_releases_live_pool(postgres):
    import traceback
    from types import SimpleNamespace

    events = []

    def fail_rollback():
        events.append("rollback")
        raise RuntimeError("private fixture rollback details")

    def fail_close():
        events.append("raw-close" if postgres else "close")
        raise RuntimeError("private fixture close details")

    conn = SimpleNamespace(rollback=fail_rollback, close=fail_close)
    if postgres:
        conn.schema = "bank_sinopac"
        conn._conn = SimpleNamespace(close=fail_close)
        conn.close = lambda: events.append("unsafe-release")
    with pytest.raises(repair().RollbackFailure, match="rollback failed; connection close failed; discard connection") as caught:
        repair()._rollback_or_close(conn)
    assert caught.value.__cause__ is not None
    assert "private fixture" not in "".join(traceback.format_exception(caught.value))
    assert events == ["rollback", "raw-close" if postgres else "close"]


@pytest.mark.parametrize("flag", ["execute", "skip_conflicts"])
@pytest.mark.parametrize("value", ["false", "true", 1, None])
def test_mutation_flags_require_boolean_opt_in(store, flag, value):
    with pytest.raises(ValueError, match="boolean"):
        repair().repair_connection(store.conn, bank="sinopac", user_id=7, **{flag: value})
