"""樂天國際銀行網銀 DOM collect → normalized store。"""
from __future__ import annotations

import json
import re
from datetime import datetime

from backend.core.account_classify import ProductType
from backend.core.persist._common import _num_real, _slash_date_to_iso
from backend.core.store import BankStore


def _is_income(value: object) -> bool:
    return value is True or str(value).strip().lower() in {"1", "true", "+", "income", "credit"}


def _money(value: object) -> float | None:
    raw = "" if value is None else str(value)
    cleaned = re.sub(r"[^0-9.+-]", "", raw)
    return _num_real(cleaned)


def _txn_datetime(row: dict) -> str | None:
    date = _slash_date_to_iso(row.get("sysDate"))
    if not date:
        return None
    time = str(row.get("sysTime") or "").strip()
    if not time:
        return date
    if len(time) == 5:
        time += ":00"
    return f"{date}T{time}"


def persist_rakuten(
    data: dict,
    store: BankStore,
    rules: list[dict] | None = None,
) -> dict:
    """寫入樂天活存帳戶、每日餘額與六個月交易明細。"""
    today = datetime.now().strftime("%Y-%m-%d")
    results = data.get("twd_txn_results") or []
    accounts_by_no: dict[str, dict] = {}
    txns: list[dict] = []
    seen_snapshots: set[str] = set()

    for result in results:
        if not isinstance(result, dict):
            continue
        fingerprint = json.dumps(result, ensure_ascii=False, sort_keys=True, default=str)
        if fingerprint in seen_snapshots:
            continue
        seen_snapshots.add(fingerprint)
        raw_accounts = result.get("accounts") or []
        fallback_no = result.get("account_no")
        for raw in raw_accounts:
            if not isinstance(raw, dict):
                continue
            account_no = raw.get("acctNo") or raw.get("account_no")
            if not account_no:
                continue
            balance = _money(raw.get("balance"))
            accounts_by_no[str(account_no)] = {
                "account_no": str(account_no),
                "currency": "TWD",
                "branch": None,
                "nickname": raw.get("nickname") or "樂天活存",
                "type": "活期存款",
                "product_type": ProductType.DEPOSIT,
                "raw_balance": balance,
                "raw_balance_date": today if balance is not None else None,
            }
            fallback_no = fallback_no or account_no

        for row in result.get("txDetails") or []:
            if not isinstance(row, dict):
                continue
            when = _txn_datetime(row)
            amount = _money(row.get("amt"))
            desc = str(row.get("txDesc") or "").strip()
            if not when or amount is None or not desc:
                continue
            account_no = row.get("account_no") or fallback_no
            if not account_no and len(accounts_by_no) == 1:
                account_no = next(iter(accounts_by_no))
            if not account_no:
                continue
            income = _is_income(row.get("amtSign"))
            txns.append({
                "account_no": str(account_no),
                "datetime": when,
                "account_date": when[:10],
                "desc": desc,
                "expend": None if income else abs(amount),
                "income": abs(amount) if income else None,
                "balance": _money(row.get("balance")),
                "counterparty_bank": row.get("bankId"),
                "counterparty_acct": row.get("nickNameOrAcct"),
                "memo": str(row.get("memo") or "").strip() or None,
            })

    accounts = list(accounts_by_no.values())
    if accounts:
        store.upsert_accounts(accounts)

    balances = [a["raw_balance"] for a in accounts if a["raw_balance"] is not None]
    balance_days = 0
    if balances:
        store.upsert_balance_history([{
            "snapshotDate": today,
            "twdBalance": sum(balances),
            "fxBalance": None,
            "loanBalance": None,
        }])
        balance_days = 1

    twd_new = store.upsert_twd_txns(txns, rules=rules) if txns else 0
    endpoints = data.get("_all_endpoints") or []
    if endpoints:
        store.put_daily_metric("rakuten_endpoints", {"endpoints": endpoints}, today)

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
    store.log_sync(delta)
    return delta
