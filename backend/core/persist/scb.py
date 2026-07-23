"""渣打台灣 (SCB) persist 邏輯.

從 persist.py 拆分而來 (2026-06-17, W persist.py split refactor)。
"""
from __future__ import annotations

from datetime import datetime

from backend.core import classify
from backend.core.store import BankStore
from backend.core.persist._common import _ubot_date


def _scb_due_to_stmt(due_iso: str | None) -> str | None:
    """SCB '最近一期繳款截止日' (YYYY-MM-DD) → 推算結帳日 (due - 約 25 天).

    SCB 信用卡帳單通常結帳日 → 繳款日約 25 天。
    e.g. due='2025-09-26' → stmt='2025-09-01' (近似)

    精確結帳日 SCB card_text 沒給, 這是 best-effort 估算。
    None / 解析失敗 → None.
    """
    if not due_iso:
        return None
    try:
        due = datetime.strptime(due_iso, "%Y-%m-%d")
        from datetime import timedelta
        stmt = due - timedelta(days=25)
        return stmt.strftime("%Y-%m-%d")
    except (ValueError, TypeError):
        return None

def persist_scb(data: dict, store: BankStore, rules: list[dict] | None = None) -> dict:
    """渣打 SCB → store 入庫。

    映射：
      api_responses['crditAcctList'].body.sharedCard.sharedCards[] → cards (UPSERT)
      api_responses['crditAcctList'].body.sharedCard 額度 → daily_metrics (E2EE 加密)
      api_responses['getMoneyOverview'].body.accounts → daily_metrics (E2EE 加密)
      home_text + card_text 解析 → daily_metrics 帳戶+信用卡 dashboard
      api_responses 全保留 → daily_metrics (endpoint 地圖)

    特點：
    - 金融資料 E2EE 加密（accountAmt/creditLimitAmt 是 hex string）
    - 卡號明碼遮罩 cardNoForDisplay（`9065-XXXX-XXXX-7052`） + 加密 cardNo
    - card_text 含明碼額度（TWD 310,000）+ 自動扣繳資訊（使用者看到的值）
    """
    today = datetime.now().strftime("%Y-%m-%d")
    delta: dict = {"bank": "scb", "scope": "structured"}

    apis = data.get("api_responses") or {}
    cards_n = 0

    # === A. crditAcctList → 信用卡入 cards 表 (合併 card_text per-card due_date) ===
    # 2026-06-15: per-card 信用額度/已用/到期日 完整化 (Step 3 收尾 SCB)
    #   SCB 特殊處: 共用額度 (shared limit) — 兩張卡共用 TWD 310,000
    #   data flow: card_text → shared limit + per-card due_date → 寫進 cards 表
    import re as _re_scb

    # 先解析 card_text 拿共用 limit + per-card due_date
    card_text = data.get("card_text") or ""
    shared_credit_limit: float | None = None
    shared_available: float | None = None
    per_card_due: dict[str, str | None] = {}  # {last4: 'YYYY-MM-DD'}
    if card_text:
        # 共用 limit (兩張卡都套這個值)
        m = _re_scb.search(r"信用額度\s*\n+\s*TWD\s+([\d,]+(?:\.\d+)?)", card_text)
        if m:
            shared_credit_limit = float(m.group(1).replace(",", ""))
        m = _re_scb.search(r"可用額度\s*\n+\s*TWD\s+([\d,]+(?:\.\d+)?)", card_text)
        if m:
            shared_available = float(m.group(1).replace(",", ""))
        # per-card due_date: 切 by 卡號然後找後段
        parts = _re_scb.split(r"(\d{4}-XXXX-XXXX-\d{4})", card_text)
        for i in range(1, len(parts), 2):
            display_no = parts[i]  # '9065-XXXX-XXXX-7052'
            last4 = display_no.split("-")[-1]
            after = parts[i + 1] if i + 1 < len(parts) else ""
            due_m = _re_scb.search(
                r"最近一期繳款截止日\s*\n+\s*(\d{4}/\d{2}/\d{2})", after[:300])
            per_card_due[last4] = _ubot_date(due_m.group(1)) if due_m else None

    # used_credit = shared limit - shared available (兩張卡共用)
    shared_used: float | None = None
    if shared_credit_limit is not None and shared_available is not None:
        shared_used = shared_credit_limit - shared_available

    cc_hits = apis.get("crditAcctList") or []
    if cc_hits:
        cc_resp = cc_hits[0].get("resp") or {}
        cc_body = cc_resp.get("body") or {}
        shared = cc_body.get("sharedCard") or {}
        shared_cards = shared.get("sharedCards") or []

        cards_payload = []
        for c in shared_cards:
            if not isinstance(c, dict):
                continue
            display_no = c.get("cardNoForDisplay") or ""  # "9065-XXXX-XXXX-7052"
            last4 = display_no.split("-")[-1] if "-" in display_no else display_no[-4:]
            cards_payload.append({
                "number": f"****{last4}" if last4 else display_no,
                "card_no_full_masked": display_no,
                "name": c.get("cardTypeName"),
                "brand": None,
                "primary": "正卡" if c.get("primarycard") else "附卡",
                "status": "有效" if c.get("open") else "已停用",
                # Step 3 (SCB): per-card 信用卡 4 欄
                # SCB 兩張卡共用 limit/used — 同值寫入兩張卡
                # statement_close_date: SCB card_text 沒給結帳日 (只給繳款截止日) → 用 due-30
                # payment_due_date: per-card 從 card_text 抽 ("最近一期繳款截止日")
                "credit_limit": shared_credit_limit,
                "used_credit": shared_used,
                "statement_close_date": _scb_due_to_stmt(per_card_due.get(last4)),
                "payment_due_date": per_card_due.get(last4),
                "active": bool(c.get("open")),
            })
        if cards_payload:
            store.upsert_cards(cards_payload)
            cards_n = len(cards_payload)

        # 額度/帳戶資料（E2EE 加密 + 明碼遮罩混合）
        store.put_daily_metric("scb_card_shared_limits", {
            "n_cards": len(shared_cards),
            "credit_limit_encrypted": shared.get("creditLimitAmt"),
            "available_limit_encrypted": shared.get("availiableLimitAmt"),  # 拼字保留 API 原樣
            "cash_advance_amt_encrypted": shared.get("cashAdvanceAmt"),
            "cash_advance_available_encrypted": shared.get("availableCashAdvanceAmt"),
            "single_cards_present": cc_body.get("singleCards") is not None,
            "encrypted": True,
        }, today)

    # === B. card_text 解析：明碼額度 + 卡片繳款資訊 (保留 daily_metric snapshot) ===
    if card_text:
        cd: dict = {}
        if shared_credit_limit is not None:
            cd["credit_limit"] = shared_credit_limit
        if shared_available is not None:
            cd["available_credit"] = shared_available
        m = _re_scb.search(r"預借現金額度\s*\n+\s*TWD\s+([\d,]+(?:\.\d+)?)", card_text)
        if m:
            cd["cash_advance_limit"] = float(m.group(1).replace(",", ""))
        m = _re_scb.search(r"可用預借現金餘額\s*\n+\s*TWD\s+([\d,]+(?:\.\d+)?)", card_text)
        if m:
            cd["cash_advance_available"] = float(m.group(1).replace(",", ""))
        cd["currency"] = "TWD"
        if per_card_due:
            cd["per_card_due"] = per_card_due
        if cd:
            store.put_daily_metric("scb_card_text_summary", cd, today)

    # === C. getMoneyOverview → dashboard 統計 ===
    mo_hits = apis.get("getMoneyOverview") or []
    if mo_hits:
        mo_resp = mo_hits[0].get("resp") or {}
        body = mo_resp.get("body") or {}
        store.put_daily_metric("scb_money_overview_meta", {
            "n_accounts": len(body.get("accounts") or []),
            "has_credit_cards": body.get("creditCards") is not None,
            "has_investments": body.get("investments") is not None,
            "has_loans": body.get("loans") is not None,
            "n_month_assets": len(body.get("monthAssets") or []),
            "trust_account_flag": body.get("trustAccountFlag"),
            "totalAccountAmtCurrency": body.get("totalAccountAmtCurrency"),
            "encrypted": True,
        }, today)
        store.put_daily_metric("scb_money_overview_raw", {
            "body_encrypted": body,
            "header_code": mo_resp.get("header", {}).get("code"),
        }, today)

    # === D. 從 home_text 解析 dashboard 顯示值 ===
    home_text = data.get("home_text") or ""
    if home_text:
        import re
        dashboard: dict = {}

        def _extract_after(label: str):
            m = re.search(rf"{re.escape(label)}\s*\n+\s*TWD\s+([\d,]+)", home_text)
            if m:
                try:
                    return int(m.group(1).replace(",", ""))
                except ValueError:
                    return m.group(1)
            return None

        for label, key in [
            ("您目前總資產", "total_assets"),
            ("您目前貸款尚欠總金額", "total_debts"),
            ("台幣活存總資產", "twd_demand"),
            ("台幣定存總資產", "twd_term"),
            ("您目前的投資市值", "investment_mv"),
        ]:
            val = _extract_after(label)
            if val is not None:
                dashboard[key] = val
        for label, key in [
            ("外幣活存總資產", "fx_demand_approx_twd"),
            ("外幣定存總資產", "fx_term_approx_twd"),
        ]:
            m = re.search(rf"{re.escape(label)}\s*\n+\s*約等值\s*TWD\s+([\d,]+)", home_text)
            if m:
                try:
                    dashboard[key] = int(m.group(1).replace(",", ""))
                except ValueError:
                    dashboard[key] = m.group(1)
        if "您目前沒有任何保險" in home_text:
            dashboard["insurance_status"] = "none"
        m = re.search(r"您的專屬理財專員[::\s]+([^\s]+)\s+理財專線請撥打\s+(\+?[\d\s\-]+)", home_text)
        if m:
            dashboard["rm_name"] = m.group(1)
            dashboard["rm_phone"] = m.group(2).strip()
        if dashboard:
            store.put_daily_metric("scb_dashboard_text", dashboard, today)

    # === D2. consumptionDetail → 信用卡逐筆消費 (2026-06-13 升級) ===
    # SCB API: /mobilebank/rest/creditcard/consumptionDetail
    # 回應 NF_000021 = 查無歷史帳單（使用者 SCB 一年沒消費 by-design）
    # 真實 transaction body schema 未實證；保留通用 key 嘗試
    cd_hits = apis.get("consumptionDetail") or []
    cd_meta = {"requests": len(cd_hits), "results": []}
    billed_n = 0
    if cd_hits:
        billed_payload: list[dict] = []
        for hit in cd_hits:
            resp = hit.get("resp") or {}
            header = resp.get("header") or {}
            body = resp.get("body") or {}
            req_body_for_card = (hit.get("req_body") or {}).get("body") or {}
            cd_meta["results"].append({
                "code": header.get("code"),
                "message": (header.get("message") or "")[:80],
                "card_no_encrypted": (req_body_for_card.get("cardNo") or "")[:32],
                "start_date": req_body_for_card.get("startDate"),
                "end_date": req_body_for_card.get("endDate"),
                "txn_count": len(body.get("transactionList") or body.get("transactions") or []),
            })
            txns = body.get("transactionList") or body.get("transactions") or body.get("details") or []
            for t in txns:
                if not isinstance(t, dict):
                    continue
                date = t.get("transactionDate") or t.get("postingDate") or t.get("txnDate")
                desc = t.get("merchantName") or t.get("description") or t.get("merchantDesc")
                twd_amount = t.get("localAmount") or t.get("twdAmount") or t.get("amount") or 0
                fx_currency = t.get("currency") or t.get("originalCurrency") or "TWD"
                fx_amount = t.get("originalAmount") or t.get("foreignAmount")
                card_no = t.get("cardNo") or t.get("cardNumber") or ""
                card_last4 = card_no[-4:] if card_no else ""
                try:
                    twd_int = int(float(str(twd_amount).replace(",", "")))
                except (ValueError, TypeError):
                    twd_int = 0
                try:
                    fx_float = float(str(fx_amount).replace(",", "")) if fx_amount else None
                except (ValueError, TypeError):
                    fx_float = None
                billed_payload.append({
                    "card_no": f"****{card_last4}" if card_last4 else None,
                    "date": date,
                    "desc": desc or "",
                    "amount": twd_int,
                    "currency": "TWD",
                    "consume_currency": fx_currency if fx_currency != "TWD" else None,
                    "consume_amount": fx_float,
                    "txn_type": classify.classify_by_desc_and_sign(desc or "", twd_int),
                })
        if billed_payload:
            billed_n = store.upsert_card_billed(billed_payload, rules=rules) or 0
    store.put_daily_metric("scb_consumption_detail_meta", cd_meta, today)

    # === E. endpoint 地圖 dump ===
    eps = data.get("_all_endpoints") or []
    if eps:
        store.put_daily_metric("scb_endpoints", {"endpoints": eps}, today)

    delta["balance_days"] = 0
    delta["twd_txn_new"] = 0
    delta["card_billed_new"] = billed_n
    delta["card_unbilled"] = 0
    delta["card_current"] = 0
    delta["cards_n"] = cards_n
    store.log_sync(delta)
    return delta
