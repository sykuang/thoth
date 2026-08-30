from __future__ import annotations

from datetime import date

import pytest

from backend.core.base import BankCollectResult, BankCrawler, validate_history_coverage


class _Crawler(BankCrawler):
    def login(self, page) -> bool:
        return True

    def collect(self, page, collector) -> BankCollectResult:
        return BankCollectResult(card_bill_facts_ok=False)


def _coverage(*windows: dict, mode: str = "full") -> dict:
    return {
        "mode": mode,
        "domains": [{
            "domain": "twd_transactions",
            "expected": [{
                "identity": "acct-a",
                "start": "2025-08-31",
                "end": "2026-08-30",
            }],
            "windows": list(windows),
        }],
    }


def test_history_coverage_accepts_contiguous_complete_and_explicit_empty_windows():
    summary = validate_history_coverage(_coverage(
        {
            "identity": "acct-a", "start": "2025-08-31", "end": "2026-02-28",
            "status": "complete", "pages": 2,
        },
        {
            "identity": "acct-a", "start": "2026-03-01", "end": "2026-08-30",
            "status": "explicit_empty", "pages": 1,
        },
    ), expected_mode="full", expected_domains=frozenset({"twd_transactions"}))

    assert summary == {
        "ok": True,
        "mode": "full",
        "domains": ["twd_transactions"],
        "identities": 1,
        "windows": 2,
        "start": "2025-08-31",
        "end": "2026-08-30",
    }


@pytest.mark.parametrize(
    "mutation", [
        "gap", "overlap", "starts_early", "ends_late", "missing_identity",
        "failed", "mode", "sensitive_domain", "compact_date", "week_date",
        "whitespace_identity",
    ],
)
def test_history_coverage_fails_closed_on_incomplete_evidence(mutation):
    value = _coverage(
        {
            "identity": "acct-a", "start": "2025-08-31", "end": "2026-02-28",
            "status": "complete", "pages": 1,
        },
        {
            "identity": "acct-a", "start": "2026-03-01", "end": "2026-08-30",
            "status": "complete", "pages": 1,
        },
    )
    if mutation == "gap":
        value["domains"][0]["windows"][1]["start"] = "2026-03-02"
    elif mutation == "overlap":
        value["domains"][0]["windows"][1]["start"] = "2026-02-28"
    elif mutation == "starts_early":
        value["domains"][0]["windows"][0]["start"] = "2025-08-30"
    elif mutation == "ends_late":
        value["domains"][0]["windows"][1]["end"] = "2026-08-31"
    elif mutation == "missing_identity":
        value["domains"][0]["expected"].append({
            "identity": "acct-b",
            "start": "2025-08-31",
            "end": "2026-08-30",
        })
    elif mutation == "failed":
        value["domains"][0]["windows"][1]["status"] = "failed"
    elif mutation == "mode":
        value["mode"] = "incremental"
    elif mutation == "sensitive_domain":
        value["domains"][0]["domain"] = "acct-a"
    elif mutation == "compact_date":
        value["domains"][0]["expected"][0]["start"] = "20250831"
    elif mutation == "whitespace_identity":
        value["domains"][0]["expected"][0]["identity"] = " "
        value["domains"][0]["windows"][0]["identity"] = " "
        value["domains"][0]["windows"][1]["identity"] = " "
    else:
        value["domains"][0]["windows"][0]["start"] = "2025-W35-7"

    with pytest.raises(ValueError, match="history coverage"):
        validate_history_coverage(
            value,
            expected_mode="full",
            expected_domains=frozenset({"twd_transactions"}),
        )


@pytest.mark.parametrize("mutation", ["duplicate", "missing_required"])
def test_history_coverage_requires_exact_unique_adapter_domains(mutation):
    value = _coverage({
        "identity": "acct-a", "start": "2025-08-31", "end": "2026-08-30",
        "status": "complete", "pages": 1,
    })
    expected = frozenset({"twd_transactions"})
    if mutation == "duplicate":
        value["domains"].append(dict(value["domains"][0]))
    else:
        expected = frozenset({"twd_transactions", "card_billed_transactions"})

    with pytest.raises(ValueError, match="history coverage"):
        validate_history_coverage(
            value, expected_mode="full", expected_domains=expected,
        )


def test_history_coverage_accepts_authoritative_empty_domain():
    summary = validate_history_coverage(
        {
            "mode": "full",
            "domains": [{
                "domain": "card_billed_transactions",
                "expected": [],
                "windows": [],
                "empty_window": {
                    "start": "2025-08-31", "end": "2026-08-30",
                    "status": "explicit_empty", "pages": 1,
                },
            }],
        },
        expected_mode="full",
        expected_domains=frozenset({"card_billed_transactions"}),
    )

    assert summary["identities"] == 0
    assert summary["windows"] == 1
    assert summary["start"] == "2025-08-31"
    assert summary["end"] == "2026-08-30"


def test_transaction_window_start_uses_full_floor_or_cursor_overlap(monkeypatch):
    crawler = _Crawler(name="test")
    crawler.transaction_cursors = {"twd_transactions": {"acct-a": date(2026, 8, 20)}}
    floor = date(2025, 8, 31)

    monkeypatch.setenv("BANK_CRAWLER_HISTORY_MODE", "full")
    assert crawler.transaction_window_start("acct-a", floor=floor) == floor

    monkeypatch.setenv("BANK_CRAWLER_HISTORY_MODE", "incremental")
    assert crawler.transaction_window_start(
        "acct-a", floor=floor, overlap_days=7,
    ) == date(2026, 8, 13)
    assert crawler.transaction_window_start("acct-b", floor=floor) == floor


@pytest.mark.parametrize("overlap_days", [-1, 32, True, 1.5])
def test_transaction_window_start_rejects_unbounded_overlap(monkeypatch, overlap_days):
    crawler = _Crawler(name="test")
    crawler.transaction_cursors = {"twd_transactions": {"acct-a": date(2026, 8, 20)}}
    monkeypatch.setenv("BANK_CRAWLER_HISTORY_MODE", "incremental")

    with pytest.raises(ValueError, match="overlap_days"):
        crawler.transaction_window_start(
            "acct-a", floor=date(2025, 8, 31), overlap_days=overlap_days,
        )


def test_transaction_cursor_configuration_is_domain_scoped_and_copied():
    crawler = _Crawler(name="test")
    crawler.transaction_cursors = {}
    source = {"acct-a": date(2026, 8, 20)}

    crawler.configure_transaction_cursor("twd_transactions", source)
    source["acct-a"] = date(2026, 8, 21)

    assert crawler.transaction_start_for("acct-a") == date(2026, 8, 20)
    assert crawler.transaction_start_for("acct-a", domain="card_billed_transactions") is None
