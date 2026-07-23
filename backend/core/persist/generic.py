"""Generic dump persist (fallback for banks 沒專屬 persist)。

從 persist.py 拆分而來 (2026-06-17, W persist.py split refactor)。
"""
from __future__ import annotations

from datetime import datetime

from backend.core.store import BankStore


def persist_generic_dump(bank: str, data: dict, store: BankStore, rules: list[dict] | None = None) -> dict:
    """新接入銀行第一輪 collect 的 dump-only persist。

    只把 collect 回的 final_url / main_text / frames preview / _all_endpoints
    當 daily_metric 存進去，不解析業務資料。等之後寫好 parser 再升級到 persist_<bank>。
    """
    today = datetime.now().strftime("%Y-%m-%d")
    delta: dict = {
        "bank": bank,
        "balance_days": 0,
        "twd_txn_new": 0,
        "card_billed_new": 0,
        "card_unbilled": 0,
        "card_current": 0,
        "scope": "dump_only",
    }

    if data.get("final_url"):
        store.put_daily_metric(f"{bank}_final_url", {"url": data["final_url"]}, today)
    if data.get("main_text"):
        store.put_daily_metric(f"{bank}_main_text_preview",
                                {"text": data["main_text"][:2000]}, today)
    if data.get("frames"):
        store.put_daily_metric(f"{bank}_frames",
                                {"count": len(data["frames"]),
                                 "frames": [{"url": f.get("url"), "preview": (f.get("text_preview") or "")[:500]}
                                            for f in data["frames"][:5]]}, today)
    if data.get("_all_endpoints"):
        store.put_daily_metric(f"{bank}_endpoints",
                                {"endpoints": data["_all_endpoints"]}, today)
    store.log_sync(delta)
    return delta
