"""One-off, user-scoped SinoPac datetime repair; no parser or global dedup changes.

Connection API expects sqlite3.Row or the existing bank_pg adapter. Dry-run is
read-only. Reports contain full financial rows: never send them to public logs.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from backend.core.store import _dedup_key


KEY_COLUMNS = ("account_no", "txn_datetime", "expend", "income", "balance", "raw_description")
OVERLAYS = ("description_overwrite", "tags_overwrite", "splits_overwrite")
KNOWN_COLUMNS = set(KEY_COLUMNS + OVERLAYS + (
    "id", "user_id", "account_date", "description", "counterparty_bank", "counterparty_acct",
    "memo", "first_seen", "dedup_key", "category", "subcategory", "flow_type", "income_category",
    "auto_excluded", "is_subscription", "legacy_category",
))


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def plan_repair(rows, *, bank, user_id, columns):
    """Pure planner. Input is a complete SELECT * snapshot for the selected user."""
    if bank != "sinopac" or type(user_id) is not int or user_id < 1:
        raise ValueError("explicit SinoPac bank and positive user_id required")
    selected = sorted((dict(r) for r in rows if r["user_id"] == user_id), key=lambda r: r["id"])
    if not set(columns) >= KNOWN_COLUMNS or any(set(r) != set(columns) for r in selected):
        raise ValueError("incomplete schema / SELECT * snapshot")
    if len({r["id"] for r in selected}) != len(selected) or len({r["dedup_key"] for r in selected}) != len(selected):
        raise ValueError("duplicate row identity in snapshot")
    snapshot = dict(bank=bank, user_id=user_id, columns=sorted(columns), rows=selected)
    report: dict = dict(snapshot=snapshot, digest=hashlib.sha256(_json(snapshot).encode()).hexdigest(),
                  actions=[], conflicts=[])
    by_key = {r["dedup_key"]: r for r in selected}
    groups = defaultdict(list)
    parsed = {}
    invalid = {}
    canonical_peers = defaultdict(list)
    conflicted_ids = set()
    for row in selected:
        time = row["txn_datetime"]
        legacy = isinstance(time, str) and re.fullmatch(r"[0-9]{4}/[0-9]{2}/[0-9]{2}[0-9]{2}:[0-9]{2}", time)
        current = isinstance(time, str) and re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:00", time)
        try:
            if not legacy and not current:
                raise ValueError
            canonical = datetime.strptime(time, "%Y/%m/%d%H:%M" if legacy else "%Y-%m-%dT%H:%M:%S").isoformat(timespec="seconds")
        except ValueError:
            invalid[row["id"]] = "invalid datetime"
            continue
        base = _dedup_key(*(row[c] for c in KEY_COLUMNS))
        # Reject delimiter ambiguity instead of reconstructing an identity by guesswork.
        if any(isinstance(row[c], str) and any(s in row[c] for s in ("\x1e", "\x1f")) for c in KEY_COLUMNS):
            invalid[row["id"]] = "ambiguous dedup delimiters"
            continue
        match = re.fullmatch(re.escape(base) + r"\x1e(0|[1-9][0-9]*)", row["dedup_key"])
        if not match:
            invalid[row["id"]] = "dedup key does not match raw fields and occurrence"
            continue
        occurrence = int(match.group(1))
        groups[base].append((occurrence, row["id"]))
        parsed[row["id"]] = (bool(legacy), canonical, occurrence)
        if current:
            canonical_peers[(row["account_no"], canonical)].append((base, row["id"]))
    for group in groups.values():
        if sorted(n for n, _ in group) != list(range(len(group))):
            for _, row_id in group:
                invalid[row_id] = "ambiguous occurrence gap"
    for row in selected:
        reason = invalid.get(row["id"])
        if reason:
            conflicted_ids.add(row["id"])
            report["conflicts"].append(dict(id=row["id"], reason=reason,
                audit={c: dict(legacy=row[c], canonical=None, after=row[c]) for c in columns}))
            continue
        legacy, canonical, occurrence = parsed[row["id"]]
        if not legacy:
            continue
        after = dict(row, txn_datetime=canonical)
        base = _dedup_key(*(after[c] for c in KEY_COLUMNS))
        after["dedup_key"] = base + "\x1e" + str(occurrence)
        counterpart = by_key.get(after["dedup_key"])
        differences = []
        if counterpart:
            if counterpart["id"] in invalid:
                differences.append("invalid canonical counterpart")
            for c in columns:
                if c in {"id", "txn_datetime", "dedup_key", "first_seen"} or row[c] == counterpart[c]:
                    continue
                if c in OVERLAYS and (row[c] is None or counterpart[c] is None):
                    after[c] = counterpart[c] if row[c] is None else row[c]
                else:
                    differences.append(c)
        elif any(peer != base for peer, _ in canonical_peers[(row["account_no"], canonical)]):
            differences.append("ambiguous changed native values at same minute")
            conflicted_ids.update(peer_id for _, peer_id in canonical_peers[(row["account_no"], canonical)])
        audit = {c: dict(legacy=row[c], canonical=counterpart[c] if counterpart else None,
                         after=after[c]) for c in columns}
        if differences:
            conflicted_ids.add(row["id"])
            if counterpart:
                conflicted_ids.add(counterpart["id"])
            report["conflicts"].append(dict(id=row["id"], reason="different: " + ", ".join(differences), audit=audit))
            continue
        report["actions"].append(dict(
            keep_id=row["id"], delete_id=counterpart["id"] if counterpart else None,
            after=after, audit=audit))
    # Quarantine both ends of any action connected to a conflict, irrespective of row order.
    while True:
        overlapping = [a for a in report["actions"] if {a["keep_id"], a["delete_id"]} & conflicted_ids]
        if not overlapping:
            break
        for action in overlapping:
            conflicted_ids.add(action["keep_id"])
            if action["delete_id"] is not None:
                conflicted_ids.add(action["delete_id"])
            report["conflicts"].append(dict(id=action["keep_id"], reason="action overlaps conflicted rows",
                                             audit=action["audit"]))
            report["actions"].remove(action)
    report["conflicted_ids"] = sorted(conflicted_ids)
    return report


class RollbackFailure(RuntimeError):
    """The connection must be discarded, never reused or committed."""


def _rollback_or_close(conn):
    try:
        conn.rollback()
    except BaseException:
        try:
            if hasattr(conn, "schema"):
                # bank_pg.close() exits a pool context that may commit: kill the socket first.
                conn._conn.close()
                try:
                    conn.close()
                except BaseException:
                    pass  # Raw connection is closed; adapter cleanup may re-raise rollback.
            else:
                conn.close()
        except BaseException:
            raise RollbackFailure("rollback failed; connection close failed; discard connection") from RuntimeError(
                "database cleanup error details suppressed")
        raise RollbackFailure("rollback failed; connection closed; discard connection") from RuntimeError(
            "database rollback error details suppressed")


def repair_connection(conn, *, bank, user_id, execute=False, expected_digest=None, backup_path=None,
                      skip_conflicts: bool = False):
    """Own an idle connection; back up the complete scoped snapshot before writes.

    PostgreSQL callers supply the existing bank_pg.Connection, not raw psycopg.
    backup_path is a NEW private logical row-backup file, fsynced under the lock.
    Persist/copy it out of ephemeral cloud storage before retiring the job.
    skip_conflicts applies only independent actions, never resolving a conflict.
    """
    import os

    if type(execute) is not bool or type(skip_conflicts) is not bool:
        raise ValueError("execute and skip_conflicts must be boolean opt-ins")
    if bank != "sinopac" or type(user_id) is not int or user_id < 1:
        raise ValueError("explicit SinoPac bank and positive user_id required")
    postgres = hasattr(conn, "schema")
    table = "bank_sinopac.twd_transactions" if postgres else "main.twd_transactions"
    if postgres:
        if conn.bank != bank or conn.schema != "bank_sinopac":
            raise ValueError("connection bank must be sinopac")
        active = conn._conn.info.transaction_status.value != 0
    else:
        active = conn.in_transaction
    if active:
        raise ValueError("repair requires an idle connection")
    if execute:
        if not expected_digest or backup_path is None:
            raise ValueError("apply requires reviewed expected_digest and new backup_path")
    try:
        conn.execute("BEGIN IMMEDIATE" if execute and not postgres else "BEGIN")
        if postgres:
            if conn.execute("SELECT current_schema() AS schema").fetchone()["schema"] != "bank_sinopac":
                raise ValueError("connection bank search_path must be bank_sinopac")
            if execute:
                # ponytail: one-off table lock; do not reuse for online maintenance.
                conn.execute("LOCK TABLE bank_sinopac.twd_transactions IN EXCLUSIVE MODE")
        else:
            databases = conn.execute("PRAGMA database_list").fetchall()
            if not any(r["name"] == "main" and Path(r["file"]).name == "sinopac.sqlite" for r in databases):
                raise ValueError("connection bank must be sinopac.sqlite")
        if execute:
            trigger_sql = (
                "SELECT 1 FROM pg_catalog.pg_trigger "
                "WHERE tgrelid = 'bank_sinopac.twd_transactions'::pg_catalog.regclass AND NOT tgisinternal LIMIT 1"
                if postgres else
                "SELECT 1 FROM main.sqlite_master WHERE type='trigger' AND tbl_name='twd_transactions' COLLATE NOCASE "
                "UNION ALL SELECT 1 FROM temp.sqlite_master "
                "WHERE type='trigger' AND tbl_name='twd_transactions' COLLATE NOCASE LIMIT 1"
            )
            if conn.execute(trigger_sql).fetchone() is not None:
                raise ValueError("non-internal trigger present; nothing applied")
            if postgres:
                incoming = conn.execute(
                    "SELECT 1 FROM pg_catalog.pg_constraint WHERE contype = 'f' "
                    "AND confrelid = 'bank_sinopac.twd_transactions'::pg_catalog.regclass LIMIT 1"
                ).fetchone() is not None
            else:
                incoming = False
                for child in conn.execute("SELECT name FROM main.sqlite_master WHERE type='table'").fetchall():
                    name = child["name"].replace('"', '""')
                    if any(fk["table"].lower() == "twd_transactions" for fk in
                           conn.execute(f'PRAGMA main.foreign_key_list("{name}")').fetchall()):
                        incoming = True
                        break
            # Refuse even NO ACTION dependencies rather than guess which columns are safe.
            if incoming:
                raise ValueError("incoming foreign key present; nothing applied")
        column_sql = ("SELECT column_name AS name FROM information_schema.columns "
                      "WHERE table_schema = 'bank_sinopac' AND table_name = 'twd_transactions' "
                      "ORDER BY ordinal_position" if postgres else "PRAGMA main.table_info(twd_transactions)")
        columns = [r["name"] for r in conn.execute(column_sql).fetchall()]
        rows = conn.execute(f"SELECT * FROM {table} WHERE user_id = ? ORDER BY id", (user_id,)).fetchall()
        report = plan_repair(rows, bank=bank, user_id=user_id, columns=columns)
        conflicted_ids = set(report["conflicted_ids"]) | {c["id"] for c in report["conflicts"]}
        if any({a["keep_id"], a["delete_id"]} & conflicted_ids for a in report["actions"]):
            raise ValueError("planner action touches conflicted rows; nothing applied")
        if not execute:
            _rollback_or_close(conn)
            return report
        if report["digest"] != expected_digest:
            raise ValueError("snapshot changed since review")
        if report["conflicts"] and not skip_conflicts:
            raise ValueError("conflicts require manual review; nothing applied")
        assert backup_path is not None
        fd = os.open(backup_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as backup:
            backup.write(_json(report))
            backup.flush()
            os.fsync(backup.fileno())
        for action in report["actions"]:
            if action["delete_id"] is not None:
                conn.execute(f"DELETE FROM {table} WHERE user_id=? AND id=?",
                             (user_id, action["delete_id"]))
            after = action["after"]
            conn.execute(f"UPDATE {table} SET txn_datetime=?, dedup_key=?, "
                         "description_overwrite=?, tags_overwrite=?, splits_overwrite=? WHERE user_id=? AND id=?",
                         (after["txn_datetime"], after["dedup_key"], *(after[c] for c in OVERLAYS),
                          user_id, action["keep_id"]))
        expected = {r["id"]: dict(r) for r in rows}
        for action in report["actions"]:
            expected.pop(action["delete_id"], None)
            expected[action["keep_id"]] = action["after"]
        actual = conn.execute(f"SELECT * FROM {table} WHERE user_id = ? ORDER BY id", (user_id,)).fetchall()
        if [dict(r) for r in actual] != [expected[k] for k in sorted(expected)]:
            raise ValueError("post-write verification failed; rolling back")
        conn.commit()
        return dict(report, applied=True)
    except RollbackFailure:
        raise
    except BaseException as error:
        _rollback_or_close(conn)
        if isinstance(error, (ValueError, OSError)) or not isinstance(error, Exception):
            raise
        raise RuntimeError("repair failed; transaction rolled back; database error details suppressed") from None
