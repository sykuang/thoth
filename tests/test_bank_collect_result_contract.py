"""Regression tests for the BankCrawler.collect return contract."""
from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.core.base import BankCollectResult, BankCrawler, ResponseCollector


BANK_MODULES = sorted(Path("backend/banks").glob("*.py"))
CARD_BILL_BANK_MODULES = {
    "cathay.py", "ctbc.py", "dbs.py", "esun.py", "fubon.py", "hsbc.py",
    "linebank.py", "rakuten.py", "scb.py", "scsb.py", "sinopac.py",
    "taishin.py", "ubot.py",
}


def _collect_methods(path: Path):
    tree = ast.parse(path.read_text())
    for cls in [n for n in tree.body if isinstance(n, ast.ClassDef)]:
        if not any(
            (isinstance(base, ast.Name) and base.id == "BankCrawler")
            or (isinstance(base, ast.Attribute) and base.attr == "BankCrawler")
            for base in cls.bases
        ):
            continue
        for fn in [n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "collect"]:
            yield cls.name, fn


def test_all_bank_collect_methods_return_bank_collect_result_annotation():
    """All concrete crawlers must expose the shared collect return contract."""
    offenders: list[str] = []
    for path in BANK_MODULES:
        if path.name.startswith("_"):
            continue
        for cls_name, fn in _collect_methods(path):
            annotation = ast.unparse(fn.returns) if fn.returns is not None else None
            if annotation != "BankCollectResult":
                offenders.append(f"{path}:{cls_name}.collect -> {annotation}")
    assert offenders == []


def test_collect_out_fields_are_declared_on_bank_collect_result():
    declared = set(BankCollectResult.__dataclass_fields__)
    offenders: list[str] = []
    for path in BANK_MODULES:
        for cls_name, fn in _collect_methods(path):
            keys: set[str] = set()
            for node in ast.walk(fn):
                if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                    continue
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    if (
                        isinstance(target, ast.Subscript)
                        and isinstance(target.value, ast.Name)
                        and target.value.id == "out"
                        and isinstance(target.slice, ast.Constant)
                        and isinstance(target.slice.value, str)
                    ):
                        keys.add(target.slice.value)
                    elif (
                        isinstance(target, ast.Name)
                        and target.id == "out"
                        and isinstance(node.value, ast.Dict)
                    ):
                        keys.update(
                            key.value for key in node.value.keys
                            if isinstance(key, ast.Constant) and isinstance(key.value, str)
                        )
            for key in sorted(keys - declared):
                offenders.append(f"{path}:{cls_name}.collect out[{key!r}]")
    assert offenders == []


def test_bank_collect_result_has_no_raw_field():
    assert "raw" not in BankCollectResult.__dataclass_fields__

    with pytest.raises(TypeError, match="raw"):
        BankCollectResult(raw={"ok": True})  # type: ignore[call-arg]


def test_bank_collect_result_has_typed_normalized_fields_and_explicit_transitional_fields():
    result = BankCollectResult(
        bank="demo",
        final_url="https://example.com",
        accounts=[{"account_no": "001", "currency": "TWD", "raw_balance_date": "2026-07-03"}],
        cards=[{
            "number": "****7016",
            "payment_due_date": "2026-07-02",
            "statement_close_date": "2026-06-16",
            "last_payment_date": "2026-07-03",
        }],
        twd_txns=[{"datetime": "2026-07-03T12:34:56", "account_date": "2026-07-03"}],
        card_billed_txns=[{"bill_date": "2026-07-02", "date": "2026-06-30", "post_date": "2026-07-01"}],
        card_pending_txns=[{"date": "2026-07-03", "post_date": "2026-07-03"}],
        balance_history=[{"snapshotDate": "2026-07-03", "twdBalance": 100}],
        daily_metrics=[{"category": "demo", "payload": {"ok": True}}],
        telemetry={"duration_ms": 123},
    )

    serialized = result.to_dict()

    assert serialized["final_url"] == "https://example.com"
    assert serialized["cards"] == [{
        "number": "****7016",
        "payment_due_date": "2026-07-02",
        "statement_close_date": "2026-06-16",
        "last_payment_date": "2026-07-03",
    }]
    assert serialized["accounts"][0]["raw_balance_date"] == "2026-07-03"
    assert serialized["twd_txns"][0]["datetime"] == "2026-07-03T12:34:56"
    assert serialized["card_billed_txns"][0]["post_date"] == "2026-07-01"
    assert serialized["card_pending_txns"][0]["date"] == "2026-07-03"
    assert serialized["balance_history"][0]["snapshotDate"] == "2026-07-03"
    assert serialized["daily_metrics"][0]["category"] == "demo"
    assert serialized["_collect_telemetry"] == {"duration_ms": 123}
    assert "bank" not in serialized


def test_bank_collect_result_serializes_pending_fetch_evidence():
    result = BankCollectResult(
        card_transactions_ok=False,
        pending_click_ok=True,
    )

    assert result.to_dict() == {
        "card_transactions_ok": False,
        "pending_click_ok": True,
    }


def test_bank_collect_result_serializes_canonical_card_bill_fact():
    result = BankCollectResult(
        bank="demo",
        card_bill_facts_ok=True,
        card_bill_facts=[{
            "scope": "bank",
            "status": "paid",
            "remaining_due": 0,
            "statement_close_date": "2026-08-01",
            "payment_due_date": "2026-08-20",
            "last_payment_amount": 1000,
            "last_payment_date": "2026-08-10",
        }],
    )

    assert result.to_dict()["card_bill_facts"] == [{
        "scope": "bank",
        "status": "paid",
        "remaining_due": 0,
        "statement_close_date": "2026-08-01",
        "payment_due_date": "2026-08-20",
        "last_payment_amount": 1000,
        "last_payment_date": "2026-08-10",
    }]
    assert result.to_dict()["card_bill_facts_ok"] is True


@pytest.mark.parametrize(
    ("fact", "message"),
    [
        ({"scope": "bank", "status": "paid", "remaining_due": True}, "remaining_due"),
        ({"scope": "bank", "status": "paid", "remaining_due": float("nan")}, "remaining_due"),
        ({"scope": "bank", "status": "paid", "remaining_due": -1}, "remaining_due"),
        ({"scope": "bank", "status": "unpaid", "remaining_due": 100_000_001}, "remaining_due"),
        ({"scope": "bank", "status": "mystery", "remaining_due": 0}, "status"),
        ({"scope": "bank", "status": "paid", "remaining_due": 1}, "conflicts"),
        ({"scope": "card", "status": "paid", "remaining_due": 0}, "card_no"),
        ({"scope": "bank", "status": "paid", "card_no": "****7001", "remaining_due": 0}, "card_no"),
        ({"scope": "bank", "status": "paid", "remaining_due": 0, "payment_due_date": "2026/08/20"}, "payment_due_date"),
        ({"scope": "bank", "status": "paid", "remaining_due": 0, "payment_due_date": "2026-02-29"}, "payment_due_date"),
        ({"scope": "bank", "status": "paid", "remaining_due": 0, "statement_close_date": "2026-02-31"}, "statement_close_date"),
        ({
            "scope": "bank", "status": "paid", "remaining_due": 0,
            "last_payment_amount": 1,
        }, "atomic pair"),
    ],
)
def test_bank_collect_result_rejects_invalid_card_bill_fact(fact, message):
    with pytest.raises(ValueError, match=message):
        BankCollectResult(card_bill_facts_ok=True, card_bill_facts=[fact])


def test_bank_collect_result_rejects_card_bill_facts_without_success_evidence():
    with pytest.raises(ValueError, match="card_bill_facts_ok"):
        BankCollectResult(
            card_bill_facts_ok=False,
            card_bill_facts=[{"scope": "bank", "status": "paid", "remaining_due": 0}],
        )

    with pytest.raises(ValueError, match="at least one fact"):
        BankCollectResult(card_bill_facts_ok=True, card_bill_facts=[])


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"cards": [{"number": "x", "payment_due_date": "2026/07/02"}]}, "payment_due_date"),
        ({"accounts": [{"account_no": "x", "raw_balance_date": "2026/07/03"}]}, "raw_balance_date"),
        ({"twd_txns": [{"datetime": "2026/06/0919:13"}]}, "twd_txns"),
        ({"twd_txns": [{"datetime": "2026-02-31T19:13:00"}]}, "twd_txns"),
        ({"twd_txns": [{"datetime": "2026-08-20T19:13+01:60"}]}, "twd_txns"),
        ({"card_billed_txns": [{"bill_date": "2026/05/17"}]}, "card_billed_txns"),
        ({"card_pending_txns": [{"date": "2026/07/03"}]}, "card_pending_txns"),
        ({"balance_history": [{"snapshotDate": "2026/07/03"}]}, "snapshotDate"),
    ],
)
def test_bank_collect_result_rejects_non_iso_dates(kwargs, message):
    with pytest.raises(ValueError, match=message):
        BankCollectResult(**kwargs)


def test_bank_modules_no_longer_use_legacy_data_or_raw_constructor():
    offenders = []
    forbidden = ("BankCollectResult(data=", "BankCollectResult(raw=")
    for path in BANK_MODULES:
        if path.name.startswith("_"):
            continue
        text = path.read_text()
        if any(token in text for token in forbidden):
            offenders.append(str(path))
    assert offenders == []


def test_credit_card_collectors_publish_canonical_card_bill_contract():
    offenders = []
    for path in BANK_MODULES:
        if path.name not in CARD_BILL_BANK_MODULES:
            continue
        text = path.read_text()
        if "publish_card_bill_facts(" not in text and not (
            "card_bill_facts=" in text and "card_bill_facts_ok=" in text
        ):
            offenders.append(str(path))
    assert offenders == []


@dataclass
class _GoodCrawler(BankCrawler):
    CREDENTIAL_HOSTS = frozenset({"example.com"})

    def _shared_login(self, page) -> bool:
        return True

    def login(self, page) -> bool:
        return True

    def collect(self, page, collector: ResponseCollector) -> BankCollectResult:
        return BankCollectResult(
            final_url="https://example.com",
            cards=[{"number": "****7016"}],
            card_bill_facts_ok=False,
        )

    def _host_filter(self) -> str:
        return "example.com"


@dataclass
class _BadCrawler(BankCrawler):
    CREDENTIAL_HOSTS = frozenset({"example.com"})

    def _shared_login(self, page) -> bool:
        return True

    def login(self, page) -> bool:
        return True

    def collect(self, page, collector: ResponseCollector) -> BankCollectResult:  # type: ignore[override]
        return {"ok": True}  # type: ignore[return-value]

    def _host_filter(self) -> str:
        return "example.com"


@dataclass
class _MissingBillContractCrawler(BankCrawler):
    CREDENTIAL_HOSTS = frozenset({"example.com"})

    def _shared_login(self, page) -> bool:
        return True

    def login(self, page) -> bool:
        return True

    def collect(self, page, collector: ResponseCollector) -> BankCollectResult:
        return BankCollectResult(cards=[])

    def _host_filter(self) -> str:
        return "example.com"


@dataclass
class _BadDateCrawler(BankCrawler):
    CREDENTIAL_HOSTS = frozenset({"example.com"})

    def _shared_login(self, page) -> bool:
        return True

    def login(self, page) -> bool:
        return True

    def collect(self, page, collector: ResponseCollector) -> BankCollectResult:
        return BankCollectResult(cards=[{"number": "****7016", "payment_due_date": "2026/07/02"}])

    def _host_filter(self) -> str:
        return "example.com"


def _fake_fetch(_url: str, *, page_action, **_kwargs):
    page = SimpleNamespace(
        on=lambda *_a, **_kw: None,
        url="https://example.com/app",
        frames=[],
    )
    return page_action(page)


def test_bankcrawler_run_serializes_bank_collect_result(monkeypatch, tmp_path):
    import backend.core.base as base_mod

    monkeypatch.setattr(base_mod, "DATA_ROOT", tmp_path)
    monkeypatch.setattr(base_mod.StealthyFetcher, "fetch", _fake_fetch)

    result = _GoodCrawler(name="good").run("https://example.com", headless=True)

    assert result["data"] == {
        "cards": [{"number": "****7016"}],
        "card_bill_facts_ok": False,
        "final_url": "https://example.com",
    }


def test_bankcrawler_run_rejects_bare_dict_collect_result(monkeypatch, tmp_path):
    import backend.core.base as base_mod

    monkeypatch.setattr(base_mod, "DATA_ROOT", tmp_path)
    monkeypatch.setattr(base_mod.StealthyFetcher, "fetch", _fake_fetch)

    result = _BadCrawler(name="bad").run("https://example.com", headless=True)

    assert "collect_failed: TypeError" in result["error"]
    assert "BankCollectResult" in result["error"]


def test_bankcrawler_run_rejects_missing_card_bill_contract(monkeypatch, tmp_path):
    import backend.core.base as base_mod

    monkeypatch.setattr(base_mod, "DATA_ROOT", tmp_path)
    monkeypatch.setattr(base_mod.StealthyFetcher, "fetch", _fake_fetch)

    result = _MissingBillContractCrawler(name="missing").run(
        "https://example.com", headless=True,
    )

    assert "collect_failed: ValueError" in result["error"]
    assert "card_bill_facts_ok" in result["error"]


def test_bankcrawler_run_surfaces_contract_validation_errors(monkeypatch, tmp_path):
    import backend.core.base as base_mod

    monkeypatch.setattr(base_mod, "DATA_ROOT", tmp_path)
    monkeypatch.setattr(base_mod.StealthyFetcher, "fetch", _fake_fetch)

    result = _BadDateCrawler(name="bad-date").run("https://example.com", headless=True)

    assert "collect_failed: ValueError" in result["error"]
    assert "payment_due_date" in result["error"]
