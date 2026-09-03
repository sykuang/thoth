#!/usr/bin/env python3
"""Bank crawler CLI.

銀行爬蟲 CLI。

用法：
  uv run python -m banks.cli sync cathay          # 抓取 + 增量入庫
  uv run python -m banks.cli sync cathay --headless
  uv run python -m banks.cli stats cathay         # 看 DB 各表筆數
  uv run python -m banks.cli txns cathay          # 列台幣交易明細
  uv run python -m banks.cli txns cathay --card    # 列信用卡已出帳明細
  uv run python -m banks.cli show cathay           # 列最新每日快照摘要
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.core.base import validate_history_coverage, write_private_json
from backend.core.store import BankStore

BANKS = {"cathay", "ubot", "hsbc", "ctbc", "sinopac", "scsb", "esun", "taishin", "fubon", "dbs", "scb", "linebank", "rakuten"}


def _write_private_json(path: Path, payload: dict) -> None:
    write_private_json(path, payload)


def _remove_private_json(path: Path) -> None:
    path.unlink(missing_ok=True)


def _get_crawler(bank: str):
    if bank == "cathay":
        from backend.banks.cathay import CathayCrawler, BASE
        return CathayCrawler(), f"{BASE}/mybank/"
    if bank == "ubot":
        from backend.banks.ubot import UbotCrawler, BASE
        return UbotCrawler(), BASE
    if bank == "hsbc":
        from backend.banks.hsbc import HsbcCrawler, BASE
        return HsbcCrawler(), BASE
    if bank == "ctbc":
        from backend.banks.ctbc import CtbcCrawler, BASE
        return CtbcCrawler(), BASE
    if bank == "sinopac":
        from backend.banks.sinopac import SinopacCrawler, BASE
        return SinopacCrawler(), BASE
    if bank == "scsb":
        from backend.banks.scsb import ScsbCrawler, BASE
        return ScsbCrawler(), BASE
    if bank == "esun":
        from backend.banks.esun import EsunCrawler, BASE
        return EsunCrawler(), BASE
    if bank == "taishin":
        from backend.banks.taishin import TaishinCrawler, BASE
        return TaishinCrawler(), BASE
    if bank == "fubon":
        from backend.banks.fubon import FubonCrawler, BASE
        return FubonCrawler(), BASE
    if bank == "dbs":
        from backend.banks.dbs import DbsCrawler, BASE
        return DbsCrawler(), BASE
    if bank == "scb":
        from backend.banks.scb import ScbCrawler, BASE
        return ScbCrawler(), BASE
    if bank == "linebank":
        from backend.banks.linebank import LinebankCrawler, BASE
        return LinebankCrawler(), BASE
    if bank == "rakuten":
        from backend.banks.rakuten import RakutenCrawler, BASE
        return RakutenCrawler(), BASE
    raise SystemExit(f"未支援的銀行: {bank}（目前支援: {sorted(BANKS)}）")


def cmd_sync(args):
    raw = Path(__file__).resolve().parents[1] / "backend" / "data" / f"{args.bank}_collected.json"
    # Rakuten history is DOM-normalized but still customer-bearing; canonical DB only.
    if args.bank == "rakuten":
        _remove_private_json(raw)
    crawler, login_url = _get_crawler(args.bank)
    print(f"[sync] {args.bank} 登入抓取中…（headless={args.headless}）", file=sys.stderr)
    store = BankStore(args.bank)
    try:
        crawler.configure_transaction_cursor(
            "twd_transactions", store.latest_twd_transaction_dates(),
        )
        crawler.configure_transaction_cursor(
            "card_billed_transactions", store.latest_card_transaction_dates(),
        )
        result = crawler.run(login_url=login_url, headless=args.headless)
        if result.get("error"):
            print("[sync] 失敗: crawler_failed")
            return 1
        data = result.get("data", {})
        if crawler.HISTORY_COVERAGE_REQUIRED:
            validate_history_coverage(
                data.get("history_coverage"),
                expected_mode=os.environ.get("BANK_CRAWLER_HISTORY_MODE", "full"),
                expected_domains=crawler.HISTORY_COVERAGE_DOMAINS,
            )


        # 2026-07-28: CLI 原本 13 個分支全都沒傳 rules → category/subcategory 永遠 NULL,
        # 連帶 flow_type/income_category 也拿不到分類訊號 (樂天「存款利息」只能落到
        # income/other 而非 interest_dividend)。server sync_runner 一直有傳 (見
        # sync_runner.py:547), 只有 CLI 漏掉。user_id=1 對齊 BankStore 預設單人模式。
        # server.sqlite 尚未 seed 過該 user 時 fallback 到 DEFAULT_RULES —— CLI 是單人
        # 工具, 沒 rule 就等於整個 taxonomy 失效, 不該靜默降級。
        from backend.server import rules_repo
        from backend.server.seed_rules import DEFAULT_RULES
        rules = rules_repo.list_rules(user_id=1, enabled_only=True)
        if not rules:
            rules = sorted(DEFAULT_RULES, key=lambda r: -r.get("priority", 100))
        from backend.core.persist import persist_collected

        delta = persist_collected(args.bank, data, store, rules=rules)
        if args.bank != "rakuten":
            _write_private_json(raw, result)
        stats = store.stats()
    finally:
        store.close()

    print("\n===== 增量同步結果 =====")
    print(f"  台幣交易    本次新增 {delta.get('twd_txn_new', 0)} 筆")
    print(f"  信用卡已出帳 本次新增 {delta.get('card_billed_new', 0)} 筆")
    print(f"  信用卡未出帳 刷新 {delta.get('card_unbilled', 0)} 筆")
    print(f"  信用卡即時   刷新 {delta.get('card_current', 0)} 筆")
    print(f"  餘額走勢    新增/更新 {delta.get('balance_days', 0)} 天")
    print("\n===== DB 累計庫存 =====")
    for tbl, n in stats.items():
        print(f"  {tbl:22} {n}")
    print(f"\n[db] {store.db_path}")
    return 0


def cmd_stats(args):
    store = BankStore(args.bank)
    stats = store.stats()
    store.close()
    print(f"===== {args.bank} DB 庫存 =====")
    for tbl, n in stats.items():
        print(f"  {tbl:22} {n}")
    print(f"[db] {store.db_path}")
    return 0


def _mask_amt(v):
    if isinstance(v, (int, float)) and abs(v) >= 1:
        return f"{v:,}"
    return v if v is not None else "-"


def cmd_txns(args):
    store = BankStore(args.bank)
    if args.card:
        rows = store.conn.execute(
            "SELECT card_no, bill_date, consume_date, post_date, description, amount, "
            "currency, consume_currency, consume_amount "
            "FROM card_billed_txns ORDER BY consume_date DESC LIMIT ?", (args.limit,)
        ).fetchall()
        print(f"===== {args.bank} 信用卡已出帳明細（最近 {args.limit}）=====")
        print("  消費日 → 入帳日 | 卡號 | 摘要 | 台幣金額 | 原幣別 原幣金額")
        for r in rows:
            cd = r["consume_date"] or r["bill_date"] or "?"
            pd = r["post_date"] or cd
            date_part = f"{cd}" if cd == pd else f"{cd}→{pd}"
            # 外幣：原始幣別非 TWD 且有原幣金額時才顯示
            fx = ""
            if r["consume_currency"] and r["consume_currency"] != "TWD" and r["consume_amount"]:
                fx = f" | {r['consume_currency']} {r['consume_amount']:,}"
            print(f"  {date_part} | {r['card_no']} | {r['description']} | "
                  f"{_mask_amt(r['amount'])} {r['currency'] or ''}{fx}")
    else:
        rows = store.conn.execute(
            "SELECT account_no, txn_datetime, description, expend, income, balance "
            "FROM twd_transactions ORDER BY txn_datetime DESC LIMIT ?", (args.limit,)
        ).fetchall()
        print(f"===== {args.bank} 台幣交易明細（最近 {args.limit}）=====")
        for r in rows:
            io = f"-{_mask_amt(r['expend'])}" if r['expend'] else (f"+{_mask_amt(r['income'])}" if r['income'] else "-")
            print(f"  {r['txn_datetime']} | {r['description']} | {io} | 餘額 {_mask_amt(r['balance'])}")
    store.close()
    return 0


def cmd_show(args):
    store = BankStore(args.bank)
    rows = store.conn.execute(
        "SELECT snapshot_date, category, payload_json FROM daily_metrics "
        "WHERE snapshot_date=(SELECT MAX(snapshot_date) FROM daily_metrics) ORDER BY category"
    ).fetchall()
    if not rows:
        print(f"({args.bank} 尚無快照，請先 sync)")
        store.close()
        return 0
    print(f"===== {args.bank} 最新快照 {rows[0]['snapshot_date']} =====")
    for r in rows:
        payload = json.loads(r["payload_json"])
        print(f"\n[{r['category']}]")
        _print_compact(payload, indent=2)
    store.close()
    return 0


def _print_compact(obj, indent=0):
    pad = " " * indent
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, (dict, list)):
                print(f"{pad}{k}:")
                _print_compact(v, indent + 2)
            else:
                print(f"{pad}{k}: {v:,}" if isinstance(v, (int, float)) and abs(v) >= 1000 else f"{pad}{k}: {v}")
    elif isinstance(obj, list):
        print(f"{pad}({len(obj)} 筆)")
        for item in obj[:5]:
            _print_compact(item, indent + 2)
            print(f"{pad}  ---")


def main():
    ap = argparse.ArgumentParser(prog="bank-crawler", description="銀行爬蟲 CLI")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("sync", help="抓取 + 增量入庫")
    p.add_argument("bank")
    p.add_argument("--headless", action="store_true", help="隱藏瀏覽器視窗")
    p.set_defaults(func=cmd_sync)

    p = sub.add_parser("stats", help="看 DB 各表筆數")
    p.add_argument("bank")
    p.set_defaults(func=cmd_stats)

    p = sub.add_parser("txns", help="列交易明細")
    p.add_argument("bank")
    p.add_argument("--card", action="store_true", help="改列信用卡已出帳明細")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=cmd_txns)

    p = sub.add_parser("show", help="列最新每日快照")
    p.add_argument("bank")
    p.set_defaults(func=cmd_show)

    args = ap.parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
