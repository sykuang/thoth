from __future__ import annotations

import json
import stat
from types import SimpleNamespace

import pytest


def test_cli_private_json_is_0600_and_rejects_symlink(tmp_path):
    from cli import cli

    output = tmp_path / "collected.json"
    output.write_text("old")
    output.chmod(0o644)

    cli._write_private_json(output, {"ok": True})

    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert json.loads(output.read_text()) == {"ok": True}

    target = tmp_path / "target.json"
    target.write_text("keep")
    link = tmp_path / "link.json"
    link.symlink_to(target)
    with pytest.raises(OSError):
        cli._write_private_json(link, {"bad": True})
    assert target.read_text() == "keep"

    hard_link = tmp_path / "hard-link.json"
    hard_link.hardlink_to(target)
    with pytest.raises(RuntimeError, match="single-link regular file"):
        cli._write_private_json(hard_link, {"bad": True})
    assert target.read_text() == "keep"


def test_cli_private_json_failure_preserves_previous_file(tmp_path, monkeypatch):
    from cli import cli

    output = tmp_path / "collected.json"
    output.write_text("keep")

    def fail_after_write(_payload, stream, **_kwargs):
        stream.write("partial")
        raise OSError("simulated write failure")

    monkeypatch.setattr(cli.json, "dump", fail_after_write)
    with pytest.raises(OSError, match="simulated write failure"):
        cli._write_private_json(output, {"ok": True})

    assert output.read_text() == "keep"
    assert stat.S_IMODE(output.stat().st_mode) == 0o600


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
    monkeypatch.setattr(
        cli, "_write_private_json",
        lambda *_args, **_kwargs: pytest.fail("invalid payload must not be written"),
    )
    monkeypatch.setenv("BANK_CRAWLER_HISTORY_MODE", "full")

    with pytest.raises(ValueError, match="history coverage"):
        cli.cmd_sync(SimpleNamespace(bank="sinopac", headless=True))

    assert set(crawler.cursor_domains) == {
        "twd_transactions", "card_billed_transactions",
    }
    assert store.closed is True
