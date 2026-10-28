"""台新銀行 (Taishin) persist 邏輯.

從 persist.py 拆分而來 (2026-06-17, W persist.py split refactor)。
"""
from __future__ import annotations

from backend.core.error_codes import error_step, exception_error_id, with_error_code
from datetime import date, datetime, timedelta
import json
import re
from zoneinfo import ZoneInfo

from backend.core import account_classify, classify
from backend.core.base import validate_history_coverage
from backend.core.store import BankStore
from backend.core.persist._common import _num, _num_real, _num_to_float, _slash_date_to_iso


def _validated_attested_twd_rows(data: dict, store: BankStore) -> list[dict]:
    coverage = data.get("history_coverage")
    results = data.get("twd_txn_results")
    if coverage is None and results is None:
        raise with_error_code(ValueError("invalid Taishin history"), 'TAISHIN-08-001-034')
    if not isinstance(coverage, dict):
        raise with_error_code(ValueError('invalid Taishin history'), 'TAISHIN-08-001-001')
    elif not isinstance(results, list):
        raise with_error_code(ValueError('invalid Taishin history'), 'TAISHIN-08-001-002')
    try:
        with error_step('TAISHIN-08-001-044'):
            encoded_results = json.dumps(
                results, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            ).encode("utf-8")
    except (TypeError, ValueError):
        raise with_error_code(ValueError("invalid Taishin history"), 'TAISHIN-08-001-035') from None
    if len(encoded_results) > 5_000_000:
        raise with_error_code(ValueError("invalid Taishin history"), 'TAISHIN-08-001-036')
    mode = coverage.get("mode")
    with error_step('TAISHIN-08-001-042'):
        validate_history_coverage(
            coverage,
            expected_mode=mode,
            expected_domains=frozenset({"twd_transactions"}),
        )
    domains = coverage.get("domains")
    if not isinstance(domains, list):
        raise with_error_code(ValueError('invalid Taishin history'), 'TAISHIN-08-001-003')
    elif len(domains) != 1:
        raise with_error_code(ValueError('invalid Taishin history'), 'TAISHIN-08-001-004')
    expected = domains[0].get("expected")
    windows = domains[0].get("windows")
    if not isinstance(expected, list):
        raise with_error_code(ValueError('invalid Taishin history'), 'TAISHIN-08-001-005')
    elif not isinstance(windows, list):
        raise with_error_code(ValueError('invalid Taishin history'), 'TAISHIN-08-001-006')
    from backend.banks.taishin import TaishinCrawler

    if not expected:
        empty = domains[0].get("empty_window")
        try:
            with error_step('TAISHIN-08-001-045'):
                start = date.fromisoformat(empty["start"])
            with error_step('TAISHIN-08-001-046'):
                end = date.fromisoformat(empty["end"])
        except (KeyError, TypeError, ValueError):
            raise with_error_code(ValueError("invalid Taishin history"), 'TAISHIN-08-001-037') from None
        if results:
            raise with_error_code(ValueError('invalid Taishin history'), 'TAISHIN-08-001-007')
        elif windows:
            raise with_error_code(ValueError('invalid Taishin history'), 'TAISHIN-08-001-008')
        elif end != datetime.now(ZoneInfo('Asia/Taipei')).date():
            raise with_error_code(ValueError('invalid Taishin history'), 'TAISHIN-08-001-009')
        elif start != TaishinCrawler._subtract_months(end, 12):
            raise with_error_code(ValueError('invalid Taishin history'), 'TAISHIN-08-001-010')
        return []
    if len(results) != len(expected):
        raise with_error_code(ValueError('invalid Taishin history'), 'TAISHIN-08-001-011')
    elif len(results) != len(windows):
        raise with_error_code(ValueError('invalid Taishin history'), 'TAISHIN-08-001-012')

    normalized = []
    with error_step('TAISHIN-08-001-043'):
        cursors = store.latest_twd_transaction_dates()
    for result, expectation, receipt in zip(results, expected, windows, strict=True):
        if not all(isinstance(item, dict) for item in (result, expectation, receipt)):
            raise with_error_code(ValueError("invalid Taishin history"), 'TAISHIN-08-001-038')
        identity = expectation.get("identity")
        try:
            with error_step('TAISHIN-08-001-047'):
                start = date.fromisoformat(expectation["start"])
            with error_step('TAISHIN-08-001-048'):
                end = date.fromisoformat(expectation["end"])
        except (KeyError, TypeError, ValueError):
            raise with_error_code(ValueError("invalid Taishin history"), 'TAISHIN-08-001-039') from None
        period = result.get("period")
        period_starts = {
            "7_days": end - timedelta(days=7),
            "14_days": end - timedelta(days=14),
            "1_months": TaishinCrawler._subtract_months(end, 1),
            "2_months": TaishinCrawler._subtract_months(end, 2),
            "3_months": TaishinCrawler._subtract_months(end, 3),
            "6_months": TaishinCrawler._subtract_months(end, 6),
            "12_months": TaishinCrawler._subtract_months(end, 12),
        }
        cursor = cursors.get(identity) if isinstance(identity, str) else None
        if mode == "full":
            expected_period = "12_months"
        else:
            if cursor is not None and cursor > end:
                raise with_error_code(ValueError("invalid Taishin history"), 'TAISHIN-08-001-040')
            target = max(
                period_starts["12_months"],
                (cursor - timedelta(days=7)) if cursor is not None else period_starts["12_months"],
            )
            expected_period = next(
                name for name in (
                    "7_days", "14_days", "1_months", "2_months",
                    "3_months", "6_months", "12_months",
                )
                if period_starts[name] <= target
            )
        core = {
            "identity": identity,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "status": result.get("status"),
            "pages": result.get("pages"),
        }
        if not isinstance(identity, str):
            raise with_error_code(ValueError('invalid Taishin history'), 'TAISHIN-08-001-013')
        elif not re.fullmatch('\\d{12,14}', identity):
            raise with_error_code(ValueError('invalid Taishin history'), 'TAISHIN-08-001-014')
        elif period not in period_starts:
            raise with_error_code(ValueError('invalid Taishin history'), 'TAISHIN-08-001-015')
        elif period != expected_period:
            raise with_error_code(ValueError('invalid Taishin history'), 'TAISHIN-08-001-016')
        elif start != period_starts[period]:
            raise with_error_code(ValueError('invalid Taishin history'), 'TAISHIN-08-001-017')
        elif end != datetime.now(ZoneInfo('Asia/Taipei')).date():
            raise with_error_code(ValueError('invalid Taishin history'), 'TAISHIN-08-001-018')
        elif receipt != core:
            raise with_error_code(ValueError('invalid Taishin history'), 'TAISHIN-08-001-019')
        elif any((result.get(key) != value for key, value in core.items())):
            raise with_error_code(ValueError('invalid Taishin history'), 'TAISHIN-08-001-020')
        elif not isinstance(result.get('rows'), list):
            raise with_error_code(ValueError('invalid Taishin history'), 'TAISHIN-08-001-021')
        elif not isinstance(result.get('api_rows'), list):
            raise with_error_code(ValueError('invalid Taishin history'), 'TAISHIN-08-001-022')
        elif result.get('api_row_count') != len(result['api_rows']):
            raise with_error_code(ValueError('invalid Taishin history'), 'TAISHIN-08-001-023')
        elif not isinstance(result.get('transport'), dict):
            raise with_error_code(ValueError('invalid Taishin history'), 'TAISHIN-08-001-024')
        elif not isinstance(result.get('binding_digest'), str):
            raise with_error_code(ValueError('invalid Taishin history'), 'TAISHIN-08-001-025')
        elif re.fullmatch('[0-9a-f]{64}', result['binding_digest']) is None:
            raise with_error_code(ValueError('invalid Taishin history'), 'TAISHIN-08-001-026')
        elif type(result.get('request_count')) is not int:
            raise with_error_code(ValueError('invalid Taishin history'), 'TAISHIN-08-001-027')
        elif result['request_count'] != 1:
            raise with_error_code(ValueError('invalid Taishin history'), 'TAISHIN-08-001-028')
        elif type(result.get('response_count')) is not int:
            raise with_error_code(ValueError('invalid Taishin history'), 'TAISHIN-08-001-029')
        elif result['response_count'] != 1:
            raise with_error_code(ValueError('invalid Taishin history'), 'TAISHIN-08-001-030')
        try:
            with error_step('TAISHIN-08-001-049'):
                TaishinCrawler._validate_history_transport(
                    result["transport"], identity=identity, start=start, end=end,
                )
            with error_step('TAISHIN-08-001-050'):
                validated = TaishinCrawler._validate_history_snapshot(
                    result.get("snapshot"),
                    identity=identity,
                    period=period,
                    start=start,
                    end=end,
                    api_row_count=result.get("api_row_count"),
                    api_rows=result["api_rows"],
                )
        except RuntimeError as source:
            raise with_error_code(ValueError("invalid Taishin history"), exception_error_id(source, bank="taishin") or 'TAISHIN-08-001-041') from None
        if validated['status'] != result.get('status'):
            raise with_error_code(ValueError('invalid Taishin history'), 'TAISHIN-08-001-031')
        elif validated['rows'] != result['rows']:
            raise with_error_code(ValueError('invalid Taishin history'), 'TAISHIN-08-001-032')
        elif TaishinCrawler._history_rows_digest(result['api_rows']) != result['binding_digest']:
            raise with_error_code(ValueError('invalid Taishin history'), 'TAISHIN-08-001-033')
        normalized.extend(validated["rows"])
    return normalized


def _persist_taishin(
    data: dict,
    store: BankStore,
    rules: list[dict] | None = None,
    *,
    commit: bool = True,
) -> dict:
    """台新 collect → store 入庫。

    映射：
      api_responses.query.OUTPUTDATA.SavingAccount[] → accounts(UPSERT) + balance_history
      api_responses.qryTaishinPoint.value.balance     → daily_metrics
      api_responses endpoint names                     → daily_metrics
    """
    with error_step('TAISHIN-08-002-001'):
        attested_twd_rows = _validated_attested_twd_rows(data, store)
    today = datetime.now(ZoneInfo("Asia/Taipei")).date().isoformat()
    delta: dict = {"bank": "taishin"}

    apis = data.get("api_responses") or {}
    delta["scope"] = "structured"

    # --- query: SavingAccount ---
    query_resp = apis.get("query") or {}
    output = query_resp.get("OUTPUTDATA") or {}
    savings = output.get("SavingAccount") or []
    accts = []
    twd_total = 0.0
    for s in savings:
        if not isinstance(s, dict):
            continue
        acct_no = s.get("accountNo") or ""
        if not acct_no:
            continue
        with error_step('TAISHIN-08-002-004'):
            bal = _num(s.get("balance"), _codes=('SYS-08-001-001', 'TAISHIN-08-830-002', 'SYS-08-001-003', 'TAISHIN-08-830-004', 'TAISHIN-08-830-005', 'TAISHIN-08-830-006', 'TAISHIN-08-830-007', 'SYS-08-001-008', 'SYS-08-001-009', 'TAISHIN-08-830-010')) or 0.0
        raw = {**s, "type": s.get("accountTypeName") or "活期存款", "currency": "TWD"}
        with error_step('TAISHIN-08-002-005'):
            accts.append({
                "account_no": acct_no,
                "currency": "TWD",
                "branch": None,
                "nickname": s.get("userdefineName") or None,
                "type": s.get("accountTypeName") or None,
                "product_type": account_classify.classify_account("taishin", raw),
                "raw_balance": _num_real(s.get("balance"), _codes=('SYS-08-001-001', 'TAISHIN-08-831-002', 'SYS-08-001-003', 'TAISHIN-08-831-004', 'TAISHIN-08-831-005', 'TAISHIN-08-831-006', 'SYS-08-001-007', 'TAISHIN-08-831-008', 'TAISHIN-08-831-009', 'TAISHIN-08-831-010')),
                "raw_balance_date": today,
            })
        twd_total += bal
    if accts:
        with error_step('TAISHIN-08-002-006'):
            store.upsert_accounts(accts, commit=commit)
    if accts and twd_total >= 0:
        with error_step('TAISHIN-08-002-007'):
            store.upsert_balance_history([{
                "snapshotDate": today,
                "twdBalance": twd_total if twd_total else None,
                "fxBalance": None,
            }], commit=commit)
        delta["balance_days"] = 1
        with error_step('TAISHIN-08-002-008'):
            store.put_daily_metric(
                "balance_latest", {"twd": twd_total, "n_accounts": len(accts)},
                today, commit=commit,
            )

    # --- qryTaishinPoint ---
    pts = apis.get("qryTaishinPoint") or {}
    val = pts.get("value") or {}
    if isinstance(val, dict) and val.get("balance") is not None:
        with error_step('TAISHIN-08-002-009'):
            store.put_daily_metric(
                "taishin_points",
                {"balance": val.get("balance"), "TSPOINT_balance": val.get("TSPOINT_balance")},
                today, commit=commit,
            )


    # --- 信用卡（從 frame text parser 抽出）---
    # 2026-06-11 端到端：mouse.move hover mega menu → click「查詢信用卡明細」
    # → frame URL = `RB0708/0100` → frame.body.innerText → _parse_credit_card_page
    cc_new = 0
    cc_pending_n = 0

    def _norm_date(s: str | None) -> str | None:
        """台新 frame text 日期可能是 '2026/5/12' (無零) → 規範化成 '2026-05-12'。"""
        if not s:
            return None
        try:
            y, mo, d = s.replace("-", "/").split("/")
            with error_step('TAISHIN-08-003-001'):
                return f"{int(y):04d}-{int(mo):02d}-{int(d):02d}"
        except Exception:
            return s.replace("/", "-")

    parsed_raw = data.get("credit_card_parsed")
    parsed = parsed_raw if isinstance(parsed_raw, dict) else {}
    if isinstance(parsed, dict) and not parsed.get("error"):
        # Step 2 (2026-06-14): Taishin per-card 沒 limit, 整戶 qryRealTime.crlimit
        # + billing_period.{pay_due_date, statement_date} 套到每張卡 (通常 1 張).
        period = parsed.get("billing_period") or {}
        with error_step('TAISHIN-08-002-010'):
            taishin_stmt = _slash_date_to_iso(period.get("statement_date"))  # '2026/5/12'
        with error_step('TAISHIN-08-002-011'):
            taishin_due = _slash_date_to_iso(period.get("pay_due_date"))      # '2026/05/27'
        # qryRealTime.value.crlimit = '400,000' (整戶層)
        api_resps = data.get("api_responses") or {}
        qry_real = api_resps.get("qryRealTime")
        qry_val = (qry_real[0] if isinstance(qry_real, list) else qry_real or {}).get("value") or {}
        with error_step('TAISHIN-08-002-012'):
            taishin_limit = _num_to_float(qry_val.get("crlimit"), _codes=('SYS-08-001-001', 'SYS-08-001-002', 'SYS-08-001-003', 'SYS-08-001-004', 'SYS-08-001-005', 'SYS-08-001-006', 'SYS-08-001-007', 'TAISHIN-08-832-008', 'TAISHIN-08-832-009', 'TAISHIN-08-832-010'))

        # 2026-07-02: 台新真正繳款紀錄藏在 RB0708 其他月份的「上期實繳金額明細」；
        # 不是「已繳款明細」（該段實測查無資料），也不是存款端卡費 fallback。
        payment_rows = []
        for row in parsed.get("billed_txns") or []:
            desc = str(row.get("desc") or "")
            with error_step('TAISHIN-08-002-016'):
                amt = _num_to_float(row.get("amount"), _codes=('SYS-08-001-001', 'SYS-08-001-002', 'SYS-08-001-003', 'SYS-08-001-004', 'SYS-08-001-005', 'SYS-08-001-006', 'SYS-08-001-007', 'TAISHIN-08-833-008', 'TAISHIN-08-833-009', 'TAISHIN-08-833-010'))
            if amt is None or amt >= 0:
                continue
            if "信用卡" in desc and ("扣繳" in desc or "自動轉帳" in desc or "繳" in desc):
                payment_rows.append(row)
        latest_payment = None
        if payment_rows:
            latest_payment = max(payment_rows, key=lambda r: str(r.get("post_date") or r.get("txn_date") or ""))

        # 2026-06-14 Step 2 升級：used_credit 用 doXTPA.value.001 (即時可用額度 +
        # 永久額度) 算出來，比 top_summary.unpaid (未繳已出帳) 更準確 — 它含
        # 已動用但尚未出帳的即時消費.
        # used = CRLIMIT-PERM - AVAIL-CREDIT  e.g. 400000 - 399920 = 80
        taishin_used = None
        xtpa = api_resps.get("doXTPA")
        xtpa_val = (xtpa[0] if isinstance(xtpa, list) else xtpa or {}).get("value") or {}
        # doXTPA.value.001 (card index 001) 為主卡
        card_001 = xtpa_val.get("001") if isinstance(xtpa_val, dict) else None
        if isinstance(card_001, dict):
            with error_step('TAISHIN-08-002-017'):
                crlimit_perm = _num_to_float(card_001.get("OUT-CRLIMIT-PERM"), _codes=('SYS-08-001-001', 'SYS-08-001-002', 'SYS-08-001-003', 'SYS-08-001-004', 'SYS-08-001-005', 'SYS-08-001-006', 'SYS-08-001-007', 'TAISHIN-08-834-008', 'TAISHIN-08-834-009', 'TAISHIN-08-834-010'))
            with error_step('TAISHIN-08-002-018'):
                avail = _num_to_float(card_001.get("OUT-AVAIL-CREDIT"), _codes=('SYS-08-001-001', 'SYS-08-001-002', 'SYS-08-001-003', 'SYS-08-001-004', 'SYS-08-001-005', 'SYS-08-001-006', 'SYS-08-001-007', 'TAISHIN-08-835-008', 'TAISHIN-08-835-009', 'TAISHIN-08-835-010'))
            if crlimit_perm is not None and avail is not None:
                taishin_used = crlimit_perm - avail
            # 若 doXTPA 也給 CRLIMIT, 信用度比 qryRealTime 更新
            if taishin_limit is None:
                taishin_limit = crlimit_perm

        # cards 入庫（store 需要 number 鍵 → card_no SQL 欄）
        cards = parsed.get("cards") or []
        # 2026-07-03 修正: bill_due_amount 應用 summary.remaining (本期剩餘應繳)
        # 而非 bill_amount (本期帳單總額)。1409 卡自動扣繳 case:
        #   bill_amount=80, paid=80, remaining=0
        # 舊碼寫 bill_due_amount=80 → frontend bill_status 誤判「未繳」。
        # remaining 是 UI「還要繳多少」的正確口徑; fallback 用 bill_amount - paid
        # 保護 raw 沒吐 remaining 的舊 shape。詳見 wiki
        # [[card-payment-history-native-fallback-pattern]] Taishin section.
        # last_payment_* 仍走真實 billed_txns payment row (使用者「不假造」鐵則),
        # 整戶層套到每張卡 (台新通常 1 張)。
        summary = parsed.get("summary") or {}
        with error_step('TAISHIN-08-002-013'):
            taishin_bill_due = _num_to_float(summary.get("remaining"), _codes=('SYS-08-001-001', 'SYS-08-001-002', 'SYS-08-001-003', 'SYS-08-001-004', 'SYS-08-001-005', 'SYS-08-001-006', 'SYS-08-001-007', 'TAISHIN-08-836-008', 'TAISHIN-08-836-009', 'TAISHIN-08-836-010'))
        if taishin_bill_due is None:
            with error_step('TAISHIN-08-002-019'):
                _bill = _num_to_float(summary.get("bill_amount"), _codes=('SYS-08-001-001', 'SYS-08-001-002', 'SYS-08-001-003', 'SYS-08-001-004', 'SYS-08-001-005', 'SYS-08-001-006', 'SYS-08-001-007', 'TAISHIN-08-837-008', 'TAISHIN-08-837-009', 'TAISHIN-08-837-010'))
            with error_step('TAISHIN-08-002-020'):
                _paid = _num_to_float(summary.get("paid"), _codes=('SYS-08-001-001', 'SYS-08-001-002', 'SYS-08-001-003', 'SYS-08-001-004', 'SYS-08-001-005', 'SYS-08-001-006', 'SYS-08-001-007', 'TAISHIN-08-838-008', 'TAISHIN-08-838-009', 'TAISHIN-08-838-010')) or 0
            taishin_bill_due = (_bill - _paid) if _bill is not None else None
        taishin_last_pay_amt = None
        taishin_last_pay_date = None
        if latest_payment:
            with error_step('TAISHIN-08-002-021'):
                pay_amt = _num_to_float(latest_payment.get("amount"), _codes=('SYS-08-001-001', 'SYS-08-001-002', 'SYS-08-001-003', 'SYS-08-001-004', 'SYS-08-001-005', 'SYS-08-001-006', 'SYS-08-001-007', 'TAISHIN-08-839-008', 'TAISHIN-08-839-009', 'TAISHIN-08-839-010'))
            if pay_amt is not None:
                taishin_last_pay_amt = abs(pay_amt)
            with error_step('TAISHIN-08-002-022'):
                taishin_last_pay_date = _slash_date_to_iso(
                    latest_payment.get("post_date") or latest_payment.get("txn_date"),
                )
        # 2026-06-22 v2: 不再 sentinel paid=0 → None. 0 是合法值.
        # 若有「上期實繳金額明細」payment row，該 row 的入帳日/金額才是繳款紀錄 source.

        for card in cards:
            card["credit_limit"] = taishin_limit
            card["statement_close_date"] = taishin_stmt
            card["payment_due_date"] = taishin_due
            # used_credit: 優先 doXTPA 即時算 (taishin_used)，
            # fallback top_summary.unpaid (未繳已出帳) 或 period.bill_amount
            top = parsed.get("top_summary") or {}
            with error_step('TAISHIN-08-002-023'):
                card["used_credit"] = (taishin_used
                                       if taishin_used is not None
                                       else _num_to_float(top.get("unpaid") or period.get("bill_amount"), _codes=('SYS-08-001-001', 'SYS-08-001-002', 'SYS-08-001-003', 'SYS-08-001-004', 'SYS-08-001-005', 'SYS-08-001-006', 'SYS-08-001-007', 'TAISHIN-08-840-008', 'TAISHIN-08-840-009', 'TAISHIN-08-840-010')))
            # 2026-06-22 (audit): bill_due + last_payment_amount 整戶層套
            card["bill_due_amount"] = taishin_bill_due
            card["last_payment_amount"] = taishin_last_pay_amt
            card["last_payment_date"] = taishin_last_pay_date
        if cards:
            with error_step('TAISHIN-08-002-024'):
                store.upsert_cards(cards, commit=commit)

        # 帳單期間（給 bill_date 用）
        with error_step('TAISHIN-08-002-014'):
            bill_date = _norm_date(period.get("statement_date")) or today

        # billed_txns 入庫（上期實繳明細）
        bills = parsed.get("billed_txns") or []
        billed_payload = []
        for b in bills:
            try:
                card_no_suffix = b.get("card_no_suffix") or ""
                card_no = f"****{card_no_suffix}" if card_no_suffix else None
                with error_step('TAISHIN-08-002-029'):
                    consume_date = _norm_date(b.get("txn_date"))
                with error_step('TAISHIN-08-002-030'):
                    post_date = _norm_date(b.get("post_date"))
                desc = b.get("desc")
                amt = b.get("amount")
                billed_payload.append({
                    "card_no": card_no,
                    "bill_date": bill_date,
                    "date": consume_date,
                    "post_date": post_date,
                    "desc": desc,
                    "amount": amt,
                    "currency": "TWD" if b.get("currency") == "新臺幣" else b.get("currency"),
                    "txn_type": classify.classify_by_desc_and_sign(desc, amt),
                })
            except Exception:
                continue
        if billed_payload:
            with error_step('TAISHIN-08-002-025'):
                cc_new = store.upsert_card_billed(billed_payload, rules=rules) or 0

        # pending_txns 入庫（即時消費紀錄, scope='realtime'）
        pendings = parsed.get("pending_txns") or []
        pending_payload = []
        for p in pendings:
            try:
                card_no_suffix = p.get("card_no_suffix") or ""
                card_no = f"****{card_no_suffix}" if card_no_suffix else None
                desc = p.get("desc")
                amt = p.get("amount")
                with error_step('TAISHIN-08-002-031'):
                    pending_payload.append({
                        "card_no": card_no,
                        "date": _norm_date(p.get("txn_date")),
                        "desc": desc,
                        "amount": amt,
                        "currency": "TWD",
                        "txn_type": classify.classify_by_desc_and_sign(desc, amt),
                    })
            except Exception:
                continue
        fetch_ok = (isinstance(parsed_raw, dict)
                    and parsed_raw.get("fetch_ok") is True
                    and not parsed_raw.get("error"))
        with error_step('TAISHIN-08-002-015'):
            cc_pending_n = store.refresh_card_pending(
                "realtime", pending_payload, rules=rules, fetch_ok=fetch_ok, commit=commit,
            )

        # 各類 summary → daily_metrics（SCSB 模式）
        top = parsed.get("top_summary") or {}
        if top:
            with error_step('TAISHIN-08-002-026'):
                store.put_daily_metric("taishin_card_top_summary", top, today, commit=commit)
        summary = parsed.get("summary") or {}
        if summary:
            with error_step('TAISHIN-08-002-027'):
                store.put_daily_metric(
                    "taishin_card_current_period", summary, today, commit=commit,
                )
        if period:
            with error_step('TAISHIN-08-002-028'):
                store.put_daily_metric(
                    "taishin_card_billing_period", period, today, commit=commit,
                )

    delta.setdefault("balance_days", 0)
    twd_rows = attested_twd_rows
    with error_step('TAISHIN-08-002-002'):
        twd_new = (
            store.upsert_twd_txns(
                twd_rows, rules=rules, commit=commit,
                suppress_history_cursor=data.get("history_coverage") is not None,
            )
            if twd_rows else 0
        )
    delta["twd_txn_new"] = twd_new
    delta["card_billed_new"] = cc_new
    delta["card_unbilled"] = 0  # 台新「未出帳款」表頁無逐筆明細，僅頂部 TWD 80 摘要
    delta["card_current"] = cc_pending_n

    with error_step('TAISHIN-08-002-003'):
        store.log_sync(delta, commit=commit)
    return delta


def persist_taishin(
    data: dict,
    store: BankStore,
    rules: list[dict] | None = None,
    *,
    commit: bool = True,
) -> dict:
    """Validate and persist one Taishin payload atomically."""
    try:
        delta = _persist_taishin(data, store, rules, commit=False)
        if commit:
            with error_step('TAISHIN-08-004-001'):
                store.commit()
        return delta
    except Exception:
        if commit:
            with error_step('TAISHIN-08-004-002'):
                store.rollback()
        raise
