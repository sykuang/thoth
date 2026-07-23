"""Phase 5.1 — /rules CRUD + preview + recategorize end-to-end.

Phase 5.1 — /rules CRUD + preview + recategorize 端到端。

掛在 backend/server/routers/rules.py。
"""
from __future__ import annotations

import pytest


def _register_and_token(client) -> str:
    r = client.post(
        "/auth/register",
        json={"email": "ruler@palace.example", "password": "SyntheticTestPassword02!"},
    )
    assert r.status_code == 201, r.text
    return r.json()["token"]


# ---- list/create ----

def test_create_and_list_rule(client):
    token = _register_and_token(client)
    h = {"Authorization": f"Bearer {token}"}
    # 先記下 register 後 default seed 的條數
    base_n = len(client.get("/rules", headers=h).json())
    r = client.post(
        "/rules", headers=h,
        json={"name": "transit-custom", "pattern": "北捷", "category": "交通",
              "priority": 100},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["id"] >= 1
    assert body["name"] == "transit-custom"

    r = client.get("/rules", headers=h)
    assert r.status_code == 200, r.text
    items = r.json()
    assert len(items) == base_n + 1
    names = {it["name"] for it in items}
    assert "transit-custom" in names


def test_list_rules_unauthorized_401(client):
    r = client.get("/rules")
    assert r.status_code == 401


def test_create_rule_unauthorized_401(client):
    r = client.post(
        "/rules",
        json={"name": "x", "pattern": "a", "category": "C"},
    )
    assert r.status_code == 401


def test_create_rule_invalid_regex_400(client):
    """壞 regex pattern 應該被擋，回 400。"""
    token = _register_and_token(client)
    h = {"Authorization": f"Bearer {token}"}
    r = client.post(
        "/rules", headers=h,
        json={"name": "bad", "pattern": "(unclosed", "category": "C"},
    )
    assert r.status_code == 400, r.text


# ---- update ----

def test_update_rule(client):
    token = _register_and_token(client)
    h = {"Authorization": f"Bearer {token}"}
    cr = client.post(
        "/rules", headers=h,
        json={"name": "x", "pattern": "a", "category": "C", "priority": 10},
    )
    rid = cr.json()["id"]
    r = client.put(
        f"/rules/{rid}", headers=h,
        json={"name": "x2", "priority": 500, "enabled": False},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["name"] == "x2"
    assert body["priority"] == 500
    assert body["enabled"] == 0


def test_update_rule_unauthorized_401(client):
    r = client.put("/rules/1", json={"name": "x"})
    assert r.status_code == 401


def test_update_rule_not_found_404(client):
    token = _register_and_token(client)
    h = {"Authorization": f"Bearer {token}"}
    r = client.put("/rules/9999", headers=h, json={"name": "x"})
    assert r.status_code == 404, r.text


# ---- delete ----

def test_delete_rule(client):
    token = _register_and_token(client)
    h = {"Authorization": f"Bearer {token}"}
    cr = client.post(
        "/rules", headers=h,
        json={"name": "x-tmp", "pattern": "a", "category": "C"},
    )
    rid = cr.json()["id"]
    n_before = len(client.get("/rules", headers=h).json())
    r = client.delete(f"/rules/{rid}", headers=h)
    assert r.status_code == 204, r.text
    after = client.get("/rules", headers=h).json()
    assert len(after) == n_before - 1
    assert all(it["id"] != rid for it in after)


def test_delete_rule_unauthorized_401(client):
    r = client.delete("/rules/1")
    assert r.status_code == 401


def test_delete_rule_not_found_404(client):
    token = _register_and_token(client)
    h = {"Authorization": f"Bearer {token}"}
    r = client.delete("/rules/9999", headers=h)
    assert r.status_code == 404, r.text


# ---- preview ----

def test_preview_pattern_returns_matching_indices(client):
    token = _register_and_token(client)
    h = {"Authorization": f"Bearer {token}"}
    r = client.post(
        "/rules/preview", headers=h,
        json={"pattern": "北捷|台鐵", "sample_texts": [
            "北捷儲值 500",
            "早餐店",
            "台鐵自強號",
            "便利商店",
        ]},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["matched_indices"] == [0, 2]
    assert body["matched_count"] == 2
    assert body["total"] == 4


def test_preview_unauthorized_401(client):
    r = client.post("/rules/preview",
                    json={"pattern": "x", "sample_texts": ["y"]})
    assert r.status_code == 401


def test_preview_invalid_regex_400(client):
    token = _register_and_token(client)
    h = {"Authorization": f"Bearer {token}"}
    r = client.post(
        "/rules/preview", headers=h,
        json={"pattern": "(invalid", "sample_texts": ["a"]},
    )
    assert r.status_code == 400, r.text


# ---- recategorize ----

def test_recategorize_runs_and_returns_counts(client, tmp_path, monkeypatch):
    """recategorize 應對 user 的所有 bank.sqlite 跑 categorize。

    2026-06-14: conftest 已 setenv BANK_DATA_ROOT=tmp_path; 直接用同路徑,
    不另開 tmp_path/banks 子目錄 (避免 server route _data_root() 跟 test BankStore 路徑不一致).
    """
    import backend.core.store as store_mod

    token = _register_and_token(client)
    h = {"Authorization": f"Bearer {token}"}

    # 先建 rule
    client.post("/rules", headers=h,
                json={"name": "transit", "pattern": "北捷", "category": "交通"})

    # 預塞一筆 txn (BankStore 透過 _data_root() 讀 conftest 的 BANK_DATA_ROOT=tmp_path)
    bs = store_mod.BankStore("sinopac")
    bs.upsert_twd_txns([{
        "account_no": "001", "datetime": "2026-06-12", "desc": "北捷加值",
        "expend": 500, "income": None, "balance": 999,
    }])
    bs.close()

    # 跑 recategorize
    r = client.post("/rules/recategorize", headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert "updated" in body
    assert body["updated"] >= 1

    # 驗證 category 真的寫進去了
    bs = store_mod.BankStore("sinopac")
    row = bs.conn.execute(
        "SELECT category FROM twd_transactions WHERE description='北捷加值'"
    ).fetchone()
    bs.close()
    assert row["category"] == "交通"


def test_recategorize_unauthorized_401(client):
    r = client.post("/rules/recategorize")
    assert r.status_code == 401


# ---- Phase 8.4 (2026-06-18) recategorize 保護手動分類 ----

def test_recategorize_default_protects_manually_categorized_txns(client, tmp_path, monkeypatch):
    """使用者指示 (2026-06-18)：「分類規則應該只套用在未分類吧而不是所有交易」

    場景:
      1. 先預塞一筆 txn (走 BankStore.upsert，category=None 因無 rule)
      2. 加 rule 把 desc 命中映射到「交通」
      3. 跑 recategorize → category 從 None 變「交通」(updated=1)
      4. **使用者手動把該 row 改成「醫療」** (模擬 UI PATCH /transactions/.../id)
      5. 加新 rule 「同 desc 命中映射到 其他」(priority 高過原 rule)
      6. 預設 recategorize (force=False) → **應保護「醫療」不動** (protected=1)
      7. force=True → 才真的覆寫成「其他」
    """
    import backend.core.store as store_mod
    token = _register_and_token(client)
    h = {"Authorization": f"Bearer {token}"}

    # 1. 預塞 txn — 還沒 rule, category 預設 None
    bs = store_mod.BankStore("sinopac", user_id=1)
    bs.upsert_twd_txns([{
        "account_no": "001", "datetime": "2026-06-12", "desc": "北捷加值",
        "expend": 500, "income": None, "balance": 999,
    }])
    bs.close()

    # 2. 加 rule
    client.post("/rules", headers=h,
                json={"name": "transit", "pattern": "北捷", "category": "交通"})

    # 3. recat 把 None → 交通
    r = client.post("/rules/recategorize", headers=h)
    body = r.json()
    assert body["updated"] >= 1
    assert body["protected"] == 0  # 還沒 user manual, 不算 protected

    # 4. 使用者手動改成「醫療」(直接 SQL 模擬 PATCH /transactions/.../id)
    bs = store_mod.BankStore("sinopac", user_id=1)
    bs.conn.execute(
        "UPDATE twd_transactions SET category=? WHERE description=? AND user_id=?",
        ("醫療", "北捷加值", 1),
    )
    bs.conn.commit()
    bs.close()

    # 5. 加新 rule 改類別 + priority 高 (模擬未來 default rule 升級)
    client.post("/rules", headers=h,
                json={"name": "transit_v2", "pattern": "北捷",
                      "category": "其他", "priority": 999})

    # 6. 預設 force=False — 應保護「醫療」不動
    r = client.post("/rules/recategorize", headers=h)
    body = r.json()
    assert body["protected"] == 1, \
        f"expected protected=1 (manual 醫療 should be preserved), got {body}"
    bs = store_mod.BankStore("sinopac", user_id=1)
    row = bs.conn.execute(
        "SELECT category FROM twd_transactions WHERE description=?", ("北捷加值",)
    ).fetchone()
    bs.close()
    assert row["category"] == "醫療", \
        f"manual category 醫療 should be preserved, got {row['category']}"

    # 7. force=True — 覆寫成「其他」
    r = client.post("/rules/recategorize?force=true", headers=h)
    body = r.json()
    assert body["protected"] == 0, "force=True 不該有 protected 計數"
    bs = store_mod.BankStore("sinopac", user_id=1)
    row = bs.conn.execute(
        "SELECT category FROM twd_transactions WHERE description=?", ("北捷加值",)
    ).fetchone()
    bs.close()
    assert row["category"] == "其他", \
        f"force=True should override 醫療→其他, got {row['category']}"


def test_recategorize_default_still_fills_null_categories(client, tmp_path, monkeypatch):
    """預設 force=False 仍應對 category=NULL 的真未分類 row 跑 rule.
    (確認 protect 邏輯沒矯枉過正)"""
    import backend.core.store as store_mod
    token = _register_and_token(client)
    h = {"Authorization": f"Bearer {token}"}

    bs = store_mod.BankStore("sinopac", user_id=1)
    bs.upsert_twd_txns([{
        "account_no": "001", "datetime": "2026-06-12", "desc": "北捷加值",
        "expend": 500, "income": None, "balance": 999,
    }])
    bs.close()

    client.post("/rules", headers=h,
                json={"name": "transit", "pattern": "北捷", "category": "交通"})

    r = client.post("/rules/recategorize", headers=h)
    body = r.json()
    assert body["updated"] >= 1
    assert body["protected"] == 0

    bs = store_mod.BankStore("sinopac", user_id=1)
    row = bs.conn.execute(
        "SELECT category FROM twd_transactions WHERE description=?", ("北捷加值",)
    ).fetchone()
    bs.close()
    assert row["category"] == "交通"


# ---- categories ----

def test_get_distinct_categories(client):
    token = _register_and_token(client)
    h = {"Authorization": f"Bearer {token}"}
    # seed default 已有不少 category。新增三條 custom 後驗證 distinct
    client.post("/rules", headers=h,
                json={"name": "r1-custom", "pattern": "a", "category": "自訂A"})
    client.post("/rules", headers=h,
                json={"name": "r2-custom", "pattern": "b", "category": "自訂B"})
    client.post("/rules", headers=h,
                json={"name": "r3-custom", "pattern": "c", "category": "自訂A"})  # dup
    r = client.get("/rules/categories", headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert "自訂A" in body["categories"]
    assert "自訂B" in body["categories"]
    # default seed 帶來的 category 也應該在
    assert "交通" in body["categories"]
    # distinct（不重複）
    assert len(body["categories"]) == len(set(body["categories"]))


def test_get_categories_unauthorized_401(client):
    r = client.get("/rules/categories")
    assert r.status_code == 401


def test_rename_category_updates_rules_and_transactions_for_owner_only(client):
    from backend.core.store import BankStore

    owner = client.post(
        "/auth/register",
        json={"email": "category-owner@palace.example", "password": "SyntheticTestPassword02!"},
    ).json()
    other = client.post(
        "/auth/register",
        json={"email": "category-other@palace.example", "password": "SyntheticTestPassword02!"},
    ).json()
    owner_h = {"Authorization": f"Bearer {owner['token']}"}
    other_h = {"Authorization": f"Bearer {other['token']}"}
    for headers in (owner_h, other_h):
        response = client.post(
            "/rules",
            headers=headers,
            json={"name": "pet-rule", "pattern": "寵物", "category": "寵物"},
        )
        assert response.status_code == 201, response.text

    for user_id in (owner["user_id"], other["user_id"]):
        store = BankStore("sinopac", user_id=user_id)
        store.upsert_twd_txns([{
            "account_no": "001",
            "datetime": "2026-07-18",
            "desc": "寵物用品",
            "expend": 500,
            "income": None,
            "balance": 999,
        }])
        store.conn.execute(
            "UPDATE twd_transactions SET category=?, subcategory=? WHERE user_id=?",
            ("寵物", "飼料", user_id),
        )
        store.conn.commit()
        store.close()

    response = client.put(
        "/rules/categories",
        headers=owner_h,
        json={"old_name": "寵物", "name": "毛孩"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["rules_updated"] == 1
    assert response.json()["transactions_updated"] == 1
    assert "毛孩" in client.get("/rules/categories", headers=owner_h).json()["categories"]
    assert "寵物" in client.get("/rules/categories", headers=other_h).json()["categories"]

    owner_store = BankStore("sinopac", user_id=owner["user_id"])
    owner_row = owner_store.conn.execute(
        "SELECT category, subcategory FROM twd_transactions WHERE user_id=?",
        (owner["user_id"],),
    ).fetchone()
    owner_store.close()
    assert owner_row is not None
    assert (owner_row["category"], owner_row["subcategory"]) == ("毛孩", "飼料")

    other_store = BankStore("sinopac", user_id=other["user_id"])
    other_row = other_store.conn.execute(
        "SELECT category FROM twd_transactions WHERE user_id=?",
        (other["user_id"],),
    ).fetchone()
    other_store.close()
    assert other_row is not None
    assert other_row["category"] == "寵物"


def test_rename_category_accepts_slash_and_preserves_exact_old_name(client):
    token = _register_and_token(client)
    headers = {"Authorization": f"Bearer {token}"}
    response = client.post(
        "/rules",
        headers=headers,
        json={"name": "odd-label", "pattern": "x", "category": " Space/Label "},
    )
    assert response.status_code == 201, response.text

    response = client.put(
        "/rules/categories",
        headers=headers,
        json={"old_name": " Space/Label ", "name": "整理後"},
    )

    assert response.status_code == 200, response.text
    categories = client.get("/rules/categories", headers=headers).json()["categories"]
    assert "整理後" in categories
    assert " Space/Label " not in categories


def test_rename_category_rejects_existing_target(client):
    token = _register_and_token(client)
    headers = {"Authorization": f"Bearer {token}"}
    for name in ("原分類", "既有分類"):
        response = client.post(
            "/rules",
            headers=headers,
            json={"name": f"rule-{name}", "pattern": name, "category": name},
        )
        assert response.status_code == 201, response.text

    response = client.put(
        "/rules/categories",
        headers=headers,
        json={"old_name": "原分類", "name": "既有分類"},
    )

    assert response.status_code == 409, response.text
    categories = client.get("/rules/categories", headers=headers).json()["categories"]
    assert "原分類" in categories
    assert "既有分類" in categories


def test_manage_categories_includes_transaction_only_label(client):
    from backend.core.store import BankStore

    user = client.post(
        "/auth/register",
        json={"email": "category-orphan@palace.example", "password": "SyntheticTestPassword02!"},
    ).json()
    headers = {"Authorization": f"Bearer {user['token']}"}
    store = BankStore("sinopac", user_id=user["user_id"])
    store.upsert_twd_txns([{
        "account_no": "001",
        "datetime": "2026-07-18",
        "desc": "無規則分類",
        "expend": 300,
        "income": None,
        "balance": 999,
    }])
    store.conn.execute(
        "UPDATE twd_transactions SET category=? WHERE user_id=?",
        ("交易孤兒", user["user_id"]),
    )
    store.conn.commit()
    store.close()

    response = client.get("/rules/categories?include_all=true", headers=headers)

    assert response.status_code == 200, response.text
    assert "交易孤兒" in response.json()["categories"]

    response = client.request(
        "DELETE",
        "/rules/categories",
        headers=headers,
        json={"name": "交易孤兒"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["rules_updated"] == 0
    assert response.json()["transactions_updated"] == 1


def test_manage_categories_reads_legacy_transaction_only_table(client):
    import os
    import sqlite3
    from pathlib import Path

    user = client.post(
        "/auth/register",
        json={"email": "category-legacy-list@palace.example", "password": "SyntheticTestPassword02!"},
    ).json()
    headers = {"Authorization": f"Bearer {user['token']}"}
    db_path = Path(os.environ["BANK_DATA_ROOT"]) / "cathay.sqlite"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE twd_transactions "
            "(id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, category TEXT)",
        )
        conn.execute(
            "INSERT INTO twd_transactions (user_id, category) VALUES (?, ?)",
            (user["user_id"], "舊表孤兒"),
        )

    response = client.get("/rules/categories?include_all=true", headers=headers)

    assert response.status_code == 200, response.text
    assert "舊表孤兒" in response.json()["categories"]

    response = client.post(
        "/rules",
        headers=headers,
        json={"name": "collision-source", "pattern": "source", "category": "來源分類"},
    )
    assert response.status_code == 201, response.text
    response = client.put(
        "/rules/categories",
        headers=headers,
        json={"old_name": "來源分類", "name": "舊表孤兒"},
    )
    assert response.status_code == 409, response.text


@pytest.mark.parametrize("failure_point", ["bank", "rules"])
def test_rename_category_rolls_back_every_bank_when_one_write_fails(
    client, monkeypatch, failure_point,
):
    from backend.core.store import BankStore
    from backend.server import rules_repo
    from backend.server.db_facade.transactions import TransactionsWriteMixin

    user = client.post(
        "/auth/register",
        json={"email": "category-rollback@palace.example", "password": "SyntheticTestPassword02!"},
    ).json()
    headers = {"Authorization": f"Bearer {user['token']}"}
    response = client.post(
        "/rules",
        headers=headers,
        json={"name": "atomic-rule", "pattern": "atomic", "category": "原分類"},
    )
    assert response.status_code == 201, response.text

    for bank in ("cathay", "ubot"):
        store = BankStore(bank, user_id=user["user_id"])
        store.upsert_twd_txns([{
            "account_no": "001",
            "datetime": "2026-07-18",
            "desc": f"atomic-{bank}",
            "expend": 100,
            "income": None,
            "balance": 999,
        }])
        store.conn.execute(
            "UPDATE twd_transactions SET category=? WHERE user_id=?",
            ("原分類", user["user_id"]),
        )
        store.conn.commit()
        store.close()

    original = TransactionsWriteMixin.replace_category

    def fail_on_ubot(self, **kwargs):
        changed = original(self, **kwargs)
        if self._bank == "ubot":
            raise RuntimeError("injected category write failure")
        return changed

    if failure_point == "bank":
        monkeypatch.setattr(TransactionsWriteMixin, "replace_category", fail_on_ubot)
    else:
        def fail_on_rules(*args, **kwargs):
            raise RuntimeError("injected category write failure")

        monkeypatch.setattr(rules_repo, "rename_category", fail_on_rules)

    with pytest.raises(RuntimeError, match="injected category write failure"):
        client.put(
            "/rules/categories",
            headers=headers,
            json={"old_name": "原分類", "name": "新分類"},
        )

    assert "原分類" in client.get("/rules/categories", headers=headers).json()["categories"]
    for bank in ("cathay", "ubot"):
        store = BankStore(bank, user_id=user["user_id"])
        row = store.conn.execute(
            "SELECT category FROM twd_transactions WHERE user_id=?",
            (user["user_id"],),
        ).fetchone()
        store.close()
        assert row is not None
        assert row["category"] == "原分類"


def test_rollback_restores_legacy_table_without_optional_columns(client, monkeypatch):
    import os
    import sqlite3
    from pathlib import Path

    from backend.server.db_facade.transactions import TransactionsWriteMixin

    user = client.post(
        "/auth/register",
        json={"email": "category-legacy-rollback@palace.example", "password": "SyntheticTestPassword02!"},
    ).json()
    headers = {"Authorization": f"Bearer {user['token']}"}
    response = client.post(
        "/rules",
        headers=headers,
        json={"name": "legacy-atomic", "pattern": "legacy", "category": "原分類"},
    )
    assert response.status_code == 201, response.text

    data_root = Path(os.environ["BANK_DATA_ROOT"])
    for bank in ("cathay", "ubot"):
        with sqlite3.connect(data_root / f"{bank}.sqlite") as conn:
            conn.execute(
                "CREATE TABLE twd_transactions "
                "(id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, category TEXT)",
            )
            conn.execute(
                "INSERT INTO twd_transactions (user_id, category) VALUES (?, ?)",
                (user["user_id"], "原分類"),
            )

    original = TransactionsWriteMixin.replace_category

    def fail_on_ubot(self, **kwargs):
        changed = original(self, **kwargs)
        if self._bank == "ubot":
            raise RuntimeError("injected legacy write failure")
        return changed

    monkeypatch.setattr(TransactionsWriteMixin, "replace_category", fail_on_ubot)

    with pytest.raises(RuntimeError):
        client.put(
            "/rules/categories",
            headers=headers,
            json={"old_name": "原分類", "name": "新分類"},
        )

    for bank in ("cathay", "ubot"):
        with sqlite3.connect(data_root / f"{bank}.sqlite") as conn:
            category = conn.execute(
                "SELECT category FROM twd_transactions WHERE user_id=?",
                (user["user_id"],),
            ).fetchone()[0]
        assert category == "原分類"


def test_delete_rollback_restores_exact_optional_values(client, monkeypatch):
    import os
    import sqlite3
    from pathlib import Path

    from backend.server import rules_repo

    user = client.post(
        "/auth/register",
        json={"email": "category-delete-rollback@palace.example", "password": "SyntheticTestPassword02!"},
    ).json()
    headers = {"Authorization": f"Bearer {user['token']}"}
    response = client.post(
        "/rules",
        headers=headers,
        json={"name": "delete-atomic", "pattern": "delete", "category": "毛孩"},
    )
    assert response.status_code == 201, response.text

    db_path = Path(os.environ["BANK_DATA_ROOT"]) / "cathay.sqlite"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE twd_transactions "
            "(id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, category TEXT, "
            "subcategory TEXT, auto_excluded INTEGER)",
        )
        conn.execute(
            "INSERT INTO twd_transactions "
            "(user_id, category, subcategory, auto_excluded) VALUES (?, ?, ?, ?)",
            (user["user_id"], "毛孩", "飼料", None),
        )

    def fail_on_rules(*args, **kwargs):
        raise RuntimeError("injected delete rules failure")

    monkeypatch.setattr(rules_repo, "delete_category", fail_on_rules)

    with pytest.raises(RuntimeError, match="injected delete rules failure"):
        client.request(
            "DELETE",
            "/rules/categories",
            headers=headers,
            json={"name": "毛孩"},
        )

    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT category, subcategory, auto_excluded "
            "FROM twd_transactions WHERE user_id=?",
            (user["user_id"],),
        ).fetchone()
    assert row == ("毛孩", "飼料", None)


def test_delete_category_removes_rules_and_clears_transactions(client):
    from backend.core.store import BankStore

    user = client.post(
        "/auth/register",
        json={"email": "category-delete@palace.example", "password": "SyntheticTestPassword02!"},
    ).json()
    headers = {"Authorization": f"Bearer {user['token']}"}
    response = client.post(
        "/rules",
        headers=headers,
        json={"name": "pet-rule", "pattern": "寵物", "category": "毛孩"},
    )
    assert response.status_code == 201, response.text

    store = BankStore("sinopac", user_id=user["user_id"])
    store.upsert_twd_txns([{
        "account_no": "001",
        "datetime": "2026-07-18",
        "desc": "寵物用品",
        "expend": 500,
        "income": None,
        "balance": 999,
    }])
    store.conn.execute(
        "UPDATE twd_transactions SET category=?, subcategory=? WHERE user_id=?",
        ("毛孩", "飼料", user["user_id"]),
    )
    store.conn.commit()
    store.close()

    response = client.request(
        "DELETE",
        "/rules/categories",
        headers=headers,
        json={"name": "毛孩"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["rules_updated"] == 1
    assert response.json()["transactions_updated"] == 1
    assert "毛孩" not in client.get("/rules/categories", headers=headers).json()["categories"]
    assert all(rule["category"] != "毛孩" for rule in client.get("/rules", headers=headers).json())

    store = BankStore("sinopac", user_id=user["user_id"])
    row = store.conn.execute(
        "SELECT category, subcategory FROM twd_transactions WHERE user_id=?",
        (user["user_id"],),
    ).fetchone()
    store.close()
    assert row is not None
    assert row["category"] is None
    assert row["subcategory"] is None


# ===========================================================================
# Phase 8.1 (2026-06-15): subcategory 子分類 (路由層)
# ===========================================================================

def test_create_rule_with_subcategory(client):
    token = _register_and_token(client)
    h = {"Authorization": f"Bearer {token}"}
    r = client.post(
        "/rules", headers=h,
        json={"name": "breakfast", "pattern": "早餐", "category": "飲食",
              "subcategory": "早餐"},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["category"] == "飲食"
    assert body["subcategory"] == "早餐"


def test_create_rule_subcategory_default_null(client):
    token = _register_and_token(client)
    h = {"Authorization": f"Bearer {token}"}
    r = client.post(
        "/rules", headers=h,
        json={"name": "no-sub", "pattern": "x", "category": "C"},
    )
    body = r.json()
    assert body["subcategory"] is None


def test_update_rule_subcategory(client):
    token = _register_and_token(client)
    h = {"Authorization": f"Bearer {token}"}
    cr = client.post(
        "/rules", headers=h,
        json={"name": "x", "pattern": "a", "category": "飲食"},
    )
    rid = cr.json()["id"]
    # 加 subcategory
    r = client.put(f"/rules/{rid}", headers=h,
                   json={"subcategory": "餐廳"})
    assert r.status_code == 200, r.text
    assert r.json()["subcategory"] == "餐廳"
    # 改 subcategory
    r2 = client.put(f"/rules/{rid}", headers=h,
                    json={"subcategory": "火鍋"})
    assert r2.json()["subcategory"] == "火鍋"


def test_update_rule_clear_subcategory_via_empty_string(client):
    """PUT subcategory='' → 清掉 (回 None)."""
    token = _register_and_token(client)
    h = {"Authorization": f"Bearer {token}"}
    cr = client.post(
        "/rules", headers=h,
        json={"name": "x", "pattern": "a", "category": "飲食",
              "subcategory": "餐廳"},
    )
    rid = cr.json()["id"]
    r = client.put(f"/rules/{rid}", headers=h, json={"subcategory": ""})
    assert r.status_code == 200, r.text
    assert r.json()["subcategory"] is None


def test_get_subcategories_all(client):
    token = _register_and_token(client)
    h = {"Authorization": f"Bearer {token}"}
    # default seed 已塞「餐廳, 早餐, 加油, …」(臣妾在 seed_rules 加的範例)
    r = client.get("/rules/subcategories", headers=h)
    assert r.status_code == 200, r.text
    subs = r.json()["subcategories"]
    # default 至少要有飲食.餐廳 / 飲食.食品雜貨 / 交通.大眾運輸 / 交通.計程車 / 交通.自駕
    assert "餐廳" in subs
    assert "大眾運輸" in subs
    # 自訂後也要出現
    client.post("/rules", headers=h,
                json={"name": "ramen", "pattern": "拉麵", "category": "飲食",
                      "subcategory": "拉麵"})
    subs2 = client.get("/rules/subcategories", headers=h).json()["subcategories"]
    assert "拉麵" in subs2


def test_get_subcategories_filter_by_category(client):
    token = _register_and_token(client)
    h = {"Authorization": f"Bearer {token}"}
    # default seed: 飲食 → 餐廳/食品雜貨; 交通 → 大眾運輸/計程車/自駕
    food_subs = client.get(
        "/rules/subcategories", headers=h, params={"category": "飲食"},
    ).json()["subcategories"]
    assert "餐廳" in food_subs
    assert "食品雜貨" in food_subs
    assert "大眾運輸" not in food_subs  # 交通 的不能跑進來

    transit_subs = client.get(
        "/rules/subcategories", headers=h, params={"category": "交通"},
    ).json()["subcategories"]
    assert "大眾運輸" in transit_subs
    assert "計程車" in transit_subs
    assert "自駕" in transit_subs
    assert "餐廳" not in transit_subs


def test_get_subcategories_unauthorized_401(client):
    r = client.get("/rules/subcategories")
    assert r.status_code == 401


def test_rename_subcategory_updates_one_category_only(client):
    from backend.core.store import BankStore

    user = client.post(
        "/auth/register",
        json={"email": "subcategory-owner@palace.example", "password": "SyntheticTestPassword02!"},
    ).json()
    headers = {"Authorization": f"Bearer {user['token']}"}
    for category in ("飲食", "購物"):
        response = client.post(
            "/rules",
            headers=headers,
            json={
                "name": f"{category}-coffee",
                "pattern": category,
                "category": category,
                "subcategory": "咖啡",
            },
        )
        assert response.status_code == 201, response.text

    store = BankStore("sinopac", user_id=user["user_id"])
    store.upsert_twd_txns([
        {"account_no": "001", "datetime": "2026-07-19", "desc": "早餐咖啡",
         "expend": 100, "income": None, "balance": 900},
        {"account_no": "001", "datetime": "2026-07-20", "desc": "買咖啡豆",
         "expend": 300, "income": None, "balance": 600},
    ])
    rows = store.conn.execute(
        "SELECT id FROM twd_transactions WHERE user_id=? ORDER BY id",
        (user["user_id"],),
    ).fetchall()
    store.conn.execute(
        "UPDATE twd_transactions SET category=?, subcategory=? WHERE id=?",
        ("飲食", "咖啡", rows[0]["id"]),
    )
    store.conn.execute(
        "UPDATE twd_transactions SET category=?, subcategory=? WHERE id=?",
        ("購物", "咖啡", rows[1]["id"]),
    )
    store.conn.commit()
    store.close()

    response = client.put(
        "/rules/subcategories",
        headers=headers,
        json={"category": "飲食", "old_name": "咖啡", "name": "咖啡廳"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["rules_updated"] == 1
    assert response.json()["transactions_updated"] == 1
    rules = client.get("/rules", headers=headers).json()
    assert any(r["category"] == "飲食" and r["subcategory"] == "咖啡廳" for r in rules)
    assert any(r["category"] == "購物" and r["subcategory"] == "咖啡" for r in rules)
    store = BankStore("sinopac", user_id=user["user_id"])
    labels = [
        (row["category"], row["subcategory"])
        for row in store.conn.execute(
            "SELECT category, subcategory FROM twd_transactions WHERE user_id=? ORDER BY id",
            (user["user_id"],),
        ).fetchall()
    ]
    store.close()
    assert labels == [("飲食", "咖啡廳"), ("購物", "咖啡")]


def test_delete_subcategory_clears_label_without_deleting_rule(client):
    from backend.core.store import BankStore

    user = client.post(
        "/auth/register",
        json={"email": "subcategory-delete@palace.example", "password": "SyntheticTestPassword02!"},
    ).json()
    headers = {"Authorization": f"Bearer {user['token']}"}
    created = client.post(
        "/rules",
        headers=headers,
        json={"name": "breakfast-custom", "pattern": "早餐店",
              "category": "飲食", "subcategory": "早餐"},
    ).json()
    store = BankStore("sinopac", user_id=user["user_id"])
    store.upsert_twd_txns([{
        "account_no": "001", "datetime": "2026-07-20", "desc": "早餐店",
        "expend": 80, "income": None, "balance": 920,
    }])
    store.conn.execute(
        "UPDATE twd_transactions SET category=?, subcategory=? WHERE user_id=?",
        ("飲食", "早餐", user["user_id"]),
    )
    store.conn.commit()
    store.close()

    response = client.request(
        "DELETE", "/rules/subcategories", headers=headers,
        json={"category": "飲食", "name": "早餐"},
    )

    assert response.status_code == 200, response.text
    rule = next(r for r in client.get("/rules", headers=headers).json() if r["id"] == created["id"])
    assert rule["subcategory"] is None
    store = BankStore("sinopac", user_id=user["user_id"])
    row = store.conn.execute(
        "SELECT category, subcategory FROM twd_transactions WHERE user_id=?",
        (user["user_id"],),
    ).fetchone()
    store.close()
    assert row is not None
    assert (row["category"], row["subcategory"]) == ("飲食", None)


def test_manage_subcategories_includes_transaction_only_label(client):
    from backend.core.store import BankStore

    user = client.post(
        "/auth/register",
        json={"email": "subcategory-orphan@palace.example", "password": "SyntheticTestPassword02!"},
    ).json()
    headers = {"Authorization": f"Bearer {user['token']}"}
    store = BankStore("sinopac", user_id=user["user_id"])
    store.upsert_twd_txns([{
        "account_no": "001", "datetime": "2026-07-20", "desc": "手動分類",
        "expend": 80, "income": None, "balance": 920,
    }])
    store.conn.execute(
        "UPDATE twd_transactions SET category=?, subcategory=? WHERE user_id=?",
        ("飲食", "深夜食堂", user["user_id"]),
    )
    store.conn.commit()
    store.close()

    response = client.get(
        "/rules/subcategories", headers=headers,
        params={"category": "飲食", "include_all": "true"},
    )

    assert response.status_code == 200, response.text
    assert "深夜食堂" in response.json()["subcategories"]
