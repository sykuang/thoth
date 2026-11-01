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
from backend.core.error_codes import bank_error_id, error_step
from backend.core.sync_errors import raise_crawler_failure, safe_failure_message

BANKS = {"cathay", "ubot", "hsbc", "ctbc", "sinopac", "scsb", "esun", "taishin", "fubon", "dbs", "scb", "linebank", "rakuten"}


def _write_private_json(path: Path, payload: dict) -> None:
    write_private_json(path, payload)


def _remove_private_json(path: Path) -> None:
    path.unlink(missing_ok=True)


def _get_crawler(bank: str):
    if bank not in BANKS:
        raise SystemExit(f"未支援的銀行: {bank}（目前支援: {sorted(BANKS)}）")
    with error_step(bank_error_id(bank, "00-001-001")):
        cls_name = f"{bank.capitalize()}Crawler"
        mod = __import__(f"backend.banks.{bank}", fromlist=[cls_name, "BASE"])
        crawler_cls, base_url = getattr(mod, cls_name), mod.BASE
    with error_step(bank_error_id(bank, "00-002-001")):
        crawler = crawler_cls()
    return crawler, f"{base_url}/mybank/" if bank == "cathay" else base_url


def cmd_sync(args):
    try:
        return _cmd_sync(args)
    except Exception as exc:
        print(f"[sync] 失敗: {safe_failure_message(exc, args.bank)}")
        return 1


def _cmd_sync(args):
    raw = Path(__file__).resolve().parents[1] / "backend" / "data" / f"{args.bank}_collected.json"
    # E.SUN and Rakuten history is customer-bearing; canonical DB only.
    if args.bank in {"esun", "rakuten"}:
        with error_step(bank_error_id(args.bank, "09-007-001")):
            _remove_private_json(raw)
    crawler, login_url = _get_crawler(args.bank)
    print(f"[sync] {args.bank} 登入抓取中…（headless={args.headless}）", file=sys.stderr)
    with error_step(bank_error_id(args.bank, "00-003-001")):
        store = BankStore(args.bank)
    try:
        with error_step(bank_error_id(args.bank, "00-004-001")):
            crawler.configure_transaction_cursor(
                "twd_transactions", store.latest_twd_transaction_dates(),
            )
        with error_step(bank_error_id(args.bank, "00-005-001")):
            crawler.configure_transaction_cursor(
                "account_transactions",
                getattr(store, "latest_account_transaction_dates", lambda: {})(),
            )
        with error_step(bank_error_id(args.bank, "00-006-001")):
            crawler.configure_transaction_cursor(
                "card_billed_transactions", store.latest_card_transaction_dates(),
            )
        with error_step(bank_error_id(args.bank, "09-900-001")):
            result = crawler.run(login_url=login_url, headless=args.headless)
        if result.get("error"):
            raise_crawler_failure(crawler, result)
        data = result.get("data", {})
        if crawler.HISTORY_COVERAGE_REQUIRED:
            with error_step(bank_error_id(args.bank, "07-950-001")):
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
        with error_step(bank_error_id(args.bank, "09-006-001")):
            rules = rules_repo.list_rules(user_id=1, enabled_only=True)
        if not rules:
            rules = sorted(DEFAULT_RULES, key=lambda r: -r.get("priority", 100))
        from backend.core.persist import persist_collected

        with error_step(bank_error_id(args.bank, "08-900-001")):
            delta = persist_collected(args.bank, data, store, rules=rules)
        if args.bank not in {"esun", "rakuten"}:
            with error_step(bank_error_id(args.bank, "09-005-001")):
                _write_private_json(raw, result)
        with error_step(bank_error_id(args.bank, "08-910-001")):
            stats = store.stats()
    except BaseException:
        try:
            store.close()
        except Exception:
            pass  # Cleanup must not replace the original action ID.
        raise
    else:
        with error_step(bank_error_id(args.bank, "08-920-001")):
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
