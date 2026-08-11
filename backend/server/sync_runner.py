"""Phase 1 - Sync runner (background thread + sync_jobs state machine).

Phase 1 — Sync runner（背景 thread + sync_jobs state machine）。

策略：
  - `run_sync_job(user_id, bank, headless)` 同步 INSERT queued、開 daemon thread、立刻回 job_id
  - thread 跑 `_exec_sync(job_id)`：UPDATE running → 取 user_id+bank → 設 env BANK_CRAWLER_USER_ID
    → dispatch 對應 BankCrawler & persist_* → UPDATE done|failed
  - 任何 exception 都 catch、寫 error_msg、status=failed（thread 不該炸到外面）

WebSocket 推播留給 T1.8；Phase 1 只有 polling /sync/jobs/{id}。

⚠️ 鐵律：同一 thread 內設 env 而非 process-wide，避免污染其他 job。
       做法：暫存舊值 → 設新值 → 跑完還原。Python threading 沒有 thread-local env，
       所以 Phase 1 用 lock 序列化 dispatch（一次只跑一個 job，與真實 Scrapling 串行化一致）。

Plan B B5 (2026-06-19): server DB sync_jobs 表全境改走 SyncJobsRepo, 不再寫 raw SQL。
"""
from __future__ import annotations

import json
import os
import threading
import traceback

from backend.server import sync_jobs_repo
from backend.server.dashboard_cache import clear_dashboard_cache

# Push notification taps must target Expo Router file-system routes, not stale
# pseudo routes like /sync or /cards. Query metadata stays as separate data keys.
CARDS_TAB_ROUTE = "/(tabs)/cards"

# 白名單：與 cli/cli.py 的 BANKS 一致（雖然不 import cli 避免 boot 連環）
SUPPORTED_BANKS = frozenset({
    "cathay", "ubot", "hsbc", "ctbc", "sinopac", "scsb", "esun", "taishin", "fubon",
    "dbs", "scb", "linebank", "rakuten",
})

# Phase 1 全域 dispatch lock — 同一時刻只跑一個 job（Scrapling 並非真 thread-safe）
_dispatch_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run_sync_job(user_id: int, bank: str, headless: bool = True, *, batch_id: int | None = None) -> int:
    """[Legacy] INSERT queued → 開 daemon thread → 立刻回 job_id。

    L5-1 起新 caller 應改用 `run_sync_job_for_account(account_id)`, 走
    bank_accounts + bank_credentials_v2 路徑。本 fn 仍能用, sync_runner 會
    自動找該 user 的「預設」account 跑 (有的話); 沒有就 fallback to legacy
    BANK_CRAWLER_USER_ID 走 v1 表。

    2026-06-23: 加 `batch_id` — `/sync/all` 跟 scheduler 帶進來, 收尾走 batch
    summary push (取代每家銀行各推 sync_done). 單支路徑不帶 → legacy 單則 push.
    """
    bank = bank.lower()
    if bank not in SUPPORTED_BANKS:
        raise ValueError(f"unknown bank: {bank!r}; supported: {sorted(SUPPORTED_BANKS)}")
    job_id = sync_jobs_repo.queue(user_id=user_id, bank=bank, batch_id=batch_id)

    t = threading.Thread(target=_exec_sync, args=(job_id,), daemon=True)
    t.start()
    return job_id


def run_sync_job_for_account(
    account_id: int,
    headless: bool = True,
    *,
    batch_id: int | None = None,
) -> int:
    """[L5-1] INSERT queued → 開 daemon thread, 走 account_id 路徑。

    從 bank_accounts 表讀 (user_id, bank), 帶入 sync_jobs 的 account_id 欄位;
    daemon thread 會設 BANK_CRAWLER_ACCOUNT_ID env, 讓 BankCreds.load() 走
    from_account() 路徑取 v2 表的 cred。

    2026-06-23: 加 `batch_id` — `/sync/all` 跟 scheduler 帶進來, 收尾走 batch
    summary push (取代每家銀行各推 sync_done). 單支路徑不帶 → legacy 單則 push.
    """
    from backend.server.creds_store import AccountsRepo

    acct = AccountsRepo().get(account_id)
    if acct is None:
        raise ValueError(f"account_id {account_id} not found")
    if acct.bank not in SUPPORTED_BANKS:
        raise ValueError(f"unknown bank: {acct.bank!r}; supported: {sorted(SUPPORTED_BANKS)}")
    job_id = sync_jobs_repo.queue(
        user_id=acct.user_id, bank=acct.bank, account_id=account_id,
        batch_id=batch_id,
    )

    t = threading.Thread(target=_exec_sync, args=(job_id,), daemon=True)
    t.start()
    return job_id


def get_job(job_id: int) -> dict | None:
    """單 job 狀態（給 polling / WebSocket / 測試用）。"""
    return sync_jobs_repo.get(job_id)


def list_recent_jobs(user_id: int, limit: int = 50) -> list[dict]:
    """近 N 筆 job（per user）。"""
    return sync_jobs_repo.list_recent_for_user(user_id, limit)


# ---------------------------------------------------------------------------
# Internal: 跑在 daemon thread 裡
# ---------------------------------------------------------------------------

def _exec_sync(job_id: int) -> None:
    """thread entry：UPDATE running → dispatch → UPDATE done|failed。

    所有 exception 都 catch；thread 不該炸到外面。

    L5-1: 若 job 有 account_id, 設 BANK_CRAWLER_ACCOUNT_ID env (新路徑);
          否則 fallback to legacy BANK_CRAWLER_USER_ID env (老路徑)。
    """
    # 1. 撈 job & 標 running
    job = get_job(job_id)
    if job is None:
        return  # job 不在了（被刪除？）—— silent return
    user_id = job["user_id"]
    bank = job["bank"]
    account_id = job.get("account_id")  # L5-1: 新欄, 老 job 為 None
    batch_id = job.get("batch_id")  # 2026-06-23: batch 內 job → skip 個別 sync_done

    # L14 (2026-06-23 使用者指示): sync 前 snapshot 該 user 全部 cards 的
    # (bill_due_amount, last_payment_date) — sync 後 diff 偵測「新帳單」/「新繳款」
    # 帳單／繳款除 HSBC 外按整戶事實合併；HSBC 保留逐卡
    # (除了既有 sync_done push 之外).
    cards_before: list = []
    try:
        from backend.server.card_events import snapshot_cards
        cards_before = snapshot_cards(bank=bank, user_id=user_id)
    except Exception:
        # snapshot 失敗不擋 sync — 就當沒 baseline 比對
        cards_before = []

    # 2. 設 thread 級 env 並 dispatch（用 lock 序列化）
    # 重要：status='running' 必須等拿到 _dispatch_lock 後才寫。
    # scheduler / sync-all 會一次排多個 daemon thread，但 Scrapling dispatch 只能串行；
    # 若排隊等 lock 的 job 先標 running，/sync/jobs 的 stale sweep 會把後段銀行
    # （常見是永豐 sinopac）誤判為 stuck >15min 而 failed。單獨 sync 不會排隊，所以不踩。
    old_user_id = os.environ.get("BANK_CRAWLER_USER_ID")
    old_account_id = os.environ.get("BANK_CRAWLER_ACCOUNT_ID")
    summary: dict | None = None
    error: str | None = None
    try:
        with _dispatch_lock:
            sync_jobs_repo.mark_running(job_id)
            if account_id is not None:
                # L5-1 新路徑: 走 from_account, v2 表
                os.environ["BANK_CRAWLER_ACCOUNT_ID"] = str(account_id)
                # user_id 也設, 給 rules_repo / categorize 用
                os.environ["BANK_CRAWLER_USER_ID"] = str(user_id)
            else:
                # Legacy: 走 from_db, v1 表
                os.environ["BANK_CRAWLER_USER_ID"] = str(user_id)
                os.environ.pop("BANK_CRAWLER_ACCOUNT_ID", None)
            try:
                summary = _dispatch_crawler_and_persist(bank, user_id=user_id, headless=True)
            finally:
                # 還原 env，避免污染後續 thread / process
                if old_user_id is None:
                    os.environ.pop("BANK_CRAWLER_USER_ID", None)
                else:
                    os.environ["BANK_CRAWLER_USER_ID"] = old_user_id
                if old_account_id is None:
                    os.environ.pop("BANK_CRAWLER_ACCOUNT_ID", None)
                else:
                    os.environ["BANK_CRAWLER_ACCOUNT_ID"] = old_account_id
    except Exception as e:
        error = f"{type(e).__name__}: {e}\n{traceback.format_exc()}"

    # 3. 寫回 DB
    if error is None:
        # Persist 已落地後先清 process-local aggregate cache；frontend 看見 done
        # 才會 refetch，不能讓 refetch 又拿到同步前的 30 秒舊值。
        clear_dashboard_cache(user_id)
        sync_jobs_repo.mark_done(
            job_id,
            json.dumps(summary, ensure_ascii=False) if summary else "{}",
        )
        # L11 (2026-06-22): push notification on sync success.
        # 失敗一律吞 — push 失敗絕不該擋 sync 結果寫回。
        # 2026-06-23: batch_id 為非 None → skip 個別 sync_done, 改走 batch summary
        # (避免「同步全部」推 12 則噪音, 使用者 Plan A).
        if batch_id is None:
            _send_sync_notification(user_id=user_id, bank=bank, ok=True, summary=summary)

        # L14: 偵測新帳單 + 新繳款；非 HSBC 的整戶繳款由 diff 層合併。
        try:
            from backend.server.card_events import diff_snapshots, snapshot_cards
            cards_after = snapshot_cards(bank=bank, user_id=user_id)
            events = diff_snapshots(cards_before, cards_after)
            for ev in events:
                _send_card_event_notification(user_id=user_id, event=ev)
        except Exception:
            import logging
            logging.getLogger("backend.sync.push").exception(
                "[push] card event detection failed user_id=%s bank=%s",
                user_id, bank,
            )
    else:
        sync_jobs_repo.mark_failed(job_id, error[:8000])
        # 失敗一律個別推 — 失敗不能漏 (使用者同意), batch 內也照推
        _send_sync_notification(
            user_id=user_id, bank=bank, ok=False, error_brief=_brief_error(error),
        )

    # 2026-06-23: batch 收尾 — 不論 ok/fail, 最後一個 job 都該 trigger batch summary
    # (若是 batch 內最後一個失敗 job, 沒人 trigger 就漏推; claim 是 atomic 的, 不會 double-fire).
    if batch_id is not None:
        _maybe_send_batch_summary(batch_id=batch_id, user_id=user_id)


def _send_sync_notification(
    user_id: int,
    bank: str,
    ok: bool,
    summary: dict | None = None,
    error_brief: str | None = None,
) -> None:
    """L11: 通知 user 同步結果。失敗一律吞 — push 失敗絕不該影響 sync。

    PUSH_PROVIDER=none (default) 時是 no-op,開源 user 不會受影響。

    2026-06-22 (登入 OK 但通知沒收到 debug): 把整段 silent fail 換成 logger.info/warning,
    這樣 prod log 可以追到「push 真的有被觸發了嗎」/「provider 噴什麼錯」。
    任何例外仍吞 — 但會 logger.exception 記下來。
    """
    import logging
    logger = logging.getLogger("backend.sync.push")
    try:
        from backend.server.push import NotificationPayload, get_notifier
        bank_label = _BANK_LABELS.get(bank, bank)
        if ok:
            title = f"{bank_label} 同步完成"
            body = _format_sync_summary(summary)
            data = {"deep_link": CARDS_TAB_ROUTE, "kind": "sync_done", "bank": bank}
        else:
            title = f"{bank_label} 同步失敗"
            body = error_brief or "未知錯誤,點開查看詳情"
            data = {"deep_link": CARDS_TAB_ROUTE, "kind": "sync_failed", "bank": bank}
        payload = NotificationPayload(
            title=title, body=body, data=data,
            category="sync_done" if ok else "sync_failed",
        )
        notifier = get_notifier()
        logger.info(
            "[push] dispatch user_id=%s bank=%s ok=%s notifier=%s",
            user_id, bank, ok, notifier.__class__.__name__,
        )
        result = notifier.send_to_user(user_id=user_id, payload=payload)
        logger.info(
            "[push] result user_id=%s bank=%s delivered=%s failed=%s errors=%s invalid=%s",
            user_id, bank,
            getattr(result, "delivered_count", "?"),
            getattr(result, "failed_count", "?"),
            getattr(result, "errors", "?"),
            getattr(result, "invalid_tokens", "?"),
        )
    except Exception:
        # 任何 push 例外都吞 — 但 log 下來給後續 debug 用
        logger.exception("[push] notification dispatch failed user_id=%s bank=%s", user_id, bank)


def _maybe_send_batch_summary(*, batch_id: int, user_id: int) -> None:
    """2026-06-23 (Plan A): atomic 收尾, 拿到 row 的呼者推一則 batch summary push.

    流程:
      1. claim_for_notification → None 就 return (還沒收完 / 已被別人推過)
      2. 撈該 batch 全部 sync_jobs, 分 done/failed/in-flight
         (in-flight 理論上是 0, 因為 claim 已 gate, 但防 race 仍處理)
      3. compose title/body, 推給該 user
      4. 任何 exception 都 log + 吞 (push 失敗不該擋 mark_done 落地)
    """
    import logging
    logger = logging.getLogger("backend.sync.push")
    try:
        from backend.server import sync_batches_repo, sync_jobs_repo as _jobs_repo
        from backend.server.push import NotificationPayload, get_notifier

        claimed = sync_batches_repo.claim_for_notification(batch_id)
        if claimed is None:
            return  # 還沒收完, 或別 thread 搶到了

        jobs = _jobs_repo.list_by_batch(batch_id)
        done_jobs = [j for j in jobs if j["status"] == "done"]
        failed_jobs = [j for j in jobs if j["status"] == "failed"]
        total = claimed["total_jobs"]

        # Compose body
        ok_count = len(done_jobs)
        fail_count = len(failed_jobs)
        all_ok = fail_count == 0 and ok_count > 0
        all_fail = ok_count == 0 and fail_count > 0

        if all_ok:
            title = "同步全部完成"
            txn_total = _sum_txn_count(done_jobs)
            if txn_total > 0:
                body = f"{ok_count} 家完成 · 共 {txn_total} 筆"
            else:
                body = f"{ok_count} 家完成"
            kind_str = "sync_all_done"
        elif all_fail:
            title = "同步全部失敗"
            body = f"0/{total} 家成功 · 請查看詳情"
            kind_str = "sync_all_failed"
        else:
            title = "同步全部完成"
            fail_label = _format_failed_banks(failed_jobs)
            body = f"{ok_count}/{total} 家完成 · 失敗: {fail_label}"
            kind_str = "sync_all_done"

        payload = NotificationPayload(
            title=title,
            body=body,
            data={
                "deep_link": CARDS_TAB_ROUTE,
                "kind": kind_str,
                "batch_id": str(batch_id),
            },
            category=kind_str,
        )
        notifier = get_notifier()
        logger.info(
            "[push] batch dispatch user_id=%s batch_id=%s ok=%d fail=%d total=%d notifier=%s",
            user_id, batch_id, ok_count, fail_count, total, notifier.__class__.__name__,
        )
        result = notifier.send_to_user(user_id=user_id, payload=payload)
        logger.info(
            "[push] batch result user_id=%s batch_id=%s delivered=%s failed=%s",
            user_id, batch_id,
            getattr(result, "delivered_count", "?"),
            getattr(result, "failed_count", "?"),
        )

        # Payment reminders must be evaluated after fresh card data lands, not
        # only by the 09:00 global sweep. If the daily sweep runs before the
        # user's scheduled sync (or before a manual sync discovers a new bill),
        # the reminder would otherwise be missed until the next day. The
        # payment-reminder dispatcher has per-day dedupe, so this is safe even
        # when the global sweep already sent something earlier.
        try:
            from backend.server import payment_reminder_notifications as prn

            tz = os.environ.get("PAYMENT_REMINDER_TZ", "Asia/Taipei")
            reminder_result = prn.dispatch_daily_payment_reminders(
                user_id=user_id, tz=tz,
            )
            logger.info(
                "[push] post-sync payment reminders user_id=%s batch_id=%s result=%s",
                user_id, batch_id, reminder_result,
            )
        except Exception:
            logger.exception(
                "[push] post-sync payment reminders failed user_id=%s batch_id=%s",
                user_id, batch_id,
            )
    except Exception:
        logger.exception(
            "[push] batch summary failed user_id=%s batch_id=%s",
            user_id, batch_id,
        )


def _sum_txn_count(done_jobs: list[dict]) -> int:
    """累加 batch 內所有 done job 的 txn 數 (容忍各 bank summary shape 不同)."""
    total = 0
    for j in done_jobs:
        raw = j.get("result_summary") or "{}"
        try:
            summary = json.loads(raw) if isinstance(raw, str) else raw
        except (ValueError, json.JSONDecodeError):
            continue
        if not isinstance(summary, dict):
            continue
        # summary 結構各 bank 略有差異 — _format_sync_summary 同款邏輯
        if "txn_count" in summary:
            try:
                total += int(summary["txn_count"])
            except (TypeError, ValueError):
                pass
        else:
            for key in ("deposit_txn_count", "card_txn_count"):
                if key in summary:
                    try:
                        total += int(summary[key] or 0)
                    except (TypeError, ValueError):
                        pass
    return total


def _format_failed_banks(failed_jobs: list[dict]) -> str:
    """失敗銀行清單 label, 超過 3 家用「A、B、C 等 N 家」."""
    labels: list[str] = []
    for j in failed_jobs:
        bank_code = j.get("bank") or "unknown"
        labels.append(_BANK_LABELS.get(bank_code, bank_code))
    if len(labels) <= 3:
        return "、".join(labels)
    head = "、".join(labels[:3])
    return f"{head} 等 {len(labels)} 家"


def _send_card_event_notification(*, user_id: int, event) -> None:
    """L14 (2026-06-23): 推 new_bill / new_payment 事件通知.

    Push 失敗一律吞 — 不該擋 sync mark_done.

    event: card_events.CardEvent (避免 import 循環, 用 duck type)
    """
    import logging
    logger = logging.getLogger("backend.sync.push")
    try:
        from backend.server.card_events import mask_card_no
        from backend.server.push import NotificationPayload, get_notifier

        bank_label = _BANK_LABELS.get(event.bank, event.bank)
        card_disp = event.nickname or (mask_card_no(event.card_no) if event.card_no else None)
        amt_str = _fmt_amount(event.amount)

        if event.kind == "new_bill":
            title = f"{bank_label} 新帳單"
            if event.prev_amount:
                bill_text = f"本期應繳 {amt_str} (上次 {_fmt_amount(event.prev_amount)})"
            else:
                bill_text = f"本期應繳 {amt_str}"
            body = f"{card_disp} {bill_text}" if card_disp else bill_text
            data = {
                "deep_link": CARDS_TAB_ROUTE,
                "kind": "new_bill",
                "bank": event.bank,
                "amount": str(event.amount),
            }
            if event.card_no:
                data["card_no"] = event.card_no
            category = "new_bill"
        elif event.kind == "new_payment":
            title = f"{bank_label} 已繳款"
            body = f"{event.date} 繳款 {amt_str}"
            if card_disp:
                body = f"{card_disp} {body}"
            data = {
                "deep_link": CARDS_TAB_ROUTE,
                "kind": "new_payment",
                "bank": event.bank,
                "amount": str(event.amount),
                "date": event.date or "",
            }
            if event.card_no:
                data["card_no"] = event.card_no
            category = "new_payment"
        else:
            logger.warning("[push] unknown event kind: %s", event.kind)
            return

        payload = NotificationPayload(title=title, body=body, data=data, category=category)
        notifier = get_notifier()
        logger.info(
            "[push] card event dispatch user_id=%s kind=%s bank=%s card=%s amount=%s",
            user_id, event.kind, event.bank, event.card_no, event.amount,
        )
        result = notifier.send_to_user(user_id=user_id, payload=payload)
        logger.info(
            "[push] card event result user_id=%s kind=%s delivered=%s failed=%s",
            user_id, event.kind,
            getattr(result, "delivered_count", "?"),
            getattr(result, "failed_count", "?"),
        )
    except Exception:
        logger.exception(
            "[push] card event notification failed user_id=%s kind=%s",
            user_id, getattr(event, "kind", "?"),
        )


def _fmt_amount(v: float) -> str:
    """格式化金額: 1234.0 → 'NT$1,234'; 1234.56 → 'NT$1,234.56'."""
    if v == int(v):
        return f"NT${int(v):,}"
    return f"NT${v:,.2f}"


def _format_sync_summary(summary: dict | None) -> str:
    """把 summary dict 攤成一行可讀文字。"""
    if not summary:
        return "已更新"
    parts: list[str] = []
    # summary 結構各 bank 略有差異,只挑幾個常見鍵
    if "txn_count" in summary:
        parts.append(f"{summary['txn_count']} 筆")
    elif "deposit_txn_count" in summary or "card_txn_count" in summary:
        d = summary.get("deposit_txn_count") or 0
        c = summary.get("card_txn_count") or 0
        if d:
            parts.append(f"存款 {d} 筆")
        if c:
            parts.append(f"信用卡 {c} 筆")
    if not parts:
        return "已更新"
    return "、".join(parts)


def _brief_error(error: str) -> str:
    """錯誤字串太長,只取第一行 100 字。"""
    first_line = error.split("\n", 1)[0]
    return first_line[:100]


_BANK_LABELS = {
    "cathay": "國泰世華",
    "ubot": "聯邦銀行",
    "hsbc": "匯豐銀行",
    "ctbc": "中國信託",
    "sinopac": "永豐銀行",
    "scsb": "上海商銀",
    "esun": "玉山銀行",
    "taishin": "台新銀行",
    "fubon": "富邦銀行",
    "dbs": "星展銀行",
    "scb": "渣打銀行",
    "linebank": "LINE Bank",
    "rakuten": "樂天國際銀行",
}


def _dispatch_crawler_and_persist(bank: str, user_id: int, headless: bool = True) -> dict:
    """Phase 1：真正跑 Scrapling crawler + persist_* 入庫。

    回傳 summary dict（會 JSON 序列化到 sync_jobs.result_summary）。
    任何例外（login fail / network / parse）由 caller `_exec_sync` 抓並記 error_msg。

    測試會 monkey-patch 這個 fn 來避免真去登入銀行（rate limit / 鎖帳）。

    Phase 5.1：撈 user 的分類 rules 並傳給 persist_*，讓寫入時 hook categorize。

    Phase C (2026-06-17)：user_id 為必填——所有 INSERT 透過 BankStore(bank, user_id)
    stamp 給每一 row, 達成 multi-tenant row-level isolation。
    """
    # 延遲 import：避免 server bootstrap 階段就拖 scrapling 進來
    from backend.core.store import BankStore
    from backend.server import rules_repo

    # Phase 5.1：撈 user 的 enabled rules（rules_repo 已按 priority DESC 排序）
    rules: list[dict] | None = None
    try:
        rules = rules_repo.list_rules(user_id=user_id, enabled_only=True)
    except Exception:
        rules = None

    crawler_module_map = {
        "cathay":  ("backend.banks.cathay",  "CathayCrawler",  None),
        "ubot":    ("backend.banks.ubot",    "UbotCrawler",    None),
        "hsbc":    ("backend.banks.hsbc",    "HsbcCrawler",    None),
        "ctbc":    ("backend.banks.ctbc",    "CtbcCrawler",    None),
        "sinopac": ("backend.banks.sinopac", "SinopacCrawler", None),
        "scsb":    ("backend.banks.scsb",    "ScsbCrawler",    None),
        "esun":    ("backend.banks.esun",    "EsunCrawler",    None),
        "taishin": ("backend.banks.taishin", "TaishinCrawler", None),
        "fubon":   ("backend.banks.fubon",   "FubonCrawler",   None),
        "dbs":     ("backend.banks.dbs",     "DbsCrawler",     None),
        "scb":     ("backend.banks.scb",     "ScbCrawler",     None),
        "linebank": ("backend.banks.linebank", "LinebankCrawler", None),
        "rakuten":  ("backend.banks.rakuten",  "RakutenCrawler",  None),
    }
    if bank not in crawler_module_map:
        raise ValueError(f"unknown bank: {bank!r}")

    mod_name, cls_name, _ = crawler_module_map[bank]
    mod = __import__(mod_name, fromlist=[cls_name, "BASE"])
    crawler_cls = getattr(mod, cls_name)
    base_url = mod.BASE

    crawler = crawler_cls()
    # cathay 特例（cli 也是這樣寫）
    login_url = f"{base_url}/mybank/" if bank == "cathay" else base_url

    result = crawler.run(login_url=login_url, headless=headless)
    if result.get("error"):
        raise RuntimeError(f"crawler error: {result['error']} (url={result.get('final_url')})")
    data = result.get("data", {})

    store = BankStore(bank, user_id=user_id)
    try:
        from backend.core.persist import persist_collected

        delta = persist_collected(bank, data, store, rules=rules)
        stats = store.stats()
    finally:
        store.close()

    return {"delta": delta, "stats": stats}
