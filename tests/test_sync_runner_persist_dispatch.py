"""驗證 sync_runner 對每家銀行 dispatch 對 persist_X 函式 — 防止 fubon
退化回 persist_generic_dump 之類的迷宮 bug 重現.

歷史教訓 (commit 修 fubon 那次): sync_runner.py:293-294 之前是
  elif bank == 'fubon':
      from backend.core.persist import persist_generic_dump
      delta = persist_generic_dump(bank, data, store, rules=rules)
害 server-mode fubon 跑完 0 cards / 0 billed (只 dump 3 metric);
CLI 卻早就用 persist_fubon 拿到 3 cards + 1 billed.

這 test 確保 sync_runner._dispatch_crawler_and_persist 的銀行 → persist 映射
精準對齊預期, 任何「忘記補上 persist_X」/「誤用 persist_generic_dump」
都會在 CI 立刻被抓到.
"""
from __future__ import annotations

import ast
from pathlib import Path


SYNC_RUNNER = Path(__file__).resolve().parents[1] / "backend" / "server" / "sync_runner.py"

# 預期每家銀行該用哪個 persist_X 函式 (對齊 CLI cli/cli.py 的選擇)
EXPECTED_PERSIST = {
    "cathay":  "persist_cathay",
    "ubot":    "persist_ubot",
    "hsbc":    "persist_hsbc",
    "ctbc":    "persist_ctbc",
    "sinopac": "persist_sinopac",
    "scsb":    "persist_scsb",
    "esun":    "persist_esun",
    "taishin": "persist_taishin",
    "fubon":   "persist_fubon",     # 重點: 不可退化為 persist_generic_dump
    "dbs":     "persist_dbs",
    "scb":     "persist_scb",
    "linebank": "persist_linebank",
    "rakuten":  "persist_rakuten",
}


def _extract_persist_map() -> dict[str, str]:
    """從 sync_runner.py 解出 bank → persist_X 對應關係.

    策略: ast.walk 整個 _dispatch_crawler_and_persist function, 找所有 If node
    test 形如 bank == "xxx" 的, body 裡找 from backend.core.persist import persist_X.
    """
    src = SYNC_RUNNER.read_text(encoding="utf-8")
    tree = ast.parse(src)

    func = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_dispatch_crawler_and_persist":
            func = node
            break
    assert func is not None, "找不到 _dispatch_crawler_and_persist"

    mapping: dict[str, str] = {}

    for sub in ast.walk(func):
        if not isinstance(sub, ast.If):
            continue
        test = sub.test
        # bank == "xxx"
        if not (isinstance(test, ast.Compare)
                and isinstance(test.left, ast.Name)
                and test.left.id == "bank"
                and len(test.ops) == 1
                and isinstance(test.ops[0], ast.Eq)
                and len(test.comparators) == 1
                and isinstance(test.comparators[0], ast.Constant)
                and isinstance(test.comparators[0].value, str)):
            continue
        bank_name = test.comparators[0].value
        # 找 body 裡的 import persist_X
        for stmt in sub.body:
            if isinstance(stmt, ast.ImportFrom):
                for alias in stmt.names:
                    if alias.name.startswith("persist_"):
                        mapping[bank_name] = alias.name
                        break

    return mapping


def test_fubon_uses_persist_fubon_not_generic_dump():
    """關鍵 regression: fubon 必須 dispatch 到 persist_fubon, 不可用 persist_generic_dump."""
    mapping = _extract_persist_map()
    assert "fubon" in mapping, f"sync_runner 沒處理 fubon: 看到的 mapping={mapping}"
    assert mapping["fubon"] == "persist_fubon", (
        f"fubon dispatch 錯! 應該 persist_fubon, 實際 {mapping['fubon']}. "
        f"persist_generic_dump 只 dump metric 不抽 cards/billed/pending → 線上 fubon 帳本會空."
    )


def test_all_banks_have_correct_persist_dispatch():
    """全 12 家銀行的 persist dispatch 都對."""
    mapping = _extract_persist_map()
    missing = [b for b in EXPECTED_PERSIST if b not in mapping]
    assert not missing, f"sync_runner 缺 dispatch: {missing}"

    wrong = {
        b: (got, want)
        for b, want in EXPECTED_PERSIST.items()
        if (got := mapping.get(b)) and got != want
    }
    assert not wrong, f"sync_runner dispatch 錯: {wrong}"


def test_no_bank_falls_back_to_generic_dump():
    """SUPPORTED_BANKS 任何一家都不該走 persist_generic_dump
    (那只給未實作專屬 persist 的銀行用, 12 家全都有 persist_X)."""
    mapping = _extract_persist_map()
    fallbacks = {b: p for b, p in mapping.items() if p == "persist_generic_dump"}
    assert not fallbacks, (
        f"這些銀行不該用 persist_generic_dump: {fallbacks}. "
        f"專屬 persist_X 才能抽 cards/billed/pending 完整入庫."
    )
