"""Static guard for category label management in Settings."""
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SETTINGS_HOME = ROOT / "frontend/src/app/(tabs)/settings/index.tsx"
CATEGORIES_SCREEN = ROOT / "frontend/src/app/(tabs)/settings/categories.tsx"
LABELS_SCREEN = ROOT / "frontend/src/app/(tabs)/settings/labels.tsx"


def test_settings_exposes_category_label_management() -> None:
    home = SETTINGS_HOME.read_text(encoding="utf-8")
    screen = LABELS_SCREEN.read_text(encoding="utf-8")

    assert 'href="/(tabs)/settings/labels"' in home
    assert 'href="/(tabs)/settings/categories"' in home
    assert "分類與標籤" in home
    assert "自動分類規則" in home
    assert 'testID="category-labels-card"' in screen
    assert "method: 'PUT'" in screen
    assert "method: 'DELETE'" in screen
    assert "'/rules/categories?include_all=true'" in screen
    assert "body: { old_name: oldName, name: newName }" in screen
    assert "body: { name: category }" in screen
    assert "sortCategoryKeys(categoriesQ.data?.categories ?? [])" in screen


def test_deleted_categories_are_not_readded_from_static_defaults() -> None:
    screen = LABELS_SCREEN.read_text(encoding="utf-8")

    assert "EXPENSE_CATEGORIES" not in screen


def test_label_management_has_clean_tabs_and_editable_subcategories_and_hashtags() -> None:
    screen = LABELS_SCREEN.read_text(encoding="utf-8")

    assert 'testID="label-tab-categories"' in screen
    assert 'testID="label-tab-hashtags"' in screen
    assert screen.count('accessibilityRole="tab"') == 2
    assert screen.count("selected: labelTab ===") == 2
    assert "'/rules/subcategories?" in screen
    assert "'/rules/subcategories'" in screen
    assert "'/transactions/tags/popular'" in screen
    assert "'/transactions/tags'" in screen
    assert "編輯子分類" in screen
    assert "編輯 Hashtag" in screen


def test_label_mutation_errors_invalidate_all_name_bearing_caches() -> None:
    screen = LABELS_SCREEN.read_text(encoding="utf-8")

    assert screen.count("onError: handleLabelMutationError") == 6
    assert "function handleLabelMutationError(e: ApiError)" in screen
    assert "invalidateCategoryData();\n    setStatus({ kind: 'err'" in screen


def test_settings_home_uses_grouped_rows_without_future_placeholders() -> None:
    home = SETTINGS_HOME.read_text(encoding="utf-8")

    for group in ("資料與顯示", "分類與自動化", "安全性"):
        assert group in home
    assert 'testID="settings-classification-group"' in home
    assert 'testID="settings-fx-disclosure"' in home
    assert 'testID="settings-card-date-disclosure"' in home
    assert "accessibilityState={{ expanded }}" in home
    assert "更多設定 (主題 / 語系 / 備份匯出) 之後加進來" not in home


def test_labels_and_rule_engine_are_separate_screens() -> None:
    labels = LABELS_SCREEN.read_text(encoding="utf-8")
    rules = CATEGORIES_SCREEN.read_text(encoding="utf-8")

    assert "新增規則" not in labels
    assert "Regex pattern" not in labels
    assert 'testID="category-labels-card"' not in rules
    assert "自動分類規則" in rules
    assert "進階操作" in rules
    assert rules.index('testID="rules-create-toggle"') < rules.index("規則清單")
    preview = rules[rules.index('testID="rules-preview-toggle"'):]
    assert 'placeholder="Regex pattern"' in preview
    assert rules.count("accessibilityState={{ expanded:") == 2
    assert 'className="gap-2 mt-4"' in rules
    assert 'className="flex-row gap-3 mt-4"' not in rules
