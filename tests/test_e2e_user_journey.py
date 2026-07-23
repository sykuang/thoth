"""L1 — Backend E2E user journey tests.

每個 test 模擬一個真實 user 從零開始的完整旅程：
  register → set creds → trigger sync → query data → add rule → recategorize

不 mock route handlers，只 mock `_dispatch_crawler_and_persist`（不真開 chromium 打銀行）。
其餘全走真實 FastAPI dispatch + 真實 SQLite I/O + 真實 JWT verify + 真實 Fernet encrypt。

對比現有 test：
  - test_auth_routes.py 等只測單一 endpoint isolation
  - 這裡測「多 endpoint 串起來的 user journey」端到端一致性

Schema 註記（避免下次寫 test 又錯名）：
  GET /credentials → [{bank, has_creds, fields_set}]
  GET /sync/jobs/{id} → {id, user_id, bank, status, created_at, started_at,
                          finished_at, error_msg, result_summary}
  POST /rules/recategorize → {total_rows, updated, skipped, per_bank}
  POST /rules/preview → {pattern, matched_indices, matched_count, total}
"""
from __future__ import annotations

import time

import pytest


# ============================================================
# helpers
# ============================================================

def register(client, email="newuser@palace.example", password="SyntheticTestPassword03!"):
    r = client.post("/auth/register", json={"email": email, "password": password})
    assert r.status_code == 201, r.text
    return r.json()["token"]


def headers(token):
    return {"Authorization": f"Bearer {token}"}


def mock_dispatch(monkeypatch, *, delta=None, fail=False, sleep=0.0):
    """Mock _dispatch_crawler_and_persist 不要真的開 chromium。

    真實簽章: (bank: str, user_id: int, headless: bool = True) -> dict
    本 mock 不真的插資料到 bank.sqlite（recategorize 會掃空表，affected=0 也 OK）。
    """
    import backend.server.sync_runner as sr

    def fake(bank: str, user_id: int, headless: bool = True):
        if sleep:
            time.sleep(sleep)
        if fail:
            raise RuntimeError(f"simulated crawler failure for {bank}")
        return {
            "delta": delta or {"twd_txn_new": 5, "card_billed_new": 2},
            "stats": {"accounts": 1, "cards": 1, "twd_transactions": 5},
        }

    monkeypatch.setattr(sr, "_dispatch_crawler_and_persist", fake)


def wait_for_job(client, token, job_id, max_wait=3.0, poll=0.05):
    """輪詢 job 直到 terminal status 或 timeout。"""
    deadline = time.time() + max_wait
    last_job = None
    while time.time() < deadline:
        r = client.get(f"/sync/jobs/{job_id}", headers=headers(token))
        assert r.status_code == 200, r.text
        last_job = r.json()
        if last_job["status"] in {"done", "failed"}:
            return last_job
        time.sleep(poll)
    pytest.fail(f"job {job_id} did not finish within {max_wait}s; last={last_job}")


# ============================================================
# E2E test 1: 完整新使用者旅程
# ============================================================

def test_e2e_full_user_journey_happy_path(client, monkeypatch):
    """新使用者：register → set creds → sync → 看 jobs → recategorize。

    驗證 6 個 endpoint 端到端：
      POST /auth/register
      PUT  /credentials/sinopac
      GET  /credentials
      POST /sync/sinopac
      GET  /sync/jobs/{id}
      POST /rules/recategorize
    """
    mock_dispatch(monkeypatch)
    token = register(client, email="journey1@palace.example")

    # === Step 1: 設永豐憑證（3 欄）===
    r = client.put(
        "/credentials/sinopac",
        headers=headers(token),
        json={"national_id": "B123456789", "user_code": "myuser", "password": "secret-pw"},
    )
    assert r.status_code == 204, r.text

    # === Step 2: 列 credentials 確認寫進去 ===
    r = client.get("/credentials", headers=headers(token))
    assert r.status_code == 200
    creds = r.json()
    sinopac_entry = next((c for c in creds if c["bank"] == "sinopac"), None)
    assert sinopac_entry is not None, f"sinopac not in {creds}"
    assert sinopac_entry["has_creds"] is True
    assert set(sinopac_entry["fields_set"]) == {"national_id", "user_code", "password"}

    # === Step 3: 觸發 sync ===
    r = client.post("/sync/sinopac", headers=headers(token), json={})
    assert r.status_code == 202, r.text
    job_id = r.json()["job_id"]
    assert isinstance(job_id, int)

    # === Step 4: 輪詢 job 直到 done ===
    job = wait_for_job(client, token, job_id)
    assert job["status"] == "done", f"sync failed: {job}"
    assert job["bank"] == "sinopac"
    assert job["error_msg"] is None
    assert job["result_summary"]  # JSON string with crawler delta + stats

    # === Step 5: 列 jobs 看到剛才那筆 ===
    r = client.get("/sync/jobs", headers=headers(token))
    assert r.status_code == 200
    jobs = r.json()
    assert any(j["id"] == job_id for j in jobs)

    # === Step 6: recategorize（即使沒手動加 rule，register 已 seed 10 條 default）===
    r = client.post("/rules/recategorize", headers=headers(token))
    assert r.status_code == 200, r.text
    result = r.json()
    assert "total_rows" in result
    assert "updated" in result
    assert "skipped" in result
    assert "per_bank" in result


# ============================================================
# E2E test 2: sync 失敗 → job status = failed + error_msg 記錄
# ============================================================

def test_e2e_sync_failure_records_error_msg(client, monkeypatch):
    """模擬爬蟲炸 → job status=failed + error_msg 含原因。

    驗證錯誤 propagate 端到端正確：crawler raise → sync_runner catch →
    sync_jobs.error_msg 寫入 → GET /sync/jobs/{id} 看得到。
    """
    mock_dispatch(monkeypatch, fail=True)
    token = register(client, email="fail@palace.example")

    client.put(
        "/credentials/sinopac",
        headers=headers(token),
        json={"national_id": "A1", "user_code": "u", "password": "p"},
    )

    r = client.post("/sync/sinopac", headers=headers(token), json={})
    assert r.status_code == 202
    job_id = r.json()["job_id"]

    job = wait_for_job(client, token, job_id)
    assert job["status"] == "failed"
    assert job["error_msg"] is not None
    assert "simulated crawler failure" in job["error_msg"]
    # result_summary 不該被填（因為 except 之後沒寫 summary）
    # （sync_runner 寫法是 error 設了就走 error 分支，不寫 result_summary）


# ============================================================
# E2E test 3: per-user 隔離（creds + jobs + rules 三層）
# ============================================================

def test_e2e_per_user_isolation_creds_jobs_rules(client, monkeypatch):
    """User A 看不到 User B 的 credentials / jobs / rules。

    驗證跨 endpoint 的 user_id 隔離一致性。
    """
    mock_dispatch(monkeypatch)
    token_a = register(client, email="alice@palace.example")
    token_b = register(client, email="bob@palace.example")

    # A 設 cathay creds + sync
    r = client.put(
        "/credentials/cathay",
        headers=headers(token_a),
        json={"cust_id": "A001", "user_id": "alice", "password": "alice-pw"},
    )
    assert r.status_code == 204, r.text
    r = client.post("/sync/cathay", headers=headers(token_a), json={})
    a_job_id = r.json()["job_id"]
    wait_for_job(client, token_a, a_job_id)

    # B 列 credentials → cathay 該是 has_creds=False（B 自己沒設）
    r = client.get("/credentials", headers=headers(token_b))
    assert r.status_code == 200
    b_creds = r.json()
    cathay_for_b = next((c for c in b_creds if c["bank"] == "cathay"), None)
    assert cathay_for_b is not None  # endpoint 永遠列所有 bank
    assert cathay_for_b["has_creds"] is False, f"B 不該看到 A 的 cathay creds: {cathay_for_b}"
    assert cathay_for_b["fields_set"] == []

    # B 列 jobs → 不該看到 A 的 sync job
    r = client.get("/sync/jobs", headers=headers(token_b))
    assert r.status_code == 200
    b_jobs = r.json()
    assert not any(j["id"] == a_job_id for j in b_jobs), f"B 看到 A 的 job: {b_jobs}"

    # B 直接 GET A 的 job → 404 (隱藏存在性)
    r = client.get(f"/sync/jobs/{a_job_id}", headers=headers(token_b))
    assert r.status_code == 404, f"expected 404, got {r.status_code}: {r.text}"

    # A 列 rules（register 已 seed default）
    r = client.get("/rules", headers=headers(token_a))
    assert r.status_code == 200
    a_rules = r.json()
    assert len(a_rules) >= 1, f"A 應有 default rules: {a_rules}"

    # B 也該有自己的 default rules
    r = client.get("/rules", headers=headers(token_b))
    assert r.status_code == 200
    b_rules = r.json()
    assert len(b_rules) >= 1, f"B 應有自己的 default rules: {b_rules}"

    # rules id 不該重疊（各自獨立的 PK）
    a_ids = {r["id"] for r in a_rules}
    b_ids = {r["id"] for r in b_rules}
    assert a_ids.isdisjoint(b_ids), f"rules id 重疊: A={a_ids} B={b_ids}"

    # 所有 a_rules 都該屬於 user_a，b 同理
    for rule in a_rules:
        assert rule["user_id"] == a_rules[0]["user_id"]
    for rule in b_rules:
        assert rule["user_id"] == b_rules[0]["user_id"]
    assert a_rules[0]["user_id"] != b_rules[0]["user_id"]


# ============================================================
# E2E test 4: 新增規則 → preview → update → delete 完整 lifecycle
# ============================================================

def test_e2e_rule_full_lifecycle(client, monkeypatch):
    """加自訂 rule → preview → recategorize → toggle → delete。

    驗證 rules engine 端到端：CRUD + 預覽 + 套用。
    """
    mock_dispatch(monkeypatch, delta={"twd_txn_new": 3, "card_billed_new": 0})
    token = register(client, email="rules@palace.example")

    # 設 creds + sync (灌些假料但 mock 不真的插)
    client.put(
        "/credentials/sinopac",
        headers=headers(token),
        json={"national_id": "A1", "user_code": "u", "password": "p"},
    )
    r = client.post("/sync/sinopac", headers=headers(token), json={})
    wait_for_job(client, token, r.json()["job_id"])

    # === Step 1: 新增自訂 rule ===
    r = client.post(
        "/rules",
        headers=headers(token),
        json={
            "name": "測試咖啡店",
            "pattern": "星巴克|路易莎",
            "category": "餐飲",
            "priority": 1,
            "enabled": True,
        },
    )
    assert r.status_code == 201, r.text
    new_rule = r.json()
    rule_id = new_rule["id"]
    assert new_rule["category"] == "餐飲"
    assert new_rule["enabled"] == 1
    assert new_rule["priority"] == 1

    # === Step 2: preview 看哪些 sample 會 match ===
    r = client.post(
        "/rules/preview",
        headers=headers(token),
        json={
            "pattern": "星巴克|路易莎",
            "sample_texts": ["星巴克 信義店", "麥當勞", "路易莎 大安店", "全聯"],
        },
    )
    assert r.status_code == 200, r.text
    preview = r.json()
    assert preview["pattern"] == "星巴克|路易莎"
    assert preview["total"] == 4
    assert preview["matched_count"] == 2
    assert preview["matched_indices"] == [0, 2]

    # === Step 3: recategorize ===
    r = client.post("/rules/recategorize", headers=headers(token))
    assert r.status_code == 200, r.text
    rc = r.json()
    assert rc["total_rows"] >= 0  # 可能 0（mock 沒真寫資料）
    assert isinstance(rc["per_bank"], dict)

    # === Step 4: update rule (toggle disabled + 改 priority) ===
    r = client.put(
        f"/rules/{rule_id}",
        headers=headers(token),
        json={"enabled": False, "priority": 999},
    )
    assert r.status_code == 200, r.text
    updated = r.json()
    assert updated["id"] == rule_id
    assert updated["enabled"] == 0
    assert updated["priority"] == 999

    # === Step 5: 刪 rule ===
    r = client.delete(f"/rules/{rule_id}", headers=headers(token))
    assert r.status_code in (200, 204), r.text

    # === Step 6: GET 看不到剛才那條 ===
    r = client.get("/rules", headers=headers(token))
    rules = r.json()
    assert not any(rule["id"] == rule_id for rule in rules), \
        f"deleted rule 還在: {rules}"


# ============================================================
# E2E test 5: 401 unauthorized chain（所有 protected endpoint 一致）
# ============================================================

def test_e2e_unauthorized_consistent_across_endpoints(client):
    """所有 protected endpoint 對無 token / 假 token 都該回 401。"""
    protected = [
        ("GET", "/auth/me"),
        ("GET", "/credentials"),
        ("PUT", "/credentials/sinopac"),
        ("POST", "/sync/sinopac"),
        ("GET", "/sync/jobs"),
        ("GET", "/sync/jobs/1"),
        ("GET", "/rules"),
        ("POST", "/rules"),
        ("POST", "/rules/recategorize"),
        ("POST", "/rules/preview"),
        ("GET", "/rules/categories"),
    ]
    for method, path in protected:
        # 無 token
        r = client.request(method, path, json={} if method != "GET" else None)
        assert r.status_code == 401, f"{method} {path} no-token expected 401, got {r.status_code}"
        # 假 token
        r = client.request(
            method, path,
            headers={"Authorization": "Bearer fake-token-xyz"},
            json={} if method != "GET" else None,
        )
        assert r.status_code == 401, \
            f"{method} {path} bad-token expected 401, got {r.status_code}: {r.text}"
