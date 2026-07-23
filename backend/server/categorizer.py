"""Phase 5.1 - Category rule regex engine (with real timeout for ReDoS protection).

Phase 5.1 — 分類規則 regex 引擎（真 timeout 防 ReDoS）。

兩個層次：
  - `safe_match(pattern, text, timeout)`：用第三方 `regex` lib 跑（支援真 timeout）。
    壞 pattern / ReDoS → 回 False，不炸到外面。
  - `categorize(text, rules)`：按 list 順序找第一個 match 的 rule。
    rules 由 caller（rules_repo.list_rules）已按 priority DESC 排好。

W1 修正（2026-06-17）：
  原本走 stdlib `re` + `signal.SIGALRM`，但 SIGALRM 只在 main thread 有效；
  FastAPI sync route 跑在 threadpool worker thread → timeout 完全失效。
  改用 `regex` lib，它的 `regex.search(..., timeout=N)` 是純 Python 層內建
  時間檢查（每幾百 步 backtrack 檢查一次），thread-safe 真有用。

W2 修正：catch 收窄為 `(regex.error, regex.RegexError, TimeoutError)`，
  不再 bare `Exception`，避免吞掉真正的 bug。
"""
from __future__ import annotations


import regex as _regex  # 第三方 lib（W1：真 timeout、thread-safe）

REGEX_TIMEOUT_SEC = 2


_REGEX_SAFE_ERRORS = (_regex.error, TimeoutError)


def safe_match(pattern: str, text: str, timeout: int = REGEX_TIMEOUT_SEC) -> bool:
    """跑 regex 但帶 timeout（避免 ReDoS pattern 卡死 server）。

    回 True / False。壞 regex / timeout 一律 False。其餘例外向上 propagate
    （我們希望真 bug 別被吞）。

    Phase 8.3 (2026-06-18) — IGNORECASE flag:
      預設帶 ``regex.IGNORECASE``，讓 ``TAOBAO`` pattern 也 match ``taobao``、
      ``Lotte`` pattern 也 match ``LOTTE``. 對 default rule 作者就不必為每個
      merchant 都寫大小寫雙寫. 唯一 caveat: 全形大小寫 (Ｔ vs ｔ, Ｗ vs ｗ)
      regex lib **不會** fold — 全形 merchant 仍需大小寫雙寫.
      詳見 wiki [[fullwidth-regex-case-folding-pitfall]].
    """
    if not text or not pattern:
        return False
    try:
        # regex lib 的 timeout 是 float 秒；compile + search 一次完成
        return bool(_regex.search(pattern, text, timeout=float(timeout),
                                  flags=_regex.IGNORECASE))
    except _REGEX_SAFE_ERRORS:
        return False


def categorize_with_excluded(
    text: str | None, rules: list[dict],
) -> tuple[str | None, str | None, bool]:
    """Phase 8.3: 同 categorize_with_sub, 但同時回 auto_excluded flag.

    第一個命中的 rule 決定全部三個值 (category, subcategory, auto_excluded).
    用在 store.upsert_* — 命中標 auto_excluded=1 的 rule (信用卡還款/轉帳/退款/
    回饋等) 後, 該筆 txn 在 stats aggregate 自動 skip income/expense 桶。

    Args:
      text: 交易 description.
      rules: [{ pattern, category, subcategory, auto_excluded, ... }, ...],
             已由 caller 按 priority DESC 排好 + filter enabled=1.
    Returns:
      (category, subcategory, auto_excluded). 無 match → (None, None, False).
    """
    if not text or not rules:
        return (None, None, False)
    for r in rules:
        pattern = r.get("pattern") or ""
        if safe_match(pattern, text):
            sub = r.get("subcategory")
            if sub == "" or sub is None:
                sub = None
            auto_excluded = bool(r.get("auto_excluded", 0))
            return (r.get("category"), sub, auto_excluded)
    return (None, None, False)
