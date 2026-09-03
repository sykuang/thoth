"""樂天國際銀行 attested DOM collect → normalized store。"""
from __future__ import annotations

from calendar import monthrange
from datetime import date, datetime, timedelta
import json
import re
from zoneinfo import ZoneInfo

from backend.banks.rakuten import RakutenCrawler
from backend.core.account_classify import ProductType
from backend.core.base import validate_history_coverage
from backend.core.persist._common import _num_real, _slash_date_to_iso
from backend.core.store import BankStore


def _is_income(value: object) -> bool:
    return value is True


def _money(value: object) -> float | None:
    raw = "" if value is None else str(value)
    cleaned = re.sub(r"[^0-9.+-]", "", raw)
    return _num_real(cleaned)


def _txn_datetime(row: dict) -> str | None:
    day = _slash_date_to_iso(row.get("sysDate"))
    if not day:
        return None
    time = str(row.get("sysTime") or "").strip()
    return f"{day}T{time}" if time else day


def _today() -> date:
    return datetime.now(ZoneInfo("Asia/Taipei")).date()


def _iso_date(value: object, error: str) -> date:
    if not isinstance(value, str) or re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) is None:
        raise ValueError(error)
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ValueError(error) from None


def _six_month_floor(as_of: date) -> date:
    month_index = as_of.year * 12 + as_of.month - 1 - 5
    return date(month_index // 12, month_index % 12 + 1, 1)


def _validate_rakuten_history(data: dict, store: BankStore) -> date:
    error = "invalid Rakuten history coverage"
    coverage = data.get("history_coverage")
    if not isinstance(coverage, dict):
        raise ValueError(error)
    mode = coverage.get("mode")
    if mode not in {"full", "incremental"}:
        raise ValueError(error)
    as_of = _iso_date(coverage.get("as_of"), error)
    today = _today()
    if as_of > today or (today - as_of).days > 1:
        raise ValueError(error)
    validate_history_coverage(
        coverage,
        expected_mode=mode,
        expected_domains=frozenset({"twd_transactions"}),
    )
    try:
        encoded = json.dumps(
            {
                "history_coverage": coverage,
                "account_options": data.get("account_options"),
                "twd_txn_results": data.get("twd_txn_results"),
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
    except (TypeError, ValueError, RecursionError):
        raise ValueError(error) from None
    if len(encoded) > 5_000_000:
        raise ValueError(error)

    domain = coverage["domains"][0]
    expected = domain["expected"]
    windows = domain["windows"]
    expected_by_identity = {
        item["identity"]: item for item in expected if isinstance(item, dict)
    }
    inventory = data.get("account_options")
    inventory_ids = []
    if isinstance(inventory, list):
        for item in inventory:
            if (
                not isinstance(item, dict)
                or set(item) != {"identity"}
                or not isinstance(item.get("identity"), str)
            ):
                raise ValueError(error)
            inventory_ids.append(item["identity"])
    results = data.get("twd_txn_results")
    if (
        not expected
        or len(expected_by_identity) != len(expected)
        or len(inventory_ids) != len(set(inventory_ids))
        or set(inventory_ids) != set(expected_by_identity)
        or not isinstance(results, list)
        or len(results) != len(windows)
    ):
        raise ValueError(error)

    receipts = []
    identities = set()
    for result in results:
        try:
            receipt = RakutenCrawler._validated_history_result(result)
        except RuntimeError:
            raise ValueError(error) from None
        identity = result["account_no"]
        if identity not in expected_by_identity:
            raise ValueError(error)
        identities.add(identity)
        receipts.append({
            key: receipt[key]
            for key in ("identity", "start", "end", "status", "pages")
        })
        start = _iso_date(receipt["start"], error)
        end = _iso_date(receipt["end"], error)
        native_end = date(start.year, start.month, monthrange(start.year, start.month)[1])
        if end != min(native_end, as_of):
            raise ValueError(error)
    if receipts != windows or identities != set(expected_by_identity):
        raise ValueError(error)

    floor = _six_month_floor(as_of)
    existing = store.latest_twd_transaction_dates()
    for identity, item in expected_by_identity.items():
        cursor = existing.get(identity)
        if mode == "incremental" and type(cursor) is date and cursor > as_of:
            raise ValueError(error)
        expected_start = floor
        if mode == "incremental" and type(cursor) is date:
            expected_start = max(floor, (cursor - timedelta(days=7)).replace(day=1))
        if (
            _iso_date(item.get("start"), error) != expected_start
            or _iso_date(item.get("end"), error) != as_of
        ):
            raise ValueError(error)
    return as_of


def _persist_rakuten(
    data: dict,
    store: BankStore,
    rules: list[dict] | None,
    *,
    as_of: date,
) -> dict:
    today = as_of.isoformat()
    results = data["twd_txn_results"]
    accounts_by_no: dict[str, dict] = {}
    txns: list[dict] = []

    for result in results:
        raw = result["accounts"][0]
        account_no = result["account_no"]
        balance = _money(raw["balance"])
        accounts_by_no[account_no] = {
            "account_no": account_no,
            "currency": "TWD",
            "branch": None,
            "nickname": "樂天活存",
            "type": "活期存款",
            "product_type": ProductType.DEPOSIT,
            "raw_balance": balance,
            "raw_balance_date": today,
        }
        for row in result["txDetails"]:
            when = _txn_datetime(row)
            amount = _money(row["amt"])
            if when is None or amount is None:
                raise ValueError("invalid Rakuten history coverage")
            txns.append({
                "account_no": account_no,
                "datetime": when,
                "account_date": when[:10],
                "desc": row["txDesc"].strip(),
                "expend": None if _is_income(row["amtSign"]) else abs(amount),
                "income": abs(amount) if _is_income(row["amtSign"]) else None,
                "balance": _money(row["balance"]),
                "counterparty_bank": None,
                "counterparty_acct": row["nickNameOrAcct"],
                "memo": row["memo"].strip() or None,
            })

    accounts = list(accounts_by_no.values())
    store.upsert_accounts(accounts, commit=False)
    balances = [account["raw_balance"] for account in accounts]
    balance_days = 0
    if balances:
        store.upsert_balance_history([{
            "snapshotDate": today,
            "twdBalance": sum(balances),
            "fxBalance": None,
            "loanBalance": None,
        }], commit=False)
        balance_days = 1
    twd_new = store.upsert_twd_txns(txns, rules=rules, commit=False) if txns else 0
    delta = {
        "bank": "rakuten",
        "scope": "structured",
        "accounts": len(accounts),
        "balance_days": balance_days,
        "twd_txn_new": twd_new,
        "card_billed_new": 0,
        "card_unbilled": 0,
        "card_current": 0,
    }
    store.log_sync(delta, commit=False)
    return delta


def persist_rakuten(
    data: dict,
    store: BankStore,
    rules: list[dict] | None = None,
    *,
    commit: bool = True,
) -> dict:
    """Validate and persist one Rakuten history payload atomically."""
    try:
        as_of = _validate_rakuten_history(data, store)
        delta = _persist_rakuten(data, store, rules, as_of=as_of)
        if commit:
            store.commit()
        return delta
    except Exception:
        store.conn.rollback()
        raise
