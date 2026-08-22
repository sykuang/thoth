"""Phase 5.1 → Phase 8 — Auto-seed default rules on user register.

Phase 5.1 — register 時自動 seed default rules。
Phase 8 (2026-06-15) — DEFAULT_RULES 擴充到 13 主類 + 5 收入類完整覆蓋 +
                       POST /rules/reset 一鍵恢復.
"""
from __future__ import annotations

import sqlite3


def test_register_seeds_default_rules(client):
    """新註冊 user 應自動有完整 default rules (Phase 8: 13 主類 + 5 收入類覆蓋)."""
    r = client.post(
        "/auth/register",
        json={"email": "seedee@palace.example", "password": "SyntheticTestPassword02!"},
    )
    assert r.status_code == 201, r.text
    token = r.json()["token"]
    h = {"Authorization": f"Bearer {token}"}
    r = client.get("/rules", headers=h)
    assert r.status_code == 200, r.text
    rules = r.json()
    # Phase 8: 從 10 條擴到 ~25 條 (13 主類 + 訂閱 flag + transfer/payment + 5 收入 + cashback/refund)
    assert len(rules) >= 20, f"expected >=20 default rules, got {len(rules)}: {rules}"

    cats = {r["category"] for r in rules}
    # Phase 8 (2026-06-15): 13 主類完整覆蓋 + 5 收入類 + 轉帳/還款
    must_have = {
        # 13 主類
        "飲食", "酒菸", "購物", "居住", "交通",
        "通訊", "娛樂", "醫療", "教育", "旅遊",
        "金融", "投資", "其他",
        # 收入 5 類 + transfer/payment
        "薪資", "獎金", "利息股息", "投資收益",
        "轉帳", "還款",
    }
    assert must_have.issubset(cats), \
        f"missing categories: {must_have - cats}"


def test_seed_default_rules_idempotent(client):
    """重 seed 同一 user 不該重複塞同名 rule（idempotency by name+user）。"""
    from backend.server.seed_rules import seed_default_rules
    client.post("/auth/register",
                json={"email": "seed-idem@palace.example", "password": "SyntheticTestPassword02!"})
    import backend.server.rules_repo as rr
    # 第一次已 seed 過了（在 register 時）。再 call 一次：
    seed_default_rules(user_id=1)
    rules = rr.list_rules(user_id=1)
    # 應該不會變兩倍
    name_counts: dict[str, int] = {}
    for r in rules:
        name_counts[r["name"]] = name_counts.get(r["name"], 0) + 1
    for name, n in name_counts.items():
        assert n == 1, f"rule {name!r} 重複塞了 {n} 次"


def test_schema_migrates_only_unchanged_food_gateway_pattern():
    """Remove gateway prefixes from the old default without overwriting user edits."""
    from backend.server import db
    from backend.server.seed_rules import DEFAULT_RULES

    old_pattern = (
        r"ＳＵＫＩＹＡ|SUKIYA|連加|街口電支|可不可熟成|瑞苗媽媽|春陽茶事|創義麵|義麵|"
        r"拉麵店|麵屋|燒肉|壽司郎|ＴａｐＰａｙ|TapPay|ＡＰＥ.*美食|１０１美食|"
        r"美食街|餐酒|食堂|お好み|定食|cafe|CAFE|Cafe"
    )
    new_pattern = (
        r"ＳＵＫＩＹＡ|SUKIYA|可不可熟成|瑞苗媽媽|春陽茶事|創義麵|義麵|"
        r"拉麵店|麵屋|燒肉|壽司郎|ＡＰＥ.*美食|１０１美食|"
        r"美食街|餐酒|食堂|お好み|定食|cafe|CAFE|Cafe"
    )
    seeded_pattern = next(r["pattern"] for r in DEFAULT_RULES if r["name"] == "餐飲全形連鎖")
    assert seeded_pattern == new_pattern
    customized_pattern = old_pattern + "|我的餐廳"

    conn = sqlite3.connect(":memory:")
    db._ensure_schema(conn)
    fixtures = (
        (1, "餐飲全形連鎖", old_pattern, "2026-08-22T00:00:01.000Z"),
        (2, "餐飲全形連鎖", customized_pattern, "2026-08-22T00:00:02.000Z"),
        (3, "自訂同內容", old_pattern, "2026-08-22T00:00:03.000Z"),
        (4, "餐飲全形連鎖", new_pattern, "2026-08-22T00:00:04.000Z"),
    )
    for user_id, name, pattern, timestamp in fixtures:
        conn.execute(
            "INSERT INTO category_rules "
            "(user_id, name, pattern, category, subcategory, priority, enabled, "
            "auto_excluded, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (user_id, name, pattern, "飲食", "餐廳", 105, 1, 0, timestamp, timestamp),
        )
    conn.commit()

    db._ensure_schema(conn)

    rows = {
        row[0]: (row[1], row[2], row[3])
        for row in conn.execute(
            "SELECT user_id, name, pattern, updated_at FROM category_rules ORDER BY user_id",
        )
    }
    assert rows[1][0:2] == ("餐飲全形連鎖", new_pattern)
    assert rows[1][2] != "2026-08-22T00:00:01.000Z"
    assert rows[2] == (
        "餐飲全形連鎖", customized_pattern, "2026-08-22T00:00:02.000Z",
    )
    assert rows[3] == ("自訂同內容", old_pattern, "2026-08-22T00:00:03.000Z")
    assert rows[4] == ("餐飲全形連鎖", new_pattern, "2026-08-22T00:00:04.000Z")

    first_run_rows = rows
    db._ensure_schema(conn)
    second_run_rows = {
        row[0]: (row[1], row[2], row[3])
        for row in conn.execute(
            "SELECT user_id, name, pattern, updated_at FROM category_rules ORDER BY user_id",
        )
    }
    assert second_run_rows == first_run_rows


# ============================================================
# Phase 8 (2026-06-15) — POST /rules/reset 一鍵恢復預設
# ============================================================

def test_reset_endpoint_wipes_and_reseeds(client):
    """POST /rules/reset 砍掉所有 rule 重塞 DEFAULT_RULES, 回 {deleted, added}."""
    # 註冊 user → 自動 seed default
    r = client.post(
        "/auth/register",
        json={"email": "reset-user@palace.example", "password": "SyntheticTestPassword02!"},
    )
    assert r.status_code == 201
    token = r.json()["token"]
    h = {"Authorization": f"Bearer {token}"}

    # 確認 default 已 seed
    initial_count = len(client.get("/rules", headers=h).json())
    assert initial_count >= 20

    # 使用者手動加一條自訂 rule
    r = client.post("/rules", headers=h, json={
        "name": "custom_test", "pattern": "test", "category": "其他", "priority": 50,
    })
    assert r.status_code == 201
    after_add = len(client.get("/rules", headers=h).json())
    assert after_add == initial_count + 1  # 多一條自訂

    # POST /rules/reset
    r = client.post("/rules/reset", headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert "deleted" in body and "added" in body
    assert body["deleted"] == after_add, \
        f"應刪掉 {after_add} 條, 實際 {body['deleted']}"
    assert body["added"] == initial_count, \
        f"應重塞 {initial_count} 條 default, 實際 {body['added']}"

    # 確認自訂 rule 不見了 (被 reset 砍了)
    final = client.get("/rules", headers=h).json()
    custom_rules = [r for r in final if r["name"] == "custom_test"]
    assert len(custom_rules) == 0, "reset 後不該還有自訂 rule"
    # 但 default rules 重新塞回去
    assert len(final) == initial_count


def test_reset_requires_auth(client):
    """POST /rules/reset 未登入應 401, 防止匿名 wipe attack."""
    r = client.post("/rules/reset")
    assert r.status_code == 401


def test_default_rules_contain_income_5_categories(client):
    """Phase 8: 5 收入類 (薪資/獎金/利息股息/投資收益/退稅)
    必須在 DEFAULT_RULES 裡, 確保 income 分類 out-of-box 就能 work.
    """
    from backend.server.seed_rules import DEFAULT_RULES
    rule_names = {r["name"] for r in DEFAULT_RULES}
    assert "薪資" in rule_names, "missing 薪資 rule"
    assert "獎金" in rule_names, "missing 獎金 rule"
    assert "利息股息" in rule_names, "missing 利息股息 rule"
    assert "投資收益" in rule_names, "missing 投資收益 rule"
    assert "退稅" in rule_names, "missing 退稅 (其他 income)"


def test_default_rules_cover_13_main_categories(client):
    """Phase 8: 13 主類 (Phase 6 taxonomy) 每類至少有 1 條 default rule."""
    from backend.server.seed_rules import DEFAULT_RULES
    main_13 = {
        "飲食", "酒菸", "購物", "居住", "交通",
        "通訊", "娛樂", "醫療", "教育", "旅遊",
        "金融", "投資", "其他",
    }
    covered = {r["category"] for r in DEFAULT_RULES if r["category"] in main_13}
    assert covered == main_13, \
        f"DEFAULT_RULES 沒覆蓋到主類: {main_13 - covered}"
