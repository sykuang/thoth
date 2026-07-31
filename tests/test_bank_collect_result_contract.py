"""Regression tests for the BankCrawler.collect return contract."""
from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.core.base import BankCollectResult, BankCrawler, ResponseCollector


BANK_MODULES = sorted(Path("backend/banks").glob("*.py"))


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


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"cards": [{"number": "x", "payment_due_date": "2026/07/02"}]}, "payment_due_date"),
        ({"accounts": [{"account_no": "x", "raw_balance_date": "2026/07/03"}]}, "raw_balance_date"),
        ({"twd_txns": [{"datetime": "2026/06/0919:13"}]}, "twd_txns"),
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


@dataclass
class _GoodCrawler(BankCrawler):
    def login(self, page) -> bool:
        return True

    def collect(self, page, collector: ResponseCollector) -> BankCollectResult:
        return BankCollectResult(final_url="https://example.com", cards=[{"number": "****7016"}])

    def _host_filter(self) -> str:
        return "example.com"


@dataclass
class _BadCrawler(BankCrawler):
    def login(self, page) -> bool:
        return True

    def collect(self, page, collector: ResponseCollector) -> BankCollectResult:  # type: ignore[override]
        return {"ok": True}  # type: ignore[return-value]

    def _host_filter(self) -> str:
        return "example.com"


@dataclass
class _BadDateCrawler(BankCrawler):
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

    assert result["data"] == {"cards": [{"number": "****7016"}], "final_url": "https://example.com"}


def test_bankcrawler_run_rejects_bare_dict_collect_result(monkeypatch, tmp_path):
    import backend.core.base as base_mod

    monkeypatch.setattr(base_mod, "DATA_ROOT", tmp_path)
    monkeypatch.setattr(base_mod.StealthyFetcher, "fetch", _fake_fetch)

    result = _BadCrawler(name="bad").run("https://example.com", headless=True)

    assert "collect_failed: TypeError" in result["error"]
    assert "BankCollectResult" in result["error"]


def test_bankcrawler_run_surfaces_contract_validation_errors(monkeypatch, tmp_path):
    import backend.core.base as base_mod

    monkeypatch.setattr(base_mod, "DATA_ROOT", tmp_path)
    monkeypatch.setattr(base_mod.StealthyFetcher, "fetch", _fake_fetch)

    result = _BadDateCrawler(name="bad-date").run("https://example.com", headless=True)

    assert "collect_failed: ValueError" in result["error"]
    assert "payment_due_date" in result["error"]
