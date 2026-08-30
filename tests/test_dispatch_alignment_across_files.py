"""Cross-file dispatch consistency: cli ↔ sync_runner ↔ rules ↔ transactions.

歷史教訓:
1. fubon silent regression (commit b21e721) — sync_runner 用 persist_generic_dump
   但 cli 用 persist_fubon, server-mode 完全沒抽 cards/billed.
2. 任何一份 bank 白名單漂移 (新加 / 移除一家), 都可能造成 server-mode 跟 CLI 不同步.

這 test 確保四個檔案的 bank registry / persist function 對映完全一致:
- cli/cli.py args.bank == "X" → persist_X
- backend/server/sync_runner.py bank == "X" → persist_X
- backend/server/sync_runner.py crawler_module_map[bank] = (mod, cls)
- backend/server/routers/sync.py SUPPORTED_BANKS (re-export 自 sync_runner)
- backend/server/routers/rules.py SUPPORTED_BANKS
- backend/server/routers/transactions.py KNOWN_BANKS

任何一處漂移都會在 CI 立刻被抓到.
"""
from __future__ import annotations

import ast
from pathlib import Path

from backend.core.persist import PERSISTERS

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "cli" / "cli.py"
SYNC_RUNNER = ROOT / "backend" / "server" / "sync_runner.py"
RULES = ROOT / "backend" / "server" / "routers" / "rules.py"
TRANSACTIONS = ROOT / "backend" / "server" / "routers" / "transactions.py"
BANK_DATA = ROOT / "backend" / "core" / "bank_data.py"


# Map module alias used in `var = alias.NAME` re-exports → file containing the literal.
# Lets `_extract_set_literal` follow one hop of indirection so that re-export
# patterns like `KNOWN_BANKS = bank_data.KNOWN_BANKS` resolve to the underlying
# literal in `backend/core/bank_data.py` instead of failing with "not found".
_REEXPORT_MAP: dict[str, Path] = {
    "bank_data": BANK_DATA,
}


# ---------------- AST utilities ----------------

def _parse(path: Path) -> ast.AST:
    return ast.parse(path.read_text(encoding="utf-8"))


def _extract_set_literal(tree: ast.AST, var_name: str) -> set[str]:
    """Extract a top-level set/tuple/frozenset literal assigned to var_name.

    Also handles single-hop re-export aliases: when the assignment is
    `var = some_module.var`, this resolves to the underlying literal in the
    module mapped by `_REEXPORT_MAP`. This keeps the alignment test useful
    even after callers switch from inline literals to importing a shared
    source-of-truth tuple.
    """
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name) and tgt.id == var_name:
                    val = node.value
                    # frozenset({...})
                    if isinstance(val, ast.Call) and isinstance(val.func, ast.Name) and val.func.id == "frozenset":
                        if val.args and isinstance(val.args[0], ast.Set):
                            return {e.value for e in val.args[0].elts if isinstance(e, ast.Constant) and isinstance(e.value, str)}
                    # tuple / set / list literal
                    if isinstance(val, (ast.Tuple, ast.Set, ast.List)):
                        return {e.value for e in val.elts if isinstance(e, ast.Constant) and isinstance(e.value, str)}
                    # single-hop re-export: var = module.attr (e.g. bank_data.KNOWN_BANKS)
                    if (
                        isinstance(val, ast.Attribute)
                        and isinstance(val.value, ast.Name)
                        and val.value.id in _REEXPORT_MAP
                    ):
                        target_path = _REEXPORT_MAP[val.value.id]
                        target_attr = val.attr
                        return _extract_set_literal(_parse(target_path), target_attr)
    raise AssertionError(f"找不到 {var_name} 定義")


def _extract_crawler_module_map(tree: ast.AST) -> dict[str, tuple[str, str]]:
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if (
                    isinstance(tgt, ast.Name)
                    and tgt.id in {"crawler_module_map", "_CRAWLER_MODULE_MAP"}
                ):
                    if isinstance(node.value, ast.Dict):
                        out = {}
                        for k, v in zip(node.value.keys, node.value.values):
                            if isinstance(k, ast.Constant) and isinstance(v, ast.Tuple):
                                mod = v.elts[0].value if isinstance(v.elts[0], ast.Constant) else None
                                cls = v.elts[1].value if isinstance(v.elts[1], ast.Constant) else None
                                if k.value and mod and cls:
                                    out[k.value] = (mod, cls)
                        return out
    raise AssertionError("找不到 crawler_module_map")


def _extract_persist_dispatch(tree: ast.AST) -> dict[str, str]:
    """Walk全 tree, 找 If(bank == "X" 或 args.bank == "X"): body 內 persist_X import."""
    out: dict[str, str] = {}
    for branch in ast.walk(tree):
        if not isinstance(branch, ast.If):
            continue
        t = branch.test
        if not isinstance(t, ast.Compare):
            continue
        if len(t.ops) != 1 or not isinstance(t.ops[0], ast.Eq):
            continue
        if len(t.comparators) != 1 or not isinstance(t.comparators[0], ast.Constant):
            continue
        bank_name = t.comparators[0].value
        if not isinstance(bank_name, str):
            continue
        # bank == "X"
        is_bank = (isinstance(t.left, ast.Name) and t.left.id == "bank") or \
                  (isinstance(t.left, ast.Attribute) and t.left.attr == "bank")
        if not is_bank:
            continue
        for stmt in branch.body:
            if isinstance(stmt, ast.ImportFrom) and stmt.module == "backend.core.persist":
                for n in stmt.names:
                    if n.name.startswith("persist_"):
                        out[bank_name] = n.name
                        break
    return out


def _extract_bank_imports(tree: ast.AST) -> dict[str, list[str]]:
    """從 cli/cli.py 抽 from backend.banks.X import (CrawlerCls, ...) 列表."""
    out: dict[str, list[str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("backend.banks."):
            bank = node.module.split(".")[-1]
            out.setdefault(bank, []).extend(n.name for n in node.names)
    return out


# ---------------- Tests ----------------

def test_supported_banks_aligns_across_files():
    """sync_runner.SUPPORTED_BANKS / rules.SUPPORTED_BANKS / transactions.KNOWN_BANKS 必須完全同源."""
    sr = _extract_set_literal(_parse(SYNC_RUNNER), "SUPPORTED_BANKS")
    rules = _extract_set_literal(_parse(RULES), "SUPPORTED_BANKS")
    txns = _extract_set_literal(_parse(TRANSACTIONS), "KNOWN_BANKS")

    assert sr == rules, (
        f"sync_runner vs rules SUPPORTED_BANKS 不一致!\n"
        f"  only in sync_runner: {sr - rules}\n"
        f"  only in rules:       {rules - sr}"
    )
    assert sr == txns, (
        f"sync_runner vs transactions 不一致!\n"
        f"  only in sync_runner: {sr - txns}\n"
        f"  only in transactions: {txns - sr}"
    )


def test_crawler_module_map_aligns_with_supported_banks():
    """crawler_module_map 的 key set 必須等於 SUPPORTED_BANKS."""
    sr_tree = _parse(SYNC_RUNNER)
    supported = _extract_set_literal(sr_tree, "SUPPORTED_BANKS")
    crawler = _extract_crawler_module_map(sr_tree)

    assert set(crawler) == supported, (
        f"crawler_module_map vs SUPPORTED_BANKS 漂移!\n"
        f"  only in map:       {set(crawler) - supported}\n"
        f"  only in supported: {supported - set(crawler)}"
    )


def test_cli_persist_dispatch_aligns_with_server_persist_dispatch():
    """CLI and server both route through the same canonical registry."""
    assert "persist_collected(" in CLI.read_text()
    assert "persist_collected(" in SYNC_RUNNER.read_text()


def test_cli_crawler_imports_align_with_sync_runner_crawler_map():
    """CLI 的 from backend.banks.X import XCrawler 必須跟 sync_runner crawler_module_map 對映一致."""
    cli_imports = _extract_bank_imports(_parse(CLI))
    sr_crawler = _extract_crawler_module_map(_parse(SYNC_RUNNER))

    cli_banks = set(cli_imports)
    sr_banks = set(sr_crawler)

    assert cli_banks == sr_banks, (
        f"cli vs sync_runner 處理的 bank module 集合不一致!\n"
        f"  only in cli: {cli_banks - sr_banks}\n"
        f"  only in sr:  {sr_banks - cli_banks}"
    )

    for bank in sr_banks:
        sr_cls = sr_crawler[bank][1]
        cli_classes = [n for n in cli_imports[bank] if "Crawler" in n]
        assert sr_cls in cli_classes, (
            f"bank={bank} sync_runner 用 {sr_cls}, 但 cli 沒 import 它 (cli imports: {cli_imports[bank]})"
        )


def test_all_supported_banks_have_persist_function():
    """SUPPORTED_BANKS 每家都必須有 shared registry entry."""
    supported = _extract_set_literal(_parse(SYNC_RUNNER), "SUPPORTED_BANKS")
    assert set(PERSISTERS) == supported
