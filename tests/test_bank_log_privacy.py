from __future__ import annotations

import inspect
import re

from backend.banks import ctbc, fubon, linebank, scsb, sinopac, ubot
from backend.server import payment_reminder_notifications, scheduler, sync_job_worker, sync_runner
from backend.server.push.providers import expo, multi, none


def test_touched_bank_logs_never_interpolate_raw_exceptions() -> None:
    raw_exception_log = re.compile(r"_log\(f[^\n]*\{e(?:!r)?(?:[:}])")

    for module in (ctbc, fubon, linebank, scsb, sinopac, ubot):
        assert not raw_exception_log.search(inspect.getsource(module)), module.__name__


def test_sinopac_cli_does_not_log_dynamic_endpoint_paths() -> None:
    source = inspect.getsource(sinopac)
    assert not re.search(r"_log\(f[^\n]*_all_endpoints", source)


def test_sync_push_logs_only_counts_without_tokens_or_tracebacks() -> None:
    source = inspect.getsource(sync_runner._send_sync_notification)
    assert "invalid_tokens" not in source
    assert "getattr(result, \"errors\"" not in source
    assert "logger.exception" not in source

    source = inspect.getsource(sync_runner._send_card_event_notification)
    assert "event.card_no" not in source.split("notifier.send_to_user", 1)[1]
    assert "event.amount" not in source.split("notifier.send_to_user", 1)[1]
    assert "logger.exception" not in source

    source = inspect.getsource(payment_reminder_notifications.dispatch_daily_payment_reminders)
    assert "invalid_tokens" not in source
    assert "getattr(result, \"errors\"" not in source
    assert "logger.exception" not in source

    source = inspect.getsource(multi.MultiNotifier.send_to_token)
    assert ".send_to_token 例外" not in source
    assert "error_type=%s" in source

    source = inspect.getsource(multi.MultiNotifier)
    assert "例外: %s" not in source

    source = inspect.getsource(expo.ExpoPushProvider._send_batch)
    assert "payload.title" not in source
    assert "payload.body" not in source
    assert "_safe_text(resp)" not in source
    assert "type(e).__name__, e" not in source

    source = inspect.getsource(none.NoOpNotifier)
    assert "payload.title" not in source
    assert "payload.body" not in source

    assert "logger.exception" not in inspect.getsource(sync_runner._exec_sync)
    assert "logger.exception" not in inspect.getsource(sync_runner._maybe_send_batch_summary)
    assert "logger.exception" not in inspect.getsource(
        payment_reminder_notifications.dispatch_daily_payment_reminders_for_all_users,
    )
    assert "logger.exception" not in inspect.getsource(scheduler)
    assert "logger.exception" not in inspect.getsource(sync_job_worker)


def test_touched_bank_logs_never_dump_raw_urls_or_endpoint_collections() -> None:
    forbidden = {
        ctbc: ("url={nav_result", "{card_resources}", "url={result.get", "resource: {data.get"),
        linebank: ("transaction url={txn_url}", "endpoint: {out['_all_endpoints']"),
        scsb: ("url={out['overview_url']}", "已跳到 {page.url}", "url={result['url']}",
               "最終 url: {data.get", "Overview url: {data.get", "endpoint: {data.get"),
        ubot: ("url={result.get", "endpoint: {data.get"),
    }

    for module, markers in forbidden.items():
        source = inspect.getsource(module)
        for marker in markers:
            assert marker not in source, (module.__name__, marker)


def test_touched_bank_logs_never_dump_account_amounts() -> None:
    forbidden = {
        ctbc: ("台幣存款餘額:", "額度={cc.get", "應繳={cc.get"),
        ubot: ("本期應繳={cs.get", "總額={dt.get"),
    }

    for module, markers in forbidden.items():
        source = inspect.getsource(module)
        for marker in markers:
            assert marker not in source, (module.__name__, marker)
