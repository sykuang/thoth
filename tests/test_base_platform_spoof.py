from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

from backend.core.base import BankCollectResult, BankCrawler, ResponseCollector


@dataclass
class _StubCrawler(BankCrawler):
    def login(self, page) -> bool:  # pragma: no cover
        return True

    def collect(self, page, collector: ResponseCollector) -> BankCollectResult:  # pragma: no cover
        return BankCollectResult()

    def _host_filter(self) -> str:
        return "example.com"


def test_base_crawler_inherits_complete_macos_platform_spoof(monkeypatch, tmp_path):
    import backend.core.base as base_mod

    monkeypatch.setattr(base_mod, "DATA_ROOT", tmp_path)
    kwargs = _StubCrawler(name="stub")._build_fetch_kwargs()
    init_script = Path(kwargs["init_script"])

    try:
        assert "Macintosh; Intel Mac OS X" in kwargs["useragent"]
        assert kwargs["extra_headers"]["sec-ch-ua-platform"] == '"macOS"'
        assert kwargs["extra_headers"]["sec-ch-ua-platform-version"] == '"15.0.0"'
        assert kwargs["locale"] == "zh-TW"
        script = init_script.read_text()
        assert "navigator, 'platform'" in script
        assert "navigator, 'userAgentData'" in script
        assert "navigator, 'appVersion'" in script
    finally:
        for cleanup in kwargs["__cleanups__"]:
            cleanup()

    assert not init_script.exists()


def test_bank_crawlers_do_not_override_base_platform_spoof():
    fetch_hooks = {
        "FETCH_USERAGENT",
        "FETCH_EXTRA_HEADERS",
        "FETCH_LOCALE",
        "FETCH_INIT_SCRIPT",
    }
    offenders = []

    for path in sorted(Path("backend/banks").glob("*.py")):
        tree = ast.parse(path.read_text())
        for node in tree.body:
            if not isinstance(node, ast.ClassDef):
                continue
            if not any(isinstance(base, ast.Name) and base.id == "BankCrawler" for base in node.bases):
                continue
            for statement in node.body:
                target = statement.target if isinstance(statement, ast.AnnAssign) else None
                targets = statement.targets if isinstance(statement, ast.Assign) else []
                names = [target, *targets]
                for name in names:
                    if isinstance(name, ast.Name) and name.id in fetch_hooks:
                        offenders.append(f"{path}:{node.name}.{name.id}")

    assert offenders == []
