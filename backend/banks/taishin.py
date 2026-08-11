#!/usr/bin/env python3
"""Taishin Bank personal e-banking crawler (my.taishinbank.com.tw).

台新銀行 my.taishinbank.com.tw 個人網銀抓取器。

登入入口：https://my.taishinbank.com.tw/TIBNetBank/（RWD SPA in iframe）
流程：開頁 → 找 iframe `main` (src 含 `svc/rwd/index.html`) → 4 欄全 type=password
      → 用 placeholder 精準匹配「身分證字號/使用者代號/使用者密碼/驗證碼」→ OCR captcha → 點 #loginBtn

⚠️ 鐵律（見 wiki/concepts/taiwan-bank-login-retry-account-lockout-lesson.md）：
   login 失敗**絕不自動重打**——max_attempts=1。
   OCR 階段（送出前）可換圖重試最多 5 次（安全）。
"""
from __future__ import annotations

import base64
import contextlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from backend.core.base import BankCollectResult, BankCrawler, ResponseCollector
from backend.core.card_bills import (
    card_bill_date,
    card_bill_money,
    make_card_bill_fact,
    publish_card_bill_facts,
)
from backend.core.creds import TaishinCreds
from backend.core.captcha import ocr_bytes

BASE = "https://my.taishinbank.com.tw/TIBNetBank/"
IFRAME_HINT = "svc/rwd/index.html"
LOGIN_BTN_ID = "loginBtn"

# 4 欄全 type=password，靠 placeholder 精準匹配
FIELD_PLACEHOLDERS = {
    "national_id": "身分證字號",
    "user_code":   "使用者代號",
    "password":    "使用者密碼",
    "captcha":     "驗證碼",
}


def _log(*a):
    print(*a, file=sys.stderr)


def _ph_sel(ph: str) -> str:
    return f"input[placeholder='{ph}']"


class TaishinLoginError(RuntimeError):
    pass


def _taishin_card_bill_fact(parsed: dict):
    if not isinstance(parsed, dict) or parsed.get("fetch_ok") is not True:
        return None
    summary = parsed.get("summary")
    period = parsed.get("billing_period")
    payment_candidates = []
    for row in parsed.get("billed_txns") or []:
        if not isinstance(row, dict) or isinstance(row.get("amount"), bool):
            continue
        raw_amount = row.get("amount")
        if raw_amount is None:
            continue
        try:
            amount = float(raw_amount)
        except (TypeError, ValueError):
            continue
        payment_date = card_bill_date(row.get("post_date") or row.get("txn_date"))
        payment_amount = card_bill_money(abs(amount))
        if amount < 0 and payment_date and payment_amount is not None:
            payment_candidates.append((payment_date, payment_amount))
    payment_date, payment_amount = max(payment_candidates, default=(None, None))
    return make_card_bill_fact(
        remaining_due=summary.get("remaining") if isinstance(summary, dict) else None,
        statement_close_date=period.get("statement_date") if isinstance(period, dict) else None,
        payment_due_date=period.get("pay_due_date") if isinstance(period, dict) else None,
        last_payment_amount=payment_amount,
        last_payment_date=payment_date,
    )


class TaishinCrawler(BankCrawler):
    def __init__(self):
        super().__init__(name="taishin")
        self.creds = TaishinCreds.load()

    def _host_filter(self) -> str:
        return "taishinbank.com"

    def _find_login_frame(self, page):
        for f in page.frames:
            if IFRAME_HINT in (f.url or ""):
                return f
        return None

    def _logged_in(self, page) -> bool:
        """W (2026-06-17): positive signal 4 條件 AND（iframe + 30s wait，對齊 SCSB 鐵律）

        Taishin SPA in iframe + 登入後常有 popup，須保留 30s 等待窗口。

        1) urlOk: my.taishinbank.com.tw（login-after main domain）
        2) noLoginForm: svc/rwd/index.html login iframe 已消失
        3) lenOk: main page + iframe innerText 合計 >= 500
        4) kw >= 2: 內銀區關鍵字命中 ≥ 2 個

        任一 fail → 視為未登入。每 5s retry 一次，最多 6 輪 (30s)。
        """
        import re
        keywords = ["我知道了", "系統斷信", "3個月後提醒", "前往修改", "訊息通知",
                    "帳戶總覽", "我的資產", "台幣存款", "我的帳戶", "信用卡管理",
                    "網銀首頁", "資產總額", "存款餘額",
                    # 虛擬鍵盤關鍵字（使用者被要求改密碼時會出現）
                    "大陸身份", "外國身份", "清除", "虛擬鍵盤"]
        keyword_regex = "|".join(keywords)

        url = (page.url or "").lower()
        if "taishinbank.com" not in url:
            return False

        for wait_round in range(6):  # 6 * 5 = 30 秒
            # noLoginForm: iframe 消失
            try:
                if self._find_login_frame(page) is not None:
                    page.wait_for_timeout(5000)
                    continue
            except Exception:
                page.wait_for_timeout(5000)
                continue

            # lenOk + kw
            texts = []
            for f in [page, *list(page.frames)]:
                try:
                    txt = f.evaluate("() => document.body && document.body.innerText || ''")
                    if txt:
                        texts.append(txt)
                except Exception:
                    pass
            joined = "\n".join(texts)
            if len(joined) >= 500:
                kws_found = re.findall(keyword_regex, joined)
                if len(set(kws_found)) >= 2:
                    _log(
                        f"[taishin][logged_in] 4 條件命中 (round {wait_round+1}, "
                        f"len={len(joined)}, kws={set(kws_found)})",
                    )
                    return True
            page.wait_for_timeout(5000)
        return False

    def _close_popups(self, page):
        """關所有登入後 popup — 強化版 v4 (2026-06-11)。

        策略改為「**強制 hide modal**」而非靠各種按鈕匹配：
          (1) 用 JS 找所有 visible modal / dialog / overlay → 強行 `display:none` + remove
          (2) 用 Esc key 試踢
          (3) 最後再點一輪「我知道了/關閉」等按鈕做備援

        強制 deadline 25 秒（避免 collect 卡死），整個 method 超時就 break。
        """
        import time as _t
        deadline = _t.monotonic() + 25.0
        total_closed = 0

        # ── Step 1: JS 強行 hide modal ──
        # 注意：絕不誤殺 top nav！只 hide y > 200 且占畫面 ≥30% 的 modal-class 元素
        try:
            hidden = page.evaluate("""() => {
                const candidates = document.querySelectorAll(
                    '.modal, [role="dialog"], [class*="modal"], [class*="popup"], [class*="dialog"], ' +
                    '[class*="overlay"], [class*="mask"], [id*="modal"], [id*="popup"]'
                );
                let hidden = 0;
                const vw = window.innerWidth;
                const vh = window.innerHeight;
                for (const el of candidates) {
                    const cs = window.getComputedStyle(el);
                    if (cs.display === 'none' || cs.visibility === 'hidden') continue;
                    const r = el.getBoundingClientRect();
                    if (r.width < 50 || r.height < 50) continue;
                    // 保護 top nav 區域（y < 200 不動）
                    if (r.y < 200 && r.height < 100) continue;
                    // 保護畫面右上角小元件（< 30% viewport 寬高 + 在頂部）
                    if (r.y < 200 && r.width < vw * 0.4) continue;
                    el.style.setProperty('display', 'none', 'important');
                    el.style.setProperty('visibility', 'hidden', 'important');
                    hidden += 1;
                }
                document.body.classList.remove('modal-open');
                document.body.style.removeProperty('overflow');
                document.body.style.removeProperty('padding-right');
                document.querySelectorAll('.modal-backdrop, [class*="backdrop"], [class*="overlay-mask"]').forEach(b => b.remove());
                return hidden;
            }""")
            if hidden:
                _log(f"[taishin][popup] JS 強制 hide {hidden} 個 modal/overlay")
                total_closed += hidden
        except Exception as e:
            _log(f"[taishin][popup] JS hide modal 失敗: {e}")

        # 同樣對 iframes 做一次（保護 top nav 同樣邏輯）
        for f in page.frames:
            if f == page.main_frame:
                continue
            if _t.monotonic() > deadline:
                _log("[taishin][popup] deadline 到，frames 略過")
                break
            try:
                hidden = f.evaluate("""() => {
                    const candidates = document.querySelectorAll(
                        '.modal, [role="dialog"], [class*="modal"], [class*="popup"], [class*="dialog"], ' +
                        '[class*="overlay"], [class*="mask"], [id*="modal"], [id*="popup"]'
                    );
                    let hidden = 0;
                    const vw = window.innerWidth;
                    for (const el of candidates) {
                        const cs = window.getComputedStyle(el);
                        if (cs.display === 'none' || cs.visibility === 'hidden') continue;
                        const r = el.getBoundingClientRect();
                        if (r.width < 50 || r.height < 50) continue;
                        // 保護 top nav 區域
                        if (r.y < 200 && r.height < 100) continue;
                        if (r.y < 200 && r.width < vw * 0.4) continue;
                        el.style.setProperty('display', 'none', 'important');
                        hidden += 1;
                    }
                    return hidden;
                }""")
                if hidden:
                    _log(f"[taishin][popup] frame ({f.url[:60]}) hide {hidden} 個 modal")
                    total_closed += hidden
            except Exception:
                pass

        # ── Step 2: Esc 試踢（很多 modal 會響應）──
        if _t.monotonic() < deadline:
            try:
                for _ in range(3):
                    page.keyboard.press("Escape")
                    page.wait_for_timeout(300)
            except Exception:
                pass

        # ── Step 3: 點「我知道了/關閉」備援（單次，不再 multi-round）──
        if _t.monotonic() < deadline:
            button_texts = ["我知道了", "關閉", "稍後", "3個月後提醒", "3個月後再提醒",
                            "暫不", "略過", "不再顯示", "下次再說", "稍後再說", "不再提醒",
                            "I Know", "Close", "OK", "確定"]
            for f in [page] + [fr for fr in page.frames if fr != page.main_frame]:
                if _t.monotonic() > deadline:
                    break
                kind = "page" if f == page else "frame"
                try:
                    clicked = f.evaluate("""(texts) => {
                        let n = 0;
                        const all = document.querySelectorAll('button, a, span, div, [role="button"]');
                        for (const el of all) {
                            const t = (el.textContent || el.innerText || '').trim();
                            if (!texts.includes(t)) continue;
                            if (el.offsetParent === null) continue;
                            const r = el.getBoundingClientRect();
                            if (r.width < 5 || r.height < 5) continue;
                            if (el.tagName === 'DIV' || el.tagName === 'SPAN') {
                                if (el.querySelector('button, a, [role="button"]')) continue;
                            }
                            try { el.click(); n += 1; } catch (e) {}
                        }
                        return n;
                    }""", button_texts)
                    if clicked:
                        _log(f"[taishin][popup] {kind}: 額外點掉 {clicked} 個按鈕")
                        total_closed += clicked
                except Exception:
                    pass

        if total_closed:
            _log(f"[taishin][popup] 累計關/隱藏 {total_closed} 個 popup")
        elapsed = 25.0 - max(0, deadline - _t.monotonic())
        _log(f"[taishin][popup] _close_popups 耗時 {elapsed:.1f}s")
        return total_closed

    def _try_ancestor_clicks(self, target_frame, page, debug_dir) -> bool:
        """台新信用卡 mega menu hover 策略 v2 (2026-06-11 真實 mouse)。

        實測揭示：信用卡 menu 是 `<li class="_nav_menu__item">` hover 觸發 mega menu。
        JS dispatch hover 無效（Vue/React 認得真實 mouse event），必須用 page.mouse.move
        真實 hover 到 LI 中心點 → 等 mega menu 展開 → 直接 click 子項。

        子項目錄（vision 揭示）：
          【帳單/查詢】 信用卡總覽、權益概覽、**查詢信用卡明細**、附卡人消費查詢、信用卡消費分析
          【點數/里數】 查詢與兌換點數
          【額度/分期理財】 查詢分期訂單、單筆消費分期、調升信用卡額度
          【預借現金】 申請預借現金
          【繳費】 繳信用卡費
        """
        # Step 1: 找 LI bbox（page coordinate，需加 iframe offset）
        try:
            li_bbox = target_frame.evaluate("""() => {
                for (const sp of document.querySelectorAll('span._nav_menu__text')) {
                    if ((sp.textContent || '').trim() !== '信用卡') continue;
                    if (sp.offsetParent === null) continue;
                    // d2: SPAN → STRONG → LI
                    const li = sp.parentElement.parentElement;
                    if (!li || li.tagName !== 'LI') return null;
                    const r = li.getBoundingClientRect();
                    return {x: r.x, y: r.y, w: r.width, h: r.height};
                }
                return null;
            }""")
        except Exception as e:
            _log(f"[taishin][collect] 取 LI bbox 例外: {e}")
            return False

        if not li_bbox:
            _log("[taishin][collect] ❌ 找不到信用卡 LI")
            return False

        _log(f"[taishin][collect] 信用卡 LI bbox @({int(li_bbox['x'])},{int(li_bbox['y'])}) {int(li_bbox['w'])}x{int(li_bbox['h'])}")

        # Step 2: 找 iframe 在主 page 的位置（加 offset）
        iframe_offset_x = 0
        iframe_offset_y = 0
        try:
            for fel in page.query_selector_all("iframe"):
                src = fel.get_attribute("src") or ""
                if "svc/rwd" in src:
                    fbox = fel.bounding_box()
                    if fbox:
                        iframe_offset_x = fbox["x"]
                        iframe_offset_y = fbox["y"]
                        _log(f"[taishin][collect] iframe offset: ({int(iframe_offset_x)},{int(iframe_offset_y)})")
                    break
        except Exception as e:
            _log(f"[taishin][collect] 取 iframe offset 失敗（用 0,0）: {e}")

        # Step 3: 真實 mouse.move 到 LI 中心
        li_center_x = iframe_offset_x + li_bbox["x"] + li_bbox["w"] / 2
        li_center_y = iframe_offset_y + li_bbox["y"] + li_bbox["h"] / 2
        _log(f"[taishin][collect] mouse.move → page coord ({li_center_x:.0f},{li_center_y:.0f})")

        try:
            # 先 move 到主畫面中央（避開所有 nav）→ 再 move 到 LI（保證觸發 mouseenter）
            page.mouse.move(li_center_x, 400)
            page.wait_for_timeout(200)
            page.mouse.move(li_center_x, li_center_y, steps=10)
            page.wait_for_timeout(1500)  # 等 mega menu 展開動畫
            page.screenshot(path=str(debug_dir / "mega_open.png"), full_page=False)
        except Exception as e:
            _log(f"[taishin][collect] mouse.move 例外: {e}")
            return False

        # Step 4: dump mega menu 內可見 link（hover 還在，menu 還展開）
        try:
            mega_links = target_frame.evaluate("""(li_y) => {
                const out = [];
                for (const a of document.querySelectorAll('a, button, li')) {
                    if (a.offsetParent === null) continue;
                    const r = a.getBoundingClientRect();
                    if (r.width < 5 || r.height < 5) continue;
                    if (r.y < li_y + 30) continue;  // 必須在 LI 下方
                    if (r.y > li_y + 700) continue;  // mega menu 範圍 (~700px 高)
                    const t = (a.textContent || '').trim();
                    if (!t || t.length > 25) continue;
                    if (a.tagName === 'LI' && a.querySelector('a, button')) continue;
                    out.push({tag: a.tagName, text: t, y: r.y, x: r.x, w: r.width, h: r.height});
                }
                // dedupe by (text, ~y)
                const seen = new Set();
                return out.filter(o => {
                    const k = `${o.text}_${Math.round(o.y/3)}`;
                    if (seen.has(k)) return false;
                    seen.add(k);
                    return true;
                });
            }""", li_bbox["y"])
        except Exception as e:
            _log(f"[taishin][collect] dump mega links 失敗: {e}")
            mega_links = []

        _log(f"[taishin][collect] mega menu 內 {len(mega_links)} 個可見 link:")
        for l in mega_links[:30]:
            _log(f"  {l['tag']}@({int(l['x'])},{int(l['y'])}) {l['text']!r}")

        # 判斷 mega menu 是否真的開了（找信用卡相關字樣）
        card_indicators = ["信用卡", "帳單", "消費明細", "紅利", "預借現金", "繳信"]
        opened = sum(1 for l in mega_links if any(k in l["text"] for k in card_indicators))
        _log(f"[taishin][collect] mega menu 含信用卡關鍵字 link 數 = {opened}")

        if opened == 0:
            _log("[taishin][collect] ❌ mega menu 未展開（無信用卡關鍵字 link）")
            return False

        # Step 5: 找優先目標並 click
        priority_targets = [
            "查詢信用卡明細",
            "信用卡消費分析",
            "附卡人消費查詢",
            "信用卡總覽",
        ]
        target_link = None
        for pri in priority_targets:
            for l in mega_links:
                if l["text"] == pri:
                    target_link = l
                    break
            if target_link is None:
                for l in mega_links:
                    if pri in l["text"]:
                        target_link = l
                        break
            if target_link:
                break

        if not target_link:
            _log("[taishin][collect] ❌ mega menu 內找不到優先目標")
            return False

        _log(f"[taishin][collect] 點 mega menu 子項: {target_link['text']!r} @({int(target_link['x'])},{int(target_link['y'])})")
        # 真實 mouse.move 到子項 → click（保 hover 同時 click）
        try:
            click_x = iframe_offset_x + target_link["x"] + target_link["w"] / 2
            click_y = iframe_offset_y + target_link["y"] + target_link["h"] / 2
            page.mouse.move(click_x, click_y, steps=5)
            page.wait_for_timeout(300)
            page.mouse.click(click_x, click_y)
            _log(f"[taishin][collect] page.mouse.click({click_x:.0f},{click_y:.0f})")
            page.wait_for_timeout(6000)
            with contextlib.suppress(Exception):
                page.screenshot(path=str(debug_dir / "card_detail.png"), full_page=False)
            _log(f"[taishin][collect] click 後 url={page.url[:100]}")
            return True
        except Exception as e:
            _log(f"[taishin][collect] click 子項例外: {e}")
            return False

    def _parse_credit_card_page(self, text: str) -> dict:
        """從「查詢信用卡明細」frame text (RB0708/0100) 抽結構化資料。

        2026-06-11 sample frame text 包含以下 5 段：
          [1] 頂部卡片：即時消費紀錄 N 筆 / 未出帳 TWD M / 分期金額 / 筆數
          [2] 當期應繳款項明細：6 欄（前期/新增/帳單/最低/已繳/剩餘）
          [3] 上期實繳金額明細：5-column table（消費日期/入帳起息日/明細/幣別/金額）
          [4] 即時消費紀錄（卡片末四碼）：6-column table（日期/時間/明細/金額/國別/授權）
          [5] 卡名：「Richart卡(原FlyGo鈦金商務) (卡號末四碼:1409)」

        Returns:
          {
            cards: [{number, name, ...}],
            pending_txns: [{txn_date, desc, amount, card_no_suffix}],
            billed_txns: [{txn_date, post_date, desc, currency, amount, card_no_suffix}],
            summary: {prev_balance, new_charges, bill_amount, min_pay, paid, remaining, ...},
            top_summary: {pending_count, pending_amount_twd, installment_amount, installment_count},
            billing_period: {start, end, bill_date, pay_due_date, bill_amount, min_pay},
          }
        """
        import re
        error_text = text.lower()
        error_markers = (
            "系統錯誤", "系統忙碌", "請稍後再試", "登入逾時", "連線逾時",
            "連線已逾時", "請重新登入", "登入失效",
            "system error", "try again later", "session expired", "login required",
            "timed out", "timeout", "log in again", "login again", "unexpected error",
        )
        realtime_headers = ("即時消費紀錄", "消費日期", "消費時間", "消費明細", "授權結果")
        out: dict = {
            "fetch_ok": (not any(marker in error_text for marker in error_markers)
                         and all(header in text for header in realtime_headers)),
            "cards": [], "pending_txns": [], "billed_txns": [],
            "summary": {}, "top_summary": {}, "billing_period": {},
        }

        # ─── [5] 卡名 + 末四碼 ───
        # 「Richart卡(原FlyGo鈦金商務) (卡號末四碼:1409)」
        card_match = re.search(r"([^\n]+?)\s*\(卡號末四碼[::]\s*(\d{4})\)", text)
        card_no_suffix = None
        if card_match:
            card_name = card_match.group(1).strip()
            card_no_suffix = card_match.group(2)
            out["cards"].append({
                "number": f"****{card_no_suffix}",
                "name": card_name,
                "currency": "TWD",
            })

        # ─── [1] 頂部卡片摘要 ───
        # latest_n: 「即時消費紀錄\n最新一筆紀錄\nN」(N 是最近一筆 row index，非總數)
        m = re.search(r"即時消費紀錄\s*\n\s*最新一筆紀錄\s*\n\s*(\d+)\s*\n\s*消費日期", text)
        if m:
            out["top_summary"]["latest_n_marker"] = int(m.group(1))
        # unbilled: 「未出帳明細\nTWD\n80」
        m = re.search(r"未出帳明細\s*\n\s*TWD\s*\n\s*([\d,]+)", text)
        if m:
            out["top_summary"]["pending_amount_twd"] = int(m.group(1).replace(",", ""))
        # installment: 「分期交易金額\nN\n分期交易筆數\nM」
        m = re.search(r"分期交易金額\s*\n\s*([\d,]+)\s*\n\s*分期交易筆數\s*\n\s*([\d,]+)", text)
        if m:
            out["top_summary"]["installment_amount"] = int(m.group(1).replace(",", ""))
            out["top_summary"]["installment_count"] = int(m.group(2).replace(",", ""))
        # 即時消費紀錄總筆數: 「共 N 筆資料資料日期：YYYY/MM/DD HH:MM:SS」
        m = re.search(r"共\s*(\d+)\s*筆資料\s*資料日期[:：](\d{4}/\d{1,2}/\d{1,2}\s+\d{1,2}:\d{2}:\d{2})", text)
        if m:
            out["top_summary"]["pending_total_count"] = int(m.group(1))
            out["top_summary"]["snapshot_at"] = m.group(2)

        # ─── [2] 當期應繳款項明細 ───
        # 6 欄：前期餘額、本期新增款項、本期帳單金額、本期最低應繳金額、本期已繳總金額、本期剩餘應繳款項
        for label, key in [
            ("前期餘額", "prev_balance"),
            ("本期新增款項", "new_charges"),
            ("本期帳單金額", "bill_amount"),
            ("本期最低應繳金額", "min_pay"),
            ("本期已繳總金額", "paid"),
            ("本期剩餘應繳款項", "remaining"),
        ]:
            # 格式可能是「label\n\nN\n」或「label\n\n N\n」
            m = re.search(rf"{label}\s*\n+\s*([\d,]+(?:\.\d+)?)\s*\n", text)
            if m:
                out["summary"][key] = float(m.group(1).replace(",", ""))

        # 自動扣款帳號 (sensitive 但 frame text 已 masked 為 00288*****072722)
        m = re.search(r"自動扣款帳號[:：]([\d*]+)", text)
        if m:
            out["summary"]["auto_debit_account_masked"] = m.group(1)

        # ─── [3] 當月帳單期間 ───
        # 「2026/05 信用卡明細」+「入帳期間\n\n2026/4/13 - 2026/5/12」
        m = re.search(r"入帳期間\s*\n+\s*(\d{4}/\d{1,2}/\d{1,2})\s*-\s*(\d{4}/\d{1,2}/\d{1,2})", text)
        if m:
            out["billing_period"]["statement_start"] = m.group(1)
            out["billing_period"]["statement_end"] = m.group(2)
        m = re.search(r"帳單結帳日\s*\n+\s*(\d{4}/\d{1,2}/\d{1,2})", text)
        if m:
            out["billing_period"]["statement_date"] = m.group(1)
        m = re.search(r"繳款截止日\s*\n+\s*(\d{4}/\d{1,2}/\d{1,2})", text)
        if m:
            out["billing_period"]["pay_due_date"] = m.group(1)
        # 本期帳單金額（已在 summary 抓過，這裡再抓一次走 billing_period.bill_amount）
        m = re.search(r"本期帳單金額\s*\n+\s*新臺幣\s*([\d,]+(?:\.\d+)?)", text)
        if m:
            out["billing_period"]["bill_amount"] = float(m.group(1).replace(",", ""))
        m = re.search(r"最低應繳金額\s*\n+\s*新臺幣\s*([\d,]+(?:\.\d+)?)", text)
        if m:
            out["billing_period"]["min_pay"] = float(m.group(1).replace(",", ""))

        # ─── [3] 上期實繳金額明細表（billed / payment section） ───
        # 5-column: 消費日期 / 入帳起息日 / 消費明細 / 約定幣別 / 消費金額
        #
        # 2026-07-03 修正 (0.3.64): 舊 regex 用「消費金額」當 anchor + 「沒有更多資料了」
        # 收尾，但當上期實繳查無資料時 regex 會貪婪抓到下方「當期消費明細」的
        # rows，把當期消費（如捷運 40 元）誤當扣繳紀錄。
        #
        # 修法：改用 section partition。
        #   text 內三個段落順序：上期實繳金額明細 → 當期消費明細 → 當期帳單說明
        # 只 parse「上期實繳金額明細」到「當期消費明細」中間的區塊；
        # 若該區塊包含「查無資料」，直接視為空 → billed_txns 不含此頁 row。
        if "上期實繳金額明細" in text:
            after_paid = text.split("上期實繳金額明細", 1)[1]
            paid_block, _sep, _rest = after_paid.partition("當期消費明細")
            if "查無資料" not in paid_block:
                # 只抓真實 rows，格式：yyyy/MM/dd 開頭
                entries = re.findall(
                    r"(\d{4}/\d{1,2}/\d{1,2})\s*\n\s*\t?\s*\n?\s*"
                    r"(\d{4}/\d{1,2}/\d{1,2})\s*\n\s*\t?\s*\n?\s*"
                    r"([^\n]+?)\s*\n\s*\t?\s*\n?\s*"
                    r"(新臺幣|美金|日圓|歐元|港幣|人民幣)\s*\n\s*\t?\s*\n?\s*"
                    r"(-?[\d,]+(?:\.\d+)?)",
                    paid_block,
                )
                for txn_date, post_date, desc, currency, amount in entries:
                    out["billed_txns"].append({
                        "txn_date": txn_date,
                        "post_date": post_date,
                        "desc": desc.strip(),
                        "currency": currency,
                        "amount": float(amount.replace(",", "")),
                        "card_no_suffix": card_no_suffix,
                    })

        # ─── [4] 即時消費紀錄表（pending） ───
        # 6-column: 消費日期 / 消費時間 / 消費明細 / 新臺幣消費金額 / 消費國別 / 授權結果
        # marker：「依消費日期排序」開始，「沒有更多資料了」結束
        # 但即時消費紀錄段在文件下方：「即時消費紀錄\n返回\nRichart卡(原...) (卡號末四碼:1409)\n下載\n列印\n依消費日期排序...」
        pending_section = re.search(
            r"依消費日期排序\s*\n\s*消費日期.*?授權結果\s*\n(.*?)\n\s*共\s*(\d+)\s*筆資料",
            text, re.DOTALL,
        )
        if pending_section:
            block = pending_section.group(1)
            # 每筆: 2026/05/21\n\t\n18:55:41\n\t\n台北大眾捷運股份有限公司\n\t\n1\n\t\nTW\n\t\n成功
            entries = re.findall(
                r"(\d{4}/\d{1,2}/\d{1,2})\s*\n\s*\t?\s*\n?\s*"
                r"(\d{1,2}:\d{2}:\d{2})\s*\n\s*\t?\s*\n?\s*"
                r"([^\n]+?)\s*\n\s*\t?\s*\n?\s*"
                r"(-?[\d,]+(?:\.\d+)?)\s*\n\s*\t?\s*\n?\s*"
                r"([A-Z]{2,3}|TW|JP|US|HK|CN|KR|SG|MY|TH|VN|UK|DE|FR|IT|ES|AU|NZ|CA)\s*\n\s*\t?\s*\n?\s*"
                r"([^\n]+?)\s*(?:\n|$)",
                block,
            )
            for txn_date, txn_time, desc, amount, country, status in entries:
                out["pending_txns"].append({
                    "txn_date": txn_date,
                    "txn_time": txn_time,
                    "desc": desc.strip(),
                    "amount": float(amount.replace(",", "")),
                    "country": country,
                    "status": status.strip(),
                    "card_no_suffix": card_no_suffix,
                })

        return out

    def _ocr_captcha(self, frame, max_attempts=5):
        """從 captcha img 抓 base64 → ocr_bytes 6 碼純數字（送出前安全重試）。"""
        for n in range(1, max_attempts + 1):
            try:
                cap_b64 = frame.evaluate("""() => {
                    const inputs = [...document.querySelectorAll('input')];
                    const capInput = inputs.find(i => i.placeholder === '驗證碼');
                    if (!capInput) return null;
                    let parent = capInput.parentElement;
                    for (let i = 0; i < 5 && parent; i++) {
                        const img = parent.querySelector('img');
                        if (img && img.naturalWidth >= 80 && img.naturalWidth <= 250) {
                            const canvas = document.createElement('canvas');
                            canvas.width = img.naturalWidth;
                            canvas.height = img.naturalHeight;
                            canvas.getContext('2d').drawImage(img, 0, 0);
                            return canvas.toDataURL('image/png').split(',')[1];
                        }
                        parent = parent.parentElement;
                    }
                    return null;
                }""")
                if not cap_b64:
                    _log(f"[taishin][cap] 第 {n} 次抓 captcha base64 失敗")
                    continue
                raw = base64.b64decode(cap_b64)
                text = ocr_bytes(raw, expected_len=6, alnum_only=True)
                if text and len(text) == 6 and text.isdigit():
                    _log(f"[taishin][cap] 第 {n} 次 OCR 成功: {text}")
                    return text
                _log(f"[taishin][cap] 第 {n}/{max_attempts} 次 OCR 失敗（讀到 {text!r}），換圖")
                # 換圖：點旁邊的 refresh icon（如有）
                try:
                    frame.evaluate("""() => {
                        const inputs = [...document.querySelectorAll('input')];
                        const capInput = inputs.find(i => i.placeholder === '驗證碼');
                        let parent = capInput.parentElement;
                        for (let i = 0; i < 5 && parent; i++) {
                            const refresh = parent.querySelector('[class*="refresh"], [class*="reload"], i.fa-sync, .icon-refresh');
                            if (refresh) { refresh.click(); return true; }
                            parent = parent.parentElement;
                        }
                        return false;
                    }""")
                    frame.evaluate("() => new Promise(r => setTimeout(r, 1500))")
                except Exception as e:
                    _log(f"[taishin][cap] 換圖失敗: {e}")
            except Exception as e:
                _log(f"[taishin][cap] OCR 失敗: {e}")
        return None

    def _submit_login_once(self, page) -> tuple[bool, str]:
        """執行單次 fill帳密 + OCR captcha + click 登入鈕 + 等 10 秒判斷結果。

        回傳 (success, reason):
          - (True, "ok")               — 登入成功進主畫面
          - (False, "no_frame")        — 找不到登入 iframe
          - (False, "fill_failed")     — 填欄位失敗
          - (False, "ocr_failed")      — OCR 5 次都失敗（未送 login）
          - (False, "submit_failed")   — click 登入鈕失敗
          - (False, "not_logged_in")   — 送出但 _logged_in 偵測不到（可能 popup 阻擋）

        實測揭示：台新「上次未正常登出」popup 後**必須**再呼叫此 helper 一次才能進主畫面,
        是 by-design 兩階段登入流程，**不算** retry。但呼叫超過 2 次（即不止 1 次救援）
        則視為真實 retry，違反 max_attempts=1 鐵律 → 停手。
        """
        frame = self._find_login_frame(page)
        if frame is None:
            if "my.taishinbank.com.tw" in (page.url or ""):
                _log("[taishin][login] ✓ session 可能仍有效")
                return True, "ok"
            _log("[taishin][login] 找不到 login iframe")
            return False, "no_frame"

        _log(f"[taishin][login] login iframe → {frame.url[:100]}")

        # 填 3 欄
        try:
            for label in ["national_id", "user_code", "password"]:
                ph = FIELD_PLACEHOLDERS[label]
                val = getattr(self.creds, label)
                frame.fill(_ph_sel(ph), val)
                page.wait_for_timeout(200)
                _log(f"[taishin][login]   ✓ {label} (len {len(val)})")
        except Exception as e:
            _log(f"[taishin][login] 填欄位失敗: {e}")
            return False, "fill_failed"

        # OCR captcha
        captcha = self._ocr_captcha(frame, max_attempts=5)
        if not captcha:
            _log("[taishin][login] OCR 5 次都失敗，放棄（未送 login）")
            return False, "ocr_failed"
        try:
            frame.fill(_ph_sel(FIELD_PLACEHOLDERS["captcha"]), captcha)
            page.wait_for_timeout(300)
        except Exception as e:
            _log(f"[taishin][login] 填 captcha 失敗: {e}")
            return False, "fill_failed"

        _log(f"[taishin][login] ⚠️ 送出 login（captcha={captcha}）")
        try:
            frame.click(f"#{LOGIN_BTN_ID}", timeout=8000)
        except Exception as e:
            _log(f"[taishin][login] click 登入鈕失敗: {e}")
            return False, "submit_failed"

        page.wait_for_timeout(10000)

        if self._logged_in(page):
            return True, "ok"
        return False, "not_logged_in"

    def login(self, page) -> bool:
        """台新登入——兩階段流程支援：

        Step 1: 首次 fill+submit
        Step 2: 若撞「上次未正常登出」popup → 點「重新登入」（清 session redirect 回登入頁）
                → **必須**再 fill+submit 一次（此非 retry，是 by-design 兩階段路徑）
        Step 3: 第 2 次仍失敗 → 鐵律停手（避免無限循環）

        實測揭示：台新「重新登入」按鈕後**需要**再次登入才能進主畫面, 視為救援流程一部分,
        不算 max_attempts=1 的第 2 次 submit。但 step 3 後就絕不再試。
        """
        page.wait_for_timeout(10000)
        _log(f"[taishin][login] 起始 url={page.url}")

        # 載入時若有殘留 popup 先清（少數 case：上次未正常關 browser）
        try:
            kicked = self.handle_dup_login_modal(page)
            if kicked:
                _log("[taishin][login] 載入時就有殘留 popup，已踢")
                page.wait_for_timeout(4000)
        except Exception as e:
            _log(f"[taishin][login] 初始 dup-handler 略過: {e}")

        # ====== Step 1: 首次 submit ======
        _log("[taishin][login] === Step 1: 首次 submit ===")
        ok, reason = self._submit_login_once(page)
        if ok:
            _log(f"[taishin][login] ✅ Step 1 登入成功 -> {page.url}")
            return True
        _log(f"[taishin][login] Step 1 結果: ok={ok} reason={reason}")

        # ====== Step 2: 偵測「上次未正常登出」popup → 點「重新登入」→ 再 submit ======
        # 實測揭示：這是台新 by-design 兩階段流程, **第 2 次 submit 不算 retry**。
        # 但必須確認 popup 真的有出現（不是亂猜重試）。
        if reason != "not_logged_in":
            # Internal: OCR / fill / submit mechanical failure — DON'T recover
            # to avoid burning a wrong-password attempt at the bank.
            _log(f"[taishin][login] ❌ Step 1 機械故障 ({reason}) → 不救援")
            self._dump_login_failed(page)
            raise TaishinLoginError(
                f"台新登入失敗 (Step 1 表單操作異常): {reason}; url={page.url}",
            )

        # 偵測 popup
        try:
            kicked = self.handle_dup_login_modal(page)
        except Exception as e:
            _log(f"[taishin][login] dup-handler 例外: {e}")
            kicked = False

        if not kicked:
            # 沒 popup 又 not_logged_in → 可能真的密碼錯或別種 error
            _log("[taishin][login] ❌ Step 1 失敗但無重複登入彈窗 → 疑似帳號或密碼錯誤,停手")
            self._dump_login_failed(page)
            raise TaishinLoginError(
                f"Step 1 not_logged_in 且無 dup-popup; 疑似帳密錯; url={page.url}",
            )

        _log("[taishin][login] === Step 2: dup-popup 點掉「重新登入」→ 等 redirect ===")
        page.wait_for_timeout(10000)

        # redirect 後可能直接進主畫面（esun-style）或回登入頁（台新-style）
        if self._logged_in(page):
            _log(f"[taishin][login] ✅ Step 2 「重新登入」一鍵成功（esun-style）-> {page.url}")
            return True

        # 台新-style：回登入頁，需再 fill+submit
        _log("[taishin][login] === Step 3: 二次 submit (by-design 兩階段) ===")
        ok2, reason2 = self._submit_login_once(page)
        if ok2:
            _log(f"[taishin][login] ✅ Step 3 二次登入成功 -> {page.url}")
            return True

        _log(f"[taishin][login] ❌ Step 3 失敗 reason={reason2} → 停手,不再試第 3 次")
        self._dump_login_failed(page)
        from backend.banks._login_debug import snapshot as _login_snapshot
        snap = _login_snapshot(page)
        # Internal policy: max_attempts=2, MUST NOT auto-retry beyond Step 3.
        # See wiki/concepts/taiwan-bank-login-retry-account-lockout-lesson.
        raise TaishinLoginError(
            f"台新登入失敗 (Step 3): {reason2}; url={page.url}\n{snap}",
        )

    def _dump_login_failed(self, page):
        """login 失敗時 dump 截圖 + frame 錯誤訊息 — 給人類核對。"""
        from backend.core.store import _data_root
        debug_dir = _data_root() / "taishin_collect"
        debug_dir.mkdir(parents=True, exist_ok=True)
        try:
            page.screenshot(path=str(debug_dir / "login_FAILED.png"), full_page=True)
            _log(f"[taishin][login] 失敗截圖已存: {debug_dir}/login_FAILED.png")
        except Exception:
            pass

        try:
            frame = self._find_login_frame(page)
            if frame:
                err_texts = frame.evaluate("""() => {
                    return [...document.querySelectorAll('div, span, p')]
                        .filter(e => e.offsetParent !== null)
                        .map(e => (e.innerText || '').trim())
                        .filter(t => t && t.length < 200 && /錯誤|失敗|無效|不正確|請重|請聯絡|密碼|驗證碼|帳號|身分|鎖|停用/.test(t))
                        .slice(0, 5);
                }""")
                if err_texts:
                    _log(f"[taishin][login] frame 錯誤訊息: {err_texts}")
        except Exception:
            pass

    def collect(self, page, collector: ResponseCollector) -> BankCollectResult:
        """台新 collect — C 策略：popup 強行 hide → 直接點 top nav「信用卡」。

        2026-06-11 實測揭示：台新主畫面 top nav 在主 page（非 iframe）有 9 個 menu,
        「信用卡」是第 5 個，約 x:1390/2160 寬。直接從 DOM 找元素點，不依賴座標猜測。
        """
        out: dict = {}
        page.wait_for_timeout(8000)

        from backend.core.store import _data_root
        debug_dir = _data_root() / "taishin_collect"
        debug_dir.mkdir(parents=True, exist_ok=True)

        out["initial_url"] = page.url
        with contextlib.suppress(Exception):
            page.screenshot(path=str(debug_dir / "00_initial.png"), full_page=False)

        # ── Step 1: 關所有 popup（強行 hide 版）──
        self._close_popups(page)
        page.wait_for_timeout(2000)
        with contextlib.suppress(Exception):
            page.screenshot(path=str(debug_dir / "01_after_close_popups.png"), full_page=False)

        # ── Step 2: 從所有 frames（含主 page）找 top nav「信用卡」DOM 元素並點 ──
        # 台新 SPA 整個介面在 svc/rwd iframe 內，top nav 也在裡面（不在主 page）
        clicked_credit_card = False
        target_frame = None
        target_info = None
        try:
            for f in [page] + [fr for fr in page.frames if fr != page.main_frame]:
                kind = "page" if f == page else f'frame({(f.url or "")[:60]})'
                try:
                    found = f.evaluate("""() => {
                        // 找 text='信用卡' 的元素，但更要找它的可點父鏈（a / button / [onclick] / [role=button]）
                        const all = document.querySelectorAll('a, button, [role="menuitem"], li, span, div');
                        const out = [];
                        for (const el of all) {
                            const t = (el.textContent || el.innerText || '').trim();
                            if (t !== '信用卡' && t !== '信用卡 ') continue;
                            if (el.offsetParent === null) continue;
                            const r = el.getBoundingClientRect();
                            if (r.width < 5 || r.height < 5) continue;
                            // 排除巢狀容器（內部還有 a/button → 點外層 onclick 可能無效）
                            if (['DIV', 'LI', 'SPAN'].includes(el.tagName)) {
                                if (el.querySelector('a, button')) continue;
                            }
                            // 找可點祖先（最近的 a/button/[onclick]/[role=button]）
                            let click_target = el;
                            let p = el;
                            for (let i = 0; i < 8 && p; i++) {
                                if (p.tagName === 'A' || p.tagName === 'BUTTON' ||
                                    p.onclick || p.getAttribute('role') === 'button' ||
                                    p.classList.contains('_nav_menu__item') ||
                                    (p.className && /menu|nav|item/i.test(p.className))) {
                                    click_target = p;
                                    break;
                                }
                                p = p.parentElement;
                            }
                            const ct_r = click_target.getBoundingClientRect();
                            out.push({
                                tag: el.tagName, x: r.x, y: r.y, w: r.width, h: r.height,
                                href: el.getAttribute('href') || '',
                                click_tag: click_target.tagName,
                                click_class: click_target.className || '',
                                click_x: ct_r.x, click_y: ct_r.y, click_w: ct_r.width, click_h: ct_r.height,
                                click_visible: window.getComputedStyle(click_target).display !== 'none',
                            });
                        }
                        return out;
                    }""")
                except Exception:
                    found = []
                if found:
                    _log(f"[taishin][collect] {kind} 找到 {len(found)} 個「信用卡」候選:")
                    for i, n in enumerate(found[:5]):
                        _log(f"  [{i}] text={n['tag']}@({int(n['x'])},{int(n['y'])}) {int(n['w'])}x{int(n['h'])} "
                             f"→ click_target={n['click_tag']}.{n['click_class'][:40]!r}"
                             f" @({int(n['click_x'])},{int(n['click_y'])}) "
                             f"{int(n['click_w'])}x{int(n['click_h'])} visible={n['click_visible']}")
                    if target_info is None:
                        # 優先 top nav (y < 250) + visible click_target
                        top_nav = [n for n in found if n["y"] < 250 and n["click_visible"]]
                        target_info = top_nav[0] if top_nav else (found[0] if found else None)
                        target_frame = f
                        if target_info:
                            _log(f"[taishin][collect] 採用 {kind} 第一個 top-nav 候選")

            if target_info and target_frame:
                # 2026-06-11 (C 路徑教訓): 直接 click SPAN 文字無效 (toggle 'on' class 但無 routing)
                # 需逐層 click ancestors，找到真正能展開 dropdown / 跳頁的層級
                clicked_credit_card = self._try_ancestor_clicks(target_frame, page, debug_dir)
            else:
                _log("[taishin][collect] 全 frames 都找不到「信用卡」元素")
        except Exception as e:
            _log(f"[taishin][collect] 找信用卡 nav 例外: {e}")

        out["clicked_credit_card"] = clicked_credit_card

        # ── Step 3: 等信用卡頁載入 + 等 API call 跑完（10 秒） ──
        page.wait_for_timeout(10000)
        with contextlib.suppress(Exception):
            page.screenshot(path=str(debug_dir / "02_after_card_click.png"), full_page=True)
        out["after_card_click_url"] = page.url
        _log(f"[taishin][collect] click 後 final url={page.url[:100]}")

        # ── Step 4: dump 信用卡頁 frame text + 攔 API ──
        # 已經點過「查詢信用卡明細」（在 _try_ancestor_clicks 內），現在直接 dump
        credit_card_frame = None
        if clicked_credit_card:
            for f in page.frames:
                if f == page.main_frame:
                    continue
                try:
                    ct = f.evaluate("() => document.body.innerText.slice(0, 12000)")
                    if ct and ("信用卡" in ct or "帳單" in ct or "消費" in ct or "應繳" in ct):
                        credit_card_frame = f
                        out["credit_card_page_text"] = ct
                        out["credit_card_frame_url"] = f.url[:200]
                        _log(f"[taishin][collect] 信用卡頁 frame text len={len(ct)} url={f.url[:100]}")
                        break
                except Exception:
                    pass

        # 信用卡 menu hover 後也順便 dump 完整 mega menu 截圖（archive）
        card_submenu = []
        out["card_submenu"] = card_submenu  # 保留欄位但不再 click

        # ── Step 4b: parse 信用卡頁 frame text 抽結構化資料 ──
        page_text = out.get("credit_card_page_text") or ""
        if page_text:
            try:
                parsed = self._parse_credit_card_page(page_text)

                # 2026-07-03 修正 (0.3.64): 台新繳款事實在「切到某月帳單頁後，
                # 該頁的『上期實繳金額明細』」，post_date = 該月帳單被扣繳日。
                # 例：切到 05 月頁 → 抓到 04/27 -56 (04 月帳單被扣)。
                # 舊碼三個 bug 一次修：
                #   (1) month_options[:12] 含 index=0 「其他月份」placeholder,
                #       select_option(index=0) 觸發空重載造成 rows 錯亂。
                #   (2) 順序不穩定 (log 有時 06→05→04, 有時反過來)。
                #   (3) parser 內 billed_txns 也含當期消費明細退款 row (負值 desc
                #       如「年費減免」「一卡通餘額退款」)，crawler filter 用
                #       「'信用卡' in desc」勉強擋掉但非明確 partition。這裡改成
                #       直接吃 parser 給的 billed_txns (parser 已 partition 到
                #       「上期實繳金額明細」section)。
                if credit_card_frame:
                    month_options = []
                    try:
                        month_options = credit_card_frame.evaluate("""() => {
                            const sels = [...document.querySelectorAll('select')];
                            const monthSel = sels.find(s => [...s.options].some(o => /^\\d{4}\\/\\d{2}$/.test((o.textContent || '').trim())));
                            if (!monthSel) return [];
                            return [...monthSel.options]
                                .map((o, index) => ({index, text: (o.textContent || '').trim()}))
                                .filter(o => /^\\d{4}\\/\\d{2}$/.test(o.text));
                        }""") or []
                    except Exception as e:
                        _log(f"[taishin][collect] 月份下拉 dump 失敗: {e}")
                    # 穩定按時間降序排 (2026/06, 2026/05, ...)，log 才不會神秘跳
                    month_options.sort(key=lambda o: o.get("text") or "", reverse=True)
                    out["credit_card_month_options"] = month_options
                    seen_billed_keys = {
                        (r.get("txn_date"), r.get("post_date"), r.get("desc"), r.get("amount"))
                        for r in parsed.get("billed_txns", [])
                    }
                    for opt in month_options[:12]:
                        try:
                            credit_card_frame.evaluate("location.hash = '#/RB0708/0100?ts=' + Date.now()")
                            page.wait_for_timeout(4000)
                            for f in page.frames:
                                if "svc/rwd" in (f.url or ""):
                                    credit_card_frame = f
                                    break
                            credit_card_frame.locator("select").nth(1).select_option(index=opt.get("index") or 0)
                            page.wait_for_timeout(5000)
                            month_text = credit_card_frame.evaluate("() => document.body.innerText.slice(0, 50000)") or ""
                            month_parsed = self._parse_credit_card_page(month_text)
                            added = 0
                            for row in month_parsed.get("billed_txns", []):
                                key = (row.get("txn_date"), row.get("post_date"), row.get("desc"), row.get("amount"))
                                if key in seen_billed_keys:
                                    continue
                                parsed.setdefault("billed_txns", []).append(row)
                                seen_billed_keys.add(key)
                                # parser 已 partition 到「上期實繳金額明細」section，
                                # 所有負值 row 都是真扣繳。仍過濾 desc 有「扣繳/轉帳」
                                # 字樣保險。
                                desc = str(row.get("desc") or "")
                                amt = float(row.get("amount") or 0)
                                if amt < 0 and ("扣繳" in desc or "轉帳" in desc):
                                    added += 1
                            _log(f"[taishin][collect] 月份 {opt.get('text')} 補 payment rows={added}")
                        except Exception as e:
                            _log(f"[taishin][collect] 月份 {opt.get('text')} 查詢失敗: {e}")

                out["credit_card_parsed"] = parsed
                _log(f"[taishin][collect] parser 結果: "
                     f"cards={len(parsed.get('cards', []))} "
                     f"pending={len(parsed.get('pending_txns', []))} "
                     f"billed={len(parsed.get('billed_txns', []))} "
                     f"summary={'有' if parsed.get('summary') else '無'}")
            except Exception as e:
                _log(f"[taishin][collect] parser 例外: {e}")
                out["credit_card_parsed"] = {"error": str(e)}

        # ── Step 5: 台幣存款交易明細（RB0102/0100 查詢交易明細）──
        # 2026-06-30: 使用者要求補齊 account drilldown 的存款交易。
        # getNbMenuData 顯示臺幣服務 → 臺幣帳戶查詢 → 查詢交易明細 href=RB0102/0100。
        # 先進 route dump form/results 與攔截 API；persist parser 下一步依真 raw shape 寫。
        try:
            txn_frame = None
            for f in page.frames:
                if "svc/rwd" in (f.url or ""):
                    txn_frame = f
                    break
            if txn_frame:
                nav = txn_frame.evaluate("""() => {
                    location.hash = '#/RB0102/0100?ts=' + Date.now();
                    return location.href;
                }""")
                _log(f"[taishin][twd] goto RB0102/0100 → {nav[:120]}")
                page.wait_for_timeout(8000)
                with contextlib.suppress(Exception):
                    page.screenshot(path=str(debug_dir / "03_twd_txn_query.png"), full_page=True)
                for f in page.frames:
                    if "svc/rwd" in (f.url or ""):
                        txn_frame = f
                        break
                twd_text = txn_frame.evaluate("() => document.body.innerText.slice(0, 30000)") or ""
                out["twd_txn_page_text"] = twd_text
                out["twd_txn_frame_url"] = txn_frame.url[:300]
                out["twd_txn_form_controls"] = txn_frame.evaluate(r"""() => ({
                    selects: [...document.querySelectorAll('select')].map((s, si) => ({
                        index: si, id: s.id, name: s.name, value: s.value,
                        visible: s.offsetParent !== null,
                        options: [...s.options].map((o, oi) => ({index: oi, value: o.value || '', text: (o.textContent || '').trim()})).slice(0, 20),
                    })),
                    inputs: [...document.querySelectorAll('input')].map((i, ii) => ({
                        index: ii, id: i.id, name: i.name, type: i.type, value: i.value,
                        visible: i.offsetParent !== null,
                        placeholder: i.placeholder || '',
                        text: (i.closest('label,div,td,tr')?.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 120),
                    })).slice(0, 80),
                    buttons: [...document.querySelectorAll('button,a,[role=button]')].map((b, bi) => ({
                        index: bi, tag: b.tagName, id: b.id, cls: (b.className || '').toString().slice(0,80), href: b.getAttribute('href') || '',
                        text: (b.textContent || '').replace(/\s+/g, ' ').trim(), visible: b.offsetParent !== null,
                    })).filter(b => b.text || b.href).slice(0, 120),
                })""")
                _log(f"[taishin][twd] page text_len={len(twd_text)} url={txn_frame.url[:120]}")

                twd_results = []
                account_options = (out.get("twd_txn_form_controls") or {}).get("selects", [{}])[0].get("options", [])[1:]
                for acct_idx, opt in enumerate(account_options[:10], start=1):
                    query_result = {"ok": False}
                    try:
                        # Taishin RB0102 uses visible native-looking selects backed by Vue.
                        # Direct selectedIndex+dispatch reads back in DOM but does not update
                        # Vue model; Playwright select_option(index=...) fires the trusted path.
                        txn_frame.locator("select").nth(0).select_option(index=opt.get("index") or acct_idx)
                        page.wait_for_timeout(800)
                        txn_frame.locator("select").nth(1).select_option(index=3)  # 1個月
                        page.wait_for_timeout(800)
                        txn_frame.locator("select").nth(2).select_option(index=0)  # 由新到舊
                        page.wait_for_timeout(500)
                        selected = txn_frame.evaluate("""() => {
                            const sels = [...document.querySelectorAll('select')];
                            return {
                                accountText: sels[0]?.options[sels[0].selectedIndex]?.textContent?.trim() || '',
                                periodText: sels[1]?.options[sels[1].selectedIndex]?.textContent?.trim() || '',
                                sortText: sels[2]?.options[sels[2].selectedIndex]?.textContent?.trim() || '',
                            };
                        }""")
                        txn_frame.locator("input[value='查詢']").first.click(timeout=8000)
                        query_result = {"ok": True, **(selected or {})}
                    except Exception as e:
                        query_result = {"ok": False, "error": str(e)}
                    _log(f"[taishin][twd] 帳號#{acct_idx} 查詢: {query_result}")
                    page.wait_for_timeout(8000)
                    for f in page.frames:
                        if "svc/rwd" in (f.url or ""):
                            txn_frame = f
                            break
                    result_text = txn_frame.evaluate("() => document.body.innerText.slice(0, 50000)") or ""
                    twd_results.append({
                        "selected_text": opt.get("text"),
                        "query_result": query_result,
                        "url": txn_frame.url[:300],
                        "text": result_text,
                    })
                    with contextlib.suppress(Exception):
                        page.screenshot(path=str(debug_dir / f"04_twd_txn_result_{acct_idx}.png"), full_page=True)
                    if acct_idx < len(account_options[:10]):
                        with contextlib.suppress(Exception):
                            txn_frame.evaluate("location.hash = '#/RB0102/0100?ts=' + Date.now()")
                            page.wait_for_timeout(5000)
                            for f in page.frames:
                                if "svc/rwd" in (f.url or ""):
                                    txn_frame = f
                                    break
                out["twd_txn_results"] = twd_results
            else:
                out["twd_txn_error"] = "rwd_frame_not_found"
                _log("[taishin][twd] 找不到 svc/rwd frame")
        except Exception as e:
            out["twd_txn_error"] = str(e)
            _log(f"[taishin][twd] probe 失敗: {e}")

        # ── Step 6: dump 所有攔到的 API responses ──
        hits_by_endpoint = {}
        for h in collector.hits:
            if h.resp_json is None:
                continue
            if h.endpoint not in hits_by_endpoint:
                hits_by_endpoint[h.endpoint] = h.resp_json
        out["api_responses"] = hits_by_endpoint
        out["final_url"] = page.url
        out["_all_endpoints"] = sorted(hits_by_endpoint.keys())
        parsed = out.get("credit_card_parsed") or {}
        publish_card_bill_facts(out, [_taishin_card_bill_fact(parsed)])
        _log(f"[taishin][collect] 攔到 {len(hits_by_endpoint)} 個 endpoint")
        return BankCollectResult(**out)


if __name__ == "__main__":
    import json
    crawler = TaishinCrawler()
    try:
        result = crawler.run(login_url=BASE, headless=True)
    except TaishinLoginError as e:
        result = {"error": "login_failed_stop", "detail": str(e)}

    out_file = Path(__file__).resolve().parents[1] / "data" / "taishin_collected.json"
    out_file.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    _log(f"\n[taishin][done] 已存: {out_file}")
    if result.get("error"):
        _log(f"  ❌ error: {result['error']}")
    else:
        data = result.get("data", {})
        _log(f"  url: {data.get('final_url')}")
        _log(f"  frames: {len(data.get('frames', []))}")
        _log(f"  endpoints: {data.get('_all_endpoints', [])}")
