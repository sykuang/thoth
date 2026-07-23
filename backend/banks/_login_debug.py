"""登入失敗診斷 helper — 雲端環境失真用。

設計動機 (2026-06-18, after timeout-revert lesson):
  雲端 (Azure Container Apps) sync 失敗看不到 page 真實內容。
  銀行爬蟲 login 失敗只能猜 (帳密 / OTP / 鎖卡 / WAF / IP ban / 渲染慢)，
  以前靠加 timeout (60s) 想 mask 過去 — 完全錯方向。

  真正解法 = 失敗瞬間 dump 4 樣最小證據，原地塞進 LoginError message:
    1. final URL (login 是否真的有跳轉？)
    2. document.title (頁面標題反應 server-side state)
    3. body.innerText 前 N 字 (錯誤訊息 / 警示 banner / OTP 提示直接看得到)
    4. visible alert/error element text (.alert / [role=alert] / .error 等)

  訊息會被 sync_runner 包進 sync_jobs.error_msg (PG/SQLite TEXT 欄無容量限制)，
  使用者 / 臣妾打開 PG sync_jobs 表就能看到 cloud 真實 page 狀態，免起 cloud shell / blob。

  禁加 screenshot dump 到 blob — 多一個 infra 依賴、Azure Files 又踩 SQLite 鎖 lesson。
  純 text snapshot 足夠 ≥90% 病因定位 (帳密錯 → 看 alert text；OTP → 看 body keyword；
  WAF → 看 title 或 url 跳到 captcha 頁；IP ban → 看 403 訊息)。

Usage:
    from backend.banks._login_debug import snapshot

    # 在 raise LoginError 前 call snapshot 把證據塞訊息
    raise CtbcLoginError(
        f"登入送出後 ~20s 仍未進內銀區；可能帳密錯或撞 OTP\\n"
        f"{snapshot(page)}"
    )

  輸出格式 (single block，多行；safe to embed in error message)：
    === login fail evidence ===
    url: <final url>
    title: <document.title>
    body[:800]: <first 800 chars of innerText>
    alerts: [<text of .alert / [role=alert] / .error elements>]
    ===
"""

from __future__ import annotations

# 內文截長 — 銀行錯誤訊息通常前 800 字內，超過就是雜訊
_BODY_LIMIT = 800
_ALERT_LIMIT = 5  # 最多列 5 條 alert，多就是 noise
_ALERT_TEXT_LIMIT = 200  # 每條 alert 最多 200 字


def snapshot(page) -> str:
    """Snapshot 當前 page 4 樣 evidence 回 single string block。

    所有 evaluate 都 try/except 包住 — 出證據時 page 可能已死、navigation in-flight，
    一點 JS error 不該蓋掉 root cause LoginError。
    """
    lines = ["=== login fail evidence ==="]

    # 1. URL
    try:
        lines.append(f"url: {page.url}")
    except Exception as e:
        lines.append(f"url: <unavailable: {e}>")

    # 2. document.title
    try:
        title = page.evaluate("document.title") or "(empty)"
        lines.append(f"title: {title}")
    except Exception as e:
        lines.append(f"title: <unavailable: {e}>")

    # 3. body.innerText 前 N 字
    try:
        body = page.evaluate(
            "document.body && (document.body.innerText || document.body.textContent) || ''",
        ) or ""
        # 壓掉多餘空白讓 800 字塞得進有意義內容
        body_clean = " ".join(body.split())
        lines.append(f"body[:{_BODY_LIMIT}]: {body_clean[:_BODY_LIMIT]}")
    except Exception as e:
        lines.append(f"body[:{_BODY_LIMIT}]: <unavailable: {e}>")

    # 4. visible alert / error elements
    try:
        alerts = page.evaluate(
            r"""
            () => {
              const selectors = [
                '.alert', '[role=alert]', '.error', '.error-message',
                '.warning', '.notice', '.notification',
                '.text-danger', '.text-error', '.message-error',
              ];
              const seen = new Set();
              const out = [];
              for (const sel of selectors) {
                for (const el of document.querySelectorAll(sel)) {
                  const r = el.getBoundingClientRect();
                  if (!(r.width || r.height)) continue;  // hidden
                  const cs = getComputedStyle(el);
                  if (cs.display === 'none' || cs.visibility === 'hidden') continue;
                  const txt = (el.innerText || el.textContent || '').trim();
                  if (!txt || seen.has(txt)) continue;
                  seen.add(txt);
                  out.push(txt);
                  if (out.length >= 20) return out;
                }
              }
              return out;
            }
            """,
        ) or []
        # 每條截 200 字 + 最多 5 條
        trimmed = [a[:_ALERT_TEXT_LIMIT] for a in alerts[:_ALERT_LIMIT]]
        lines.append(f"alerts: {trimmed}")
    except Exception as e:
        lines.append(f"alerts: <unavailable: {e}>")

    lines.append("===")
    return "\n".join(lines)
