from __future__ import annotations

from types import SimpleNamespace

import pytest


def test_cli_configures_both_cursors_and_validates_before_persist(monkeypatch):
    from cli import cli

    class FakeCrawler:
        HISTORY_COVERAGE_REQUIRED = True
        HISTORY_COVERAGE_DOMAINS = frozenset({"twd_transactions"})
        cursor_domains = []

        def configure_transaction_cursor(self, domain, cursor):
            self.cursor_domains.append(domain)

        def run(self, *, login_url, headless):
            return {"data": {"card_bill_facts_ok": False}}

    class FakeStore:
        closed = False

        def __init__(self, bank):
            pass

        def latest_twd_transaction_dates(self):
            return {}

        def latest_card_transaction_dates(self):
            return {}

        def close(self):
            self.closed = True

    crawler = FakeCrawler()
    store = FakeStore("sinopac")
    monkeypatch.setattr(cli, "_get_crawler", lambda bank: (crawler, "https://example.com"))
    monkeypatch.setattr(cli, "BankStore", lambda bank: store)
    monkeypatch.setenv("BANK_CRAWLER_HISTORY_MODE", "full")

    with pytest.raises(ValueError, match="history coverage"):
        cli.cmd_sync(SimpleNamespace(bank="sinopac", headless=True))

    assert set(crawler.cursor_domains) == {
        "twd_transactions", "card_billed_transactions",
    }
    assert store.closed is True
