#!/usr/bin/env python3
"""CTBC (Chinatrust) personal e-banking crawler.

中國信託(CTBC)個人網銀抓取器。

CTBC = Akamai Bot Manager + Angular SPA + IBM MobileFirst 後端。
登入：formcontrolname custIxd/userIxd/pxd（無圖形驗證碼）→ 點登入。
  ⚠️ 若前次未正常登出，會跳「確認訊息」彈窗 → 必須點「確認登入」（否則卡登入頁）。
session 持久化（user_data_dir）→ 首次綁定裝置後免 OTP（實測首次就免）。
登入後 API：/IB/api/adapters/IB_Adapter/resource/{name}（參數加密，走 intercept 攔 JSON）。

設計規範：dump 真值不猜測 → collect 先 dump endpoint，摸清明細 API 再補 parse。
"""
from __future__ import annotations
import contextlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from backend.core.base import BankCollectResult, BankCrawler, ResponseCollector
from backend.banks._login_debug import snapshot as _login_snapshot
from backend.core.creds import CtbcCreds


# W (2026-06-17): ctbc 也統一 raise CtbcLoginError, 跟其他 11 家一致.
# session 還在的 fallback (interstitial / _logged_in) 仍回 True;
# 只在 SEL_ID 找不到 + 不在內銀區 + 登入後仍未進內銀區時 raise.
class CtbcLoginError(RuntimeError):
    """CTBC 登入失敗（絕對失敗，重打會鎖卡）。"""

BASE = "https://www.ctbcbank.com/twrbc/twrbc-general/ot001/010"

SEL_ID = 'input[formcontrolname="custIxd"]'    # 身分證字號
SEL_USER = 'input[formcontrolname="userIxd"]'  # 使用者代號
SEL_PWD = 'input[formcontrolname="pxd"]'       # 網銀密碼
SEL_SUBMIT = "a.btn_submit"


def _close_entry_announcement(page) -> bool:
    """關閉入口頁可見的公告 modal，並確認登入表單已顯示。"""
    clicked = page.evaluate(
        """
        () => {
          const visible = (e) => {
            if (!e || !(e.offsetWidth || e.offsetHeight || e.getClientRects().length)) {
              return false;
            }
            const style = getComputedStyle(e);
            return style.display !== 'none' && style.visibility !== 'hidden';
          };
          const modal = [...document.querySelectorAll('.modal')]
            .find(e => visible(e) && /重要公告/.test(e.innerText || ''));
          const close = modal && [...modal.querySelectorAll('a.btn_close')].find(visible);
          if (!close) return false;
          close.click();
          return true;
        }
        """,
    )
    if not clicked:
        return False
    page.wait_for_timeout(500)
    try:
        page.wait_for_selector(SEL_ID, state="visible", timeout=5000)
    except Exception:
        return False
    return True


# 「確認訊息」彈窗的「確認登入」按鈕（前次未正常登出時跳出）
JS_CONFIRM_LOGIN = (
    "(() => { const b=[...document.querySelectorAll('button,a,[role=button]')]"
    ".find(x=>x.offsetParent!==null && /確認登入|確定|確認/.test((x.textContent||'').trim())"
    "  && (x.textContent||'').trim().length<8); if(b){ b.click(); return true;} return false; })()"
)

# W (2026-06-17): positive signal 4 條件 AND，對齊 SCSB 鐵律
# 1) urlOk: twrbc-home / qu000 (login-after path)
# 2) lenOk: innerText >= 500 (內銀區滿載)
# 3) kw >= 2 (內銀區關鍵字)
# 4) noLoginForm: custIxd / userIxd / pxd 都不可見
# 任一 fail → 視為未登入（即使有 logoutBtn 也信不過）。
JS_LOGGED_IN_POSITIVE = """
() => {
  const url = location.href.toLowerCase();
  const urlOk = /twrbc-home|qu000/.test(url);
  const visible = (e) => {
    if (!e) return false;
    const r = e.getBoundingClientRect();
    const cs = getComputedStyle(e);
    return !!(r.width || r.height || e.getClientRects().length)
      && cs.display !== 'none' && cs.visibility !== 'hidden';
  };
  const noLoginForm = !visible(document.querySelector('input[formcontrolname="custIxd"]'))
    && !visible(document.querySelector('input[formcontrolname="userIxd"]'))
    && !visible(document.querySelector('input[formcontrolname="pxd"]'));
  const body = document.body?.innerText || document.body?.textContent || '';
  const lenOk = body.length >= 500;
  const KW = ['帳戶總覽','我的總覽','資產總額','存款','轉帳','信用卡','登出',
              '台幣存款','外幣存款','基金','投資','貸款','繳費','個人設定','安全'];
  const hit = KW.filter(k => body.includes(k)).length;
  const kwOk = hit >= 2;
  return {
    ok: urlOk && lenOk && kwOk && noLoginForm,
    urlOk, lenOk, kwOk, noLoginForm,
    url: location.href, txt_len: body.length, hit
  };
}
"""


def _log(*a):
    print(*a, file=sys.stderr)


def _filter_valid_ctbc_details(detail_list_raw: list) -> tuple[list[dict], int]:
    """CTBC qu002/011 detailList raw → (filtered, skipped_count).

    Schema validate: 每筆合法 detail 必有 actDtTm (persist 算 txn_datetime 的唯一
    來源, txn_datetime 在 PG 端是 NOT NULL). 缺欄或非 dict → skip + count.

    抽成純函式方便 test (collector 主流程 mock SPA 太重, 此函式不依賴 page/SPA).
    2026-06-22 prod 06-22 04:00 job#152 NotNullViolation 修法.
    """
    out: list[dict] = []
    skipped = 0
    for d in detail_list_raw or []:
        if not isinstance(d, dict):
            skipped += 1
            continue
        if not (d.get("actDtTm") or "").strip():
            skipped += 1
            continue
        out.append(d)
    return out, skipped


# 2026-06-22 (multi-account + m1~m5 拓展) CTBC ebmwResource POST helper
# SPA 用 Angular HttpClient 包裝, 純 fetch() 沒帶 interceptor token 拿到 HTML redirect
# (v5 註解實證). 但 ResponseCollector.auth_token 已攔到 SPA 第一次 auto-fire 的 Bearer
# → 直接帶 Bearer + 從 SPA 已 fire 的 qu002/011 hit 抄 URL/req body 結構, 改 rqData 重打.
_CTBC_MONTHS = ("m0", "m1", "m2", "m3", "m4", "m5")


def _build_qu002_011_post_body(account_id: str, month_type: str, template_body: dict) -> dict:
    """從 SPA 既有 qu002/011 req_body 當模板, 套新 (accountId, type) 算同層 POST body.

    Template body 範例 (collector 從 SPA 自動 fire 那次抄來):
      {"resource":"/twrbc-deposit/qu002/011", "rqData":{"accountId":"...", "type":"m0", ...}}

    回傳同 shape, rqData.accountId / type 改成 caller 指定. 其他欄位 (e.g. encrypt
    flags, locale) 原樣保留 — SPA 怎麼打我們就怎麼打.

    純函式方便 test. month_type 必須 m0~m5 之一.
    """
    if month_type not in _CTBC_MONTHS:
        raise ValueError(f"month_type 必須是 {_CTBC_MONTHS} 之一, got: {month_type!r}")
    if not isinstance(template_body, dict):
        raise ValueError(f"template_body 必須是 dict, got: {type(template_body).__name__}")
    new_body = {**template_body}
    rq = dict(template_body.get("rqData") or {})
    rq["accountId"] = account_id
    rq["type"] = month_type
    new_body["rqData"] = rq
    return new_body


class CtbcCrawler(BankCrawler):
    def __init__(self):
        super().__init__(name="ctbc")
        self.creds = CtbcCreds.load()

    def _host_filter(self) -> str:
        return "ctbcbank.com"

    def _logged_in(self, page) -> bool:
        try:
            r = page.evaluate(JS_LOGGED_IN_POSITIVE)
        except Exception:
            return False
        if isinstance(r, dict):
            ok = bool(r.get("ok"))
            if not ok:
                _log(
                    f"[login] _logged_in=False  "
                    f"urlOk={r.get('urlOk')} lenOk={r.get('lenOk')} "
                    f"kwOk={r.get('kwOk')} noLoginForm={r.get('noLoginForm')} "
                    f"txt_len={r.get('txt_len')} hit={r.get('hit')} "
                    f"url={r.get('url','')[:120]}",
                )
            return ok
        return bool(r)

    def _enter_overview_if_interstitial(self, page) -> bool:
        """CTBC ot001 有時不是登入表單，而是「您已經登入了」中繼頁。

        這代表 server-side session 還在但 SPA 沒進 twrbc-home；必須點「我的總覽」
        進 home，否則 login() 等表單會誤判 login_failed。
        """
        try:
            clicked = page.evaluate(
                """
                () => {
                  const body = document.body?.innerText || document.body?.textContent || '';
                  if (!/您已經登入了|我的總覽/.test(body)) return null;
                  const visible = (e) => {
                    if (!e) return false;
                    const r = e.getBoundingClientRect();
                    const cs = getComputedStyle(e);
                    return !!(r.width || r.height || e.getClientRects().length)
                      && cs.display !== 'none' && cs.visibility !== 'hidden';
                  };
                  const a = [...document.querySelectorAll('a,button,[role=button]')]
                    .find(x => visible(x) && (x.textContent || '').trim() === '我的總覽');
                  if (a) { a.click(); return true; }
                  return false;
                }
                """,
            )
            if clicked:
                _log("[login] 偵測到『您已經登入了』中繼頁，點『我的總覽』進 home")
                page.wait_for_timeout(6000)
                return self._logged_in(page)
        except Exception as e:
            _log(f"[login] 中繼頁處理失敗: {e}")
        return False

    # ---------- 登入 ----------
    def login(self, page) -> bool:
        """CTBC 登入——鐵律 max_attempts=1，失敗 raise CtbcLoginError。

        ⚠️ 一旦點下 SEL_SUBMIT，失敗就 raise CtbcLoginError 中止，**絕不重打**——
        重打多次 CTBC 直接鎖帳號。session 復用 / interstitial fallback 仍 return True。

        Return:
          True:  session 復用 / 過 interstitial / 登入成功
        Raise:
          CtbcLoginError: 登入表單沒出現 + 不在內銀區，或送出後 ~20s 仍未進內銀區
        """
        page.wait_for_timeout(3500)
        if _close_entry_announcement(page):
            _log("[login] 已關閉入口重要公告，登入表單已顯示")
        if self._enter_overview_if_interstitial(page) or self._logged_in(page):
            _log(f"[login] ✅ session 還在，免登入 -> {page.url}")
            return True

        # 等登入表單
        try:
            page.wait_for_selector(SEL_ID, state="visible", timeout=15000)
        except Exception:
            if self._logged_in(page):
                return True
            raise CtbcLoginError(
                f"登入表單 SEL_ID={SEL_ID} 未出現；也不在內銀區；url={page.url}\n"
                f"{_login_snapshot(page)}",
            ) from None

        page.fill(SEL_ID, self.creds.national_id)
        page.wait_for_timeout(300)
        page.fill(SEL_USER, self.creds.user_code)
        page.wait_for_timeout(300)
        page.fill(SEL_PWD, self.creds.password)
        page.wait_for_timeout(300)
        try:
            page.click(SEL_SUBMIT, timeout=8000)
        except Exception:
            page.evaluate(
                "(() => { const a=document.querySelector('a.btn_submit')"
                "||[...document.querySelectorAll('a,button')].find(x=>/登入/.test(x.textContent||'')); if(a) a.click(); })()",
            )
        _log("[login] 已送出，等登入結果…")
        page.wait_for_timeout(5000)

        # 處理「確認訊息」彈窗（前次未正常登出 → 點確認登入）
        # 真相（2026-06-16 root cause）：
        #   - 彈窗只在「server-side ghost session 殘留」時出現，正常 fresh session 沒彈窗
        #   - 上一版 bug: 找不到彈窗就 break + 死等 20s _logged_in → 卡死
        #   - 彈窗出現時機不固定（5~20s），需持續 retry 找
        # 解法：每 2s 重 evaluate 一次，最多 8 次（共 ~16s）。找到就點、點完就 break；
        #   找不到也不 break，繼續輪詢——可能是 fresh session 正常登入中。
        # 真正的根治在 base.run() 的 logout() — 讓 ghost session 不再產生。
        for attempt in range(8):
            if self._logged_in(page):
                break
            if page.evaluate(JS_CONFIRM_LOGIN):
                _log(f"[login] 點了『確認訊息』彈窗 (attempt={attempt+1})")
                page.wait_for_timeout(6000)
                break  # 彈窗只有一次，點完就交給下面 _logged_in loop 確認
            page.wait_for_timeout(2000)

        # 等登入完成（最多 ~20 秒；2026-06-18 revert from 60s — 加 timeout 不是 root cause
        # 真正的雲端失敗證據改寫進 error_msg 由 _login_debug.snapshot() 撈，看 sync_jobs 表）
        for _ in range(20):
            page.wait_for_timeout(1000)
            if self._logged_in(page):
                _log(f"[login] ✅ 成功 -> {page.url}")
                return True
            # OTP 偵測
            try:
                body = page.css_first("body").text or ""
                if any(m in body for m in ["簡訊驗證", "一次性密碼", "動態密碼", "OTP 驗證", "認證碼"]):
                    _log("[login] ⚠️ 撞 OTP，需使用者手動（headful 視窗輸入）")
            except Exception:
                pass

        _log(f"[login] 失敗，url={page.url}")
        raise CtbcLoginError(
            f"登入送出後 ~20s 仍未進內銀區；可能帳密錯或撞 OTP；url={page.url}\n"
            f"{_login_snapshot(page)}",
        )

    # ---------- 抓取 ----------
    def logout(self, page) -> bool:
        """CTBC 登出是 HTML modal 兩段式：點「登出」後還要點「確認」。

        base.logout() 只會點 header 登出，CTBC 會停在「確認訊息 / 確定登出？」modal，
        若不再點確認，server-side session 沒真正結束 → 下次 sync 落入「已經登入了」
        interstitial / 登入表單不出現的 intermittent state。
        """
        import sys as _sys

        clicked = page.evaluate(
            """
            () => {
              const visible = (e) => {
                if (!e) return false;
                const r = e.getBoundingClientRect();
                const cs = getComputedStyle(e);
                return !!(r.width || r.height || e.getClientRects().length)
                  && cs.display !== 'none' && cs.visibility !== 'hidden';
              };
              const btn = document.querySelector('#btnHeaderLogout')
                || [...document.querySelectorAll('a,button,[role=button]')]
                    .find(x => visible(x) && (x.textContent || '').trim() === '登出');
              if (btn && visible(btn)) { btn.click(); return (btn.textContent || '登出').trim(); }
              return null;
            }
            """,
        )
        if not clicked:
            print("[ctbc][logout] ⚠️ 沒找到 header 登出按鈕", file=_sys.stderr)
            return False
        print(f"[ctbc][logout] 已點「{clicked}」，等待確認 modal", file=_sys.stderr)

        confirmed = False
        for attempt in range(8):
            page.wait_for_timeout(1000)
            confirmed_text = page.evaluate(
                """
                () => {
                  const body = document.body?.innerText || document.body?.textContent || '';
                  if (!/確定登出|確認訊息/.test(body)) return null;
                  const visible = (e) => {
                    if (!e) return false;
                    const r = e.getBoundingClientRect();
                    const cs = getComputedStyle(e);
                    return !!(r.width || r.height || e.getClientRects().length)
                      && cs.display !== 'none' && cs.visibility !== 'hidden';
                  };
                  const btn = [...document.querySelectorAll('button,a,[role=button],div,span')]
                    .find(x => visible(x) && (x.textContent || '').trim() === '確認');
                  if (btn) { btn.click(); return (btn.textContent || '').trim(); }
                  return null;
                }
                """,
            )
            if confirmed_text:
                print(f"[ctbc][logout] ✓ 已點登出確認「{confirmed_text}」(attempt={attempt+1})", file=_sys.stderr)
                confirmed = True
                break

        if not confirmed:
            print("[ctbc][logout] ⚠️ 找不到『確定登出？』確認按鈕", file=_sys.stderr)
            return False

        # 等 CTBC 前端把 header 切回「登入」或登入表單，代表 server-side logout 完成。
        for _ in range(12):
            page.wait_for_timeout(1000)
            logged_out = page.evaluate(
                """
                () => {
                  const visible = (e) => {
                    if (!e) return false;
                    const r = e.getBoundingClientRect();
                    const cs = getComputedStyle(e);
                    return !!(r.width || r.height || e.getClientRects().length)
                      && cs.display !== 'none' && cs.visibility !== 'hidden';
                  };
                  const loginBtn = document.querySelector('#btnHeaderLogin');
                  const logoutBtn = document.querySelector('#btnHeaderLogout');
                  const loginForm = document.querySelector('input[formcontrolname="custIxd"]');
                  return visible(loginBtn) || visible(loginForm) || !visible(logoutBtn);
                }
                """,
            )
            if logged_out:
                print(f"[ctbc][logout] ✅ 登出完成 -> {page.url}", file=_sys.stderr)
                return True

        print("[ctbc][logout] ⚠️ 已點確認但頁面仍像登入狀態（best-effort）", file=_sys.stderr)
        return True

    def collect(self, page, collector: ResponseCollector) -> BankCollectResult:
        """登入後抓帳戶彙總 + 台幣帳戶明細。

        ⚠️ CTBC API 機制（2026-06-10 實測摸清）：統一 POST 端點 `IB_Adapter/resource/ebmwResource`，
        body 帶 `resource` 欄位指定功能（像 RPC route），`rqData` 放查詢參數，回明文
        `{sys, code:"0000", rsData:{...}}`。故抓資料**不靠點擊導航**，直接複用前端的 token
        重打 ebmwResource 即可（同 HSBC 直打 API 思路）。

        已驗證 resource：
          /twrbc-home/qu000/010      首頁總覽（含 ebAcctSummaryInq 全帳戶彙總，登入自動載入）
          /twrbc-deposit/qu001/010   台幣存款（twdAcctSummaryResponse 帳號+餘額）
          /twrbc-deposit/qu002/010   台幣存款月份選擇器（dateRanges, 6 個月）
          /twrbc-deposit/qu002/011   台幣存款逐筆交易明細 (2026-06-20 補上)
                                     rqData={accountId, type:"m0"..."m5"}, m0=本月
        """
        out: dict = {}

        page.wait_for_timeout(5000)

        # 1) 首頁總覽彙總（登入後自動載入，攔即可）—— ebAcctSummaryInq 含台幣/信用卡/信貸彙總
        home = self._latest_rsdata(collector, "/twrbc-home/qu000/010")
        out["summary"] = (home or {}).get("ebAcctSummaryInq") if isinstance(home, dict) else None

        # 2) 台幣存款帳戶（點臺幣存款 link 觸發，或直接複用攔到的）
        self._goto_twd_deposit(page)
        page.wait_for_timeout(3000)
        dep = self._latest_rsdata(collector, "/twrbc-deposit/qu001/010")
        out["twd_deposit"] = (dep or {}).get("twdAcctSummaryResponse") if isinstance(dep, dict) else None

        # 2.5) 台幣逐筆交易明細 (2026-06-20: known TODO 補上)
        # SPA route: /twrbc/twrbc-deposit/qu002/010 (date range picker) → fires qu002/011
        # qu002/011 rqData = {accountId, type:"m0"|"m1"|...} (m0=本月, m1=上月, ...)
        # 設計：先 goto qu002/010 載 dateRanges，再 _post_ebmw 各 type
        # gracefully handle: 某月 fail 不擋整個 sync, 累積最多歷史
        out["twd_history"] = self._collect_twd_deposit_history(page, collector, out["twd_deposit"])

        # 3) 信用卡明細 (2026-06-13 升級：分 pending + billed 兩段抓)
        # CTBC 信用卡 mega menu 子選單：
        #   - 「即時消費明細」 → /twrbc-card/qu041/010 = 9 筆即時未入帳 (pending)
        #   - 「帳單明細查詢」 → /twrbc-card/qu0??/010 = 已出帳逐筆 (billed)
        #   - 「未出帳單明細」 → /twrbc-card/qu0??/010 = 已授權未入帳 (unbilled)
        # 攻法：hover「信用卡/點數」→ 點目標子選單 → wait → 攔 ebmwResource API
        card_targets = ["即時消費明細", "帳單明細查詢", "未出帳單明細", "信用卡繳款記錄"]
        nav_logs: list[dict] = []
        for target_text in card_targets:
            nav_result = self._goto_credit_card(page, target_text=target_text)
            nav_logs.append({"target": target_text, **nav_result})
            _log(f"[collect][card-nav] {target_text} → clicked={nav_result.get('clicked')}, "
                 f"url={nav_result.get('after_click_url', '?')[:80]}")
            page.wait_for_timeout(6000)
        out["card_nav_probe"] = nav_logs  # 改成多家紀錄
        out["card_nav_probe_2"] = None     # 舊欄位保留向後相容

        # 2026-06-22 (使用者指示「一頁一頁看」, 之前 navProbe 只試 3 個 hard-code menu,
        # 沒探索其他子選單). 加 mega menu 全 dump probe — hover 信用卡/點數 後, 把
        # 出現的所有 sub-menu link text + href dump 出來. 後續用此資料找「繳款紀錄」
        # 類 menu, ship 0.3.32 hard-code 新 target.
        try:
            card_link = page.locator("a:has-text('信用卡/點數')").first
            if card_link.count() > 0:
                card_link.hover()
                page.wait_for_timeout(1500)
                menu_items = page.evaluate("""() => {
                    const items = [];
                    for (const el of document.querySelectorAll('a, button, [role="menuitem"]')) {
                        const t = (el.textContent || '').trim();
                        if (!t || t.length > 50) continue;
                        const r = el.getBoundingClientRect();
                        if (r.width < 5 || r.height < 5) continue;
                        const cs = window.getComputedStyle(el);
                        if (cs.display === 'none' || cs.visibility === 'hidden') continue;
                        // 只收 mega menu 區內 item (y 在 100-400 之間, 經驗值)
                        if (r.y < 100 || r.y > 500) continue;
                        items.push({
                            tag: el.tagName,
                            text: t,
                            href: el.getAttribute('href') || '',
                            x: Math.round(r.x),
                            y: Math.round(r.y),
                        });
                    }
                    return items;
                }""")
                out["card_mega_menu_dump"] = menu_items
                _log(f"[collect][card-mega-menu-dump] {len(menu_items)} items")
        except Exception as e:
            out["card_mega_menu_dump"] = {"error": str(e)}
            _log(f"[collect][card-mega-menu-dump] ERROR: {e}")

        # 抽所有 creditcard / creditCard / card 相關的 resource
        card_resources = sorted({
            (h.req_body or {}).get("resource", "")
            for h in collector.hits
            if "ebmwResource" in h.url and isinstance(h.req_body, dict)
            and any(k in (h.req_body.get("resource") or "").lower() for k in ("credit", "card", "ccrd", "stmt", "bill"))
        } - {""})
        out["card_resources"] = card_resources
        _log(f"[collect][card-resources] {card_resources}")

        # 把所有 card 相關 resource 的最新 rsData 撈下來
        card_api_dump = {}
        for r in card_resources:
            data = self._latest_rsdata(collector, r)
            if data is not None:
                card_api_dump[r] = data
        out["card_api_dump"] = card_api_dump

        # 最終 url + 截圖（信用卡頁）
        out["card_final_url"] = page.url

        out["_final_url"] = page.url
        out["_all_resources"] = sorted({
            (h.req_body or {}).get("resource", "")
            for h in collector.hits
            if "ebmwResource" in h.url and isinstance(h.req_body, dict)
        } - {""})

        return BankCollectResult(**out)

    @staticmethod
    def _latest_rsdata(collector: ResponseCollector, resource: str):
        """取某 resource 最新成功回應的 rsData（CTBC 格式 {sys, code, rsData}）。"""
        hits = [h for h in collector.hits
                if "ebmwResource" in h.url and isinstance(h.req_body, dict)
                and h.req_body.get("resource") == resource
                and isinstance(h.resp_json, dict) and h.resp_json.get("code") == "0000"]
        if hits:
            return hits[-1].resp_json.get("rsData")
        return None

    def _collect_twd_deposit_history(self, page, collector: ResponseCollector, twd_deposit) -> list:
        """抓台幣交易明細 (qu002/011, m0~m5 × all accounts).

        2026-06-22 升級 (多帳號 + m1~m5):
          1) page.goto qu002/010 → SPA auto-fire qu002/011(accountId=info_list[0], type='m0')
             → collector 撈 m0 hit 當 template (拿 URL 完整路徑 + req_body shape +
                Bearer token)
          2) 對 info_list 每個 account × m0~m5 12 組組合, 用 page.evaluate(fetch)
             帶 collector.auth_token + template URL POST 重打
          3) 同樣套 _filter_valid_ctbc_details schema validate
          4) 每月 detail dedup 由下游 store.upsert_twd_txns 處理 (m0~m5 overlap 必然)

        為何不點月份 button: button selector 變動性高 (Angular SPA 動態 class),
        直接走 HttpClient layer 模仿 SPA 自己的 POST 比 DOM 互動穩定.

        為何不純 fetch(): SPA 用 Angular HttpClient + interceptor 加 Bearer token,
        純 page.evaluate(fetch) 沒過 interceptor 拿 redirect HTML (v5 實證).
        現在我們從 collector.auth_token 取 SPA 攔到的 Bearer 直接帶 → 成功.

        2026-06-22 (root cause fix): collector 是 raw 結構守門員 — 每個 (account, month)
        都過 `_filter_valid_ctbc_details` schema validate. CTBC API 偶有 detail row
        缺 actDtTm (prod 06-22 04:00 job#152 NotNullViolation 實證). 純函式邏輯抽到
        module-level 方便 test.

        回傳 [{account_no, months: {m0: [...], m1: [...], ...}, errors: {...}}].
        """
        history = []
        info_list = (twd_deposit or {}).get("demDepBalSummaryResponse", {}).get("infoList") or []
        if not info_list:
            return history

        # Step 1: 進 SPA route 觸發 auto-fire qu002/011 (with default accountId=info_list[0], type=m0)
        with contextlib.suppress(Exception):
            page.goto("https://www.ctbcbank.com/twrbc/twrbc-deposit/qu002/010",
                      wait_until="domcontentloaded", timeout=15000)
        # SPA bootstrap + HttpClient call 完整 round trip ~3-5s
        page.wait_for_timeout(5000)

        # 從 collector 撈 SPA auto-fire 的 qu002/011 hit, 拿來當之後 POST 的 template
        template_hit = self._latest_qu002_011_hit(collector)
        if template_hit is None:
            _log("[twd-history] qu002/011 template hit 找不到 (SPA 沒 fire?)")
            return history
        template_body = template_hit.req_body if isinstance(template_hit.req_body, dict) else {}
        template_url = template_hit.url  # 完整 URL: https://www.ctbcbank.com/.../ebmwResource

        # Step 2: 對每個 (account, month) 組合迴圈 fetch
        bearer = collector.auth_token  # SPA 攔到的 'Bearer eyJ...'
        if not bearer:
            _log("[twd-history] collector.auth_token 沒攔到 Bearer, 無法主動 POST")
            return history

        for acct in info_list:
            account_id = acct.get("accountId")
            if not account_id:
                continue
            months_data: dict[str, list[dict]] = {}
            errors: dict[str, str] = {}
            for month in _CTBC_MONTHS:
                try:
                    detail_list_raw = self._fetch_qu002_011(
                        page, template_url, template_body, account_id, month, bearer,
                    )
                except Exception as e:
                    errors[month] = f"fetch_error: {e!r}"[:200]
                    continue
                if not isinstance(detail_list_raw, list):
                    errors[month] = "non_list_response"
                    continue
                detail_list, skipped = _filter_valid_ctbc_details(detail_list_raw)
                if skipped > 0:
                    _log(f"[twd-history] account={account_id[:8]}*** {month} "
                         f"skipped {skipped} raw detail rows missing actDtTm")
                if detail_list:
                    months_data[month] = detail_list
                else:
                    errors[month] = "empty_detail_list"

            total = sum(len(v) for v in months_data.values())
            history.append({
                "account_no": account_id,
                "months": months_data,
                "errors": errors,
            })
            _log(f"[twd-history] account={account_id[:8]}*** "
                 f"total={total} months={list(months_data.keys())} errors={list(errors.keys())}")

        return history

    @staticmethod
    def _latest_qu002_011_hit(collector: ResponseCollector):
        """從 collector 撈最近一次成功的 /twrbc-deposit/qu002/011 ApiHit (含 URL + req_body)."""
        for h in reversed(collector.hits):
            if ("ebmwResource" not in h.url
                or not isinstance(h.req_body, dict)
                or h.req_body.get("resource") != "/twrbc-deposit/qu002/011"
                or not isinstance(h.resp_json, dict)
                or h.resp_json.get("code") != "0000"):
                continue
            return h
        return None

    @staticmethod
    def _fetch_qu002_011(page, template_url: str, template_body: dict,
                         account_id: str, month_type: str, bearer: str) -> list:
        """主動 POST qu002/011 (帶 Bearer + 套新 accountId/type) → 回 detailList.

        page.evaluate 內 fetch() 走的是 page context, cookies 自帶 (SPA 同 origin),
        Bearer 從 collector 攔到的 auth_token 帶上 → 過 interceptor 成功拿 JSON.

        失敗 (HTTP 非 200 / 非 JSON / code != "0000" / 無 detailList) 一律 raise,
        caller 接 except 寫進 errors[month].
        """
        post_body = _build_qu002_011_post_body(account_id, month_type, template_body)
        # 用 JSON.stringify 在 JS 端建 body, 避免 Python f-string 對 JSON 內字串引號轉義踩雷
        # post_body 用 json.dumps 轉成 str, 在 JS 端 JSON.parse 還原為 object
        body_json = json.dumps(post_body)
        url_json = json.dumps(template_url)
        bearer_json = json.dumps(bearer)
        js = f"""
        (async () => {{
            const url = {url_json};
            const body = {body_json};
            const bearer = {bearer_json};
            const resp = await fetch(url, {{
                method: 'POST',
                headers: {{
                    'content-type': 'application/json',
                    'authorization': bearer,
                }},
                credentials: 'include',
                body: JSON.stringify(body),
            }});
            if (!resp.ok) return {{__error__: 'http_' + resp.status}};
            const ct = resp.headers.get('content-type') || '';
            if (!ct.includes('json')) return {{__error__: 'non_json'}};
            const data = await resp.json();
            if (data.code !== '0000') return {{__error__: 'code_' + data.code}};
            const rs = data.rsData || {{}};
            return rs.detailList || [];
        }})()
        """
        result = page.evaluate(js)
        if isinstance(result, dict) and result.get("__error__"):
            raise RuntimeError(str(result["__error__"]))
        if not isinstance(result, list):
            raise RuntimeError(f"unexpected_result_type_{type(result).__name__}")
        return result

    def _goto_twd_deposit(self, page):
        """點首頁「臺幣存款」A.link 進存款頁（觸發 /twrbc-deposit/qu001/010）。"""
        with contextlib.suppress(Exception):
            page.evaluate(
                "(() => { const a=[...document.querySelectorAll('a.link')]"
                ".find(x=>x.offsetParent!==null && (x.textContent||'').trim()==='臺幣存款');"
                " if(a){ a.click(); return true;}"
                " const a2=[...document.querySelectorAll('a')].find(x=>x.offsetParent!==null"
                "  && (x.textContent||'').trim()==='臺幣存款' && !/轉帳/.test(x.textContent||'')); if(a2) a2.click(); })()",
            )

    def _goto_credit_card(self, page, target_text: str = "消費明細") -> dict:
        """設計規範：每家都要抓信用卡明細。

        CTBC top nav「信用卡/點數」是 hover trigger（mega menu pattern），
        click 不 navigate 只展開子選單。策略：
          1) 用 Playwright hover() 觸發 mega menu
          2) 等 dropdown 出現（前後 HTML 長度比較 / 等 200ms～1s）
          3) 找展開後新增的子選單 link（target_text exact match）
          4) click 子選單真實 link → 觸發 SPA route 切換

        2026-06-13 升級：target_text 參數化，支援多家子選單迭代抓
        （pending=即時消費明細 / billed=帳單明細查詢 / unbilled=未出帳單明細）
        """
        log: list[str] = []
        try:
            # Step 1: 找「信用卡/點數」<a>
            card_link = page.locator("a:has-text('信用卡/點數')").first
            if card_link.count() == 0:
                log.append("no top-nav 信用卡/點數")
                return {"clicked": None, "log": log}

            # Step 2: hover 觸發 mega menu
            card_link.hover()
            page.wait_for_timeout(1200)
            log.append("hovered top-nav, mega menu should be visible")

            # Step 3: 找 target_text 子選單（exact match，避免「帳單明細查詢」誤撞「未出帳單明細」）
            target = page.evaluate("""(wanted) => {
                for (const el of document.querySelectorAll('a, button, [role="menuitem"]')) {
                    const t = (el.textContent || '').trim();
                    if (t !== wanted) continue;
                    const r = el.getBoundingClientRect();
                    if (r.width < 5 || r.height < 5) continue;
                    const cs = window.getComputedStyle(el);
                    if (cs.display === 'none' || cs.visibility === 'hidden') continue;
                    return {tag: el.tagName, text: t,
                            x: Math.round(r.x), y: Math.round(r.y),
                            w: Math.round(r.width), h: Math.round(r.height)};
                }
                return null;
            }""", target_text)

            if not target:
                log.append(f"no sub-menu match {target_text!r}")
                return {"clicked": None, "log": log}

            log.append(f"will click: {target!r}")
            tlocator = page.locator(f"a:has-text('{target_text}')").first
            if tlocator.count() == 0:
                page.mouse.click(target["x"] + target["w"] // 2, target["y"] + target["h"] // 2)
                log.append("clicked by coords")
            else:
                tlocator.click()
                log.append("clicked by locator")

            page.wait_for_timeout(2500)
            log.append(f"after_click_url={page.url}")
            return {"clicked": target["text"], "after_click_url": page.url, "log": log}
        except Exception as e:
            log.append(f"ERROR: {e}")
            return {"error": str(e), "log": log}


if __name__ == "__main__":
    import json
    crawler = CtbcCrawler()
    result = crawler.run(login_url=BASE, headless=False)
    out_file = Path(__file__).resolve().parents[1] / "data" / "ctbc_collected.json"
    out_file.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    _log(f"\n[done] 已存: {out_file}")
    if result.get("error"):
        _log(f"  error: {result['error']} url={result.get('final_url')}")
    data = result.get("data", {})
    summ = data.get("summary") or {}
    _log(f"\n  台幣存款餘額: {(summ.get('twdDepositSummary') or {}).get('totalCurrentBal')}")
    cc = summ.get("creditCardSummary") or {}
    _log(f"  信用卡: 額度={cc.get('quota')} 可用={cc.get('availBal')} 應繳={cc.get('unpaidStmt')} 繳款日={cc.get('pmtExpDt')}")
    dep = data.get("twd_deposit") or {}
    demdep = (dep.get("demDepBalSummaryResponse") or {}).get("infoList") if isinstance(dep, dict) else None
    _log(f"  台幣帳戶: {demdep}")
    _log(f"  攔到的 resource: {data.get('_all_resources', [])}")
