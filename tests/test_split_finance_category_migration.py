from migrations.split_finance_category_20261005 import RAIL_RULES, migrate_rules, new_category


def _rule(rid, name, sub, pattern="x"):
    return {"id": rid, "name": name, "pattern": pattern, "category": "金融",
            "subcategory": sub, "enabled": 1}


def test_new_category_split() -> None:
    assert new_category("保險") == ("保險", None)
    assert new_category(None, "保險") == ("保險", None)
    assert new_category("稅") == ("稅費", "稅")
    assert new_category("罰款") == ("稅費", "罰款")
    assert new_category("手續費") == ("金融費用", "手續費")
    assert new_category(None, "手續費") == ("金融費用", None)


def test_rail_rules_deleted_only_when_untouched() -> None:
    name = "玉山APE付款"
    rules = [_rule(1, name, "電子支付", RAIL_RULES[name]),
             _rule(2, "街口支付", "電子支付", "我改過"),
             {"id": 3, "name": "餐廳", "pattern": "x", "category": "飲食",
              "subcategory": None, "enabled": 1}]
    out, actions = migrate_rules(rules)
    assert ("delete", 1, name, None, None) in actions
    assert [r["id"] for r in out] == [2, 3]
    assert out[0]["category"] == "金融費用"
