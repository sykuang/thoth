"""永豐 (Sinopac MMA) persist 邏輯.

從 persist.py 拆分而來 (2026-06-17, W persist.py split refactor)。
"""
from __future__ import annotations

from backend.core.error_codes import error_step, exception_error_id, with_error_code
from calendar import monthrange
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
import html
import json
import re
from zoneinfo import ZoneInfo

from backend.core import account_classify, classify
from backend.core.base import validate_history_coverage
from backend.core.money import native_money
from backend.core.store import BankStore
from backend.core.persist._common import _num, _num_real, _num_to_float


def _native_amount(
    value, currency: str, *, absolute: bool = False, _codes: tuple[str, ...] | None = None,
) -> Decimal:
    try:
        with error_step('SINOPAC-08-001-003'):
            amount = native_money(value, currency, absolute=absolute, _codes=_codes)
    except ValueError as exc:
        raise with_error_code(ValueError("invalid SinoPac native amount"), exception_error_id(exc) if _codes is not None else 'SINOPAC-08-001-001') from None
    assert amount is not None
    with error_step('SINOPAC-08-001-002'):
        return Decimal(str(amount))


def _native_number(amount: Decimal, currency: str) -> int | float:
    with error_step('SINOPAC-08-002-001'):
        return int(amount) if currency == "TWD" else float(amount)


def _sinopac_date(s) -> str | None:
    """永豐日期 '20260610' / '2026/06/10' → 'YYYY-MM-DD'。"""
    if not s:
        return None
    t = str(s).strip().replace("/", "").replace("-", "")
    if len(t) == 8 and t.isdigit():
        return f"{t[:4]}-{t[4:6]}-{t[6:8]}"
    if len(t) == 6 and t.isdigit():  # 202606 → 2026-06-01
        return f"{t[:4]}-{t[4:6]}-01"
    return None

def _sinopac_datetime(value) -> str | None:
    if not isinstance(value, str):
        return None
    text = re.sub(r"<br\s*/?>", " ", value, flags=re.IGNORECASE).strip()
    try:
        with error_step('SINOPAC-08-004-001'):
            return datetime.strptime(text, "%Y/%m/%d %H:%M").isoformat(timespec="seconds")
    except ValueError:
        return None


def _sinopac_strip_html(s) -> str:
    """永豐欄位含 HTML 標籤（如 '<font color="...">-2,000</font>'），全剝。"""
    if not s:
        return ""
    return html.unescape(re.sub(r"<[^>]*>", "", str(s))).replace("\xa0", " ").strip()

def _sinopac_split_amount(s):
    """永豐 DataText4 = '<font>+30</font>' / '<font>-2,000</font>'
    回傳 (expend, income): 收入 → income，支出 → expend。
    """
    txt = _sinopac_strip_html(s).replace(",", "")
    if not txt:
        return None, None
    try:
        with error_step('SINOPAC-08-006-001'):
            amount = float(Decimal(txt))
    except (InvalidOperation, ValueError):
        return None, None
    return (abs(amount), None) if amount < 0 else (None, amount)

def _mmyy_expired(s: str | None, today_yyyy_mm: str | None = None) -> bool:
    """卡片到期日 'MMYYYY' (DBS '122026') / 'MMYY' (Sinopac '0829') → 是否過期.

    None / 解析失敗 → False (保守當未過期, 不誤殺現用卡).
    """
    if not s:
        return False
    s = str(s).strip()
    try:
        if len(s) == 6 and s.isdigit():  # MMYYYY (DBS)
            with error_step('SINOPAC-08-007-001'):
                mo, y = int(s[:2]), int(s[2:])
        elif len(s) == 4 and s.isdigit():  # MMYY (Sinopac)
            with error_step('SINOPAC-08-007-003'):
                mo, y = int(s[:2]), 2000 + int(s[2:])
        else:
            return False
        if not (1 <= mo <= 12):
            return False
        # 比較 (y, mo) 與今日
        if today_yyyy_mm is None:
            from datetime import datetime
            today_y, today_mo = datetime.now().year, datetime.now().month
        else:
            with error_step('SINOPAC-08-007-002'):
                today_y, today_mo = int(today_yyyy_mm[:4]), int(today_yyyy_mm[5:7])
        return (y, mo) < (today_y, today_mo)
    except Exception:
        return False

def _history_floor(end: date) -> date:
    try:
        return end.replace(year=end.year - 1) + timedelta(days=1)
    except ValueError:
        return end.replace(year=end.year - 1, day=28) + timedelta(days=1)


def _today() -> date:
    return datetime.now(ZoneInfo("Asia/Taipei")).date()


def _history_windows(start: date, end: date) -> list[tuple[date, date]]:
    windows = []
    cursor = start
    while cursor <= end:
        window_end = min(
            end, date(cursor.year, cursor.month, monthrange(cursor.year, cursor.month)[1]),
        )
        windows.append((cursor, window_end))
        cursor = window_end + timedelta(days=1)
    return windows


def _strict_date(value, fmt: str, error: str) -> date:
    if not isinstance(value, str):
        raise with_error_code(ValueError(error), 'SINOPAC-08-011-001')
    try:
        with error_step('SINOPAC-08-011-003'):
            return datetime.strptime(value, fmt).date()
    except ValueError:
        raise with_error_code(ValueError(error), 'SINOPAC-08-011-002') from None


def _strict_amount(value, error: str) -> Decimal:
    if not isinstance(value, str):
        raise with_error_code(ValueError(error), 'SINOPAC-08-012-003')
    text = re.sub(r"<[^>]*>", "", value).strip()
    if re.fullmatch(
        r"[+-]?(?:(?:0|[1-9]\d*)(?:\.\d{1,6})?|[1-9]\d{0,2}(?:,\d{3})+(?:\.\d{1,6})?)",
        text,
    ) is None:
        raise with_error_code(ValueError(error), 'SINOPAC-08-012-004')
    try:
        with error_step('SINOPAC-08-012-006'):
            amount = Decimal(text.replace(",", ""))
    except InvalidOperation:
        raise with_error_code(ValueError(error), 'SINOPAC-08-012-005') from None
    if not amount.is_finite():
        raise with_error_code(ValueError(error), 'SINOPAC-08-012-001')
    elif abs(amount) > Decimal('2147483647'):
        raise with_error_code(ValueError(error), 'SINOPAC-08-012-002')
    return amount


def _validate_sinopac_history(data: dict, store: BankStore) -> date:
    error = "invalid SinoPac history coverage"
    coverage = data.get("history_coverage")
    if not isinstance(coverage, dict):
        raise with_error_code(ValueError(error), 'SINOPAC-08-013-056')
    mode = coverage.get("mode")
    if mode not in {"full", "incremental"}:
        raise with_error_code(ValueError(error), 'SINOPAC-08-013-057')
    with error_step('SINOPAC-08-013-072'):
        as_of = _strict_date(coverage.get("as_of"), "%Y-%m-%d", error)
    today = _today()
    if as_of > today:
        raise with_error_code(ValueError(error), 'SINOPAC-08-013-001')
    elif (today - as_of).days > 1:
        raise with_error_code(ValueError(error), 'SINOPAC-08-013-002')
    with error_step('SINOPAC-08-013-073'):
        validate_history_coverage(
            coverage,
            expected_mode=mode,
            expected_domains=frozenset({"account_transactions"}),
        )
    domain = coverage["domains"][0]
    expected = domain["expected"]
    windows = domain["windows"]
    encoded_bytes = 0
    try:
        history_payload = {
            "history_coverage": coverage,
            "debit_accounts": data.get("debit_accounts", []),
            "account_transactions": data.get("account_transactions", []),
        }
        for chunk in json.JSONEncoder(
            ensure_ascii=False, separators=(",", ":"),
        ).iterencode(history_payload):
            encoded_bytes += len(chunk.encode("utf-8"))
            if encoded_bytes > 5_000_000:
                raise with_error_code(ValueError(error), 'SINOPAC-08-013-058')
    except (TypeError, RecursionError):
        raise with_error_code(ValueError(error), 'SINOPAC-08-013-059') from None

    for window in windows:
        if type(window.get('pages')) is not int:
            raise with_error_code(ValueError(error), 'SINOPAC-08-013-003')
        elif window.get('pages') != 1:
            raise with_error_code(ValueError(error), 'SINOPAC-08-013-004')
    if not expected:
        empty = domain.get("empty_window")
        inventory = data.get("debit_accounts", [])
        results = data.get("account_transactions", [])
        if not isinstance(empty, dict):
            raise with_error_code(ValueError(error), 'SINOPAC-08-013-005')
        elif set(empty) != {'start', 'end', 'status', 'pages'}:
            raise with_error_code(ValueError(error), 'SINOPAC-08-013-006')
        elif empty.get('status') != 'explicit_empty':
            raise with_error_code(ValueError(error), 'SINOPAC-08-013-007')
        elif type(empty.get('pages')) is not int:
            raise with_error_code(ValueError(error), 'SINOPAC-08-013-008')
        elif empty.get('pages') != 1:
            raise with_error_code(ValueError(error), 'SINOPAC-08-013-009')
        elif inventory != []:
            raise with_error_code(ValueError(error), 'SINOPAC-08-013-010')
        elif results != []:
            raise with_error_code(ValueError(error), 'SINOPAC-08-013-011')
        elif windows:
            raise with_error_code(ValueError(error), 'SINOPAC-08-013-012')
        with error_step('SINOPAC-08-013-075'):
            start = _strict_date(empty.get("start"), "%Y-%m-%d", error)
        with error_step('SINOPAC-08-013-076'):
            end = _strict_date(empty.get("end"), "%Y-%m-%d", error)
        if start != _history_floor(end):
            raise with_error_code(ValueError(error), 'SINOPAC-08-013-013')
        elif end != as_of:
            raise with_error_code(ValueError(error), 'SINOPAC-08-013-014')
        return end
    expected_by_identity = {item["identity"]: item for item in expected}
    if len(expected_by_identity) != len(expected):
        raise with_error_code(ValueError(error), 'SINOPAC-08-013-060')
    inventory = data.get("debit_accounts")
    if not isinstance(inventory, list):
        raise with_error_code(ValueError(error), 'SINOPAC-08-013-015')
    elif len(inventory) != len(expected):
        raise with_error_code(ValueError(error), 'SINOPAC-08-013-016')
    inventory_by_identity = {}
    for item in inventory:
        if not isinstance(item, dict):
            raise with_error_code(ValueError(error), 'SINOPAC-08-013-017')
        elif set(item) != {'label', 'identity', 'currency'}:
            raise with_error_code(ValueError(error), 'SINOPAC-08-013-018')
        elif not isinstance(item.get('label'), str):
            raise with_error_code(ValueError(error), 'SINOPAC-08-013-019')
        elif not item['label']:
            raise with_error_code(ValueError(error), 'SINOPAC-08-013-020')
        elif item['label'] != item['label'].strip():
            raise with_error_code(ValueError(error), 'SINOPAC-08-013-021')
        elif not isinstance(item.get('identity'), str):
            raise with_error_code(ValueError(error), 'SINOPAC-08-013-022')
        elif re.fullmatch('\\d{14}', item['identity']) is None:
            raise with_error_code(ValueError(error), 'SINOPAC-08-013-023')
        elif not isinstance(item.get('currency'), str):
            raise with_error_code(ValueError(error), 'SINOPAC-08-013-024')
        elif re.fullmatch('[A-Z]{3}', item['currency']) is None:
            raise with_error_code(ValueError(error), 'SINOPAC-08-013-025')
        history_identity = (
            item["identity"] if item["currency"] == "TWD"
            else f'{item["identity"]}:{item["currency"]}'
        )
        if history_identity in inventory_by_identity:
            raise with_error_code(ValueError(error), 'SINOPAC-08-013-061')
        inventory_by_identity[history_identity] = item
    if set(inventory_by_identity) != set(expected_by_identity):
        raise with_error_code(ValueError(error), 'SINOPAC-08-013-062')
    results = data.get("account_transactions")
    if not isinstance(results, list):
        raise with_error_code(ValueError(error), 'SINOPAC-08-013-026')
    elif len(results) != len(windows):
        raise with_error_code(ValueError(error), 'SINOPAC-08-013-027')
    receipts = []
    total_rows = 0
    for result in results:
        if not isinstance(result, dict):
            raise with_error_code(ValueError(error), 'SINOPAC-08-013-063')
        identity = result.get("account")
        currency = result.get("currency")
        history_identity = (
            identity if currency == "TWD" else f"{identity}:{currency}"
            if isinstance(identity, str) and isinstance(currency, str) else None
        )
        receipt = result.get("receipt")
        rows = result.get("records")
        if not isinstance(identity, str):
            raise with_error_code(ValueError(error), 'SINOPAC-08-013-028')
        elif re.fullmatch('\\d{14}', identity) is None:
            raise with_error_code(ValueError(error), 'SINOPAC-08-013-029')
        elif not isinstance(currency, str):
            raise with_error_code(ValueError(error), 'SINOPAC-08-013-030')
        elif re.fullmatch('[A-Z]{3}', currency) is None:
            raise with_error_code(ValueError(error), 'SINOPAC-08-013-031')
        elif history_identity not in expected_by_identity:
            raise with_error_code(ValueError(error), 'SINOPAC-08-013-032')
        elif not isinstance(result.get('account_name'), str):
            raise with_error_code(ValueError(error), 'SINOPAC-08-013-033')
        elif result['account_name'] != inventory_by_identity[history_identity]['label']:
            raise with_error_code(ValueError(error), 'SINOPAC-08-013-034')
        elif not isinstance(receipt, dict):
            raise with_error_code(ValueError(error), 'SINOPAC-08-013-035')
        elif set(receipt) != {'identity', 'start', 'end', 'status', 'pages', 'rows'}:
            raise with_error_code(ValueError(error), 'SINOPAC-08-013-036')
        elif receipt.get('identity') != history_identity:
            raise with_error_code(ValueError(error), 'SINOPAC-08-013-037')
        elif type(receipt.get('pages')) is not int:
            raise with_error_code(ValueError(error), 'SINOPAC-08-013-038')
        elif receipt.get('pages') != 1:
            raise with_error_code(ValueError(error), 'SINOPAC-08-013-039')
        elif not isinstance(rows, list):
            raise with_error_code(ValueError(error), 'SINOPAC-08-013-040')
        elif len(rows) > 10000:
            raise with_error_code(ValueError(error), 'SINOPAC-08-013-041')
        elif type(receipt.get('rows')) is not int:
            raise with_error_code(ValueError(error), 'SINOPAC-08-013-042')
        elif receipt.get('rows') != len(rows):
            raise with_error_code(ValueError(error), 'SINOPAC-08-013-043')
        elif receipt.get('status') != ('complete' if rows else 'explicit_empty'):
            raise with_error_code(ValueError(error), 'SINOPAC-08-013-044')
        total_rows += len(rows)
        if total_rows > 50_000:
            raise with_error_code(ValueError(error), 'SINOPAC-08-013-064')
        with error_step('SINOPAC-08-013-077'):
            start = _strict_date(receipt["start"], "%Y-%m-%d", error)
        with error_step('SINOPAC-08-013-078'):
            end = _strict_date(receipt["end"], "%Y-%m-%d", error)
        if start > end:
            raise with_error_code(ValueError(error), 'SINOPAC-08-013-045')
        elif (start.year, start.month) != (end.year, end.month):
            raise with_error_code(ValueError(error), 'SINOPAC-08-013-046')
        seen_rows = set()
        for row in rows:
            keys = {f"DataText{i}" for i in range(1, 12)}
            if not isinstance(row, dict):
                raise with_error_code(ValueError(error), 'SINOPAC-08-013-047')
            elif set(row) != keys:
                raise with_error_code(ValueError(error), 'SINOPAC-08-013-048')
            elif any((not isinstance(row[key], str) for key in keys)):
                raise with_error_code(ValueError(error), 'SINOPAC-08-013-049')
            fingerprint = tuple(row[f"DataText{i}"] for i in range(1, 12))
            if fingerprint in seen_rows:
                raise with_error_code(ValueError(error), 'SINOPAC-08-013-065')
            seen_rows.add(fingerprint)
            if not re.fullmatch('\\d{4}/\\d{2}/\\d{2}<br />\\d{2}:\\d{2}', row['DataText1']):
                raise with_error_code(ValueError(error), 'SINOPAC-08-013-050')
            elif not re.fullmatch('\\d{4}/\\d{2}/\\d{2}', row['DataText2']):
                raise with_error_code(ValueError(error), 'SINOPAC-08-013-051')
            with error_step('SINOPAC-08-013-082'):
                _strict_date(
                    re.sub(r"<br\s*/?>", " ", row["DataText1"], flags=re.IGNORECASE),
                    "%Y/%m/%d %H:%M",
                    error,
                )
            with error_step('SINOPAC-08-013-083'):
                _strict_date(row["DataText2"], "%Y/%m/%d", error)
            description = _sinopac_strip_html(row["DataText3"])
            if not description:
                raise with_error_code(ValueError(error), 'SINOPAC-08-013-052')
            elif len(description) > 500:
                raise with_error_code(ValueError(error), 'SINOPAC-08-013-053')
            if any(len(row[f"DataText{i}"]) > 2_000 for i in range(6, 12)):
                raise with_error_code(ValueError(error), 'SINOPAC-08-013-066')
            with error_step('SINOPAC-08-013-084'):
                amount = _strict_amount(row["DataText4"], error)
            with error_step('SINOPAC-08-013-085'):
                balance = _strict_amount(row["DataText5"], error)
            if currency == "TWD" and any(
                value != value.to_integral_value() for value in (amount, balance)
            ):
                raise with_error_code(ValueError(error), 'SINOPAC-08-013-067')
        receipts.append({key: receipt[key] for key in (
            "identity", "start", "end", "status", "pages",
        )})
    if receipts != windows:
        raise with_error_code(ValueError(error), 'SINOPAC-08-013-068')

    with error_step('SINOPAC-08-013-074'):
        existing = store.latest_account_transaction_dates()
    expected_ends = set()
    for item in expected:
        identity = item["identity"]
        with error_step('SINOPAC-08-013-079'):
            start = _strict_date(item["start"], "%Y-%m-%d", error)
        with error_step('SINOPAC-08-013-080'):
            end = _strict_date(item["end"], "%Y-%m-%d", error)
        expected_ends.add(end)
        cursor = existing.get(identity)
        if isinstance(cursor, date) and cursor > end:
            raise with_error_code(ValueError(error), 'SINOPAC-08-013-069')
        expected_start = _history_floor(end)
        if mode == "incremental" and isinstance(cursor, date):
            expected_start = max(expected_start, cursor - timedelta(days=7))
        if start != expected_start:
            raise with_error_code(ValueError(error), 'SINOPAC-08-013-070')
        with error_step('SINOPAC-08-013-081'):
            actual = [
                (_strict_date(window["start"], "%Y-%m-%d", error),
                 _strict_date(window["end"], "%Y-%m-%d", error))
                for window in windows if window["identity"] == identity
            ]
        if actual != _history_windows(start, end):
            raise with_error_code(ValueError(error), 'SINOPAC-08-013-071')
    if len(expected_ends) != 1:
        raise with_error_code(ValueError(error), 'SINOPAC-08-013-054')
    elif next(iter(expected_ends)) != as_of:
        raise with_error_code(ValueError(error), 'SINOPAC-08-013-055')
    return next(iter(expected_ends))


def _parse_sinopac_repayment(account: str, repayment: dict) -> list[dict]:
    """Validate the native default-window receipt before any destructive write."""
    error = "invalid SinoPac repayment"
    if not isinstance(repayment, dict):
        raise with_error_code(ValueError(error), 'SINOPAC-08-014-001')
    elif set(repayment) != {'sub_account', 'currency', 'receipt', 'records'}:
        raise with_error_code(ValueError(error), 'SINOPAC-08-014-002')
    sub, currency = repayment["sub_account"], repayment["currency"]
    for value, pattern in ((account, r"[0-9]{1,64}"), (sub, r"[0-9-]{1,64}"), (currency, r"[A-Z]{3}")):
        if not isinstance(value, str):
            raise with_error_code(ValueError(error), 'SINOPAC-08-014-003')
        elif re.fullmatch(pattern, value) is None:
            raise with_error_code(ValueError(error), 'SINOPAC-08-014-004')
    receipt, records = repayment["receipt"], repayment["records"]
    if not isinstance(receipt, dict):
        raise with_error_code(ValueError(error), 'SINOPAC-08-014-005')
    elif set(receipt) != {'account', 'sub_account', 'currency', 'start', 'end', 'status', 'period', 'pages', 'rows'}:
        raise with_error_code(ValueError(error), 'SINOPAC-08-014-006')
    elif receipt.get('account') != account:
        raise with_error_code(ValueError(error), 'SINOPAC-08-014-007')
    elif receipt.get('sub_account') != sub:
        raise with_error_code(ValueError(error), 'SINOPAC-08-014-008')
    elif receipt.get('currency') != currency:
        raise with_error_code(ValueError(error), 'SINOPAC-08-014-009')
    elif receipt.get('period') != 'native_default':
        raise with_error_code(ValueError(error), 'SINOPAC-08-014-010')
    elif receipt.get('status') != 'complete':
        raise with_error_code(ValueError(error), 'SINOPAC-08-014-011')
    elif type(receipt.get('pages')) is not int:
        raise with_error_code(ValueError(error), 'SINOPAC-08-014-012')
    elif receipt['pages'] != 1:
        raise with_error_code(ValueError(error), 'SINOPAC-08-014-013')
    elif type(receipt.get('rows')) is not int:
        raise with_error_code(ValueError(error), 'SINOPAC-08-014-014')
    elif not isinstance(records, list):
        raise with_error_code(ValueError(error), 'SINOPAC-08-014-015')
    elif not 1 <= len(records) <= 10000:
        raise with_error_code(ValueError(error), 'SINOPAC-08-014-016')
    elif receipt['rows'] != len(records):
        raise with_error_code(ValueError(error), 'SINOPAC-08-014-017')
    with error_step('SINOPAC-08-014-030'):
        start, end = (_strict_date(receipt[k], "%Y-%m-%d", error) for k in ("start", "end"))
    if start.isoformat() != receipt['start']:
        raise with_error_code(ValueError(error), 'SINOPAC-08-014-018')
    elif end.isoformat() != receipt['end']:
        raise with_error_code(ValueError(error), 'SINOPAC-08-014-019')
    elif not start <= end <= _today():
        raise with_error_code(ValueError(error), 'SINOPAC-08-014-020')
    rows = []
    encoded_bytes = 0
    keys = {f"DataValue{i}" for i in range(1, 12)}
    for raw in records:
        if not isinstance(raw, dict):
            raise with_error_code(ValueError(error), 'SINOPAC-08-014-021')
        elif set(raw) != keys:
            raise with_error_code(ValueError(error), 'SINOPAC-08-014-022')
        else:
            for _error_leaf_9_value in raw.values():
                if not isinstance(_error_leaf_9_value, str):
                    raise with_error_code(ValueError(error), 'SINOPAC-08-014-035')
                elif len(_error_leaf_9_value) > 2000:
                    raise with_error_code(ValueError(error), 'SINOPAC-08-014-034')
        with error_step('SINOPAC-08-014-031'):
            raw_json = json.dumps(raw, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        encoded_bytes += len(raw_json.encode("utf-8"))
        if encoded_bytes > 5_000_000:
            raise with_error_code(ValueError(error), 'SINOPAC-08-014-026')
        row = {"raw_json": raw_json, "status": raw["DataValue10"]}
        for key, field in (("due_date", 1), ("paid_on", 2)):
            value = raw[f"DataValue{field}"]
            with error_step('SINOPAC-08-014-032'):
                parsed = _strict_date(value, "%Y/%m/%d", error)
            if parsed.strftime("%Y/%m/%d") != value:
                raise with_error_code(ValueError(error), 'SINOPAC-08-014-027')
            row[key] = parsed.isoformat()
        amounts = {}
        for key, field in (("principal", 11), ("interest", 4), ("penalty", 6),
                           ("paid_total", 3), ("principal_balance", 5)):
            value = raw[f"DataValue{field}"]
            if len(value) > 64:
                raise with_error_code(ValueError(error), 'SINOPAC-08-014-024')
            elif re.fullmatch('(?:0|[1-9][0-9]*|[1-9][0-9]{0,2}(?:,[0-9]{3})+)(?:\\.[0-9]{1,6})?', value) is None:
                raise with_error_code(ValueError(error), 'SINOPAC-08-014-025')
            with error_step('SINOPAC-08-014-033'):
                amount = Decimal(value.replace(",", ""))
            if amount > Decimal("999999999999999999"):
                raise with_error_code(ValueError(error), 'SINOPAC-08-014-028')
            amounts[key] = amount
            row[key] = format(amount.normalize(), "f")
        if amounts["principal"] + amounts["interest"] + amounts["penalty"] != amounts["paid_total"]:
            raise with_error_code(ValueError(error), 'SINOPAC-08-014-029')
        rows.append(row)
    return rows


def _persist_sinopac(
    data: dict,
    store: BankStore,
    rules: list[dict] | None = None,
    *,
    as_of: date | None = None,
) -> dict:
    """永豐 collect() 結構 → store 各業務表增量。

    映射：
      bank_balance[].SubInfo → accounts(UPSERT) + balance_history(每日快照)
      loan.details[].records → accounts + balance_history.loan_balance + daily_metrics
      all_cards.Result.Items → cards(UPSERT)
      card_summary / asset_chart / card_billing / debit_accounts → daily_metrics
      twd_transactions / card_statements / card_unbilled → 交易明細表
    """
    as_of = as_of or _today()
    today = as_of.isoformat()
    delta: dict = {}

    loan = data.get("loan") or {}
    details = loan.get("details", []) if isinstance(loan, dict) else []
    repayment_windows = []
    # Old summary-only dumps carry no repayment authority and cannot clear history.
    if any(isinstance(detail, dict) and "repayments" in detail for detail in details):
        error = "invalid SinoPac repayment inventory"
        if loan.get('fetch_ok') is not True:
            raise with_error_code(ValueError(error), 'SINOPAC-08-015-001')
        elif len(details) > 100:
            raise with_error_code(ValueError(error), 'SINOPAC-08-015-002')
        seen_accounts = set()
        for detail in details:
            if not isinstance(detail, dict):
                raise with_error_code(ValueError(error), 'SINOPAC-08-015-003')
            elif not isinstance(detail.get('account'), str):
                raise with_error_code(ValueError(error), 'SINOPAC-08-015-004')
            account = detail["account"]
            if account in seen_accounts:
                raise with_error_code(ValueError(error), 'SINOPAC-08-015-011')
            seen_accounts.add(account)
            records, repayments = detail.get("records"), detail.get("repayments")
            if not isinstance(records, list):
                raise with_error_code(ValueError(error), 'SINOPAC-08-015-005')
            elif not isinstance(repayments, list):
                raise with_error_code(ValueError(error), 'SINOPAC-08-015-006')
            elif not 1 <= len(records) <= 100:
                raise with_error_code(ValueError(error), 'SINOPAC-08-015-007')
            if any(not isinstance(row, dict) for row in records + repayments):
                raise with_error_code(ValueError(error), 'SINOPAC-08-015-012')
            expected = [(row.get("Sub1_Sub2"), row.get("Currency")) for row in records]
            actual = [(row.get("sub_account"), row.get("currency")) for row in repayments]
            if any(not isinstance(v, str) for pair in expected + actual for v in pair):
                raise with_error_code(ValueError(error), 'SINOPAC-08-015-013')
            if len(set(expected)) != len(expected):
                raise with_error_code(ValueError(error), 'SINOPAC-08-015-008')
            elif len(set(actual)) != len(actual):
                raise with_error_code(ValueError(error), 'SINOPAC-08-015-009')
            elif set(expected) != set(actual):
                raise with_error_code(ValueError(error), 'SINOPAC-08-015-010')
            for repayment in repayments:
                with error_step('SINOPAC-08-015-040'):
                    repayment_windows.append((account, repayment, _parse_sinopac_repayment(account, repayment)))
    # Keep transaction-level identity locks in a stable order across concurrent syncs.
    for account, repayment, rows in sorted(
        repayment_windows, key=lambda item: (item[0], item[1]["sub_account"], item[1]["currency"])
    ):
        with error_step('SINOPAC-08-015-020'):
            delta["loan_repayments"] = delta.get("loan_repayments", 0) + store.replace_loan_repayments(
                account, repayment["sub_account"], repayment["currency"],
                repayment["receipt"]["start"], repayment["receipt"]["end"], rows,
            )

    # --- 銀行帳戶（含 TWD/USD/JPY 各幣別行）---
    bb = data.get("bank_balance") or []
    accts = []
    with error_step('SINOPAC-08-015-016'):
        twd_total_amount = Decimal(0)
    fx_by_currency_amounts: dict[str, Decimal] = {}
    for grp in bb:
        if not isinstance(grp, dict):
            continue
        for a in grp.get("SubInfo", []) or []:
            acct_no = a.get("AcctValue") or a.get("AcctValueFormat")
            if not acct_no:
                continue
            cur = str(a.get("Curr") or "TWD").strip().upper()
            if re.fullmatch(r"[A-Z]{3}", cur) is None:
                raise with_error_code(ValueError("invalid SinoPac balance currency"), 'SINOPAC-08-015-014')
            with error_step('SINOPAC-08-015-029'):
                raw_amount = _native_amount(a.get("AvailBalance"), cur, _codes=('SINOPAC-08-830-001', 'SINOPAC-08-830-002', 'SINOPAC-08-830-003', 'SINOPAC-08-830-004', 'SINOPAC-08-830-005', 'SINOPAC-08-830-006', 'SINOPAC-08-830-007', 'SINOPAC-08-830-008', 'SINOPAC-08-830-009', 'SINOPAC-08-830-010'))
            raw_balance = _native_number(raw_amount, cur)
            raw = {**a, "currency": cur}
            accts.append({
                "account_no": acct_no, "currency": cur,
                "branch": None, "nickname": a.get("AcctText"),
                "type": a.get("AcctText"),
                "product_type": account_classify.classify_account("sinopac", raw),
                "raw_balance": raw_balance,
                "raw_balance_date": today,
            })
            if cur == "TWD":
                with error_step('SINOPAC-08-831-010'):
                    twd_total_amount = _native_amount(
                        twd_total_amount + raw_amount,
                        cur, _codes=('SINOPAC-08-831-001', 'SINOPAC-08-831-002', 'SINOPAC-08-831-003', 'SINOPAC-08-831-004', 'SINOPAC-08-831-005', 'SINOPAC-08-831-006', 'SINOPAC-08-831-007', 'SINOPAC-08-831-008', 'SINOPAC-08-831-009', 'SINOPAC-08-831-010'))
            else:
                with error_step('SINOPAC-08-832-010'):
                    fx_by_currency_amounts[cur] = _native_amount(
                        fx_by_currency_amounts.get(cur, Decimal(0)) + raw_amount,
                        cur, _codes=('SINOPAC-08-832-001', 'SINOPAC-08-832-002', 'SINOPAC-08-832-003', 'SINOPAC-08-832-004', 'SINOPAC-08-832-005', 'SINOPAC-08-832-006', 'SINOPAC-08-832-007', 'SINOPAC-08-832-008', 'SINOPAC-08-832-009', 'SINOPAC-08-832-010'))
    with error_step('SINOPAC-08-015-017'):
        twd_total = int(twd_total_amount)
    fx_by_currency = {
        currency: _native_number(amount, currency)
        for currency, amount in fx_by_currency_amounts.items()
    }
    # 貸款帳戶：ws_loanaccount + 每帳號 ws_loaninfo 真實明細。
    loan = data.get("loan") or {}
    loan_total = None
    loan_by_currency: dict[str, Decimal] = {}
    loan_metric_records = []
    if isinstance(loan, dict) and loan.get("fetch_ok") is True:
        for detail in loan.get("details") or []:
            if not isinstance(detail, dict) or not detail.get("account"):
                continue
            records = [r for r in detail.get("records") or [] if isinstance(r, dict)]
            with error_step('SINOPAC-08-015-030'):
                loan_metric_records.extend({
                    "loan_kind": record.get("LoanKind"),
                    "repayment_method": record.get("PayName"),
                    "sub_account": record.get("Sub1_Sub2"),
                    "currency": record.get("Currency"),
                    "begin_loan_date": record.get("BeginLoanDate"),
                    "loan_date": record.get("LoanDate"),
                    "maturity_date": record.get("MatureDate"),
                    "original_principal": _num_real(record.get("LoanAmt"), _codes=('SYS-08-001-001', 'SINOPAC-08-833-002', 'SYS-08-001-003', 'SINOPAC-08-833-004', 'SINOPAC-08-833-005', 'SINOPAC-08-833-006', 'SYS-08-001-007', 'SINOPAC-08-833-008', 'SINOPAC-08-833-009', 'SINOPAC-08-833-010')),
                    "principal_balance": _num_real(record.get("LoanBalance"), _codes=('SYS-08-001-001', 'SINOPAC-08-834-002', 'SYS-08-001-003', 'SINOPAC-08-834-004', 'SINOPAC-08-834-005', 'SINOPAC-08-834-006', 'SYS-08-001-007', 'SINOPAC-08-834-008', 'SINOPAC-08-834-009', 'SINOPAC-08-834-010')),
                    "interest_rate": record.get("LoanRate"),
                } for record in records)
            records_by_currency = {}
            for record in records:
                cur = str(record.get("Currency") or "TWD").strip().upper()
                records_by_currency.setdefault(cur, []).append(record)
            for cur, currency_records in records_by_currency.items():
                if re.fullmatch(r"[A-Z]{3}", cur) is None:
                    raise with_error_code(ValueError("invalid SinoPac loan currency"), 'SINOPAC-08-015-015')
                with error_step('SINOPAC-08-015-043'):
                    balances = [
                        _native_amount(
                            record.get("LoanBalance"), cur, absolute=True, _codes=('SINOPAC-08-835-001', 'SINOPAC-08-835-002', 'SINOPAC-08-835-003', 'SINOPAC-08-835-004', 'SINOPAC-08-835-005', 'SINOPAC-08-835-006', 'SINOPAC-08-835-007', 'SINOPAC-08-835-008', 'SINOPAC-08-835-009', 'SINOPAC-08-835-010'))
                        for record in currency_records
                    ]
                with error_step('SINOPAC-08-836-010'):
                    raw_amount = _native_amount(
                        sum(balances, Decimal(0)), cur, absolute=True, _codes=('SINOPAC-08-836-001', 'SINOPAC-08-836-002', 'SINOPAC-08-836-003', 'SINOPAC-08-836-004', 'SINOPAC-08-836-005', 'SINOPAC-08-836-006', 'SINOPAC-08-836-007', 'SINOPAC-08-836-008', 'SINOPAC-08-836-009', 'SINOPAC-08-836-010'))
                with error_step('SINOPAC-08-837-010'):
                    loan_by_currency[cur] = _native_amount(
                        loan_by_currency.get(cur, Decimal(0)) + raw_amount,
                        cur,
                        absolute=True, _codes=('SINOPAC-08-837-001', 'SINOPAC-08-837-002', 'SINOPAC-08-837-003', 'SINOPAC-08-837-004', 'SINOPAC-08-837-005', 'SINOPAC-08-837-006', 'SINOPAC-08-837-007', 'SINOPAC-08-837-008', 'SINOPAC-08-837-009', 'SINOPAC-08-837-010'))
                raw_balance = _native_number(raw_amount, cur)
                first = currency_records[0]
                loan_type = first.get("LoanKind") or "貸款"
                raw = {"AcctText": loan_type, "currency": cur}
                accts.append({
                    "account_no": detail["account"],
                    "currency": cur,
                    "branch": None,
                    "nickname": first.get("LoanAcctCName") or loan_type,
                    "type": loan_type,
                    "product_type": account_classify.classify_account("sinopac", raw),
                    "raw_balance": raw_balance,
                    "raw_balance_date": today,
                })
        if "TWD" in loan_by_currency:
            with error_step('SINOPAC-08-015-031'):
                loan_total = int(loan_by_currency["TWD"])
    loan_by_currency_values = {
        currency: _native_number(amount, currency)
        for currency, amount in loan_by_currency.items()
    }
    fx_total = (
        next(iter(fx_by_currency.values()))
        if len(fx_by_currency) == 1
        else None if fx_by_currency else 0
    )
    if accts:
        with error_step('SINOPAC-08-015-021'):
            store.upsert_accounts(accts, commit=False)
    if twd_total or fx_by_currency or loan_by_currency_values:
        with error_step('SINOPAC-08-015-022'):
            store.upsert_balance_history([{
                "snapshotDate": today,
                "twdBalance": twd_total if twd_total else None,
                "fxBalance": fx_total if fx_total else None,
                "loanBalance": loan_total,
            }], commit=False)
        delta["balance_days"] = 1
        with error_step('SINOPAC-08-015-023'):
            store.put_daily_metric(
                "balance_latest", {
                    "twd": twd_total,
                    "fx_raw": fx_total,
                    "fx_by_currency": fx_by_currency,
                    "loan": loan_total,
                    "loan_by_currency": loan_by_currency_values,
                }, today,
                commit=False,
            )

    # --- 信用卡清單（UPSERT）---
    # Step 2 (2026-06-14): all_cards.Result.Items 沒給 limit/已用/帳單日,
    # 只有 ExpDate (MMYY 格式 e.g. '0829' = 2029/08). 推 active:
    #   ExpDate <  今月 → 過期, active=0
    #   ExpDate >= 今月 → 有效, active=1
    # 2026-06-14 Step 2 升級：從 card_statements[最新月] 套整戶 due/stmt 到每張卡。
    # 2026-06-14 Step 3 升級：card_summary[].SubInfo[][] 是 [{DataText, DataValue}] 結構,
    #   藏「信用額度(臺幣)」/「以刷卡未請款金額」等欄, 解析後套到 cards.
    # 2026-06-22 Step 4 (audit findings): 補 bill_due_amount / last_payment_amount /
    #   last_payment_date 三欄. 永豐自扣 records 「永豐自扣已入帳，謝謝！」 desc + amount<0
    #   是 last_payment 真實 source, statement.summary.paid 是已繳金額.
    #   整戶層 by-design: sinopac 多卡共用同一帳單統合, 跟 ubot 同 pattern.
    stmt_list = data.get("card_statements") or []
    sinopac_due = None
    sinopac_stmt = None
    sinopac_bill_due = None
    sinopac_last_pay_amt = None
    sinopac_last_pay_date = None
    if stmt_list and isinstance(stmt_list[0], dict):
        # card_statements 已按月份新→舊排序，[0] 是最新月
        latest = stmt_list[0]
        sinopac_due = latest.get("payment_due_date")  # '2026/06/01'
        sinopac_stmt = latest.get("billing_cycle_date")  # '2026/05/17'
        # 轉 ISO YYYY-MM-DD
        if sinopac_due and "/" in sinopac_due:
            sinopac_due = sinopac_due.replace("/", "-")
        if sinopac_stmt and "/" in sinopac_stmt:
            sinopac_stmt = sinopac_stmt.replace("/", "-")
        # 2026-07-02: card_statements.summary.paid 只是帳單彙總金額，不能單獨
        # 生成 last_payment；必須看到 records 內真實「自扣已入帳」row 才寫日期/金額。
        # 另外不能只看最新月份：最新 statement 常尚未出現自扣 row，要跨已抓月份找最新 payment row。
        summary = latest.get("summary") or {}
        with error_step('SINOPAC-08-015-024'):
            sinopac_bill_due = _num_to_float(summary.get("current_due"), _codes=('SYS-08-001-001', 'SYS-08-001-002', 'SYS-08-001-003', 'SYS-08-001-004', 'SYS-08-001-005', 'SYS-08-001-006', 'SYS-08-001-007', 'SINOPAC-08-838-008', 'SINOPAC-08-838-009', 'SINOPAC-08-838-010'))
        payment_rows = []
        for st in stmt_list:
            if not isinstance(st, dict):
                continue
            for r in (st.get("records") or []):
                if not isinstance(r, dict):
                    continue
                desc = r.get("description") or ""
                with error_step('SINOPAC-08-015-046'):
                    amt = _num_to_float(r.get("amount"), _codes=('SYS-08-001-001', 'SYS-08-001-002', 'SYS-08-001-003', 'SYS-08-001-004', 'SYS-08-001-005', 'SYS-08-001-006', 'SYS-08-001-007', 'SINOPAC-08-839-008', 'SINOPAC-08-839-009', 'SINOPAC-08-839-010'))
                if amt is None or amt >= 0:
                    continue
                if "自扣" in desc and "入帳" in desc:
                    with error_step('SINOPAC-08-015-049'):
                        td = _sinopac_date(r.get("trans_date") or r.get("post_date"))
                    if td:
                        payment_rows.append((td, abs(amt)))
        if payment_rows:
            sinopac_last_pay_date, sinopac_last_pay_amt = max(payment_rows, key=lambda x: x[0])

    # ⚠️ 2026-06-14 Step 3: 從 card_summary 解析 limit / used / stmt / due
    # 結構: card_summary = [{"TitleInfo": "", "SubInfo": [[{"DataText", "DataValue"}, ...]]}]
    # 兩層 list — 外層每張卡一筆 / 內層 SubInfo 是 group of rows
    sinopac_limit = None
    sinopac_used = None
    cs_list = data.get("card_summary") or []
    if cs_list and isinstance(cs_list[0], dict):
        # 把第一張卡的 SubInfo 攤平成 dict
        kv_map: dict[str, str] = {}
        for group in cs_list[0].get("SubInfo") or []:
            if not isinstance(group, list):
                continue
            for row in group:
                if isinstance(row, dict) and "DataText" in row and "DataValue" in row:
                    kv_map[row["DataText"]] = row["DataValue"]
        # "信用額度(臺幣)" / "信用額度" 都試
        for k in ("信用額度(臺幣)", "信用額度"):
            v = kv_map.get(k)
            if v:
                with error_step('SINOPAC-08-015-047'):
                    sinopac_limit = _num_to_float(v, _codes=('SYS-08-001-001', 'SYS-08-001-002', 'SYS-08-001-003', 'SYS-08-001-004', 'SYS-08-001-005', 'SYS-08-001-006', 'SYS-08-001-007', 'SINOPAC-08-840-008', 'SINOPAC-08-840-009', 'SINOPAC-08-840-010'))
                if sinopac_limit:
                    break
        # used: "以刷卡未請款金額" (即時) + "本期應繳" fallback
        with error_step('SINOPAC-08-015-025'):
            used_unbilled = _num_to_float(kv_map.get("以刷卡未請款金額"), _codes=('SYS-08-001-001', 'SYS-08-001-002', 'SYS-08-001-003', 'SYS-08-001-004', 'SYS-08-001-005', 'SYS-08-001-006', 'SYS-08-001-007', 'SINOPAC-08-841-008', 'SINOPAC-08-841-009', 'SINOPAC-08-841-010'))
        with error_step('SINOPAC-08-015-026'):
            used_due = _num_to_float(kv_map.get("本期應繳"), _codes=('SYS-08-001-001', 'SYS-08-001-002', 'SYS-08-001-003', 'SYS-08-001-004', 'SYS-08-001-005', 'SYS-08-001-006', 'SYS-08-001-007', 'SINOPAC-08-842-008', 'SINOPAC-08-842-009', 'SINOPAC-08-842-010'))
        # 優先抓未請款(即時), 沒有就用本期應繳
        sinopac_used = used_unbilled if used_unbilled is not None else used_due
        # 2026-06-23: card_summary raw 明確有本期應繳 / 最近繳款金額 / 最近繳款日期.
        # 若 card_statements 沒帶 summary/records (或 card_no mapping 對不上), 仍要從
        # card_summary 寫入 cards native 欄，讓 UI 最近繳款 + payments fallback 能顯示.
        if sinopac_bill_due is None:
            with error_step('SINOPAC-08-015-032'):
                sinopac_bill_due = _num_to_float(kv_map.get("本期應繳"), _codes=('SYS-08-001-001', 'SYS-08-001-002', 'SYS-08-001-003', 'SYS-08-001-004', 'SYS-08-001-005', 'SYS-08-001-006', 'SYS-08-001-007', 'SINOPAC-08-843-008', 'SINOPAC-08-843-009', 'SINOPAC-08-843-010'))
        if sinopac_last_pay_amt is None:
            with error_step('SINOPAC-08-015-033'):
                sinopac_last_pay_amt = _num_to_float(kv_map.get("最近繳款金額"), _codes=('SYS-08-001-001', 'SYS-08-001-002', 'SYS-08-001-003', 'SYS-08-001-004', 'SYS-08-001-005', 'SYS-08-001-006', 'SYS-08-001-007', 'SINOPAC-08-844-008', 'SINOPAC-08-844-009', 'SINOPAC-08-844-010'))
        if not sinopac_last_pay_date:
            v = kv_map.get("最近繳款日期")
            if v and "/" in v:
                sinopac_last_pay_date = v.replace("/", "-")
        # card_summary 也有自己的 stmt/due, 用作 fallback (若 card_statements 沒抽到)
        if not sinopac_stmt:
            v = kv_map.get("結帳日")
            if v and "/" in v:
                sinopac_stmt = v.replace("/", "-")
        if not sinopac_due:
            v = kv_map.get("繳款截止日")
            if v and "/" in v:
                sinopac_due = v.replace("/", "-")

    ac = data.get("all_cards") or {}
    if isinstance(ac, dict):
        items = (ac.get("Result") or {}).get("Items", []) or []
        cards = [
            {"number": it.get("CardNo"), "name": it.get("Name"),
             "type": it.get("CardTypeDesc"), "association": it.get("CardBrand"),
             "is_cube": False,
             # active: ExpDate 'MMYY' < 今月 → 過期
             "active": not _mmyy_expired(it.get("ExpDate"), as_of.strftime("%Y-%m")),
             # Step 2/3: 套整戶 limit/used/due/stmt
             "credit_limit": sinopac_limit,
             "used_credit": sinopac_used,
             "payment_due_date": sinopac_due,
             "statement_close_date": sinopac_stmt,
             # 2026-06-22 Step 4: 整戶層 bill_due + last_payment 套每張卡
             "bill_due_amount": sinopac_bill_due,
             "last_payment_amount": sinopac_last_pay_amt,
             "last_payment_date": sinopac_last_pay_date,
             }
            for it in items if it.get("CardNo")
        ]
        if cards:
            with error_step('SINOPAC-08-015-034'):
                store.upsert_cards(cards, commit=False)

    # --- 每日快照：信用卡彙總 / 帳單 / 資產分析 / 扣款帳戶 / 貸款明細 ---
    for key, mtag in [("card_summary", "card_summary"), ("card_billing", "card_billing"),
                       ("asset_chart", "asset_chart"), ("debit_accounts", "debit_accounts")]:
        v = data.get(key)
        if v:
            with error_step('SINOPAC-08-015-035'):
                store.put_daily_metric(mtag, v, today, commit=False)
    if loan_metric_records:
        with error_step('SINOPAC-08-015-027'):
            store.put_daily_metric("loan", {"records": loan_metric_records}, today, commit=False)

    # --- 帳戶原幣交易明細（legacy 欄位名 twd_transactions）---
    # DataText 對應：
    #   DataText1 = 交易日 + 時間 (HTML: 'YYYY/MM/DD<br />HH:MM')
    #   DataText2 = 入帳日 (YYYY/MM/DD)
    #   DataText3 = 交易類別 (官方分類名: 「台幣匯款」「手機轉帳」「利息存入」「ATM」...)
    #   DataText4 = 金額 (HTML: '<font color="...">±NNN</font>'; +/- 區分收支)
    #   DataText5 = 餘額
    #   DataText8 = 對方資訊 (含對方帳號 + 對方名稱 + 摘要; 是真正「跟誰交易」資訊)
    #
    # 鐵則: raw description 永遠 = 銀行官方原文 (DataText3 = 交易類別), 不蓋。
    # counterparty_acct + memo 額外存對方資訊。Display join 在 backend transform
    # `_twd_to_transaction` 統一處理 (`description · counterparty_acct` 對齊 MoneyBook),
    # 所有銀行通用 — 不在 persist 層做。
    twd_new = 0
    for body in data.get("account_transactions") or []:
        if not isinstance(body, dict):
            continue
        acct_no = body.get("account")
        currency = body.get("currency")
        history_identity = acct_no if currency == "TWD" else f"{acct_no}:{currency}"
        rows = []
        for t in body.get("records", []) or []:
            with error_step('SINOPAC-08-015-036'):
                expend, income = _sinopac_split_amount(t.get("DataText4"))
            with error_step('SINOPAC-08-015-037'):
                rows.append({
                    "account_no": acct_no,
                    "currency": currency,
                    "history_identity": history_identity,
                    "history_domain": "account_transactions",
                    "datetime": _sinopac_datetime(t.get("DataText1")),
                    "account_date": _sinopac_date(_sinopac_strip_html(t.get("DataText2"))),
                    "desc": _sinopac_strip_html(t.get("DataText3")),
                    "expend": expend,
                    "income": income,
                    "balance": _num_real(_sinopac_strip_html(t.get("DataText5")), _codes=('SYS-08-001-001', 'SINOPAC-08-845-002', 'SYS-08-001-003', 'SINOPAC-08-845-004', 'SINOPAC-08-845-005', 'SINOPAC-08-845-006', 'SYS-08-001-007', 'SINOPAC-08-845-008', 'SINOPAC-08-845-009', 'SINOPAC-08-845-010')),
                    "counterparty_bank": None,
                    "counterparty_acct": _sinopac_strip_html(t.get("DataText8"))[:30] or None,
                    "memo": _sinopac_strip_html(t.get("DataText8")) or None,
                })
        with error_step('SINOPAC-08-015-028'):
            twd_new += store.upsert_twd_txns(
                rows, rules=rules, commit=False,
                suppress_history_cursor=data.get("history_coverage") is not None,
            )
    delta["twd_txn_new"] = twd_new

    # 預留位（dropdown 破完再補）
    delta.setdefault("balance_days", 0)

    # --- 信用卡帳單已請款明細（StatementInquiry HTML 解析）---
    billed_new = 0
    # card_statements: list[{month, billing_cycle_date, payment_due_date, summary, records[]}]
    # 永豐 StatementInquiry 頁有「主卡」卡號顯示但 records 只給 last4，用 all_cards 補：
    cards_by_last4: dict[str, str] = {}
    ac_items = ((data.get("all_cards") or {}).get("Result") or {}).get("Items") or []
    for it in ac_items:
        cn = (it.get("CardNo") or "").strip()
        if len(cn) >= 4:
            cards_by_last4[cn[-4:]] = cn
    for m in data.get("card_statements") or []:
        if not isinstance(m, dict):
            continue
        bill_date = m.get("billing_cycle_date")
        rows = []
        for r in m.get("records") or []:
            last4 = r.get("card_last4")
            card_no = cards_by_last4.get(last4 or "", last4)
            desc = r.get("description")
            with error_step('SINOPAC-08-015-038'):
                amt = _num(r.get("amount"), _codes=('SYS-08-001-001', 'SINOPAC-08-846-002', 'SYS-08-001-003', 'SINOPAC-08-846-004', 'SINOPAC-08-846-005', 'SINOPAC-08-846-006', 'SINOPAC-08-846-007', 'SYS-08-001-008', 'SYS-08-001-009', 'SINOPAC-08-846-010'))
            rows.append({
                "card_no": card_no,
                "bill_date": bill_date,
                "currency": "TWD",
                "date": r.get("trans_date"),
                "post_date": r.get("post_date"),
                "desc": desc,
                "amount": amt,
                "consume_country": None,
                "consume_currency": None,
                "consume_amount": None,
                "txn_type": classify.classify_by_desc_and_sign(desc, amt),
            })
        if rows:
            with error_step('SINOPAC-08-015-039'):
                billed_new += store.upsert_card_billed(rows, rules=rules)
    delta["card_billed_new"] = billed_new

    # --- 信用卡未請款（LatestTx + OutstandingDetail API）---
    unb = data.get("card_unbilled") or {}
    unb_rows = []
    if isinstance(unb, dict):
        latest = unb.get("latest_tx") or {}
        if isinstance(latest, dict):
            items = ((latest.get("Result") or {}).get("Items") or [])
            for t in items:
                if not isinstance(t, dict):
                    continue
                # 欄位名稱未明（API 回空）暫用通用映射，實測有資料時再調
                card_no = t.get("CardNo") or t.get("CardNumber")
                desc = t.get("MerchantName") or t.get("Description") or t.get("Memo")
                with error_step('SINOPAC-08-015-048'):
                    amt = _num(t.get("AuthAmount") or t.get("Amount"), _codes=('SYS-08-001-001', 'SINOPAC-08-847-002', 'SYS-08-001-003', 'SINOPAC-08-847-004', 'SINOPAC-08-847-005', 'SINOPAC-08-847-006', 'SINOPAC-08-847-007', 'SYS-08-001-008', 'SYS-08-001-009', 'SINOPAC-08-847-010'))
                unb_rows.append({
                    "card_no": card_no,
                    "date": t.get("AuthDate") or t.get("ConsumeDate") or t.get("TransDate"),
                    "desc": desc,
                    "amount": amt,
                    "currency": t.get("Currency") or "TWD",
                    "txn_type": classify.classify_by_desc_and_sign(desc, amt),
                })
    latest_raw = unb.get("latest_tx") if isinstance(unb, dict) else None
    latest_result = latest_raw.get("Result") if isinstance(latest_raw, dict) else None
    latest_ok = (isinstance(latest_raw, dict)
                 and str(latest_raw.get("ResultCode")) == "00"
                 and latest_raw.get("Error") in (None, "")
                 and isinstance(latest_result, dict)
                 and isinstance(latest_result.get("Items"), list))
    with error_step('SINOPAC-08-015-018'):
        delta["card_unbilled"] = store.refresh_card_pending(
            "unbilled", unb_rows, rules=rules, fetch_ok=latest_ok, commit=False)
    delta["card_current"] = 0

    with error_step('SINOPAC-08-015-019'):
        store.log_sync(delta, commit=False)
    return delta


def persist_sinopac(
    data: dict,
    store: BankStore,
    rules: list[dict] | None = None,
    *,
    commit: bool = True,
) -> dict:
    """Persist SinoPac atomically while preserving direct-call durability."""
    with error_step('SINOPAC-08-016-001'):
        as_of = _validate_sinopac_history(data, store)
    if not commit:
        return _persist_sinopac(data, store, rules=rules, as_of=as_of)
    try:
        delta = _persist_sinopac(data, store, rules=rules, as_of=as_of)
        with error_step('SINOPAC-08-016-002'):
            store.commit()
        return delta
    except Exception:
        with error_step('SINOPAC-08-016-003'):
            store.conn.rollback()
        raise
