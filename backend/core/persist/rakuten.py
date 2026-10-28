"""樂天國際銀行 attested DOM collect → normalized store。"""
from __future__ import annotations

from backend.core.error_codes import error_step, exception_error_id, with_error_code
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


def _money(value: object, *, _codes: tuple[str, ...] | None = None) -> float | None:
    with error_step(_codes[9] if _codes is not None and len(_codes) > 9 else None):
        raw = "" if value is None else str(value)
        cleaned = re.sub(r"[^0-9.+-]", "", raw)
        with error_step(_codes[9] if _codes is not None and len(_codes) > 9 else 'RAKUTEN-08-002-001'):
            return _num_real(cleaned, _codes=_codes)


def _txn_datetime(row: dict) -> str | None:
    with error_step('RAKUTEN-08-003-001'):
        day = _slash_date_to_iso(row.get("sysDate"))
    if not day:
        return None
    time = str(row.get("sysTime") or "").strip()
    return f"{day}T{time}" if time else day


def _today() -> date:
    return datetime.now(ZoneInfo("Asia/Taipei")).date()


def _iso_date(value: object, error: str) -> date:
    if not isinstance(value, str):
        raise with_error_code(ValueError(error), 'RAKUTEN-08-005-001')
    elif re.fullmatch('\\d{4}-\\d{2}-\\d{2}', value) is None:
        raise with_error_code(ValueError(error), 'RAKUTEN-08-005-002')
    try:
        with error_step('RAKUTEN-08-005-004'):
            return date.fromisoformat(value)
    except ValueError:
        raise with_error_code(ValueError(error), 'RAKUTEN-08-005-003') from None


def _six_month_floor(as_of: date) -> date:
    month_index = as_of.year * 12 + as_of.month - 1 - 5
    return date(month_index // 12, month_index % 12 + 1, 1)


def _validate_rakuten_history(data: dict, store: BankStore) -> date:
    error = "invalid Rakuten history coverage"
    coverage = data.get("history_coverage")
    if not isinstance(coverage, dict):
        raise with_error_code(ValueError(error), 'RAKUTEN-08-007-016')
    mode = coverage.get("mode")
    if mode not in {"full", "incremental"}:
        raise with_error_code(ValueError(error), 'RAKUTEN-08-007-017')
    with error_step('RAKUTEN-08-007-024'):
        as_of = _iso_date(coverage.get("as_of"), error)
    today = _today()
    if as_of > today:
        raise with_error_code(ValueError(error), 'RAKUTEN-08-007-001')
    elif (today - as_of).days > 1:
        raise with_error_code(ValueError(error), 'RAKUTEN-08-007-002')
    with error_step('RAKUTEN-08-007-025'):
        validate_history_coverage(
            coverage,
            expected_mode=mode,
            expected_domains=frozenset({"twd_transactions"}),
        )
    try:
        with error_step('RAKUTEN-08-007-027'):
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
        raise with_error_code(ValueError(error), 'RAKUTEN-08-007-018') from None
    if len(encoded) > 5_000_000:
        raise with_error_code(ValueError(error), 'RAKUTEN-08-007-019')

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
            if not isinstance(item, dict):
                raise with_error_code(ValueError(error), 'RAKUTEN-08-007-003')
            elif set(item) != {'identity'}:
                raise with_error_code(ValueError(error), 'RAKUTEN-08-007-004')
            elif not isinstance(item.get('identity'), str):
                raise with_error_code(ValueError(error), 'RAKUTEN-08-007-005')
            inventory_ids.append(item["identity"])
    results = data.get("twd_txn_results")
    if not expected:
        raise with_error_code(ValueError(error), 'RAKUTEN-08-007-006')
    elif len(expected_by_identity) != len(expected):
        raise with_error_code(ValueError(error), 'RAKUTEN-08-007-007')
    elif len(inventory_ids) != len(set(inventory_ids)):
        raise with_error_code(ValueError(error), 'RAKUTEN-08-007-008')
    elif set(inventory_ids) != set(expected_by_identity):
        raise with_error_code(ValueError(error), 'RAKUTEN-08-007-009')
    elif not isinstance(results, list):
        raise with_error_code(ValueError(error), 'RAKUTEN-08-007-010')
    elif len(results) != len(windows):
        raise with_error_code(ValueError(error), 'RAKUTEN-08-007-011')

    receipts = []
    identities = set()
    for result in results:
        try:
            with error_step('RAKUTEN-08-007-030'):
                receipt = RakutenCrawler._validated_history_result(result)
        except RuntimeError as source:
            raise with_error_code(ValueError(error), exception_error_id(source, bank="rakuten") or 'RAKUTEN-08-007-020') from None
        identity = result["account_no"]
        if identity not in expected_by_identity:
            raise with_error_code(ValueError(error), 'RAKUTEN-08-007-021')
        identities.add(identity)
        receipts.append({
            key: receipt[key]
            for key in ("identity", "start", "end", "status", "pages")
        })
        with error_step('RAKUTEN-08-007-028'):
            start = _iso_date(receipt["start"], error)
        with error_step('RAKUTEN-08-007-029'):
            end = _iso_date(receipt["end"], error)
        native_end = date(start.year, start.month, monthrange(start.year, start.month)[1])
        if end != min(native_end, as_of):
            raise with_error_code(ValueError(error), 'RAKUTEN-08-007-022')
    if receipts != windows:
        raise with_error_code(ValueError(error), 'RAKUTEN-08-007-012')
    elif identities != set(expected_by_identity):
        raise with_error_code(ValueError(error), 'RAKUTEN-08-007-013')

    floor = _six_month_floor(as_of)
    with error_step('RAKUTEN-08-007-026'):
        existing = store.latest_twd_transaction_dates()
    for identity, item in expected_by_identity.items():
        cursor = existing.get(identity)
        if mode == "incremental" and type(cursor) is date and cursor > as_of:
            raise with_error_code(ValueError(error), 'RAKUTEN-08-007-023')
        expected_start = floor
        if mode == "incremental" and type(cursor) is date:
            expected_start = max(floor, (cursor - timedelta(days=7)).replace(day=1))
        if _iso_date(item.get('start'), error) != expected_start:
            raise with_error_code(ValueError(error), 'RAKUTEN-08-007-014')
        elif _iso_date(item.get('end'), error) != as_of:
            raise with_error_code(ValueError(error), 'RAKUTEN-08-007-015')
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
        with error_step('RAKUTEN-08-008-006'):
            balance = _money(raw["balance"], _codes=('SYS-08-001-001', 'RAKUTEN-08-830-002', 'SYS-08-001-003', 'RAKUTEN-08-830-004', 'RAKUTEN-08-830-005', 'RAKUTEN-08-830-006', 'SYS-08-001-007', 'RAKUTEN-08-830-008', 'RAKUTEN-08-830-009', 'RAKUTEN-08-830-010'))
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
            with error_step('RAKUTEN-08-008-008'):
                when = _txn_datetime(row)
            with error_step('RAKUTEN-08-008-009'):
                amount = _money(row["amt"], _codes=('SYS-08-001-001', 'RAKUTEN-08-831-002', 'SYS-08-001-003', 'RAKUTEN-08-831-004', 'RAKUTEN-08-831-005', 'RAKUTEN-08-831-006', 'SYS-08-001-007', 'RAKUTEN-08-831-008', 'RAKUTEN-08-831-009', 'RAKUTEN-08-831-010'))
            if when is None:
                raise with_error_code(ValueError('invalid Rakuten history coverage'), 'RAKUTEN-08-008-001')
            elif amount is None:
                raise with_error_code(ValueError('invalid Rakuten history coverage'), 'RAKUTEN-08-008-002')
            with error_step('RAKUTEN-08-008-010'):
                txns.append({
                    "account_no": account_no,
                    "datetime": when,
                    "account_date": when[:10],
                    "desc": row["txDesc"].strip(),
                    "expend": None if _is_income(row["amtSign"]) else abs(amount),
                    "income": abs(amount) if _is_income(row["amtSign"]) else None,
                    "balance": _money(row["balance"], _codes=('SYS-08-001-001', 'RAKUTEN-08-832-002', 'SYS-08-001-003', 'RAKUTEN-08-832-004', 'RAKUTEN-08-832-005', 'RAKUTEN-08-832-006', 'SYS-08-001-007', 'RAKUTEN-08-832-008', 'RAKUTEN-08-832-009', 'RAKUTEN-08-832-010')),
                    "counterparty_bank": None,
                    "counterparty_acct": row["nickNameOrAcct"],
                    "memo": row["memo"].strip() or None,
                })

    accounts = list(accounts_by_no.values())
    with error_step('RAKUTEN-08-008-003'):
        store.upsert_accounts(accounts, commit=False)
    balances = [account["raw_balance"] for account in accounts]
    balance_days = 0
    if balances:
        with error_step('RAKUTEN-08-008-007'):
            store.upsert_balance_history([{
                "snapshotDate": today,
                "twdBalance": sum(balances),
                "fxBalance": None,
                "loanBalance": None,
            }], commit=False)
        balance_days = 1
    with error_step('RAKUTEN-08-008-004'):
        twd_new = store.upsert_twd_txns(
            txns, rules=rules, commit=False,
            suppress_history_cursor=data.get("history_coverage") is not None,
        ) if txns else 0
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
    with error_step('RAKUTEN-08-008-005'):
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
        with error_step('RAKUTEN-08-009-001'):
            as_of = _validate_rakuten_history(data, store)
        delta = _persist_rakuten(data, store, rules, as_of=as_of)
        if commit:
            with error_step('RAKUTEN-08-009-002'):
                store.commit()
        return delta
    except Exception:
        with error_step('RAKUTEN-08-009-003'):
            store.conn.rollback()
        raise
