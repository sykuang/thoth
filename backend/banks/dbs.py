#!/usr/bin/env python3
"""DBS Taiwan digital banking crawler (internet-banking.dbs.com.tw/digitw/).

星展（台灣）internet-banking.dbs.com.tw/digitw/ 個人網銀爬蟲。

2026-06-12 初版（dry probe + vision 驗證）:

  登入頁特徵（極友善）:
    - 純 SPA（React + styled-components），無 frameset、無 iframe
    - 表單只有 2 欄:
        #username  (type=password 眼睛遮罩) - 使用者帳號
        #password  (type=password)         - 密碼
    - #loginbutton (BUTTON text='登入')
    - **無 CAPTCHA**！不用 OCR
    - 無「立即登入」開 modal 步驟，欄位 page load 就在

  登入流程:
    Step 1: page goto /digitw/ → 自動 redirect /digitw/login → 等 8s SPA hydrate
    Step 2: native input value setter + dispatch input/change/blur 寫 username & password
            （React reactive binding，純 element.value=xxx 不會觸發 onChange）
    Step 3: click #loginbutton
    Step 4: 等 router 從 /login → /digital/... (登入成功) 或 stay /login (失敗)

  ⚠️ 鐵律 max_attempts=1 — 失敗 raise DbsLoginError，絕不重打（會鎖帳號）

  TODO 第二輪（尚未完成 — 待使用者配合錄 HAR）:
    - 現況: collect() 只 dump nav_items + page text + 攔到的 api_responses
            (assets/liabilities/customer-profile 已可解析但無逐筆 billed_txn / casa 交易明細)
    - 待補:
      1. 信用卡明細: 點主選單「信用卡 → 帳單明細 / 消費明細」, 攔 billed_txn endpoint
         → persist_dbs 補 billed_payload + card_pending refresh
      2. 存款交易明細: 點「帳戶 → 交易紀錄」, 攔 transactions endpoint
         → persist_dbs 補 twd_transactions upsert
      3. 處理可能的 2FA / OTP（首次登入或新裝置才會問, user_data_dir 持久化後免）
"""
from __future__ import annotations

import contextlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from backend.core.base import BankCollectResult, BankCrawler, ResponseCollector
from backend.banks._login_debug import snapshot as _login_snapshot
from backend.core.creds import DbsCreds

BASE = "https://internet-banking.dbs.com.tw/digitw/"
LOGIN_PATH_HINT = "/digitw/login"


def _log(*a):
    print(*a, file=sys.stderr, flush=True)


class DbsLoginError(RuntimeError):
    """DBS login 送出後失敗——立刻中止，絕不自動重打。"""


class DbsCrawler(BankCrawler):
    def __init__(self):
        super().__init__(name="dbs")
        self.creds = DbsCreds.load()

    def _host_filter(self) -> str:
        return "dbs.com.tw"

    def _logged_in(self, page) -> bool:
        """W (2026-06-17): positive signal 4 條件 AND（純 SPA，對齊 SCSB 鐵律）

        1) urlOk: dbs.com.tw 域內 + 不在 /digitw/login
        2) noLoginForm: #username + #password + #loginbutton 都不可見
        3) lenOk: body innerText >= 300（W 2026-06-18: 從 500 降 300, 對齊 hsbc SPA
           門檻——cloud evidence 揭 DBS overview clean innerText 僅 341 字，500 門檻
           造成 false negative。SPA 不像傳統 page 那樣大段文字，門檻必降。詳見
           wiki/concepts/bank-crawler-login-positive-signal-rule.md SPA 門檻表）
        4) kw >= 2: 內銀區關鍵字命中 ≥ 2 個
        """
        try:
            url = (page.url or "").lower()
            if "dbs.com.tw" not in url or LOGIN_PATH_HINT in url:
                return False

            ok = page.evaluate("""
                () => {
                  const visible = (e) => {
                    if (!e) return false;
                    const r = e.getBoundingClientRect();
                    const cs = getComputedStyle(e);
                    return !!(r.width || r.height || e.getClientRects().length)
                      && cs.display !== 'none' && cs.visibility !== 'hidden';
                  };
                  const noLoginForm = !visible(document.querySelector('#username'))
                    && !visible(document.querySelector('#password'))
                    && !visible(document.querySelector('#loginbutton'));
                  const body = document.body && document.body.innerText || '';
                  const lenOk = body.length >= 300;
                  const KW = ['帳戶總覽','資產總覽','登出','logout','存款','轉帳','信用卡',
                              '台幣','外幣','基金','投資','貸款','繳費','個人設定','安全'];
                  const kw = KW.filter(k => body.toLowerCase().includes(k.toLowerCase())).length;
                  return noLoginForm && lenOk && kw >= 2;
                }
            """)
            return bool(ok)
        except Exception:
            return False

    def login(self, page) -> bool:
        """DBS digibank 登入 — 鐵律 max_attempts=1。"""
        page.wait_for_timeout(8000)  # 等 SPA hydrate
        _log(f"[dbs][login] 起始 url={page.url}")

        # session 復用偵測
        if self._logged_in(page):
            _log("[dbs][login] ✓ session 仍有效（已不在 login 頁），跳過 login")
            return True

        # 確認在登入頁
        if LOGIN_PATH_HINT not in (page.url or ""):
            _log(f"[dbs][login] ⚠️ 不在 login 頁: {page.url}")

        # ─── Step 1: 等欄位出現 ───
        try:
            page.wait_for_selector("#username", timeout=15000)
            page.wait_for_selector("#password", timeout=5000)
            page.wait_for_selector("#loginbutton", timeout=5000)
        except Exception as e:
            _log(f"[dbs][login] ❌ 等欄位 timeout: {e}")
            with contextlib.suppress(Exception):
                page.screenshot(path=str(_debug_dir() / "01_no_fields.png"), full_page=True)
            return False

        # ─── Step 2: fill 帳密（純 keyboard type，真實鍵盤模擬）───
        # 2026-06-12 v1 教訓：native setter + dispatch → password 只收到 1 字
        # 2026-06-12 v2 教訓：page.fill → password 也只收到 1 字（同樣被 React onChange 吞）
        #   + Ctrl+A/Delete 清不掉 page.fill 寫入的 value，反而疊加
        # v3：純 keyboard.type 從頭打，click → triple-click 選文字 → Backspace 清空 → type
        try:
            # username
            u_loc = page.locator("#username")
            u_loc.click()
            page.wait_for_timeout(200)
            # 三連點選整行文字（DBS React 接得到）
            u_loc.click(click_count=3)
            page.wait_for_timeout(100)
            page.keyboard.press("Backspace")
            page.wait_for_timeout(100)
            page.keyboard.type(self.creds.username, delay=80)
            page.wait_for_timeout(300)

            # password
            p_loc = page.locator("#password")
            p_loc.click()
            page.wait_for_timeout(200)
            p_loc.click(click_count=3)
            page.wait_for_timeout(100)
            page.keyboard.press("Backspace")
            page.wait_for_timeout(100)
            page.keyboard.type(self.creds.password, delay=80)
            page.wait_for_timeout(500)
        except Exception as e:
            _log(f"[dbs][login] ❌ keyboard type 失敗: {e}")
            return False

        check = page.evaluate("""() => {
            const u = document.getElementById('username');
            const p = document.getElementById('password');
            return {
                u_len: u ? u.value.length : -1,
                p_len: p ? p.value.length : -1,
            };
        }""") or {}
        _log(f"[dbs][login] keyboard type 後: {check}")

        u_target = len(self.creds.username)
        p_target = len(self.creds.password)

        if check.get("u_len", 0) != u_target or check.get("p_len", 0) != p_target:
            _log(f"[dbs][login] ❌ fill 長度不符 (u={check.get('u_len')}/{u_target}, p={check.get('p_len')}/{p_target})")
            _log("[dbs][login] 為保護帳號，**不送出 login**（純 fill 失敗，未累計密碼錯）")
            with contextlib.suppress(Exception):
                page.screenshot(path=str(_debug_dir() / "02_fill_failed.png"), full_page=True)
            return False

        # ─── Step 3: click #loginbutton ───
        _log("[dbs][login] 送出 login")
        try:
            page.evaluate("""() => {
                const btn = document.getElementById('loginbutton');
                if (btn) btn.click();
            }""")
        except Exception as e:
            _log(f"[dbs][login] ❌ click loginbutton 失敗: {e}")
            return False

        # ─── Step 4: 等 redirect + SPA hydrate ───
        # 2026-06-18: 改 retry loop (sub-pattern 對齊 ctbc 20s)。雲端 SPA
        # hydrate 比本機慢，舊 `wait 10s + _logged_in` 一次定生死 → race
        # condition 假 fail（evidence 顯示 body 已渲染完整菜單但首次判定
        # 時還沒好，導致誤回報「登入失敗」抓到的 alerts 是 DBS 頁面正常
        # CTA「開立定存/了解更多/申請貸款」）。改 retry：每秒判一次最多 20s。
        page.wait_for_timeout(3000)  # 給 click→navigation 最初一段
        for _ in range(20):
            page.wait_for_timeout(1000)
            if self._logged_in(page):
                final_url = page.url or ""
                _log(f"[dbs][login] ✅ 登入成功 → {final_url}")
                with contextlib.suppress(Exception):
                    page.screenshot(path=str(_debug_dir() / "03_after_login.png"), full_page=True)
                return True

        final_url = page.url or ""
        _log(f"[dbs][login] ❌ 送出後 ~20s 仍未 _logged_in, url={final_url}")
        with contextlib.suppress(Exception):
            page.screenshot(path=str(_debug_dir() / "03_after_login.png"), full_page=True)

        # 失敗：找錯誤訊息（給用戶 debug 用, 不重打）
        # 2026-06-18: 原本盲掃 .alert / [role=alert] / .error class 結果
        # 把 DBS 已登入頁的正常 CTA（「開立定存」「了解更多」「申請貸款」）
        # 也撈進來當錯誤訊息 → 誤導 user。改 keyword whitelist：只認真正
        # 錯誤關鍵字（錯誤/密碼/失敗/鎖定/逾時/驗證/帳號不存在/重試等）。
        err_msg = ""
        with contextlib.suppress(Exception):
            err_msg = page.evaluate(r"""() => {
                const ERROR_KW = /錯誤|不正確|失敗|鎖定|逾時|驗證碼|帳號不存在|重試|無效|invalid|error|fail|locked|expired/i;
                const sels = ['.error', '.alert', '[class*=error]', '[class*=Error]',
                              '[role=alert]', '[class*=alert]', '[class*=Alert]'];
                for (const sel of sels) {
                    for (const el of document.querySelectorAll(sel)) {
                        if (el.offsetParent === null) continue;
                        const t = (el.textContent || '').trim();
                        if (t && t.length < 200 && ERROR_KW.test(t)) return t;
                    }
                }
                return '';
            }""") or ""

        msg = (
            f"DBS 登入失敗（url={final_url}）: {err_msg or '未知原因'}\n"
            f"{_login_snapshot(page)}"
        )
        _log(f"[dbs][login] ❌ {msg}")
        raise DbsLoginError(msg)

    def collect(self, page, collector: ResponseCollector) -> BankCollectResult:
        """DBS collect 第一輪：dump 登入後 URL + page text + endpoint 地圖。

        TODO 第二輪（見檔案頂端 docstring）: 點信用卡 menu / 帳戶 → 抓 billed_txn / casa 交易明細
        """
        out: dict = {}
        page.wait_for_timeout(8000)

        debug_dir = _debug_dir()
        with contextlib.suppress(Exception):
            page.screenshot(path=str(debug_dir / "00_home.png"), full_page=True)

        out["initial_url"] = page.url
        try:
            out["title"] = page.title()
        except Exception:
            out["title"] = ""

        # dump 主 page text（DBS 是純 SPA，無 frameset）
        try:
            txt = page.evaluate("() => (document.body.innerText || '').slice(0, 15000)") or ""
        except Exception:
            txt = ""
        out["home_text"] = txt
        _log(f"[dbs][collect] home text_len={len(txt)}")

        # dump 主 page 內所有 link/button text（找 menu）
        try:
            nav_items = page.evaluate("""() => {
                const out = [];
                for (const el of document.querySelectorAll('a, button, [role=button], [role=link]')) {
                    if (el.offsetParent === null) continue;
                    const t = (el.textContent || '').trim();
                    if (!t || t.length > 30) continue;
                    const r = el.getBoundingClientRect();
                    out.push({
                        tag: el.tagName,
                        text: t,
                        href: el.href || '',
                        x: r.x, y: r.y, w: r.width, h: r.height,
                    });
                }
                return out;
            }""")
        except Exception:
            nav_items = []
        out["nav_items"] = nav_items[:80]
        _log(f"[dbs][collect] nav items: {len(nav_items)}（含 menu/button）")

        # 第二輪 probe：照使用者截圖，從 overview 的「活期存款」帳戶名稱 / row 點進帳戶明細頁。
        # DBS dashboard 沒有顯式「交易紀錄」menu；交易明細藏在帳戶 row drilldown。
        before_detail_hit_count = len(collector.hits)
        try:
            assets_hit = collector.latest("assets")
            twd_account = None
            if assets_hit and isinstance(assets_hit.resp_json, dict):
                casa = assets_hit.resp_json.get("casa") or {}
                for acct in casa.get("accounts") or []:
                    if not isinstance(acct, dict):
                        continue
                    bal = acct.get("availableBalance") or {}
                    if bal.get("currency") == "TWD" or acct.get("schemeName") == "臺幣數位存款":
                        twd_account = acct
                        break
            acct_name = (twd_account or {}).get("schemeName") or "臺幣數位存款"
            acct_masked = (twd_account or {}).get("accountId") or ""
            acct_display = (twd_account or {}).get("displayAccountNumber") or ""
            acct_tail = (acct_display or acct_masked)[-5:] if (acct_display or acct_masked) else ""
            out["twd_account_drilldown_target"] = {
                "schemeName": acct_name,
                "accountId": acct_masked,
                "displayAccountNumber": acct_display,
                "tail": acct_tail,
            }
            click_result = page.evaluate(r"""({acctName, acctTail}) => {
                const visible = (el) => {
                    const r = el.getBoundingClientRect();
                    const st = getComputedStyle(el);
                    return r.width > 0 && r.height > 0 && st.display !== 'none' && st.visibility !== 'hidden';
                };
                const norm = (s) => (s || '').replace(/\s+/g, ' ').trim();
                const candidates = [];
                for (const el of document.querySelectorAll('a,button,[role="button"],[role="link"],li,div,span')) {
                    if (!visible(el)) continue;
                    const text = norm(el.textContent);
                    if (!text || text.length > 220) continue;
                    const hasName = acctName && text.includes(acctName);
                    const hasTail = acctTail && text.includes(acctTail);
                    if (!hasName && !hasTail) continue;
                    const r = el.getBoundingClientRect();
                    candidates.push({
                        el, text, tag: el.tagName, role: el.getAttribute('role') || '',
                        href: el.getAttribute('href') || '', cls: (el.className || '').toString().slice(0, 80),
                        x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height),
                        area: r.width * r.height,
                        score: (hasName ? 10 : 0) + (hasTail ? 20 : 0)
                            + (/TWD|帳戶餘額|活期存款|外幣/.test(text) ? 5 : 0)
                            + ((el.tagName === 'A' || el.tagName === 'BUTTON' || el.getAttribute('role')) ? 8 : 0),
                    });
                }
                candidates.sort((a, b) => (b.score - a.score) || (a.area - b.area));
                const dump = candidates.slice(0, 12).map(({el, ...rest}) => rest);
                if (!candidates.length) return {clicked: false, reason: 'no_candidate', dump};
                const c = candidates[0];
                try { c.el.scrollIntoView({block: 'center', inline: 'center'}); } catch (e) {}
                c.el.click();
                return {clicked: true, target: dump[0], dump};
            }""", {"acctName": acct_name, "acctTail": acct_tail})
            out["twd_account_drilldown_click"] = click_result
            _log(f"[dbs][twd] drilldown click={click_result.get('clicked')} target={click_result.get('target')}")
            page.wait_for_timeout(9000)
            with contextlib.suppress(Exception):
                page.screenshot(path=str(debug_dir / "01_twd_account_detail.png"), full_page=True)
            detail_text = page.evaluate("() => (document.body.innerText || '').slice(0, 40000)") or ""
            out["twd_account_detail_url"] = page.url
            out["twd_account_detail_text"] = detail_text
            out["twd_account_detail_api_endpoints"] = sorted({
                h.endpoint for h in collector.hits[before_detail_hit_count:] if h.resp_json is not None
            })
            out["twd_account_detail_controls"] = page.evaluate(r"""() => {
                const visible = (el) => {
                    const r = el.getBoundingClientRect();
                    const st = getComputedStyle(el);
                    return r.width > 0 && r.height > 0 && st.display !== 'none' && st.visibility !== 'hidden';
                };
                const norm = (s) => (s || '').replace(/\s+/g, ' ').trim();
                return [...document.querySelectorAll('a,button,[role="button"],[role="link"],select,input')]
                    .filter(visible)
                    .map((el, idx) => ({
                        idx, tag: el.tagName, role: el.getAttribute('role') || '', type: el.getAttribute('type') || '',
                        text: norm(el.textContent || el.value || el.getAttribute('aria-label') || '').slice(0, 120),
                        id: el.id || '', name: el.getAttribute('name') || '', href: el.getAttribute('href') || '',
                    }))
                    .filter(x => x.text || x.id || x.name)
                    .slice(0, 120);
            }""")

            # DBS API calls are made through frontend interceptors; raw fetch misses required
            # request decoration and returns 401. So collect via real UI clicks instead.
            before_month_click_hits = len(collector.hits)
            month_clicks = []
            for label in ("七月", "六月", "五月"):
                try:
                    clicked = page.evaluate(r"""(wanted) => {
                        const visible = (el) => {
                            const r = el.getBoundingClientRect();
                            const st = getComputedStyle(el);
                            return r.width > 0 && r.height > 0 && st.display !== 'none' && st.visibility !== 'hidden';
                        };
                        for (const el of document.querySelectorAll('button,a,[role="button"]')) {
                            const t = (el.textContent || '').replace(/\s+/g, '').trim();
                            if (t === wanted && visible(el)) {
                                el.click();
                                return {clicked: true, tag: el.tagName, text: t};
                            }
                        }
                        return {clicked: false, text: wanted};
                    }""", label)
                    page.wait_for_timeout(2500)
                    month_clicks.append({"label": label, **(clicked or {})})
                except Exception as me:
                    month_clicks.append({"label": label, "clicked": False, "error": str(me)})
            def _click_other_months_and_probe() -> dict:
                try:
                    probe = page.evaluate(r"""() => {
                        const visible = (el) => {
                            const r = el.getBoundingClientRect();
                            const st = getComputedStyle(el);
                            return r.width > 0 && r.height > 0 && st.display !== 'none' && st.visibility !== 'hidden';
                        };
                        for (const el of document.querySelectorAll('button,a,[role="button"]')) {
                            const t = (el.textContent || '').replace(/\s+/g, '').trim();
                            if (t === '其他月份' && visible(el)) {
                                el.click();
                                return {clicked: true, tag: el.tagName, text: t};
                            }
                        }
                        return {clicked: false, reason: 'not_found'};
                    }""") or {"clicked": False}
                    page.wait_for_timeout(1000)
                    return probe
                except Exception as exc:
                    return {"clicked": False, "error": str(exc)}

            other_probe = _click_other_months_and_probe()
            page.wait_for_timeout(1500)
            with contextlib.suppress(Exception):
                page.screenshot(path=str(debug_dir / "02_other_months.png"), full_page=True)
            other_probe["controls_after_click"] = page.evaluate(r"""() => {
                const visible = (el) => {
                    const r = el.getBoundingClientRect();
                    const st = getComputedStyle(el);
                    return r.width > 0 && r.height > 0 && st.display !== 'none' && st.visibility !== 'hidden';
                };
                const wanted = new Set(['2025','2026','一月','二月','三月','四月','五月','六月','七月','八月','九月','十月','十一月','十二月','其他月份']);
                return [...document.querySelectorAll('button,a,[role="button"],select,input,div,span,p')]
                    .filter(visible)
                    .map((el, idx) => {
                        const r = el.getBoundingClientRect();
                        return {
                            idx, tag: el.tagName, role: el.getAttribute('role') || '', type: el.getAttribute('type') || '',
                            text: (el.textContent || el.value || el.getAttribute('aria-label') || '').replace(/\s+/g, ' ').trim().slice(0, 160),
                            id: el.id || '', name: el.getAttribute('name') || '', cls: (el.className || '').toString().slice(0, 80),
                            x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height),
                        };
                    })
                    .filter(x => wanted.has(x.text))
                    .slice(0, 300);
            }""")

            # Click every selectable month in the last 12 months. The popover has duplicate
            # Chinese month labels under 2025/2026, so choose by nearest year-column x.
            # Important: DOM el.click() can report success without triggering DBS React handlers;
            # use real mouse coordinates and verify a new transactions-history API hit appears.
            other_month_clicks = []
            for year, month_label in (
                ("2026", "四月"), ("2026", "三月"), ("2026", "二月"), ("2026", "一月"),
                ("2025", "十二月"), ("2025", "十一月"), ("2025", "十月"), ("2025", "九月"), ("2025", "八月"),
            ):
                probe = _click_other_months_and_probe()
                if not probe.get("clicked"):
                    other_month_clicks.append({"year": year, "month": month_label, "clicked": False, "error": probe})
                    continue
                try:
                    target = page.evaluate(r"""({year, monthLabel}) => {
                        const visible = (el) => {
                            const r = el.getBoundingClientRect();
                            const st = getComputedStyle(el);
                            return r.width > 0 && r.height > 0 && st.display !== 'none' && st.visibility !== 'hidden';
                        };
                        const norm = (s) => (s || '').replace(/\s+/g, '').trim();
                        const nodes = [...document.querySelectorAll('button,a,[role="button"],div,span,p')]
                            .filter(visible)
                            .map(el => {
                                const r = el.getBoundingClientRect();
                                return {text: norm(el.textContent), tag: el.tagName, cls: (el.className || '').toString(), x: r.x, y: r.y, w: r.width, h: r.height};
                            });
                        // Prefer the narrower year label, not the wide popover column container.
                        const years = nodes.filter(n => n.text === year && n.w < 100).sort((a,b) => a.y-b.y || a.x-b.x);
                        if (!years.length) return {found: false, reason: 'year_not_found'};
                        const yr = years[0];
                        // Prefer the outer 53x32 selectable month cell, not the inner text span.
                        const monthCandidates = nodes
                            .filter(n => n.text === monthLabel && n.w >= 40 && n.h >= 24)
                            .map(n => ({...n, dist: Math.abs((n.x + n.w/2) - (yr.x + yr.w/2))}))
                            .sort((a,b) => a.dist-b.dist || Math.abs(a.w-53)-Math.abs(b.w-53) || a.y-b.y);
                        if (!monthCandidates.length) return {found: false, reason: 'month_not_found'};
                        const t = monthCandidates[0];
                        return {found: true, year, month: monthLabel, x: Math.round(t.x + t.w/2), y: Math.round(t.y + t.h/2), rawX: Math.round(t.x), rawY: Math.round(t.y), w: Math.round(t.w), h: Math.round(t.h), dist: Math.round(t.dist), tag: t.tag, cls: t.cls.slice(0, 80)};
                    }""", {"year": year, "monthLabel": month_label}) or {"found": False}
                    before_hits = len([h for h in collector.hits if h.endpoint == "inquiry" and h.resp_json is not None])
                    if target.get("found"):
                        page.mouse.click(float(target["x"]), float(target["y"]))
                    page.wait_for_timeout(3000)
                    after_hits = len([h for h in collector.hits if h.endpoint == "inquiry" and h.resp_json is not None])
                    other_month_clicks.append({
                        **target,
                        "clicked": bool(target.get("found")),
                        "api_triggered": after_hits > before_hits,
                        "api_hits_before": before_hits,
                        "api_hits_after": after_hits,
                    })
                except Exception as exc:
                    other_month_clicks.append({"year": year, "month": month_label, "clicked": False, "error": str(exc)})
            out["twd_txn_month_clicks"] = month_clicks
            out["twd_txn_other_months_probe"] = other_probe
            out["twd_txn_other_month_clicks"] = other_month_clicks
            out["twd_txn_month_click_endpoints"] = sorted({
                h.endpoint for h in collector.hits[before_month_click_hits:] if h.resp_json is not None
            })
            _log(f"[dbs][twd] month_clicks={month_clicks} other_months={other_month_clicks} endpoints={out['twd_txn_month_click_endpoints']}")
            _log(f"[dbs][twd] detail text_len={len(detail_text)} endpoints={out['twd_account_detail_api_endpoints']}")

            # Return to overview before probing top-nav card-fee shortcut.
            with contextlib.suppress(Exception):
                page.goto("https://internet-banking.dbs.com.tw/digitw/overview", wait_until="domcontentloaded", timeout=15000)
                page.wait_for_timeout(5000)
        except Exception as e:
            out["twd_account_drilldown_error"] = str(e)
            _log(f"[dbs][twd] drilldown probe failed: {e}")

        # 信用卡卡費：使用者實測指出登入後點頂部「繳卡費」才看得到最近一期帳單金額。
        # 這頁是發起繳卡費流程，不是歷史繳款紀錄；只用來補 bill_due/payment_due。
        try:
            before_card_fee_hits = len(collector.hits)
            card_fee_click = page.evaluate(r"""() => {
                const visible = (el) => {
                    const r = el.getBoundingClientRect();
                    const st = getComputedStyle(el);
                    return r.width > 0 && r.height > 0 && st.display !== 'none' && st.visibility !== 'hidden';
                };
                const norm = (s) => (s || '').replace(/\s+/g, '').trim();
                const candidates = [...document.querySelectorAll('a,button,[role="button"],[role="link"],div,span')]
                    .filter(visible)
                    .map(el => {
                        const r = el.getBoundingClientRect();
                        return {el, text: norm(el.textContent), tag: el.tagName, role: el.getAttribute('role') || '', href: el.getAttribute('href') || '', x: r.x, y: r.y, w: r.width, h: r.height};
                    })
                    .filter(x => x.text === '繳卡費')
                    .sort((a, b) => {
                        const rank = (x) => (x.tag === 'A' || x.tag === 'BUTTON' || x.role ? 0 : 1);
                        return rank(a) - rank(b) || (a.w * a.h) - (b.w * b.h);
                    });
                const dump = candidates.slice(0, 8).map(({el, ...rest}) => rest);
                if (!candidates.length) return {clicked: false, reason: 'not_found', dump};
                const c = candidates[0];
                try { c.el.scrollIntoView({block: 'center', inline: 'center'}); } catch (e) {}
                c.el.click();
                return {clicked: true, target: dump[0], dump};
            }""") or {"clicked": False}
            out["dbs_card_fee_click"] = card_fee_click
            page.wait_for_timeout(8000)
            card_fee_text = page.evaluate("() => (document.body.innerText || '').slice(0, 20000)") or ""
            out["dbs_card_fee_page_text"] = card_fee_text
            out["dbs_card_fee_page"] = self._parse_card_fee_page(card_fee_text)
            out["dbs_card_fee_endpoints"] = sorted({
                h.endpoint for h in collector.hits[before_card_fee_hits:] if h.resp_json is not None
            })
            _log(f"[dbs][card_fee] click={card_fee_click.get('clicked')} parsed={out['dbs_card_fee_page']} endpoints={out['dbs_card_fee_endpoints']}")
        except Exception as e:
            out["dbs_card_fee_error"] = str(e)
            _log(f"[dbs][card_fee] probe failed: {e}")

        out["final_url"] = page.url
        out["_all_endpoints"] = sorted({h.endpoint for h in collector.hits if h.resp_json})

        # 完整 dump 所有攔到的 API JSON（給後續 parser 用）
        # 鍵：endpoint name；值：list of resp_json（同 endpoint 可能多次呼叫）
        api_responses: dict = {}
        for h in collector.hits:
            if h.resp_json is None:
                continue
            ep = h.endpoint
            api_responses.setdefault(ep, []).append({
                "url": h.url,
                "method": h.method,
                "status": h.status,
                "resp": h.resp_json,
                "req_body": h.req_body,
            })
        out["api_responses"] = api_responses
        _log(f"[dbs][collect] dump {len(api_responses)} 個 endpoint 的 resp_json")

        _log(f"[dbs][collect] 攔到 {len(out['_all_endpoints'])} 個 endpoint: {out['_all_endpoints'][:15]}")
        return BankCollectResult(**out)


    @staticmethod
    def _parse_card_fee_page(text: str) -> dict:
        """Parse DBS top-nav「繳卡費」page text.

        The page exposes only the current bill amount and due date. It is not a
        payment-history page, so no last_payment_* should be derived from it.
        """
        import re
        out: dict = {}
        compact = re.sub(r"\s+", "", text or "")
        amount = None
        m = re.search(r"信用卡.*?最近一期帳單金額.*?TWD\s*([\d,]+(?:\.\d+)?)", compact)
        if not m:
            m = re.search(r"最近一期帳單金額.*?TWD\s*([\d,]+(?:\.\d+)?)", compact)
        if m:
            try:
                amount = float(m.group(1).replace(",", ""))
            except ValueError:
                amount = None
        if amount is not None:
            out["bill_due_amount"] = amount
            out["currency"] = "TWD"
        due = None
        m = re.search(r"繳款截止日\s*(\d{4})[-/](\d{1,2})[-/](\d{1,2})", text or "")
        if m:
            due = f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
        else:
            m = re.search(r"繳款截止日\s*(\d{1,2})月(\d{1,2})日", text or "")
            if m:
                from datetime import datetime
                due = f"{datetime.now().year:04d}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"
        if due:
            out["payment_due_date"] = due
        return out


def _debug_dir() -> Path:
    from backend.core.store import _data_root
    d = _data_root() / "dbs_collect"
    d.mkdir(parents=True, exist_ok=True)
    return d


if __name__ == "__main__":
    import json
    crawler = DbsCrawler()
    try:
        result = crawler.run(login_url=BASE, headless=False)
    except DbsLoginError as e:
        result = {"error": "login_failed_stop", "detail": str(e)}

    out_file = Path(__file__).resolve().parents[1] / "data" / "dbs_collected.json"
    out_file.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    _log(f"\n[done] 已存: {out_file}")
