"""Taishin credit card page explorer.

Headful probe：登入台新後進「查詢信用卡明細」頁 (RB0708/0100)，逐一：
  1. dump 完整 mega menu 所有含「卡」/「繳」字的 link，看是否有沒抓到的頁面。
  2. dump 目前月份下拉的完整 options（含 selected index）並試選每個月份。
  3. 找到並點擊「已繳款明細」按鈕，dump 展開後的內容。
  4. 找到並點擊左側/上方所有「信用卡繳款」相關 sub menu。
  5. 每個結果 dump 到 backend/data/taishin_explore/*.txt。

執行：
  set -a; source cli/.env; set +a
  uv run python -m backend.banks.taishin_explore
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.banks.taishin import BASE, TaishinCrawler
from backend.core.base import ResponseCollector

OUT_DIR = Path(__file__).resolve().parents[1] / "data" / "taishin_explore"


def _dump(name: str, text: str) -> Path:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / f"{name}.txt"
    path.write_text(text, encoding="utf-8")
    print(f"[dump] {path} ({len(text)} chars)")
    return path


def _dump_json(name: str, obj) -> Path:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / f"{name}.json"
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[dump] {path}")
    return path


def _find_credit_card_frame(page):
    for f in page.frames:
        if "svc/rwd" in (f.url or ""):
            return f
    return None


def _click_menu_and_dump(page, credit_card_frame, label_hint: str):
    """透過 mega menu 進「查詢信用卡明細」。"""
    # 台新 mega menu：hover 上方「信用卡」→ 點子選項。
    page.mouse.move(1200, 90)
    page.wait_for_timeout(500)
    # click 信用卡 top-level
    try:
        page.evaluate("""
        () => {
          const all = document.querySelectorAll('a, button, li');
          for (const el of all) {
            if ((el.textContent || '').trim() === '信用卡') {
              const r = el.getBoundingClientRect();
              return {x: r.x, y: r.y, w: r.width, h: r.height};
            }
          }
          return null;
        }""")
    except Exception:
        pass


def explore(page, collector: ResponseCollector, crawler: TaishinCrawler):
    print("[explore] step 1: 已登入，dump 首頁 ==")
    _dump("00_home", page.evaluate("() => document.body.innerText.slice(0, 20000)"))

    print("[explore] step 2: 透過 crawler.collect 邏輯進到信用卡明細頁 ==")
    # 重用 crawler.collect() 內同一段導航：hover mega menu → 點「查詢信用卡明細」
    # 直接呼叫既有 collect 太大坨，這裡簡化：跑到 RB0708/0100
    # 台新是 hash-based SPA，直接 set location.hash 最穩
    credit_frame = None
    # 找到含 svc/rwd 的 frame（登入後主 frame）
    for _ in range(20):
        credit_frame = _find_credit_card_frame(page)
        if credit_frame:
            break
        page.wait_for_timeout(500)
    if not credit_frame:
        print("[explore] ❌ 找不到 svc/rwd frame")
        _dump("01_no_frame", page.evaluate("() => document.body.innerText"))
        return

    credit_frame.evaluate("location.hash = '#/RB0708/0100?ts=' + Date.now()")
    page.wait_for_timeout(5000)
    credit_frame = _find_credit_card_frame(page)
    _dump("01_RB0708_initial", credit_frame.evaluate("() => document.body.innerText"))

    print("[explore] step 3: dump 頁面所有 button/link，找沒點過的 =")
    controls = credit_frame.evaluate("""() => {
      const els = document.querySelectorAll('a, button, [role="button"], [onclick]');
      return [...els].map(el => ({
        tag: el.tagName,
        text: (el.textContent || '').trim().slice(0, 60),
        href: el.getAttribute('href') || '',
        onclick: (el.getAttribute('onclick') || '').slice(0, 200),
        classes: el.className.slice(0, 100),
        visible: !!(el.offsetParent),
      })).filter(x => x.text && x.text.length > 0 && x.text.length < 60);
    }""")
    interesting = [c for c in controls if any(k in c["text"] for k in
                    ["繳", "帳單", "明細", "分期", "自動扣", "扣繳", "查詢", "月份", "歷史", "紀錄"])]
    _dump_json("02_controls_interesting", interesting)

    print("[explore] step 4: dump 月份下拉的完整 options =")
    month_dump = credit_frame.evaluate("""() => {
      const sels = [...document.querySelectorAll('select')];
      return sels.map((s, si) => ({
        select_index: si,
        selected_index: s.selectedIndex,
        options: [...s.options].map((o, oi) => ({
          option_index: oi,
          text: (o.textContent || '').trim(),
          value: o.value,
        })),
      }));
    }""")
    _dump_json("03_month_selects", month_dump)

    print("[explore] step 5: 找『已繳款明細』按鈕並嘗試點 =")
    paid_btn = credit_frame.evaluate("""() => {
      const els = document.querySelectorAll('a, button, li, span, div');
      for (const el of els) {
        const t = (el.textContent || '').trim();
        if (t === '已繳款明細' || t === '繳款紀錄' || t === '信用卡繳款記錄') {
          const r = el.getBoundingClientRect();
          return {text: t, x: r.x + r.width/2, y: r.y + r.height/2,
                  tag: el.tagName, onclick: el.getAttribute('onclick') || ''};
        }
      }
      return null;
    }""")
    _dump_json("04_paid_btn", paid_btn or {"error": "not found"})

    if paid_btn and paid_btn.get("x", -1) > 0:
        try:
            credit_frame.evaluate(f"window.scrollTo(0, {paid_btn['y'] - 200})")
            page.wait_for_timeout(500)
            credit_frame.click("text=已繳款明細", timeout=5000)
            page.wait_for_timeout(3000)
            credit_frame = _find_credit_card_frame(page) or credit_frame
            _dump("05_paid_detail", credit_frame.evaluate("() => document.body.innerText"))
        except Exception as e:
            _dump("05_paid_detail_error", f"click failed: {e}")

    print("[explore] step 6: 每個月份 select 一遍，dump 頁面 =")
    for sel_dump in month_dump or []:
        opts = sel_dump.get("options") or []
        # 找月份格式的 select
        if not any("/" in (o.get("text") or "") for o in opts):
            continue
        for opt in opts[:12]:
            text = opt.get("text", "").strip()
            if "/" not in text:
                continue
            try:
                credit_frame.evaluate("location.hash = '#/RB0708/0100?ts=' + Date.now()")
                page.wait_for_timeout(3500)
                credit_frame = _find_credit_card_frame(page) or credit_frame
                credit_frame.locator("select").nth(sel_dump["select_index"]).select_option(
                    index=opt["option_index"]
                )
                page.wait_for_timeout(4500)
                credit_frame = _find_credit_card_frame(page) or credit_frame
                body = credit_frame.evaluate("() => document.body.innerText")
                # 特別找「已繳」關鍵字周圍 500 字
                idx = body.find("已繳款明細")
                snippet = body[max(0, idx - 100):idx + 2500] if idx >= 0 else "（此月無『已繳款明細』字樣）"
                _dump(f"06_month_{text.replace('/', '_')}_full", body)
                _dump(f"06_month_{text.replace('/', '_')}_paid_section", snippet)
            except Exception as e:
                _dump(f"06_month_{text.replace('/', '_')}_error", str(e))
        break  # 只跑第一個看起來像月份的 select

    print("[explore] step 7: 逐一嘗試左側 sub menu（如果有） =")
    left_menu = credit_frame.evaluate("""() => {
      const els = document.querySelectorAll('a[href*="RB"], a[onclick*="RB"], li[onclick*="RB"]');
      return [...els].map(el => ({
        text: (el.textContent || '').trim().slice(0, 40),
        href: el.getAttribute('href') || '',
        onclick: (el.getAttribute('onclick') || '').slice(0, 200),
      })).filter(x => x.text && x.text.length < 40);
    }""")
    _dump_json("07_left_menu", left_menu)

    print("[explore] done. 檔案輸出到:", OUT_DIR)


def main():
    crawler = TaishinCrawler()
    from scrapling.fetchers import StealthyFetcher

    def action(page):
        collector = ResponseCollector()
        # login
        if not crawler.login(page):
            print("[explore] ❌ login failed")
            return {"error": "login_failed"}
        try:
            explore(page, collector, crawler)
        finally:
            # 台新沒有 crawler.logout，直接讓 fetcher 收頁
            pass
        return {"ok": True}

    StealthyFetcher.fetch(
        BASE,
        headless=False,
        network_idle=True,
        wait=2000,
        user_data_dir=str(crawler.session_dir),
        page_action=action,
    )


if __name__ == "__main__":
    main()
