"""Static guard for frontend category display order (2026-07-05 A 方案).

Frontend 沒有 JS test runner；用 Python static test 鎖住三個不變式：
1. EXPENSE_CATEGORIES 是生活記帳常用順序，不是 COICOP raw order。
2. transactions.tsx chip / category summary 不能回到 insertion / pct order。
3. detail / bulk edit 的主分類 dropdown 不能直接吃 /rules/categories 的 SQL 字典序。
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _string_literals_from_array(src: str, const_name: str) -> list[str]:
    m = re.search(rf"export const {const_name} = \[(.*?)\] as const;", src, re.S)
    assert m, f"missing {const_name} array"
    return re.findall(r"'([^']+)'", m.group(1))


def test_frontend_expense_categories_use_life_first_order() -> None:
    src = (ROOT / "frontend/src/lib/category-color.ts").read_text()
    assert _string_literals_from_array(src, "EXPENSE_CATEGORIES") == [
        "飲食", "購物", "交通", "居住", "通訊",
        "娛樂", "醫療", "教育", "旅遊", "金融",
        "投資", "酒菸", "其他",
    ]


def test_transactions_category_chips_and_summary_use_sort_helper() -> None:
    src = (ROOT / "frontend/src/app/(tabs)/transactions.tsx").read_text()
    assert "import { categorySortRank, sortCategoryKeys } from '@/lib/category-color';" in src
    assert "const categoryKeys = useMemo(() => sortCategoryKeys(Object.keys(byCategory)), [byCategory]);" in src
    assert "Object.keys(byCategory).map" not in src
    assert "categoryKeys.map" in src
    assert ".sort((a, b) => b.pct - a.pct)" not in src
    assert "categorySortRank(a.key) - categorySortRank(b.key)" in src


def test_category_dropdowns_use_life_first_order() -> None:
    detail_src = (ROOT / "frontend/src/components/transactions/TxnDetailModal.tsx").read_text()
    assert "import { sortCategoryKeys } from '@/lib/category-color';" in detail_src
    assert "const categoryOptions = sortCategoryKeys(categoriesQ.data?.categories ?? []).map" in detail_src
    assert "options={categoryOptions}" in detail_src
    assert "<CategoryPicker" in detail_src
    assert "modalTitle=\"選擇分類\"" in detail_src
    assert "options={(categoriesQ.data?.categories ?? []).map" not in detail_src

    bulk_src = (ROOT / "frontend/src/components/BulkEditSheet.tsx").read_text()
    assert "import { sortCategoryKeys } from '@/lib/category-color';" in bulk_src
    assert "const cats = sortCategoryKeys(categoriesQ.data?.categories ?? []);" in bulk_src
    assert "<CategoryPicker" in bulk_src
    assert "const cats = categoriesQ.data?.categories ?? [];" not in bulk_src


def test_category_picker_uses_bounded_moneybook_style_grid_sheet() -> None:
    src = (ROOT / "frontend/src/components/CategoryPicker.tsx").read_text()
    assert "MoneyBook-style category selector" in src
    assert "viewportSafeHeight" in src
    assert "Math.min(" in src
    assert "style={{ maxHeight: sheetMaxHeight }}" in src
    assert "style={{ maxHeight: gridMaxHeight }}" in src
    assert "style={{ width: '25%' }}" in src
    assert "分類管理" in src
    assert "支出" in src and "收入" in src
    assert "lucide-react-native" in src
    assert "CATEGORY_ICONS" in src
    assert "CategoryIcon" in src
    assert "categoryEmoji(opt.value)" not in src


def test_category_picker_does_not_bucket_neutral_categories_as_expense() -> None:
    src = (ROOT / "frontend/src/components/CategoryPicker.tsx").read_text()
    assert "EXPENSE_ONLY_SET" in src
    assert "const NEUTRAL_SET = new Set<string>(['其他', '轉帳', '還款']);" in src
    assert "expenseOptions: regular.filter((o) => EXPENSE_ONLY_SET.has(o.value))" in src
    assert "neutralOptions: regular.filter((o) =>" in src
    assert "id: 'neutral' as const, label: '其他'" in src
    assert "regular.filter((o) => !INCOME_ONLY_SET.has(o.value))" not in src


def test_generic_dropdown_still_caps_long_lists() -> None:
    src = (ROOT / "frontend/src/components/Dropdown.tsx").read_text()
    assert "maxHeightRatio = 0.52" in src
    assert "viewportSafeHeight" in src
    assert "Math.min(" in src
    assert "style={{ maxHeight: sheetMaxHeight }}" in src
    assert "style={{ maxHeight: listMaxHeight }}" in src


def test_card_detail_uses_category_meta_not_txn_type_badges() -> None:
    src = (ROOT / "frontend/src/app/(tabs)/cards/[bank]/[card_no].tsx").read_text()
    assert "txnTypeLabel" not in src
    assert "categoryMeta(t.category, t.subcategory)" in src
    assert "tag={" not in src
    assert "meta={categoryMeta" in src
