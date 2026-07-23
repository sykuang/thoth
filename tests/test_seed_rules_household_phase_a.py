"""2026-07-05 A 方案 — Household / personal-care 補成子類，不新增主類。

使用者選 A：保留既有 13 主類，不新增「日用」，但把 COICOP 05 household
與 COICOP 13 personal care 的常見生活用品補成可見子類，避免全部掉進
購物/百貨或其他。
"""
from __future__ import annotations

from backend.server.categorizer import categorize_with_excluded
from backend.server.seed_rules import DEFAULT_RULES


def _rules() -> list[dict]:
    return sorted(DEFAULT_RULES, key=lambda r: (-r["priority"], r["name"]))


def _categorize(desc: str) -> tuple[str | None, str | None, bool]:
    return categorize_with_excluded(desc, _rules())


def test_household_phase_a_does_not_add_new_main_category() -> None:
    """A 方案明確不新增「日用」主類。"""
    categories = {r["category"] for r in DEFAULT_RULES}
    assert "日用" not in categories
    assert "家庭用品" not in categories


def test_household_supplies_are_visible_shopping_subcategories() -> None:
    samples = {
        "全聯 衛生紙": ("購物", "家庭用品"),
        "家樂福 垃圾袋": ("購物", "家庭用品"),
        "Costco 廚房紙巾": ("購物", "家庭用品"),
        "小北百貨 洗衣精": ("購物", "清潔用品"),
        "寶雅 清潔劑": ("購物", "清潔用品"),
        "屈臣氏 洗髮精": ("購物", "個人清潔"),
        "康是美 牙膏": ("購物", "個人清潔"),
    }
    for desc, expected in samples.items():
        cat, sub, excluded = _categorize(desc)
        assert (cat, sub) == expected
        assert excluded is False


def test_personal_care_services_are_visible_shopping_subcategories() -> None:
    """理髮/美容服務不該丟進其他，也不該跟洗髮精等用品混成個人清潔。"""
    samples = {
        "小林髮廊 理髮": ("購物", "個人照護"),
        "QB HOUSE 剪髮": ("購物", "個人照護"),
        "美髮沙龍 染髮": ("購物", "個人照護"),
        "NAIL 美甲": ("購物", "個人照護"),
        "SPA 按摩": ("購物", "個人照護"),
    }
    for desc, expected in samples.items():
        cat, sub, excluded = _categorize(desc)
        assert (cat, sub) == expected
        assert excluded is False


def test_personal_cleaning_goods_stay_separate_from_personal_care_services() -> None:
    """買用品仍是個人清潔；理髮美容服務才是個人照護。"""
    assert _categorize("屈臣氏 洗髮精")[:2] == ("購物", "個人清潔")
    assert _categorize("小林髮廊 理髮")[:2] == ("購物", "個人照護")


def test_home_furnishing_and_repair_are_housing_subcategories() -> None:
    samples = {
        "IKEA 收納櫃": ("居住", "家具家電"),
        "HOLA 窗簾": ("居住", "家具家電"),
        "宜得利 床墊": ("居住", "家具家電"),
        "水電行 居家維修": ("居住", "居家修繕"),
        "特力屋 五金": ("居住", "居家修繕"),
    }
    for desc, expected in samples.items():
        cat, sub, excluded = _categorize(desc)
        assert (cat, sub) == expected
        assert excluded is False


def test_watsons_drugstore_medical_keyword_still_wins() -> None:
    """屈臣氏藥 / 藥局仍應歸醫療，不被個人清潔吃掉。"""
    cat, sub, excluded = _categorize("屈臣氏藥 感冒藥")
    assert (cat, sub) == ("醫療", "藥局")
    assert excluded is False
