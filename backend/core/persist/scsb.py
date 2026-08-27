"""上海商銀 (SCSB) persist 邏輯.

從 persist.py 拆分而來 (2026-06-17, W persist.py split refactor)。
"""
from __future__ import annotations

import math
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation

from backend.core import account_classify, classify
from backend.core.store import BankStore
from backend.core.persist._common import _num_real


def _scsb_page_error(text: str) -> bool:
    lower = text.lower()
    return any(marker in lower for marker in (
        "系統錯誤", "系統忙碌", "請稍後再試", "連線逾時", "連線已逾時", "請重新登入", "登入失效",
        "system error", "try again later", "session expired", "login required", "timed out",
        "timeout", "log in again", "login again", "unexpected error",
    ))


def _valid_twd_record(record: dict) -> bool:
    try:
        datetime.strptime(record.get("date") or "", "%Y/%m/%d")
    except (TypeError, ValueError):
        return False
    money = re.compile(r"^-?(?:\d{1,15}|\d{1,3}(?:,\d{3}){1,4})$")
    expense = str(record.get("expense") or "")
    deposit = str(record.get("deposit") or "")
    balance = str(record.get("balance") or "")
    return (
        isinstance(record.get("summary"), str)
        and bool(record["summary"].strip())
        and bool(expense) != bool(deposit)
        and (not expense or bool(money.fullmatch(expense)))
        and (not deposit or bool(money.fullmatch(deposit)))
        and bool(money.fullmatch(balance))
        and not expense.startswith("-")
        and not deposit.startswith("-")
    )


def _valid_card_row(row: dict, scope: str) -> bool:
    try:
        datetime.strptime(row.get("date") or "", "%Y-%m-%d")
    except (TypeError, ValueError):
        return False
    amount = row.get("amount")
    return (
        re.fullmatch(r"\*{4}\d{4}", str(row.get("card_no") or "")) is not None
        and row.get("scope") == scope
        and isinstance(row.get("desc"), str)
        and bool(row["desc"].strip())
        and isinstance(amount, (int, float))
        and not isinstance(amount, bool)
        and math.isfinite(float(amount))
        and row.get("currency") == "TWD"
    )


def _twd_int(value) -> int:
    return int(str(value or "0").replace(",", ""))


def _validated_balance(value, currency: str | None):
    raw = str(value or "").replace(",", "").strip()
    if not re.fullmatch(r"-?\d{1,15}(?:\.\d{1,2})?", raw):
        return None
    try:
        amount = Decimal(raw)
    except InvalidOperation:
        return None
    if not amount.is_finite():
        return None
    if currency in ("TWD", "新台幣"):
        return int(amount) if amount == amount.to_integral_value() else None
    return int(amount) if amount == amount.to_integral_value() else float(amount)


def _statement_amount_from_text(text: str, label: str) -> int | None:
    start = text.find(label)
    if start < 0:
        raise ValueError("missing SCSB statement amount label")
    tail = text[start + len(label):].lstrip()
    value = tail.splitlines()[0].strip() if tail else ""
    if value in {"-", "--", "---"}:
        return None
    match = re.fullmatch(
        r"(?:NT\$\s*)?(\d+|\d{1,3}(?:,\d{3})+)(?:\.(\d{1,2}))?",
        value,
    )
    if not match:
        raise ValueError("invalid SCSB statement amount")
    amount = Decimal(value.replace("NT$", "").replace(",", "").strip())
    if not amount.is_finite() or amount < 0 or amount != amount.to_integral_value():
        raise ValueError("invalid SCSB statement amount")
    return int(amount)


def _scsb_parse_card_rows(text: str, scope: str) -> tuple[list, bool]:
    """Parse SCSB card rows and report whether every data-looking row parsed."""
    rows = []
    if not text:
        return rows, False
    if "交易日" in text and "交易時間" in text:
        lines = text.splitlines()
        header_index = next((
            i for i, line in enumerate(lines)
            if "交易日" in line and "交易時間" in line and "\t" in line
        ), None)
        if header_index is None:
            return rows, False
        header_cells = [cell.strip() for cell in lines[header_index].split("\t")]
        card_index = next((
            i for i, cell in enumerate(header_cells) if "卡號末4碼" in cell
        ), None)
        desc_index = next((
            i for i, cell in enumerate(header_cells) if "商店名稱" in cell
        ), None)
        amount_index = next((
            i for i, cell in enumerate(header_cells) if "交易金額" in cell
        ), None)
        result_index = next((
            i for i, cell in enumerate(header_cells) if "交易結果" in cell
        ), None)
        if (
            card_index is None or desc_index is None
            or amount_index is None or result_index is None
        ):
            return rows, False
        candidate_rows = malformed_rows = 0
        for line in lines[header_index + 1:]:
            if not line.strip():
                continue
            if "\t" not in line:
                if re.match(r"^\s*\d{4}/\d{2}/\d{2}(?:\s|$)", line):
                    candidate_rows += 1
                    malformed_rows += 1
                continue
            candidate_rows += 1
            cells = [cell.strip() for cell in line.split("\t")]
            date_match = re.fullmatch(
                r"(\d{4}/\d{2}/\d{2})(?:\s+\d{2}:\d{2}(?::\d{2})?)?",
                cells[0] if cells else "",
            )
            if (
                date_match is None
                or len(cells) <= max(card_index, desc_index, amount_index, result_index)
            ):
                malformed_rows += 1
                continue
            try:
                datetime.strptime(date_match.group(1), "%Y/%m/%d")
            except ValueError:
                malformed_rows += 1
                continue
            result_value = cells[result_index]
            if not re.fullmatch(
                r"(?:成功|交易成功|授權成功|success|approved)", result_value, re.I,
            ):
                malformed_rows += 1
                continue
            card_match = re.search(r"(\d{4})\s*$", cells[card_index])
            amount_value = cells[amount_index]
            if card_match is None or not re.fullmatch(
                r"(?:NT\$\s*)?-?(?:\d+|\d{1,3}(?:,\d{3})+)(?:\.\d{1,2})?",
                amount_value,
            ):
                malformed_rows += 1
                continue
            desc = cells[desc_index]
            amt = _num_real(amount_value.replace("NT$", "").replace(",", "").strip())
            rows.append({
                "card_no": f"****{card_match.group(1)}",
                "scope": scope,
                "date": date_match.group(1).replace("/", "-"),
                "desc": desc,
                "amount": amt,
                "currency": "TWD",
                "txn_type": classify.classify_by_desc_and_sign(desc, amt),
            })
        return rows, candidate_rows > 0 and malformed_rows == 0 and len(rows) == candidate_rows
    if "Transaction Date" not in text:
        return rows, False
    after_header = text.split("Transaction Date", 1)[1]
    lines = after_header.splitlines()
    header_cells = (["Transaction Date"] + [
        cell.strip() for cell in (lines[0].lstrip("\t").split("\t") if lines else [])
    ])
    card_index = next((
        i for i, cell in enumerate(header_cells)
        if "last 4" in cell.lower() or "card no" in cell.lower()
    ), None)
    desc_index = next((
        i for i, cell in enumerate(header_cells) if "merchant" in cell.lower()
    ), None)
    amount_index = next((
        i for i, cell in enumerate(header_cells) if "amount" in cell.lower()
    ), None)
    if card_index is not None and desc_index is not None and amount_index is not None:
        candidate_rows = malformed_rows = 0
        for line in lines[1:]:
            if not line.strip():
                continue
            if "\t" not in line:
                if re.match(r"^\d{4}/\d{2}/\d{2}(?:\s|$)", line.strip()):
                    candidate_rows += 1
                    malformed_rows += 1
                continue
            candidate_rows += 1
            cells = [cell.strip() for cell in line.split("\t")]
            if not cells or not re.fullmatch(r"\d{4}/\d{2}/\d{2}", cells[0]):
                malformed_rows += 1
                continue
            try:
                datetime.strptime(cells[0], "%Y/%m/%d")
            except ValueError:
                malformed_rows += 1
                continue
            if len(cells) <= max(card_index, desc_index, amount_index):
                malformed_rows += 1
                continue
            card_value = cells[card_index]
            amount_value = cells[amount_index]
            if (
                not re.fullmatch(r"(?:\*{4})?\d{4}", card_value)
                or not re.fullmatch(
                    r"(?:NT\$\s*)?-?(?:\d+|\d{1,3}(?:,\d{3})+)(?:\.\d{1,2})?",
                    amount_value,
                )
            ):
                malformed_rows += 1
                continue
            desc = cells[desc_index]
            amt = _num_real(amount_value.replace("NT$", "").replace(",", "").strip())
            rows.append({
                "card_no": f"****{card_value[-4:]}",
                "scope": scope,
                "date": cells[0].replace("/", "-"),
                "desc": desc,
                "amount": amt,
                "currency": "TWD",
                "txn_type": classify.classify_by_desc_and_sign(desc, amt),
            })
        if candidate_rows:
            return rows, malformed_rows == 0 and len(rows) == candidate_rows

    for match in re.finditer(
        r"(\d{4}/\d{2}/\d{2})\s+(\d{2}:\d{2}(?::\d{2})?)?\s*([A-Za-z\s]{0,30})?\s*(\d{4})\s+([^\t\n]{1,60})\s+(NT\$\s*(?:\d+|\d{1,3}(?:,\d{3})+)(?:\.\d{1,2})?)",
        after_header,
    ):
        try:
            datetime.strptime(match.group(1), "%Y/%m/%d")
        except ValueError:
            continue
        desc = (match.group(5) or "").strip()
        amt = _num_real((match.group(6) or "").replace("NT$", "").replace(",", "").strip())
        rows.append({
            "card_no": f"****{match.group(4)}",
            "scope": scope,
            "date": match.group(1).replace("/", "-"),
            "desc": desc,
            "amount": amt,
            "currency": "TWD",
            "txn_type": classify.classify_by_desc_and_sign(desc, amt),
        })
    candidate_rows = len(re.findall(r"(?m)^\s*\d{4}/\d{2}/\d{2}\b", after_header))
    return rows, candidate_rows > 0 and len(rows) == candidate_rows


def persist_scsb(data: dict, store: BankStore, rules: list[dict] | None = None) -> dict:
    """SCSB collect() 結構 → store 表增量。

    第一版只入庫帳號與餘額（DOM regex 抽出來的），明細待 API 解密路線完成。
      accounts[] (從 overview_text regex) → accounts(UPSERT) + balance_history
      totals.twd_total                    → daily_metrics
    """
    today = datetime.now().strftime("%Y-%m-%d")
    delta: dict = {}
    has_twd_inq = "twd_inquiry" in data
    raw_twd_inq = data.get("twd_inquiry")
    if has_twd_inq and not isinstance(raw_twd_inq, dict):
        raise ValueError("invalid SCSB TWD inquiry payload")
    if isinstance(raw_twd_inq, dict) and not raw_twd_inq:
        raise ValueError("invalid SCSB TWD inquiry payload")
    twd_inq = raw_twd_inq or {}
    overview_twd_accounts = {
        account.get("account_no")
        for account in (data.get("accounts") or [])
        if isinstance(account, dict)
        and account.get("account_no")
        and account.get("currency") in ("TWD", "新台幣")
        and account_classify.is_asset_type(
            account_classify.classify_account("scsb", account)
        )
    }
    if twd_inq:
        inventory = twd_inq.get("accounts")
        records = twd_inq.get("records")
        if not isinstance(inventory, list):
            raise ValueError("invalid SCSB TWD account inventory")
        if not isinstance(records, list):
            raise ValueError("invalid SCSB TWD transaction list")
        if not inventory and (records or overview_twd_accounts):
            raise ValueError("invalid SCSB TWD account inventory")
        expected_counts = {}
        for account in inventory:
            if (
                not isinstance(account, dict)
                or not account.get("account_no")
                or isinstance(account.get("record_count"), bool)
                or not isinstance(account.get("record_count"), int)
                or account["record_count"] < 0
                or account["account_no"] in expected_counts
            ):
                raise ValueError("invalid SCSB TWD account inventory")
            expected_counts[account["account_no"]] = account["record_count"]
        actual_counts = {account_no: 0 for account_no in expected_counts}
        for record in records:
            if (
                not isinstance(record, dict)
                or record.get("account_no") not in actual_counts
                or not _valid_twd_record(record)
            ):
                raise ValueError("invalid SCSB TWD transaction account_no provenance")
            actual_counts[record["account_no"]] += 1
        if actual_counts != expected_counts:
            raise ValueError("incomplete SCSB TWD account inventory")
        if set(expected_counts) != overview_twd_accounts:
            raise ValueError("SCSB TWD inventory does not match overview accounts")

    has_card_inq = "card_inquiry" in data
    raw_card_inq = data.get("card_inquiry")
    if has_card_inq and (
        not isinstance(raw_card_inq, dict) or not raw_card_inq
    ):
        raise ValueError("invalid SCSB card inquiry payload")
    card_inq = raw_card_inq or {}
    leaves = card_inq.get("leaves", {})
    if card_inq and (not isinstance(leaves, dict) or not leaves):
        raise ValueError("invalid SCSB card inquiry leaves")
    for scope, leaf in leaves.items():
        if scope not in {"unbilled", "current", "statement"} or not isinstance(leaf, dict):
            raise ValueError("invalid SCSB card inquiry leaf")
        if "rows" in leaf:
            rows = leaf["rows"]
            if not isinstance(rows, list) or any(
                not isinstance(row, dict) or not _valid_card_row(row, scope)
                for row in rows
            ):
                raise ValueError(f"invalid SCSB {scope} rows")
            if not rows and leaf.get("empty") is not True:
                raise ValueError(f"unverified empty SCSB {scope} scope")
        if "empty" in leaf and not isinstance(leaf["empty"], bool):
            raise ValueError("invalid SCSB card empty marker")
        if "months" in leaf:
            months = leaf["months"]
            if not isinstance(months, list):
                raise ValueError("invalid SCSB statement months")
            seen_months = set()
            for month in months:
                if (
                    not isinstance(month, dict)
                    or not re.fullmatch(r"20\d{2}/(?:0[1-9]|1[0-2])", str(month.get("month") or ""))
                    or month["month"] in seen_months
                    or (
                        "due_amount" not in month
                        and not (
                            isinstance(month.get("text"), str)
                            and bool(month["text"])
                        )
                    )
                ):
                    raise ValueError("invalid SCSB statement month")
                seen_months.add(month["month"])
                if "due_amount" in month and any(
                    value is not None and (
                        isinstance(value, bool)
                        or not isinstance(value, int)
                        or value < 0
                    )
                    for value in (month.get("due_amount"), month.get("min_payment"))
                ):
                    raise ValueError("invalid SCSB statement month summary")

    with store.savepoint("scsb_persist"):
        store.delete_daily_metrics([
            "overview_text_preview",
            "scsb_card_unbilled",
            "scsb_card_current",
            "scsb_card_statement",
            "twd_inquiry_summary",
        ], commit=False)
        store.clear_sync_log(commit=False)
        # Privacy-safe collection telemetry: counts only, never DOM, URL, labels, or balances.
        delta["telemetry"] = {
            "accounts_extracted": len(data.get("accounts") or []),
        }

        # 帳戶 UPSERT + 餘額快照
        # raw_balance 直接帶進去 — scsb _extract_accounts 已抓 NT$13,065 / NT$0 / USD1.55
        # frontend 才能顯示真實 0 餘額（$0）跟貸款餘額，而非 fallback「—」
        # 用 _num_real 保留外幣小數（USD 1.55 不能截成 1）
        accts = data.get("accounts") or []
        balances_complete = False
        if accts:
            account_rows = []
            for account in accts:
                if not account.get("account_no"):
                    continue
                raw_balance = _validated_balance(
                    account.get("balance"), account.get("currency"),
                )
                account_rows.append({
                    "account_no": account["account_no"],
                    "currency": account.get("currency"),
                    "branch": None,
                    "nickname": None,
                    "type": account.get("type_header"),
                    "product_type": account_classify.classify_account("scsb", account),
                    "raw_balance": raw_balance,
                    "raw_balance_date": today if raw_balance is not None else None,
                })
            store.upsert_accounts(account_rows, commit=False)
            balances_complete = bool(account_rows) and all(
                row["raw_balance"] is not None for row in account_rows
            )
            if balances_complete:
                # 餘額合計：排除貸款（loan/mortgage 不算 asset）
                def _is_asset_account(a: dict) -> bool:
                    pt = account_classify.classify_account("scsb", a)
                    return account_classify.is_asset_type(pt)
                twd_total = sum(_validated_balance(a["balance"], a.get("currency")) or 0 for a in accts
                                if a.get("currency") in ("TWD", "新台幣")
                                and _is_asset_account(a))
                fx_total = sum(_validated_balance(a["balance"], a.get("currency")) or 0 for a in accts
                               if a.get("currency") not in ("TWD", "新台幣")
                               and _is_asset_account(a))
                loan_total = sum(_validated_balance(a["balance"], a.get("currency")) or 0 for a in accts
                                 if a.get("currency") in ("TWD", "新台幣")
                                 and account_classify.is_liability_type(
                                     account_classify.classify_account("scsb", a)))
                store.upsert_balance_history([{
                    "snapshotDate": today,
                    "twdBalance": twd_total,
                    "fxBalance": fx_total,
                    "loanBalance": loan_total,
                }], commit=False)
                delta["balance_days"] = 1
                store.put_daily_metric("balance_latest",
                                       {"twd": twd_total, "fx_raw": fx_total,
                                        "loan": loan_total, "n_accounts": len(accts)},
                                       today, commit=False)

        # totals from regex are trusted only when every account balance was readable.
        totals = data.get("totals") or {}
        if totals and balances_complete:
            store.put_daily_metric("scsb_totals", totals, today, commit=False)


        # 預留位
        delta.setdefault("balance_days", 0)

        # --- 台幣交易明細（從 _collect_twd_inquiry 抓的 tab-separated 表格）---
        twd_new = 0
        if twd_inq:
            rows = []
            for r in twd_inq.get("records") or []:
                if not isinstance(r, dict):
                    raise ValueError("invalid SCSB TWD transaction")
                amt_expense = _twd_int(r.get("expense"))
                amt_deposit = _twd_int(r.get("deposit"))
                account_no = r.get("account_no")
                if not account_no:
                    raise ValueError("SCSB TWD transaction missing account_no")
                rows.append({
                    "account_no": account_no,
                    "datetime": r.get("date"),
                    "account_date": r.get("date"),
                    "desc": r.get("summary"),
                    "expend": amt_expense,
                    "income": amt_deposit,
                    "balance": _twd_int(r.get("balance")),
                    "counterparty_bank": None,
                    "counterparty_acct": (r.get("remarks") or "")[:30] or None,
                    "memo": r.get("remarks") or None,
                })
            if rows:
                twd_new = store.upsert_twd_txns(rows, rules=rules, commit=False)
        delta["twd_txn_new"] = twd_new


        # --- 信用卡明細（設計規範：每家都要抓信用卡明細）---
        # SCSB collect 抓 3 leaves: unbilled / current / statement，皆為純 text 頁面
        card_billed_new = card_unbilled = card_current = 0
        seen_card_nos: set[str] = set()

        if isinstance(leaves, dict):
            with store.savepoint("scsb_card_pending_batch"):
                # 1) unbilled：「You currently have no new transactions」=  確認使用者目前無未入帳
                unb = leaves.get("unbilled", {})
                unb_text = (unb.get("text_final") or unb.get("text") or "")
                unb_nav_ok = isinstance(unb.get("nav"), dict) and unb["nav"].get("ok") is True
                unb_refreshed = False
                if unb_nav_ok and ("rows" in unb or unb.get("empty") is True):
                    unb_rows = unb.get("rows") or []
                    if not isinstance(unb_rows, list) or any(
                        not isinstance(row, dict) or not _valid_card_row(row, "unbilled")
                        for row in unb_rows
                    ):
                        raise ValueError("invalid SCSB unbilled rows")
                    if not unb_rows and unb.get("empty") is not True:
                        raise ValueError("unverified empty SCSB unbilled scope")
                    card_unbilled = store.refresh_card_pending(
                        "unbilled", unb_rows, rules=rules, fetch_ok=True, commit=False)
                    unb_refreshed = True
                    seen_card_nos.update(
                        row["card_no"] for row in unb_rows
                        if isinstance(row, dict) and row.get("card_no")
                    )
                elif unb_text and unb_nav_ok and not _scsb_page_error(unb_text):
                    no_txn = "no new transactions" in unb_text.lower() or "have not yet been recorded" in unb_text.lower()
                    # 無未入帳 → refresh empty list 清掉舊 pending
                    if no_txn:
                        card_unbilled = store.refresh_card_pending(
                            "unbilled", [], rules=rules, fetch_ok=True, commit=False)
                        unb_refreshed = True
                    else:
                        # 嘗試解析（未來若使用者有刷卡才會走到這）
                        unb_rows, unb_complete = _scsb_parse_card_rows(unb_text, scope="unbilled")
                        if unb_rows and unb_complete:
                            card_unbilled = store.refresh_card_pending(
                                "unbilled", unb_rows, rules=rules, fetch_ok=True,
                                commit=False)
                            unb_refreshed = True
                            for r in unb_rows:
                                if r.get("card_no"):
                                    seen_card_nos.add(r["card_no"])

                if not unb_refreshed:
                    card_unbilled = store.refresh_card_pending(
                        "unbilled", [], rules=rules, fetch_ok=False, commit=False)

                # 2) current (即時 7 天)：表格 header 在 + 解析 data rows
                cur = leaves.get("current", {})
                cur_text = (cur.get("text_final") or cur.get("text") or "")
                cur_nav_ok = isinstance(cur.get("nav"), dict) and cur["nav"].get("ok") is True
                cur_refreshed = False
                if cur_nav_ok and ("rows" in cur or cur.get("empty") is True):
                    cur_rows = cur.get("rows") or []
                    if not isinstance(cur_rows, list) or any(
                        not isinstance(row, dict) or not _valid_card_row(row, "current")
                        for row in cur_rows
                    ):
                        raise ValueError("invalid SCSB current rows")
                    if not cur_rows and cur.get("empty") is not True:
                        raise ValueError("unverified empty SCSB current scope")
                    card_current = store.refresh_card_pending(
                        "current", cur_rows, rules=rules, fetch_ok=True, commit=False)
                    cur_refreshed = True
                    seen_card_nos.update(
                        row["card_no"] for row in cur_rows
                        if isinstance(row, dict) and row.get("card_no")
                    )
                elif cur_text and cur_nav_ok and not _scsb_page_error(cur_text):
                    lower_cur = cur_text.lower()
                    explicit_empty = ("no real-time transaction" in lower_cur
                                      or "no transaction records" in lower_cur)
                    cur_rows, cur_complete = _scsb_parse_card_rows(cur_text, scope="current")
                    if cur_rows and cur_complete:
                        card_current = store.refresh_card_pending(
                            "current", cur_rows, rules=rules, fetch_ok=True, commit=False)
                        cur_refreshed = True
                        for r in cur_rows:
                            if r.get("card_no"):
                                seen_card_nos.add(r["card_no"])
                    elif explicit_empty:
                        card_current = store.refresh_card_pending(
                            "current", [], rules=rules, fetch_ok=True, commit=False)
                        cur_refreshed = True
                if not cur_refreshed:
                    card_current = store.refresh_card_pending(
                        "current", [], rules=rules, fetch_ok=False, commit=False)

                # 3) statement：抽 account_no (A99999****) + 帳單金額 + 月份迭代
                stmt = leaves.get("statement", {})
                # ⚠️ 2026-06-14 移除「從 statement 抽 masked account 當卡號」的邏輯：
                # 原 regex `[A-Z]\d{4,8}\*+` 會匹到身分證 masked (例 "A12651****"
                # = 身分證 A12651* + 4 顆星)，導致使用者 SCSB 沒辦任何信用卡，cards 表
                # 卻有一張幽靈卡。statement 頁面的 "Your account number" 永遠是
                # 身分證，從來都不是信用卡卡號。
                # 真實信用卡 masked 應該從 unbilled.text / current.text 的交易表格
                # 抽 (見上方兩個分支)，這裡完全不抽。

                # 2026-06-13 升級：statement 月份迭代資料
                # 把每月帳單摘要寫進 daily_metric（即使 due/paid 全 --- 也記錄為 0）
                months = stmt.get("months") if isinstance(stmt, dict) else []
                if months:
                    months_summary = {}
                    for m in months:
                        mo = m.get("month")
                        mo_text = m.get("text", "")
                        if not mo:
                            continue
                        if "due_amount" in m:
                            due = m.get("due_amount")
                            min_pmt = m.get("min_payment")
                            if any(
                                value is not None and (
                                    isinstance(value, bool)
                                    or not isinstance(value, int)
                                    or value < 0
                                )
                                for value in (due, min_pmt)
                            ):
                                raise ValueError("invalid SCSB statement month summary")
                            months_summary[mo] = {
                                "due_amount": due,
                                "min_payment": min_pmt,
                                "has_data": due is not None,
                            }
                            continue
                        if not mo_text:
                            continue
                        due = _statement_amount_from_text(
                            mo_text, "Current Period Total Amount Due",
                        )
                        min_pmt = _statement_amount_from_text(
                            mo_text, "Current Period Total Minimum Amount Due",
                        )
                        months_summary[mo] = {
                            "due_amount": due,
                            "min_payment": min_pmt,
                            "has_data": due is not None,
                        }
                    if months_summary:
                        store.put_daily_metric(
                            "scsb_card_statement_months", months_summary, today, commit=False)


        # cards 表 UPSERT
        if seen_card_nos:
            rows = []
            for card_no in seen_card_nos:
                # masked like A99999**** 拿不到 last4，用整段 masked 當顯示
                display_no = card_no
                rows.append({
                    "number": display_no,
                    "name": f"SCSB 卡 {display_no}",
                    "association": None,
                    "type": "credit",
                    "currency": "TWD",
                })
            store.upsert_cards(rows, commit=False)
            delta["cards"] = len(seen_card_nos)

        delta["card_billed_new"] = card_billed_new
        delta["card_unbilled"] = card_unbilled
        delta["card_current"] = card_current

        store.log_sync(delta, commit=False)

    store.commit()
    return delta
