"""富邦銀行 (Fubon) persist 邏輯.

從 persist.py 拆分而來 (2026-06-17, W persist.py split refactor)。
"""
from __future__ import annotations

import re
from datetime import datetime

from backend.core import classify
from backend.core.store import BankStore
from backend.core.persist._common import _num_to_float, _roc_to_west, _slash_date_to_iso


def _parse_fubon_deposit_accounts(deposit_text: str) -> list[dict]:
    """從富邦「我的存款」頁 (CBOQU003_Home.faces) innerText 解析活儲/定存帳戶 row.

    輸入結構 (local probe debug_fubon_deposit.py 證實):
        帳號\\t帳戶暱稱\\t存款類別\\t分行\\t幣別\\t即時餘額\\t可用餘額\\t存單號碼\\t到期日\\t功能
        00900000147012\\n\\n\\t數位活儲\\t營業部\\t臺幣\\t0.00\\t0.00\\t\\t\\t快速功能
        00900000157046\\n\\n\\t活儲存款\\t松高分行\\t臺幣\\t0.00\\t0.00\\t\\t\\t快速功能

    output: [{"account_no", "type", "branch", "currency", "raw_balance",
             "raw_balance_date", "product_type"}, ...]

    Note: 富邦帳號 12-16 碼 (數位活儲 14 碼, 一般 11-12 碼), tab/換行混排.
    用「行內第一個 12+ 碼數字」作 anchor, 之後 split tab 取後續欄.
    """
    if not deposit_text:
        return []
    out: list[dict] = []
    today = datetime.now().strftime("%Y-%m-%d")

    # 把連續 \n\n\t 規整成 \t (富邦 JSF 渲染奇怪)
    norm = re.sub(r"\n+", "\n", deposit_text)
    lines = norm.split("\n")

    # 找含 12+ 碼帳號的行 — 帳號可能單獨一行(後續 type/branch/currency 在下一行 tab 分隔)
    # 也可能 「{account_no}\t\t{type}\t{branch}\t{currency}\t{bal}\t{usable}」整行 tab
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        m = re.match(r"^(\d{11,16})\b", line)
        if not m:
            i += 1
            continue
        account_no = m.group(1)
        # 收集 row 內所有欄: 從此行的 tab 拆 + 下面 1-3 行 (富邦渲染有時帳號獨佔)
        chunks: list[str] = []
        # 先把 anchor 行 tab 拆出去掉開頭帳號的剩餘 cells
        rest = line[len(account_no):].lstrip("\t").rstrip()
        if rest:
            chunks.extend(c.strip() for c in rest.split("\t"))
        # 再 peek 後續 1-3 行直到湊滿或撞下個帳號/footer
        FOOTER_KW = re.compile(r"^(注意事項|存款總計|外幣存款|可用餘額|新台幣實體|若帳戶|＊)")
        j = i + 1
        while j < len(lines) and len(chunks) < 8:
            nxt = lines[j].rstrip()
            nxt_stripped = nxt.strip()
            if re.match(r"^\d{11,16}\b", nxt_stripped):
                break  # 下個帳號開始
            if FOOTER_KW.match(nxt_stripped):
                break  # 撞到 footer 註腳, 收手
            if not nxt_stripped:
                j += 1
                continue
            for c in nxt.split("\t"):
                cv = c.strip()
                if cv:
                    chunks.append(cv)
            j += 1

        # chunks 期望順序: [(nickname), type, branch, currency, raw_balance, usable, ...]
        # 但實際 nickname 常空, 第一個 chunk 可能直接是 type ('數位活儲' / '活儲存款' / '定期存款')
        # 用啟發式找各欄
        acct_type = None
        branch = None
        currency = None
        raw_balance: float | None = None
        nickname = None

        TYPE_KW = re.compile(r"(活儲|定存|定期存款|綜合存款|外匯活存|外匯活儲|數位活儲|薪轉|支存|支票存款|綜活存)")
        BRANCH_KW = re.compile(r"(分行|營業部|總行)$")
        CURRENCY_MAP = {"臺幣": "TWD", "台幣": "TWD", "新台幣": "TWD", "新臺幣": "TWD",
                       "美金": "USD", "美元": "USD", "日圓": "JPY", "日幣": "JPY",
                       "歐元": "EUR", "人民幣": "CNY", "港幣": "HKD"}
        NUM_RE = re.compile(r"^-?\d{1,3}(,\d{3})*(\.\d+)?$|^-?\d+(\.\d+)?$")

        for c in chunks:
            if c == "快速功能" or c == "功能" or c == "" or c == "-":
                continue
            if acct_type is None and TYPE_KW.search(c):
                acct_type = c
            elif branch is None and BRANCH_KW.search(c):
                branch = c
            elif currency is None and c in CURRENCY_MAP:
                currency = CURRENCY_MAP[c]
            elif raw_balance is None and NUM_RE.match(c):
                # 「即時餘額」是第一個出現的數字 (usable 排第二)
                raw_balance = _num_to_float(c)
            elif nickname is None and len(c) <= 20 and not NUM_RE.match(c):
                # 暫存可能的 nickname (排除已 matched 的)
                if c != acct_type and c != branch and not BRANCH_KW.search(c):
                    nickname = c

        # 至少要有 type 或 currency 才算 valid (避免雜訊行誤判)
        if not acct_type and not currency:
            i = j
            continue

        out.append({
            "account_no": account_no,
            "currency": currency or "TWD",
            "branch": branch,
            "nickname": nickname,
            "type": acct_type,
            "product_type": None,  # 由 account_classify 補
            "raw_balance": raw_balance,
            "raw_balance_date": today if raw_balance is not None else None,
        })
        i = j

    return out


def _parse_fubon_deposit_txn_results(results: list[dict]) -> list[dict]:
    """富邦「存款交易查詢」結果頁 text → twd_transactions rows.

    Collector 會對 CDSQU001 每個帳戶送一次「近1個月」查詢，留下：
      {account_no, selected_text, text}

    富邦結果頁是 JSF innerText/tab-separated table，典型欄位會包含
    「交易日期 / 摘要或交易說明 / 支出 / 存入 / 餘額 / 備註」。不同帳戶或
    無資料時文案會漂移，所以 parser 採 header anchor + row heuristic：
    - 只處理含日期開頭的 row。
    - expend/income/balance 從 row 尾端數字欄由右往左判讀。
    - 明細 description 取日期後、第一個金額欄前的文字。
    """
    rows: list[dict] = []
    for result in results or []:
        if not isinstance(result, dict):
            continue
        text = result.get("text") or ""
        if not text or "查無" in text and not re.search(r"\d{4}/\d{1,2}/\d{1,2}", text):
            continue
        account_no = result.get("account_no")
        if not account_no:
            m_acct = re.search(r"\d{10,16}", result.get("selected_text") or text)
            account_no = m_acct.group(0) if m_acct else None
        if not account_no:
            continue

        for raw_line in text.splitlines():
            raw_cells = [c.strip().replace("　", "") for c in raw_line.split("\t")]
            # Real CDSQU001 result table:
            # 帳務日期\t交易時間\t摘要\t支出金額\t存入金額\t即時餘額\t附註
            if (
                len(raw_cells) >= 6
                and re.fullmatch(r"\d{4}/\d{1,2}/\d{1,2}", raw_cells[0] or "")
                and re.match(r"\d{4}/\d{1,2}/\d{1,2}\s+\d{1,2}:\d{2}:\d{2}", raw_cells[1] or "")
            ):
                date_iso = _slash_date_to_iso(raw_cells[0])
                txn_dt = raw_cells[1].replace("/", "-")
                desc = raw_cells[2] or None
                expend = _num_to_float(raw_cells[3])
                income = _num_to_float(raw_cells[4])
                balance = _num_to_float(raw_cells[5])
                memo = raw_cells[6] if len(raw_cells) > 6 and raw_cells[6] else None
                if date_iso and desc:
                    rows.append({
                        "account_no": account_no,
                        "datetime": txn_dt,
                        "account_date": date_iso,
                        "desc": desc,
                        "expend": expend,
                        "income": income,
                        "balance": balance,
                        "counterparty_bank": None,
                        "counterparty_acct": memo[:30] if memo else None,
                        "memo": memo,
                    })
                continue

            line = raw_line.strip()
            if not line:
                continue
            m = re.match(r"^(\d{4}/\d{1,2}/\d{1,2})(?:\s+|\t+)(.+)$", line)
            if not m:
                continue
            date_iso = _slash_date_to_iso(m.group(1))
            rest = m.group(2).strip()
            if not date_iso or any(k in rest for k in ["開始查詢", "自訂查詢", "快速查詢"]):
                continue
            cells = [c.strip().replace("　", "") for c in re.split(r"\t+", rest) if c.strip().replace("　", "")]
            if not cells:
                continue

            num_cells: list[tuple[int, float]] = []
            for idx, cell in enumerate(cells):
                normalized = cell.replace(",", "").replace(" ", "")
                if re.fullmatch(r"-?\d+(?:\.\d+)?", normalized):
                    val = _num_to_float(cell)
                    if val is not None:
                        num_cells.append((idx, val))
            if not num_cells:
                continue

            balance = num_cells[-1][1] if len(num_cells) >= 2 else None
            amount_nums = num_cells[:-1] if balance is not None else num_cells
            expend = income = None
            if len(amount_nums) >= 2:
                # 富邦表格通常是 支出 / 存入 / 餘額；0 或空欄會被渲染成 0。
                expend = amount_nums[-2][1] or None
                income = amount_nums[-1][1] or None
            elif len(amount_nums) == 1:
                # 若只剩單一金額欄，依正負號判斷，正數保守視為收入。
                amt = amount_nums[0][1]
                if amt < 0:
                    expend = abs(amt)
                else:
                    income = amt

            first_num_idx = min(idx for idx, _ in num_cells)
            desc_cells = cells[:first_num_idx]
            desc = " ".join(c for c in desc_cells if c).strip() or None
            memo_cells = cells[first_num_idx + len(num_cells):]
            memo = " ".join(c for c in memo_cells if c).strip() or None
            if not desc and memo:
                desc = memo
            if not desc:
                continue
            rows.append({
                "account_no": account_no,
                "datetime": date_iso,
                "account_date": date_iso,
                "desc": desc,
                "expend": expend,
                "income": income,
                "balance": balance,
                "counterparty_bank": None,
                "counterparty_acct": None,
                "memo": memo,
            })
    return rows


def _parse_fubon_credit_card(data: dict) -> dict:
    """富邦三段頁面 text → 結構化資料。

    輸入 data 含三個 key:
      - amount_page_text   (繳款及額度查詢, CCCQU002)
      - billed_page_text   (帳單明細查詢, CCCQU003)
      - pending_page_text  (未出帳單消費明細, CCCQU004)
      - frames[]           (含我的信用卡頁 CCCQU001 卡片清單)

    輸出:
      cards[]: {number, name?, brand?, status?}
      billing_summary: {bill_date, total_due, min_due, due_date, previous_balance, payment, current_charge, ...}
      billed_txns[]: {date, post_date, desc, amount, currency}
      pending_txns[]: {date, desc, amount, currency}
      limits: {credit_limit, available_credit, cash_advance_limit, cash_advance_available}
      points: {好多金: ...}
    """

    out: dict = {}

    # === A. 從 cards_page_text 抓卡片清單（CCCQU001_Home）===
    # 注意：collect Step 4.5 已 dump 此 text 到 data["cards_page_text"]
    cards_text = data.get("cards_page_text") or ""
    cards = []
    if cards_text:
        # 卡片表格行：卡別 \t 卡種 \t 卡號(900051******7021) \t 正附卡 \t 狀態
        # 卡號格式：6 digits + 6 * + 4 digits
        for m in re.finditer(
            r"([^\n\t]+?)\s*\n?\s*\t\s*([^\n\t]+?)\s*\n?\s*\t\s*(\d{6}\*{6}\d{4})\s*\n?\s*\t\s*([正附]卡)\s*\n?\s*\t\s*([^\n\t]+)",
            cards_text,
        ):
            card_label = m.group(1).strip()
            card_brand = m.group(2).strip()
            card_no_full = m.group(3).strip()
            card_no_last4 = card_no_full[-4:]
            primary = m.group(4).strip()
            status = m.group(5).strip()
            cards.append({
                "number": f"****{card_no_last4}",
                "card_no_full_masked": card_no_full,
                "name": card_label,
                "brand": card_brand,
                "primary": primary,
                "status": status,
            })

    # fallback: 從 frames[] 找（向下相容舊版資料）
    if not cards:
        frames = data.get("frames") or []
        for fd in frames:
            url = fd.get("url") or ""
            text = fd.get("text") or ""
            if "CCCQU001" in url or "您已申請的信用卡如下" in text:
                for m in re.finditer(
                    r"([^\n\t]+?)\s*\n?\s*\t\s*([^\n\t]+?)\s*\n?\s*\t\s*(\d{6}\*{6}\d{4})\s*\n?\s*\t\s*([正附]卡)\s*\n?\s*\t\s*([^\n\t]+)",
                    text,
                ):
                    card_no_full = m.group(3).strip()
                    cards.append({
                        "number": f"****{card_no_full[-4:]}",
                        "card_no_full_masked": card_no_full,
                        "name": m.group(1).strip(),
                        "brand": m.group(2).strip(),
                        "primary": m.group(4).strip(),
                        "status": m.group(5).strip(),
                    })
                break
    out["cards"] = cards

    # === B. amount_page: 抓額度 + 本期摘要 ===
    amount_text = data.get("amount_page_text") or ""
    limits: dict = {}
    if amount_text:
        # 「正卡人信用額度\t80,000」
        m = re.search(r"正卡人信用額度\s*\t\s*([\d,]+)", amount_text)
        if m:
            limits["credit_limit"] = int(m.group(1).replace(",", ""))
        m = re.search(r"正卡人可用額度\s*\t\s*([\d,]+)", amount_text)
        if m:
            limits["available_credit"] = int(m.group(1).replace(",", ""))
        m = re.search(r"國內預借現金信用額度\s*\t\s*([\d,]+)", amount_text)
        if m:
            limits["cash_advance_limit"] = int(m.group(1).replace(",", ""))
        m = re.search(r"國內預借現金可用額度\s*\t\s*([\d,]+)", amount_text)
        if m:
            limits["cash_advance_available"] = int(m.group(1).replace(",", ""))
        # 本期循環利率
        m = re.search(r"本期循環利率\s*\t\s*([\d.]+)%", amount_text)
        if m:
            limits["revolving_rate_percent"] = float(m.group(1))
    out["limits"] = limits

    # 本期帳單表 (從 amount_text 抓)
    # 「2026/05/16\t0\t0\t無需繳款\t12.62%\t5.62%」
    billing_summary: dict = {}
    if amount_text:
        m = re.search(
            r"本期帳單結帳日\s*\t\s*應繳總金額\s*\t\s*最低應繳金額\s*\t\s*繳款截止日\s*\t.*?\n"
            r"(\d{4}/\d{1,2}/\d{1,2})\s*\t\s*([\d,]+)\s*\t\s*([\d,]+)\s*\t\s*([^\t\n]+)",
            amount_text,
        )
        if m:
            billing_summary["bill_date"] = _norm_west_date(m.group(1))
            billing_summary["total_due"] = int(m.group(2).replace(",", ""))
            billing_summary["min_due"] = int(m.group(3).replace(",", ""))
            # Normalize at the source layer: downstream cards.payment_due_date
            # must be ISO-ish YYYY-MM-DD, not bank-native YYYY/MM/DD.
            billing_summary["payment_due"] = _norm_west_date(m.group(4).strip())
        # 「無需繳款\t2026/05/05\t0\t0\t台北富邦900047****7012」
        m = re.search(
            r"繳款狀態.*?自動扣繳帳號.*?\n"
            r"([^\t\n]+)\s*\t\s*(\d{4}/\d{1,2}/\d{1,2})\s*\t\s*([\d,]+)\s*\t\s*([\d,]+)\s*\t\s*([^\t\n]+)",
            amount_text,
        )
        if m:
            billing_summary["payment_status"] = m.group(1).strip()
            billing_summary["last_payment_date"] = _norm_west_date(m.group(2))
            billing_summary["paid_amount"] = int(m.group(3).replace(",", ""))
            billing_summary["remaining_due"] = int(m.group(4).replace(",", ""))
            billing_summary["autopay_account"] = m.group(5).strip()

    # === C. billed_page: 帳單期間 + 逐筆已出帳 ===
    billed_text = data.get("billed_page_text") or ""
    billed_txns = []
    if billed_text:
        # 帳單年月: 「115/05\t80,000\t8,000\t115/05/16\t無需繳款\t12.62%\t---\t0/0」
        m = re.search(
            r"帳單年月.*?\n(\d{2,3}/\d{1,2})\s*\t\s*([\d,]+)\s*\t\s*([\d,]+)\s*\t\s*(\d{2,3}/\d{1,2}/\d{1,2})\s*\t\s*([^\t]+)",
            billed_text,
        )
        if m:
            billing_summary["bill_month_roc"] = m.group(1)
            billing_summary["statement_date_roc"] = m.group(4)
            billing_summary["statement_date"] = _roc_to_west(m.group(4))
            if not billing_summary.get("bill_date"):
                billing_summary["bill_date"] = billing_summary["statement_date"]

        # 「前期應繳總額\t \t繳(退)金額\t \t本期調整金額\t \t本期新增\t \t本期應繳總額」
        m = re.search(
            r"前期應繳總額.*?\n([\d,\-]+)\s*\t\s*-\s*\t\s*([\d,\-]+)\s*\t\s*-\s*\t\s*([\d,\-]+)\s*\t\s*\+\s*\t\s*([\d,\-]+)\s*\t\s*=\s*\t\s*([\d,\-]+)\s*\t\s*([\d,\-]+)\s*\t\s*([\d,\-]+)",
            billed_text,
        )
        if m:
            billing_summary["previous_balance"] = int(m.group(1).replace(",", ""))
            billing_summary["payment"] = int(m.group(2).replace(",", ""))
            billing_summary["adjustment"] = int(m.group(3).replace(",", ""))
            billing_summary["current_charge"] = int(m.group(4).replace(",", ""))
            billing_summary["current_due"] = int(m.group(5).replace(",", ""))
            billing_summary["min_due_billed"] = int(m.group(6).replace(",", ""))
            billing_summary["revolving_principal"] = int(m.group(7).replace(",", ""))

        # 逐筆交易：「消費日期\t消費說明\t入帳日期\t外幣折算日/幣別\t外幣金額/消費地\t臺幣金額」
        # 行格式：「115/05/05\t自動扣繳\t115/05/06\t　\t　\t-7,271」
        # 也有：「　\t前期應繳總額\t　\t　\t　\t7,271」（聚合行，跳過）
        for tm in re.finditer(
            r"(\d{2,3}/\d{1,2}/\d{1,2})\s*\t\s*([^\t\n]+?)\s*\t\s*(\d{2,3}/\d{1,2}/\d{1,2})\s*\t\s*([^\t\n]*?)\s*\t\s*([^\t\n]*?)\s*\t\s*(-?[\d,]+(?:\.\d+)?)",
            billed_text,
        ):
            consume_date = _roc_to_west(tm.group(1))
            desc = tm.group(2).strip()
            post_date = _roc_to_west(tm.group(3))
            fx_part = tm.group(5).strip()
            amount_str = tm.group(6).replace(",", "")
            # 跳過聚合行（desc 含「前期應繳/本期應繳/最低應繳」）
            if any(k in desc for k in ["應繳總額", "最低應繳", "本期調整"]):
                continue
            try:
                amount = float(amount_str)
            except ValueError:
                continue
            billed_txns.append({
                "date": consume_date,
                "post_date": post_date,
                "desc": desc,
                "amount": amount,
                "currency": "TWD",
                "fx_info": fx_part if fx_part else None,
            })
    out["billing_summary"] = billing_summary
    out["billed_txns"] = billed_txns

    # === D. pending_page: 未出帳 ===
    pending_text = data.get("pending_page_text") or ""
    pending_txns = []
    if pending_text and "查無相關資料" not in pending_text:
        # 同格式：日期\t說明\t入帳日期\t折算日/幣別\t外幣金額/消費地\t臺幣金額
        for tm in re.finditer(
            r"(\d{2,3}/\d{1,2}/\d{1,2})\s*\t\s*([^\t\n]+?)\s*\t\s*([^\t\n]*?)\s*\t\s*([^\t\n]*?)\s*\t\s*([^\t\n]*?)\s*\t\s*(-?[\d,]+(?:\.\d+)?)",
            pending_text,
        ):
            consume_date = _roc_to_west(tm.group(1))
            desc = tm.group(2).strip()
            amount_str = tm.group(6).replace(",", "")
            try:
                amount = float(amount_str)
            except ValueError:
                continue
            if any(k in desc for k in ["應繳", "最低", "調整"]):
                continue
            pending_txns.append({
                "date": consume_date,
                "desc": desc,
                "amount": amount,
                "currency": "TWD",
            })
    out["pending_txns"] = pending_txns
    # 只有 URL 確認在 CCCQU004 且內容是明確空狀態或未出帳交易表，才算可信。
    # 任意非空文字可能是舊頁／錯誤頁，不能拿來清空 pending。
    pending_url = str(data.get("pending_page_url") or "").lower()
    pending_lower = pending_text.lower()
    error_markers = (
        "系統錯誤", "系統忙碌", "請稍後再試", "連線逾時", "連線已逾時", "請重新登入", "登入失效",
        "system error", "try again later", "session expired", "login required", "timed out",
        "timeout", "log in again", "login again", "unexpected error",
    )
    pending_error = any(marker in pending_lower for marker in error_markers)
    explicit_empty = "查無相關資料" in pending_text
    transaction_table = all(k in pending_text for k in ("消費日期", "消費說明", "臺幣金額"))
    out["pending_page_ok"] = (not pending_error
                              and data.get("pending_click_ok") is True
                              and "/cccqu004/" in pending_url
                              and (explicit_empty or transaction_table))

    # === E. points (好多金) ===
    points: dict = {}
    if billed_text:
        # 「好多金\t430\t0\t0\t0\t430\t430\t116/03/31」
        m = re.search(
            r"(好多金|紅利點數|哩程)\s*\t\s*([\d,]+)\s*\t\s*([\d,]+)\s*\t\s*([\d,]+)\s*\t\s*([\d,]+)\s*\t\s*([\d,]+)\s*\t\s*([\d,]+)\s*\t\s*(\d{2,3}/\d{1,2}/\d{1,2})",
            billed_text,
        )
        if m:
            points[m.group(1)] = {
                "previous": int(m.group(2).replace(",", "")),
                "used": int(m.group(3).replace(",", "")),
                "earned": int(m.group(4).replace(",", "")),
                "adjustment": int(m.group(5).replace(",", "")),
                "balance": int(m.group(6).replace(",", "")),
                "expiring": int(m.group(7).replace(",", "")),
                "expiry_date": _roc_to_west(m.group(8)),
            }
    out["points"] = points

    return out

def _norm_west_date(s: str | None) -> str | None:
    """西元 YYYY/M/D → YYYY-MM-DD（補零）。"""
    if not s:
        return None
    try:
        y, mo, d = s.replace("-", "/").strip().split("/")
        return f"{int(y):04d}-{int(mo):02d}-{int(d):02d}"
    except Exception:
        return s.replace("/", "-")


def persist_fubon(data: dict, store: BankStore, rules: list[dict] | None = None) -> dict:
    """台北富邦 collect → store 入庫。

    映射：
      collect 三段 frame text → _parse_fubon_credit_card → 4 表
        cards (我的信用卡頁)
        card_billed_txns (帳單明細查詢)
        card_pending_txns (未出帳單消費明細, scope='realtime')
        daily_metrics × 4 (limits/billing_summary/points/endpoints)
    """
    today = datetime.now().strftime("%Y-%m-%d")
    delta: dict = {"bank": "fubon", "scope": "structured"}

    # Telemetry 2026-06-18: collector 抓不到主帳戶, 先 dump deposit-related menu
    # candidates 進 sync_jobs.result_summary 看 cloud 真實 menu 字眼,
    # 規劃下一步加哪些 target 走存款 path.
    deposit_audit = data.get("deposit_menu_audit") or []
    delta["telemetry"] = {
        "deposit_menu_audit_count": len(deposit_audit),
        "deposit_menu_audit_sample": deposit_audit[:20],  # 上限避免 row 爆
        "cards_page_text_len": len(data.get("cards_page_text") or ""),
        "amount_page_text_len": len(data.get("amount_page_text") or ""),
        "deposit_page_text_len": len(data.get("deposit_page_text") or ""),
        "deposit_page_url": data.get("deposit_page_url"),
        "initial_url": data.get("initial_url"),
        "final_url": data.get("final_url"),
    }

    # Step 7 (2026-06-18): parse 我的存款 (CBOQU003_Home.faces) 帳戶 row → accounts.
    # 之前 collector 完全沒走 deposit path, accounts:0 是 by-design gap.
    # 修法 commit 後 collect Step 7 dump deposit_page_text, 這裡 parse 後 upsert.
    deposit_accounts = _parse_fubon_deposit_accounts(data.get("deposit_page_text") or "")
    if deposit_accounts:
        # account_classify 補 product_type (跟 cathay 同 pattern)
        from backend.core import account_classify
        for a in deposit_accounts:
            a["product_type"] = account_classify.classify_account("fubon", a)
        store.upsert_accounts(deposit_accounts)
    delta["accounts_new"] = len(deposit_accounts)

    parsed = _parse_fubon_credit_card(data)

    cc_new = 0
    cc_pending_n = 0

    # cards 入庫
    # Step 2 (2026-06-14): 富邦 limits/billing_summary 是整戶層 (正卡人信用額度共用),
    # 套到每張卡的 step 2 四欄 (3 張卡都用 80,000 額度 + 同一張帳單 due/結帳日).
    # parsed 已把 limits/billing_summary 解出來, 直接從 amount_page_text 拿.
    limits_for_card = parsed.get("limits") or {}
    summary_for_card = parsed.get("billing_summary") or {}
    fubon_credit_limit = _num_to_float(limits_for_card.get("credit_limit"))
    # used = credit_limit - available_credit (整戶層已用額度)
    avail = _num_to_float(limits_for_card.get("available_credit"))
    fubon_used = (fubon_credit_limit - avail) if (fubon_credit_limit is not None and avail is not None) else None
    # billing_summary.bill_date 已是 '2026-05-16' ISO (parsed _norm_west_date 轉過)
    fubon_stmt = summary_for_card.get("bill_date") or summary_for_card.get("statement_date")
    # billing_summary.payment_due 可能是 '無需繳款' 字串 → 不存
    pay_due_raw = summary_for_card.get("payment_due") or ""
    fubon_due = pay_due_raw if (pay_due_raw and pay_due_raw[0].isdigit()) else None
    # 2026-06-22 (audit findings): billing_summary 已 parse 出 total_due (本期應繳)、
    # paid_amount (上期已繳金額)、last_payment_date 三欄,只是沒寫到 cards 表. 補上.
    # 整戶層 by-design (富邦正卡人 + 附卡共用帳單), 跟 ubot/sinopac/cathay 同 pattern.
    fubon_bill_due = _num_to_float(summary_for_card.get("total_due"))
    fubon_last_pay_amt = _num_to_float(summary_for_card.get("paid_amount"))
    fubon_last_pay_date = summary_for_card.get("last_payment_date")
    # Sentinel: 若 last_payment_date 沒抓到 (regex 沒命中) → 兩欄都 None
    if fubon_last_pay_date is None:
        fubon_last_pay_amt = None

    cards = parsed.get("cards") or []
    for card in cards:
        card["credit_limit"] = fubon_credit_limit
        card["used_credit"] = fubon_used
        card["statement_close_date"] = fubon_stmt
        card["payment_due_date"] = fubon_due
        card["bill_due_amount"] = fubon_bill_due
        card["last_payment_amount"] = fubon_last_pay_amt
        card["last_payment_date"] = fubon_last_pay_date
    if cards:
        store.upsert_cards(cards)

    # bill_date 統一從 billing_summary 取
    summary = parsed.get("billing_summary") or {}
    bill_date = summary.get("bill_date") or summary.get("statement_date") or today

    # billed_txns 入庫 (店家 → 對齊 store API)
    billed_payload = []
    # 富邦帳單明細沒帶卡號（一張帳單含多張卡;使用者 fubon 開戶設定為合併出帳, by-design).
    # billed_page_text 證實: 整戶 raw 結構只有「前期應繳/扣繳/本期應繳/註腳」5 條 row, 無 per-card 欄位.
    # 若使用者未來改設「分卡出帳」, raw 才會有 per-card hint; 目前 len(cards)==1 才填 card_no 是正確保守策略.
    default_card_no = None
    if len(cards) == 1:
        default_card_no = cards[0].get("number")  # 單卡時直接掛上
    for b in parsed.get("billed_txns") or []:
        desc = b.get("desc")
        amt = b.get("amount")
        billed_payload.append({
            "card_no": default_card_no,  # 多卡情境後續優化
            "bill_date": bill_date,
            "date": b.get("date"),
            "post_date": b.get("post_date"),
            "desc": desc,
            "amount": amt,
            "currency": b.get("currency") or "TWD",
            "txn_type": classify.classify_by_desc_and_sign(desc, amt),
        })
    if billed_payload:
        cc_new = store.upsert_card_billed(billed_payload, rules=rules) or 0

    # pending_txns 入庫 (scope='realtime')
    pending_payload = []
    for p in parsed.get("pending_txns") or []:
        desc = p.get("desc")
        amt = p.get("amount")
        pending_payload.append({
            "card_no": default_card_no,
            "date": p.get("date"),
            "desc": desc,
            "amount": amt,
            "currency": p.get("currency") or "TWD",
            "txn_type": classify.classify_by_desc_and_sign(desc, amt),
        })
    # fetch_ok: 未出帳頁真的抓到文字才算可信 (含「查無相關資料」= 確實沒有未出帳)。
    # 頁面沒抓到 (登入失敗/timeout) 時 pending_payload 空是「假消失」, 不可比對。
    # 必須無條件 call: 空 payload 時仍要 refresh 才能 sweep 掉已入帳的殘留 row。
    cc_pending_n = store.refresh_card_pending(
        "realtime", pending_payload, rules=rules,
        fetch_ok=bool(parsed.get("pending_page_ok")))

    # 富邦存款交易明細（CDSQU001）— collector 對每個帳戶送近 1 個月查詢。
    twd_txn_rows = _parse_fubon_deposit_txn_results(data.get("deposit_txn_results") or [])
    twd_new = store.upsert_twd_txns(twd_txn_rows, rules=rules) if twd_txn_rows else 0

    # daily_metrics 多段
    limits = parsed.get("limits") or {}
    if limits:
        store.put_daily_metric("fubon_card_limits", limits, today)
    if summary:
        store.put_daily_metric("fubon_card_billing_summary", summary, today)
    points = parsed.get("points") or {}
    if points:
        store.put_daily_metric("fubon_card_points", points, today)

    # endpoints dump
    eps = data.get("_all_endpoints") or []
    if eps:
        store.put_daily_metric("fubon_endpoints", {"endpoints": eps}, today)

    delta["balance_days"] = 0
    delta["twd_txn_new"] = twd_new
    delta["card_billed_new"] = cc_new
    delta["card_unbilled"] = 0
    delta["card_current"] = cc_pending_n
    store.log_sync(delta)
    return delta
