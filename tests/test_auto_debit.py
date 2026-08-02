"""Tests for /cards/auto-debit/* endpoints (Phase L10, 2026-06-20).

Coverage:
  - settings CRUD: GET/PUT/DELETE
  - eligible-accounts picker: TWD-only filter (excludes 外幣 / excluded / 貸款型)
  - reminders edge cases:
    * no setting + due in 3 days → 'no_account'
    * setting + balance < bill_due_amount → 'insufficient' with shortfall
    * setting + balance >= bill_due_amount → NO reminder
    * bill_due_amount = 0 → NO reminder (no_payment_required, C3)
    * due > 3 days away → NO reminder (D3)
    * already overdue (days < 0) → NO reminder (走 cards bill_status)
    * card excluded → NO reminder
    * validation: account_no must be TWD active deposit (422)
"""
from __future__ import annotations

from collections.abc import Iterator
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

TEST_TODAY = date(2026, 6, 30)


@pytest.fixture
def client(monkeypatch, tmp_path) -> Iterator[TestClient]:
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("JWT_SECRET", "test-secret-1234567890" * 4)
    monkeypatch.delenv("SERVER_API_KEY", raising=False)

    # 重 import app: db.py 用 module-level _DB_PATH cache
    import importlib

    import backend.server.app as app_mod
    import backend.server.db as db_mod
    import backend.server.routers.auto_debit as auto_debit_mod
    importlib.reload(db_mod)
    importlib.reload(app_mod)
    monkeypatch.setattr(auto_debit_mod, "_local_date", lambda _tz: TEST_TODAY)

    with TestClient(app_mod.app) as c:
        yield c


def _register(client: TestClient) -> tuple[int, str]:
    """Register a fresh user, return (user_id, jwt_token)."""
    r = client.post(
        "/auth/register",
        json={"email": "u@test.com", "password": "real-password-123"},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    return body["user_id"], body["token"]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _setup_bank_data(
    bank: str, user_id: int, *,
    accounts: list[dict] | None = None,
    cards: list[dict] | None = None,
) -> None:
    """Create a bank sqlite + write accounts/cards via BankStore."""
    from backend.core.store import BankStore
    store = BankStore(bank, user_id=user_id)
    if accounts:
        store.upsert_accounts(accounts)
    if cards:
        store.upsert_cards(cards)
    store.close()


# ============================================================
# Settings CRUD
# ============================================================


def test_settings_empty_when_unset(client: TestClient) -> None:
    _, token = _register(client)
    r = client.get("/cards/auto-debit/settings", headers=_auth(token))
    assert r.status_code == 200
    assert r.json() == []


def test_put_setting_persists_and_get_reads_back(client: TestClient) -> None:
    user_id, token = _register(client)
    # 先有 sinopac TWD 戶才能設
    _setup_bank_data("sinopac", user_id, accounts=[
        {"account_no": "90000000197014", "currency": "TWD", "type": "活儲",
         "raw_balance": 50000.0, "raw_balance_date": "2026-06-20"},
    ])
    r = client.put(
        "/cards/auto-debit/settings/ctbc",
        headers=_auth(token),
        json={"account_bank": "sinopac", "account_no": "90000000197014"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["card_bank"] == "ctbc"
    assert r.json()["account_bank"] == "sinopac"

    r = client.get("/cards/auto-debit/settings", headers=_auth(token))
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) == 1
    assert rows[0]["card_bank"] == "ctbc"
    assert rows[0]["account_no"] == "90000000197014"


def test_put_setting_validates_account_currency_must_be_twd(client: TestClient) -> None:
    user_id, token = _register(client)
    _setup_bank_data("sinopac", user_id, accounts=[
        {"account_no": "90000000187013", "currency": "JPY", "type": "外幣活存",
         "raw_balance": 1000000.0, "raw_balance_date": "2026-06-20"},
    ])
    r = client.put(
        "/cards/auto-debit/settings/ctbc",
        headers=_auth(token),
        json={"account_bank": "sinopac", "account_no": "90000000187013"},
    )
    assert r.status_code == 422
    assert "TWD" in r.json()["detail"]


def test_put_setting_validates_account_no_must_exist(client: TestClient) -> None:
    _, token = _register(client)
    r = client.put(
        "/cards/auto-debit/settings/ctbc",
        headers=_auth(token),
        json={"account_bank": "sinopac", "account_no": "DOES_NOT_EXIST"},
    )
    assert r.status_code == 422


def test_delete_setting_clears_it(client: TestClient) -> None:
    user_id, token = _register(client)
    _setup_bank_data("sinopac", user_id, accounts=[
        {"account_no": "ACC1", "currency": "TWD", "type": "活儲",
         "raw_balance": 10000.0, "raw_balance_date": "2026-06-20"},
    ])
    client.put(
        "/cards/auto-debit/settings/cathay",
        headers=_auth(token),
        json={"account_bank": "sinopac", "account_no": "ACC1"},
    )
    r = client.delete("/cards/auto-debit/settings/cathay", headers=_auth(token))
    assert r.status_code == 204

    r = client.get("/cards/auto-debit/settings", headers=_auth(token))
    assert r.json() == []


def test_put_setting_rejects_unknown_card_bank(client: TestClient) -> None:
    _, token = _register(client)
    r = client.put(
        "/cards/auto-debit/settings/fake_bank",
        headers=_auth(token),
        json={"account_bank": "sinopac", "account_no": "ACC1"},
    )
    assert r.status_code == 422


# ============================================================
# Eligible accounts picker (G4)
# ============================================================


def test_eligible_accounts_only_returns_twd_active_deposit(
    client: TestClient,
) -> None:
    user_id, token = _register(client)
    _setup_bank_data("sinopac", user_id, accounts=[
        {"account_no": "TWD1", "currency": "TWD", "type": "活儲",
         "raw_balance": 100000.0, "raw_balance_date": "2026-06-20"},
        {"account_no": "JPY1", "currency": "JPY", "type": "外幣活存",
         "raw_balance": 500000.0, "raw_balance_date": "2026-06-20"},
    ])
    _setup_bank_data("scsb", user_id, accounts=[
        {"account_no": "LOAN1", "currency": "TWD", "type": "貸款",
         "product_type": "loan",
         "raw_balance": 20589800.0, "raw_balance_date": "2026-06-20"},
        {"account_no": "TWDOK", "currency": "TWD", "type": "活儲",
         "raw_balance": 13065.0, "raw_balance_date": "2026-06-20"},
    ])

    r = client.get("/cards/auto-debit/eligible-accounts", headers=_auth(token))
    assert r.status_code == 200
    accts = r.json()
    nos = sorted([a["account_no"] for a in accts])
    assert nos == ["90000000197014", "TWD1", "TWDOK"] or nos == ["TWD1", "TWDOK"], (
        f"got: {nos}"
    )
    # 確認外幣戶 + loan 戶被過濾
    assert "JPY1" not in nos
    assert "LOAN1" not in nos


# ============================================================
# Reminders (logic core)
# ============================================================


def _today_plus(days: int) -> str:
    return (TEST_TODAY + timedelta(days=days)).isoformat()


def test_reminder_no_account_when_due_in_3_days_and_no_setting(
    client: TestClient,
) -> None:
    """E1+F2 'no_account': 沒設定 + due 在 3 天內 → 提醒."""
    user_id, token = _register(client)
    _setup_bank_data("ctbc", user_id, cards=[{
        "number": "****7016", "name": "中信慶豐卡", "association": "VISA",
        "type": "信用卡", "credit_limit": 50000, "used_credit": 15000,
        "statement_close_date": _today_plus(-25),
        "payment_due_date": _today_plus(2),  # 2 天後到期
        "bill_due_amount": 15000.0,
    }])

    r = client.get("/cards/auto-debit/reminders", headers=_auth(token))
    assert r.status_code == 200, r.text
    reminders = r.json()
    assert len(reminders) == 1
    rem = reminders[0]
    assert rem["reason"] == "no_account"
    assert rem["card_bank"] == "ctbc"
    assert rem["bill_due_amount"] == 15000.0
    assert rem["days_until_due"] == 2
    assert rem["account_bank"] is None


def test_non_hsbc_shared_bank_bill_emits_one_bank_level_reminder(
    client: TestClient,
) -> None:
    """非 HSBC 的整戶帳單複寫到多卡時，只能提醒一次且不可冒充任一卡。"""
    user_id, token = _register(client)
    _setup_bank_data("ctbc", user_id, cards=[{
        "number": "****3433", "name": "英雄聯盟卡",
        "payment_due_date": _today_plus(3),
        "bill_due_amount": 27916.0,
    }, {
        "number": "****3443", "name": "中華航空聯名卡",
        "payment_due_date": _today_plus(3),
        "bill_due_amount": 27916.0,
    }, {
        "number": "****5733", "name": "中油聯名卡",
        "payment_due_date": _today_plus(3),
        "bill_due_amount": 27916.0,
    }])

    r = client.get("/cards/auto-debit/reminders", headers=_auth(token))

    assert r.status_code == 200, r.text
    assert r.json() == [{
        "reason": "no_account",
        "card_bank": "ctbc",
        "card_no": "",
        "card_name": None,
        "bill_due_amount": 27916.0,
        "payment_due_date": _today_plus(3),
        "days_until_due": 3,
        "account_bank": None,
        "account_no": None,
        "account_balance": None,
        "shortfall": None,
    }]


def test_hsbc_same_due_date_and_amount_remain_per_card_reminders(
    client: TestClient,
) -> None:
    """HSBC 的帳單是 per-card；即使日期與金額相同也不可合併。"""
    user_id, token = _register(client)
    _setup_bank_data("hsbc", user_id, cards=[{
        "number": "****3254", "name": "滙豐旅人無限卡",
        "payment_due_date": _today_plus(3),
        "bill_due_amount": 12729.0,
    }, {
        "number": "****8926", "name": "滙豐 Live+ 現金回饋卡",
        "payment_due_date": _today_plus(3),
        "bill_due_amount": 12729.0,
    }])

    r = client.get("/cards/auto-debit/reminders", headers=_auth(token))

    assert r.status_code == 200, r.text
    reminders = r.json()
    assert len(reminders) == 2
    assert {row["card_no"] for row in reminders} == {"****3254", "****8926"}
    assert {row["card_name"] for row in reminders} == {
        "滙豐旅人無限卡", "滙豐 Live+ 現金回饋卡",
    }


def test_non_hsbc_shared_bill_compares_balance_once_without_multiplying_due(
    client: TestClient,
) -> None:
    """實際截圖路徑：多卡共用 27,916，只能算一次 shortfall，不能乘卡數。"""
    user_id, token = _register(client)
    _setup_bank_data("ctbc", user_id, cards=[{
        "number": "****3433", "name": "英雄聯盟卡",
        "payment_due_date": _today_plus(3),
        "bill_due_amount": 27916.0,
    }, {
        "number": "****3443", "name": "中華航空聯名卡",
        "payment_due_date": _today_plus(3),
        "bill_due_amount": 27916.0,
    }])
    _setup_bank_data("sinopac", user_id, accounts=[{
        "account_no": "TWD1", "currency": "TWD", "type": "活儲",
        "raw_balance": 10000.0, "raw_balance_date": _today_plus(-1),
    }])
    client.put(
        "/cards/auto-debit/settings/ctbc",
        headers=_auth(token),
        json={"account_bank": "sinopac", "account_no": "TWD1"},
    )

    r = client.get("/cards/auto-debit/reminders", headers=_auth(token))

    assert r.status_code == 200, r.text
    reminders = r.json()
    assert len(reminders) == 1
    assert reminders[0]["reason"] == "insufficient"
    assert reminders[0]["card_no"] == ""
    assert reminders[0]["bill_due_amount"] == 27916.0
    assert reminders[0]["shortfall"] == 17916.0


def test_reminder_insufficient_when_balance_below_due(client: TestClient) -> None:
    """C3+F2 'insufficient': balance < bill_due_amount → 提醒 + shortfall."""
    user_id, token = _register(client)
    _setup_bank_data("ctbc", user_id, cards=[{
        "number": "****7016", "name": "中信慶豐卡",
        "payment_due_date": _today_plus(1),
        "bill_due_amount": 15000.0,
    }])
    _setup_bank_data("sinopac", user_id, accounts=[
        {"account_no": "TWD1", "currency": "TWD", "type": "活儲",
         "raw_balance": 8000.0, "raw_balance_date": _today_plus(-1)},
    ])
    client.put(
        "/cards/auto-debit/settings/ctbc",
        headers=_auth(token),
        json={"account_bank": "sinopac", "account_no": "TWD1"},
    )

    r = client.get("/cards/auto-debit/reminders", headers=_auth(token))
    reminders = r.json()
    assert len(reminders) == 1
    rem = reminders[0]
    assert rem["reason"] == "insufficient"
    assert rem["account_balance"] == 8000.0
    assert rem["shortfall"] == 7000.0
    assert rem["account_no"] == "TWD1"


def test_no_reminder_when_balance_sufficient(client: TestClient) -> None:
    """有設定 + balance >= due → 不該有 reminder."""
    user_id, token = _register(client)
    _setup_bank_data("ctbc", user_id, cards=[{
        "number": "****7016",
        "payment_due_date": _today_plus(1),
        "bill_due_amount": 5000.0,
    }])
    _setup_bank_data("sinopac", user_id, accounts=[
        {"account_no": "TWD1", "currency": "TWD", "type": "活儲",
         "raw_balance": 50000.0, "raw_balance_date": _today_plus(-1)},
    ])
    client.put(
        "/cards/auto-debit/settings/ctbc",
        headers=_auth(token),
        json={"account_bank": "sinopac", "account_no": "TWD1"},
    )

    r = client.get("/cards/auto-debit/reminders", headers=_auth(token))
    assert r.json() == []


def test_no_reminder_when_bill_due_amount_zero(client: TestClient) -> None:
    """C3: bill_due_amount = 0 → no_payment_required → 不該 reminder."""
    user_id, token = _register(client)
    _setup_bank_data("ctbc", user_id, cards=[{
        "number": "****7016",
        "payment_due_date": _today_plus(1),
        "bill_due_amount": 0.0,
    }])

    r = client.get("/cards/auto-debit/reminders", headers=_auth(token))
    assert r.json() == []


def test_no_reminder_when_due_more_than_3_days_away(client: TestClient) -> None:
    """D3: due_days > 3 → 不該 reminder."""
    user_id, token = _register(client)
    _setup_bank_data("ctbc", user_id, cards=[{
        "number": "****7016",
        "payment_due_date": _today_plus(7),
        "bill_due_amount": 15000.0,
    }])

    r = client.get("/cards/auto-debit/reminders", headers=_auth(token))
    assert r.json() == []


def test_no_reminder_when_already_overdue(client: TestClient) -> None:
    """D3: due_days < 0 (過期) → 走 cards bill_status='overdue', 不在 reminder."""
    user_id, token = _register(client)
    _setup_bank_data("ctbc", user_id, cards=[{
        "number": "****7016",
        "payment_due_date": _today_plus(-5),
        "bill_due_amount": 15000.0,
    }])

    r = client.get("/cards/auto-debit/reminders", headers=_auth(token))
    assert r.json() == []


def test_reminder_includes_due_today_zero_days(client: TestClient) -> None:
    """D3 edge: days_until_due = 0 (今天到期) → 應該 reminder."""
    user_id, token = _register(client)
    _setup_bank_data("ctbc", user_id, cards=[{
        "number": "****7016",
        "payment_due_date": _today_plus(0),
        "bill_due_amount": 15000.0,
    }])

    r = client.get("/cards/auto-debit/reminders", headers=_auth(token))
    assert len(r.json()) == 1
    assert r.json()[0]["days_until_due"] == 0


def test_reminders_sorted_by_urgency_then_amount(client: TestClient) -> None:
    """不同帳單事實保留，再按 days_until_due / bill_due_amount 排序。"""
    user_id, token = _register(client)
    _setup_bank_data("ctbc", user_id, cards=[{
        "number": "****7015",
        "payment_due_date": _today_plus(3),
        "bill_due_amount": 50000.0,
    }])
    _setup_bank_data("cathay", user_id, cards=[{
        "number": "****7026",
        "payment_due_date": _today_plus(1),
        "bill_due_amount": 8000.0,
    }, {
        "number": "****7035",
        "payment_due_date": _today_plus(1),
        "bill_due_amount": 30000.0,
    }])

    r = client.get("/cards/auto-debit/reminders", headers=_auth(token))
    reminders = r.json()
    assert len(reminders) == 3
    # 金額不同就不是同一帳單事實，不可硬合併。
    assert reminders[0]["days_until_due"] == 1
    assert reminders[0]["bill_due_amount"] == 30000.0
    assert reminders[1]["days_until_due"] == 1
    assert reminders[1]["bill_due_amount"] == 8000.0
    assert reminders[2]["days_until_due"] == 3
