"""Phase 6 — /portfolio/accounts endpoint tests.

回每帳戶最新餘額清單，給 frontend「帳戶」tab 用。

語意定義:
  - 一 row = 一個 account (來自該銀行 accounts 表)
  - balance = 該 account_no 在 twd_transactions 最新 txn_datetime 的 balance
    (不分 currency, sinopac JPY 帳戶 balance 直接以 JPY 存進去)
  - 若 account_no 沒對應 txn → balance=None, snapshot_date=accounts.updated_at[:10]
  - is_stale = snapshot 超過 7 天 (比 /portfolio/summary 的 90 天嚴格)
  - 沒 accounts 表 / 沒 rows 的銀行直接 skip, 不 raise
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

# Note: client/TestClient/app fixtures all come from conftest.py to ensure
# JWT_SECRET / Fernet key / tmp_path isolation. Do NOT add local `client`
# fixture here — see the comment near auth_headers below.


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def _seed_accounts_db(root: Path, bank: str, *,
                      accounts: list[dict] | None = None,
                      txns: list[dict] | None = None,
                      include_accounts_table: bool = True) -> Path:
    """建一顆 mini sqlite, 只有 portfolio.accounts router 需要的兩張表.

    accounts: [{account_no, currency, nickname, type, product_type, updated_at?,
                raw_balance?, raw_balance_date?}, ...]
    txns:     [{account_no, txn_datetime, balance}, ...]
    """
    path = root / f"{bank}.sqlite"
    con = sqlite3.connect(str(path))
    con.row_factory = sqlite3.Row
    if include_accounts_table:
        con.executescript("""
            CREATE TABLE IF NOT EXISTS accounts (
                account_no       TEXT PRIMARY KEY,
                currency         TEXT,
                branch           TEXT,
                nickname         TEXT,
                type             TEXT,
                product_type     TEXT,
                raw_balance      REAL,
                raw_balance_date TEXT,
                excluded         INTEGER NOT NULL DEFAULT 0,
                updated_at       TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS twd_transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_no   TEXT NOT NULL,
                txn_datetime TEXT NOT NULL,
                balance      INTEGER,
                first_seen   TEXT NOT NULL,
                dedup_key    TEXT NOT NULL
            );
        """)
    else:
        # 故意不建 accounts 表 (模擬 hsbc/fubon/scb 沒帳戶表的情況)
        con.executescript("""
            CREATE TABLE IF NOT EXISTS twd_transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_no   TEXT NOT NULL,
                txn_datetime TEXT NOT NULL,
                balance      INTEGER,
                first_seen   TEXT NOT NULL,
                dedup_key    TEXT NOT NULL
            );
        """)
    now = _utcnow_iso()
    if include_accounts_table:
        for a in (accounts or []):
            con.execute(
                """INSERT INTO accounts (account_no, currency, branch, nickname,
                                          type, product_type,
                                          raw_balance, raw_balance_date,
                                          updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (a["account_no"], a.get("currency", "TWD"), a.get("branch"),
                 a.get("nickname"), a.get("type"),
                 a.get("product_type", "deposit"),
                 a.get("raw_balance"), a.get("raw_balance_date"),
                 a.get("updated_at") or now),
            )
    for i, t in enumerate(txns or []):
        con.execute(
            """INSERT INTO twd_transactions
               (account_no, txn_datetime, balance, first_seen, dedup_key)
               VALUES (?, ?, ?, ?, ?)""",
            (t["account_no"], t["txn_datetime"], t.get("balance"),
             now, f"test-{bank}-{i}"),
        )
    con.commit()
    con.close()
    return path


@pytest.fixture
def temp_data_root(tmp_path, monkeypatch):
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    return tmp_path


@pytest.fixture(autouse=True)
def _isolate_fx_service(monkeypatch):
    """Autouse: 預設讓 fx_service 全部回 None / 1.0 (TWD), 不打網路.

    任何想真實 stub 匯率的 test 用 mock_fx_service fixture 蓋掉。
    這避免任何測試意外打台銀 / open.er-api (CI 沒網會 hang/fail).
    """
    from backend.server import fx_service as fx_mod
    from backend.server.routers import portfolio as portfolio_mod

    def _stub_get_rate(currency):
        if currency and currency.strip().upper() == "TWD":
            return 1.0
        return None

    def _stub_convert(amount, currency):
        if currency and currency.strip().upper() == "TWD" and amount is not None:
            return int(amount)
        return None

    monkeypatch.setattr(fx_mod, "get_rate", _stub_get_rate)
    monkeypatch.setattr(fx_mod, "convert_to_twd", _stub_convert)
    monkeypatch.setattr(portfolio_mod.fx_service, "get_rate", _stub_get_rate)
    monkeypatch.setattr(portfolio_mod.fx_service, "convert_to_twd", _stub_convert)


# `client` fixture 從 conftest.py 取得 (isolated: tmp_path + JWT_SECRET +
# Fernet key + reload), 之前本檔自定 local fixture 只 return TestClient(app)
# 把 conftest 的 isolation shadow 掉, 導致 CI runner 乾淨 env 跑 register/login
# JWT 永遠拿不到 → CI 從 init commit 起 30 次全紅。2026-06-18 修法：刪 local
# fixture, 改用 conftest 的, CI 全綠。


@pytest.fixture
def auth_headers(client):
    email = f"portfolio-accounts-test-{datetime.now().timestamp()}@example.com"
    resp = client.post("/auth/register",
                       json={"email": email, "password": "Password123!"})
    assert resp.status_code == 201, resp.text
    token = resp.json()["token"]
    return {"Authorization": f"Bearer {token}"}


# ============================================================
# Empty case
# ============================================================

def test_portfolio_accounts_empty(temp_data_root, client, auth_headers):
    """沒任何 sqlite → 回 []."""
    r = client.get("/portfolio/accounts", headers=auth_headers)
    assert r.status_code == 200, r.text
    assert r.json() == []


# ============================================================
# Happy path — 有 accounts + txn balance
# ============================================================

def test_portfolio_accounts_with_txn_balance(temp_data_root, client, auth_headers):
    """sinopac 有 accounts + twd_transactions → balance 來自 max(txn_datetime).balance."""
    _seed_accounts_db(temp_data_root, "sinopac",
        accounts=[
            {"account_no": "90000000197014", "currency": "TWD",
             "nickname": "營業部DAWHO活期儲蓄存款",
             "type": "營業部DAWHO活期儲蓄存款", "product_type": "deposit"},
        ],
        txns=[
            {"account_no": "90000000197014",
             "txn_datetime": "2026-06-12T11:19:00", "balance": 1088682},
            # 舊一筆 — 不該被用
            {"account_no": "90000000197014",
             "txn_datetime": "2026-06-09T19:13:00", "balance": 1088367},
        ])
    r = client.get("/portfolio/accounts", headers=auth_headers)
    assert r.status_code == 200, r.text
    rows = r.json()
    assert len(rows) == 1
    row = rows[0]
    assert row["bank"] == "sinopac"
    assert row["account_no"] == "90000000197014"
    assert row["currency"] == "TWD"
    assert row["balance"] == 1088682  # 取最新那筆
    assert row["snapshot_date"] == "2026-06-12"
    assert row["nickname"] == "營業部DAWHO活期儲蓄存款"
    assert row["product_type"] == "deposit"
    assert row["is_stale"] is False or row["is_stale"] is True  # 取決於跑測時間


# ============================================================
# Fallback — accounts row 但 txn 沒對應 account_no
# ============================================================

def test_portfolio_accounts_fallback_no_txn(temp_data_root, client, auth_headers):
    """cathay accounts.account_no='900000057055' 但 txn 用 zero-padded
    '0000900000057055' → 不對齊 → balance=None, snapshot_date 走 updated_at."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    updated_at = f"{today}T21:18:02.153457+00:00"
    _seed_accounts_db(temp_data_root, "cathay",
        accounts=[
            {"account_no": "900000057055", "currency": "TWD",
             "nickname": "", "type": "數位存款帳戶１—１類(原KOKO)",
             "product_type": "deposit", "updated_at": updated_at},
        ],
        txns=[
            # zero-padded mismatch — 不該對到上面的 account_no
            {"account_no": "0000900000057055",
             "txn_datetime": "2026-06-03T15:54:53", "balance": 888987},
        ])
    r = client.get("/portfolio/accounts", headers=auth_headers)
    rows = r.json()
    assert len(rows) == 1
    row = rows[0]
    assert row["bank"] == "cathay"
    assert row["account_no"] == "900000057055"
    assert row["balance"] is None  # fallback — 不推算, 不雙重計算
    assert row["snapshot_date"] == today  # 從 updated_at[:10]
    assert row["nickname"] == ""
    assert row["type"] == "數位存款帳戶１—１類(原KOKO)"


# ============================================================
# Stale window — 7 天
# ============================================================

def test_portfolio_accounts_stale_7_days(temp_data_root, client, auth_headers):
    """snapshot 8 天前 → is_stale=True (注意 /portfolio/accounts 用 7 天非 90 天)."""
    old_date = (datetime.now(timezone.utc) - timedelta(days=8)).strftime("%Y-%m-%d")
    _seed_accounts_db(temp_data_root, "ubot",
        accounts=[
            {"account_no": "090000047047", "currency": "TWD",
             "type": "活期儲蓄", "product_type": "deposit"},
        ],
        txns=[
            {"account_no": "090000047047",
             "txn_datetime": f"{old_date}T10:00:00", "balance": 100000},
        ])
    r = client.get("/portfolio/accounts", headers=auth_headers)
    rows = r.json()
    assert len(rows) == 1
    assert rows[0]["is_stale"] is True
    assert rows[0]["balance"] == 100000  # 但 balance 還是該回


def test_portfolio_accounts_fresh_within_7_days(temp_data_root, client, auth_headers):
    """snapshot 2 天前 → is_stale=False."""
    fresh_date = (datetime.now(timezone.utc) - timedelta(days=2)).strftime("%Y-%m-%d")
    _seed_accounts_db(temp_data_root, "ubot",
        accounts=[
            {"account_no": "090000047047", "currency": "TWD",
             "product_type": "deposit"},
        ],
        txns=[
            {"account_no": "090000047047",
             "txn_datetime": f"{fresh_date}T10:00:00", "balance": 100000},
        ])
    r = client.get("/portfolio/accounts", headers=auth_headers)
    rows = r.json()
    assert rows[0]["is_stale"] is False


# ============================================================
# Multi-currency — sinopac 有 TWD + JPY 帳戶
# ============================================================

def test_portfolio_accounts_multiple_currencies(temp_data_root, client, auth_headers):
    """sinopac 兩 account — TWD + JPY, 各自 currency / balance 正確."""
    _seed_accounts_db(temp_data_root, "sinopac",
        accounts=[
            {"account_no": "90000000197014", "currency": "TWD",
             "nickname": "TWD DAWHO", "type": "TWD",
             "product_type": "deposit"},
            {"account_no": "90000000187013", "currency": "JPY",
             "nickname": "JPY DAWHO", "type": "JPY",
             "product_type": "fx_deposit"},
        ],
        txns=[
            {"account_no": "90000000197014",
             "txn_datetime": "2026-06-12T11:19:00", "balance": 1088682},
            {"account_no": "90000000187013",
             "txn_datetime": "2026-05-21T01:06:00", "balance": 1201387},
        ])
    r = client.get("/portfolio/accounts", headers=auth_headers)
    rows = r.json()
    assert len(rows) == 2
    by_account = {row["account_no"]: row for row in rows}

    twd = by_account["90000000197014"]
    assert twd["currency"] == "TWD"
    assert twd["balance"] == 1088682
    assert twd["product_type"] == "deposit"

    jpy = by_account["90000000187013"]
    assert jpy["currency"] == "JPY"
    assert jpy["balance"] == 1201387  # JPY 存在 twd_transactions 直接以 JPY 存
    assert jpy["product_type"] == "fx_deposit"


# ============================================================
# Skip — 部分銀行沒 sqlite / 沒 accounts 表
# ============================================================

def test_portfolio_accounts_skips_missing_bank(temp_data_root, client, auth_headers):
    """11 家銀行中只 seed 4 家 sqlite → 不 raise, 只回 4 家的 rows."""
    _seed_accounts_db(temp_data_root, "sinopac",
        accounts=[{"account_no": "S1", "currency": "TWD",
                   "product_type": "deposit"}],
        txns=[{"account_no": "S1", "txn_datetime": "2026-06-10T00:00:00",
               "balance": 1}])
    _seed_accounts_db(temp_data_root, "cathay",
        accounts=[{"account_no": "C1", "currency": "TWD",
                   "product_type": "deposit"}],
        txns=[{"account_no": "C1", "txn_datetime": "2026-06-10T00:00:00",
               "balance": 2}])
    _seed_accounts_db(temp_data_root, "ubot",
        accounts=[{"account_no": "U1", "currency": "TWD",
                   "product_type": "deposit"}],
        txns=[{"account_no": "U1", "txn_datetime": "2026-06-10T00:00:00",
               "balance": 3}])
    _seed_accounts_db(temp_data_root, "esun",
        accounts=[{"account_no": "E1", "currency": "TWD",
                   "product_type": "deposit"}],
        txns=[{"account_no": "E1", "txn_datetime": "2026-06-10T00:00:00",
               "balance": 4}])

    r = client.get("/portfolio/accounts", headers=auth_headers)
    assert r.status_code == 200
    rows = r.json()
    banks = sorted({row["bank"] for row in rows})
    assert banks == ["cathay", "esun", "sinopac", "ubot"]
    assert len(rows) == 4


def test_portfolio_accounts_skips_bank_without_accounts_table(temp_data_root, client, auth_headers):
    """hsbc 有 sqlite 但沒 accounts 表 → skip, 不 raise."""
    _seed_accounts_db(temp_data_root, "hsbc",
        include_accounts_table=False,
        txns=[])  # 只建 twd_transactions, 沒 accounts 表
    _seed_accounts_db(temp_data_root, "sinopac",
        accounts=[{"account_no": "S1", "currency": "TWD",
                   "product_type": "deposit"}],
        txns=[{"account_no": "S1", "txn_datetime": "2026-06-10T00:00:00",
               "balance": 1}])
    r = client.get("/portfolio/accounts", headers=auth_headers)
    rows = r.json()
    banks = {row["bank"] for row in rows}
    assert "hsbc" not in banks
    assert "sinopac" in banks


def test_portfolio_accounts_skips_bank_with_empty_accounts_table(temp_data_root, client, auth_headers):
    """fubon accounts 表存在但 0 row → skip, 不 raise."""
    _seed_accounts_db(temp_data_root, "fubon",
        accounts=[],  # 空 accounts
        txns=[])
    r = client.get("/portfolio/accounts", headers=auth_headers)
    assert r.status_code == 200
    assert r.json() == []


# ============================================================
# Auth
# ============================================================

def test_portfolio_accounts_requires_auth(temp_data_root, client):
    """沒 JWT → 401."""
    r = client.get("/portfolio/accounts")
    assert r.status_code == 401


# ============================================================
# Real-world shape check
# ============================================================

def test_portfolio_accounts_response_shape(temp_data_root, client, auth_headers):
    """檢驗每個 row 都有完整 9 個 schema 欄位."""
    _seed_accounts_db(temp_data_root, "sinopac",
        accounts=[{"account_no": "S1", "currency": "TWD",
                   "nickname": "n1", "type": "t1",
                   "product_type": "deposit"}],
        txns=[{"account_no": "S1", "txn_datetime": "2026-06-10T00:00:00",
               "balance": 100}])
    r = client.get("/portfolio/accounts", headers=auth_headers)
    row = r.json()[0]
    expected_keys = {"bank", "account_no", "currency", "nickname",
                     "nickname_overwrite",
                     "product_type", "type", "balance", "snapshot_date",
                     "is_stale", "twd_estimate", "fx_rate_used", "excluded"}
    assert set(row.keys()) == expected_keys


# ============================================================
# Date format normalization — 真實爬蟲層格式雜亂
# ============================================================

def test_portfolio_accounts_normalizes_sinopac_slash_date(temp_data_root, client, auth_headers):
    """sinopac txn_datetime 格式是 '2026/06/1211:19' (斜線, 日期+時間黏一起)
    必須正規化成 ISO 'YYYY-MM-DD' 給 frontend, 並讓 is_stale 能正確判斷."""
    fresh_date_slash = (datetime.now(timezone.utc) - timedelta(days=2)).strftime("%Y/%m/%d")
    fresh_date_iso = fresh_date_slash.replace("/", "-")
    _seed_accounts_db(temp_data_root, "sinopac",
        accounts=[{"account_no": "90000000197014", "currency": "TWD",
                   "product_type": "deposit"}],
        txns=[{"account_no": "90000000197014",
               "txn_datetime": f"{fresh_date_slash}11:19",  # 斜線 + 黏一起
               "balance": 1088682}])
    r = client.get("/portfolio/accounts", headers=auth_headers)
    rows = r.json()
    assert len(rows) == 1
    assert rows[0]["snapshot_date"] == fresh_date_iso  # 正規化成 ISO
    assert rows[0]["is_stale"] is False  # 2 天前不算 stale
    assert rows[0]["balance"] == 1088682


def test_portfolio_accounts_normalizes_ubot_space_date(temp_data_root, client, auth_headers):
    """ubot txn_datetime 用空白分隔 '2026-05-16 13:02:02', [:10] 就是 ISO 日期."""
    fresh_date = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
    _seed_accounts_db(temp_data_root, "ubot",
        accounts=[{"account_no": "U1", "currency": "TWD",
                   "product_type": "deposit"}],
        txns=[{"account_no": "U1",
               "txn_datetime": f"{fresh_date} 13:02:02",
               "balance": 500}])
    r = client.get("/portfolio/accounts", headers=auth_headers)
    rows = r.json()
    assert rows[0]["snapshot_date"] == fresh_date
    assert rows[0]["is_stale"] is False


# ============================================================
# Phase 6 — twd_estimate / fx_rate_used (外幣 → TWD 估值)
# ============================================================
#
# 鐵則:
#   - currency='TWD' → twd_estimate=balance, fx_rate_used=1.0 (不打網路)
#   - currency='JPY' + fx_service 回 0.2 → JPY 1000 → twd_estimate=200
#   - balance=None → twd_estimate=None, fx_rate_used=None
#   - fx_service.get_rate 回 None (該幣別抓不到) → 兩個都 None, 該 row 仍正常顯示
#   - fx_service 拋例外 → 該 row 不爆, 兩個都 None
#
# Mock 策略: monkeypatch backend.server.fx_service.get_rate 跟 convert_to_twd
# 避免真打台銀 / open.er-api (測試環境跑 CI 沒網)。

@pytest.fixture
def mock_fx_service(monkeypatch):
    """Patch fx_service so 測試控制每次 get_rate 回什麼.

    用法:
      mock_fx_service.set_rates({"JPY": 0.2, "USD": 31.5})
      → 任何 get_rate("JPY") 回 0.2, get_rate("EUR") 回 None
    """
    from backend.server.routers import portfolio as portfolio_mod

    class _Stub:
        def __init__(self):
            self.rates: dict[str, float] = {}
            self.call_count = 0

        def set_rates(self, rates: dict[str, float]):
            self.rates = {k.upper(): float(v) for k, v in rates.items()}

        def get_rate(self, currency: str) -> float | None:
            self.call_count += 1
            if not currency:
                return None
            ccy = currency.strip().upper()
            if ccy == "TWD":
                return 1.0
            return self.rates.get(ccy)

        def convert_to_twd(self, amount, currency):
            if amount is None or currency is None:
                return None
            rate = self.get_rate(currency)
            if rate is None:
                return None
            return round(float(amount) * rate)

    stub = _Stub()
    monkeypatch.setattr(portfolio_mod.fx_service, "get_rate", stub.get_rate)
    monkeypatch.setattr(portfolio_mod.fx_service, "convert_to_twd", stub.convert_to_twd)
    return stub


def test_portfolio_accounts_twd_estimate_for_twd_account(temp_data_root, client, auth_headers, mock_fx_service):
    """TWD 帳戶: twd_estimate=balance, fx_rate_used=1.0, 不打 fx_service."""
    _seed_accounts_db(temp_data_root, "sinopac",
        accounts=[{"account_no": "T1", "currency": "TWD",
                   "product_type": "deposit"}],
        txns=[{"account_no": "T1", "txn_datetime": "2026-06-12T10:00:00",
               "balance": 1088682}])
    r = client.get("/portfolio/accounts", headers=auth_headers)
    rows = r.json()
    assert len(rows) == 1
    assert rows[0]["balance"] == 1088682
    assert rows[0]["twd_estimate"] == 1088682  # 1:1
    assert rows[0]["fx_rate_used"] == 1.0
    # 沒打 fx_service stub (TWD 走 short-circuit)
    assert mock_fx_service.call_count == 0


def test_portfolio_accounts_includes_twd_estimate_for_fx(temp_data_root, client, auth_headers, mock_fx_service):
    """sinopac JPY 1201387 帳戶, fx_service 回 0.2 → twd_estimate = 240277."""
    mock_fx_service.set_rates({"JPY": 0.2})
    _seed_accounts_db(temp_data_root, "sinopac",
        accounts=[{"account_no": "90000000187013", "currency": "JPY",
                   "nickname": "JPY DAWHO",
                   "type": "外幣組合存款", "product_type": "fx_deposit"}],
        txns=[{"account_no": "90000000187013",
               "txn_datetime": "2026-05-21T01:06:00",
               "balance": 1201387}])
    r = client.get("/portfolio/accounts", headers=auth_headers)
    rows = r.json()
    assert len(rows) == 1
    row = rows[0]
    assert row["currency"] == "JPY"
    assert row["balance"] == 1201387
    assert row["twd_estimate"] == 240277  # 1201387 * 0.2 = 240277.4 → round 240277
    assert row["fx_rate_used"] == 0.2


def test_portfolio_accounts_no_estimate_when_balance_null(temp_data_root, client, auth_headers, mock_fx_service):
    """JPY 帳戶但沒對應 txn (balance=None) → twd_estimate=None, fx_rate_used=None."""
    mock_fx_service.set_rates({"JPY": 0.2})
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    updated_at = f"{today}T10:00:00+00:00"
    _seed_accounts_db(temp_data_root, "sinopac",
        accounts=[{"account_no": "JPY-orphan", "currency": "JPY",
                   "product_type": "fx_deposit", "updated_at": updated_at}],
        txns=[])  # 沒對應 txn
    r = client.get("/portfolio/accounts", headers=auth_headers)
    rows = r.json()
    assert len(rows) == 1
    assert rows[0]["balance"] is None
    assert rows[0]["twd_estimate"] is None
    assert rows[0]["fx_rate_used"] is None


def test_portfolio_accounts_no_estimate_when_currency_unknown(temp_data_root, client, auth_headers, mock_fx_service):
    """KRW 帳戶但 fx_service 沒這幣別 → twd_estimate=None, balance 仍正常顯示."""
    mock_fx_service.set_rates({"JPY": 0.2, "USD": 31.5})  # 沒 KRW
    _seed_accounts_db(temp_data_root, "sinopac",
        accounts=[{"account_no": "KRW1", "currency": "KRW",
                   "product_type": "fx_deposit"}],
        txns=[{"account_no": "KRW1", "txn_datetime": "2026-06-12T10:00:00",
               "balance": 1000000}])
    r = client.get("/portfolio/accounts", headers=auth_headers)
    rows = r.json()
    assert len(rows) == 1
    row = rows[0]
    assert row["currency"] == "KRW"
    assert row["balance"] == 1000000          # balance 仍給
    assert row["twd_estimate"] is None         # 但估值 None
    assert row["fx_rate_used"] is None


def test_portfolio_accounts_does_not_raise_when_fx_service_fails(temp_data_root, client, auth_headers, monkeypatch):
    """fx_service.get_rate 拋例外 → endpoint 仍 200, twd_estimate=None."""
    from backend.server.routers import portfolio as portfolio_mod

    def _boom(_currency):
        raise RuntimeError("fx service down")

    monkeypatch.setattr(portfolio_mod.fx_service, "get_rate", _boom)
    _seed_accounts_db(temp_data_root, "sinopac",
        accounts=[{"account_no": "J1", "currency": "JPY",
                   "product_type": "fx_deposit"}],
        txns=[{"account_no": "J1", "txn_datetime": "2026-06-12T10:00:00",
               "balance": 100000}])
    r = client.get("/portfolio/accounts", headers=auth_headers)
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) == 1
    assert rows[0]["balance"] == 100000        # 該 row 仍正常
    assert rows[0]["twd_estimate"] is None     # 但估值 None
    assert rows[0]["fx_rate_used"] is None


# ============================================================
# raw_balance overlay — 爬蟲層直接抓的帳號級餘額（2026-06-14）
# 解 SCSB 11101 同日多筆 txn datetime 相同 MAX() 隨機挑、43203 真實 0 餘額
# 被當「—」、26108 外幣帳戶 USD 1.55 沒入 twd_txn 的三隻 bug。
# ============================================================

def test_portfolio_accounts_raw_balance_takes_precedence_over_txn(
    temp_data_root, client, auth_headers
):
    """raw_balance 優先於 twd_transactions 最新 balance。

    回歸 SCSB 11101 bug：同日 3 筆 txn datetime 完全相同，MAX() 不分先後
    隨機挑「最早」一筆 balance=72,377，但真實活儲餘額是「最晚」的 13,065。
    爬蟲層 _extract_accounts 已從 overview 頁直接抓 13,065 並寫 raw_balance，
    portfolio 必須優先用 raw_balance，不能仍走 txn 推算。
    """
    _seed_accounts_db(temp_data_root, "scsb",
        accounts=[
            {"account_no": "11101", "currency": "TWD",
             "product_type": "deposit",
             "raw_balance": 13065.0, "raw_balance_date": "2026-06-14"},
        ],
        txns=[
            # 模擬 SCSB 同日 3 筆 datetime 完全一樣 — MAX() 會挑 id 最小（72,377）
            {"account_no": "11101", "txn_datetime": "2026-05-25T00:00:00", "balance": 72377},
            {"account_no": "11101", "txn_datetime": "2026-05-25T00:00:00", "balance": 34075},
            {"account_no": "11101", "txn_datetime": "2026-05-25T00:00:00", "balance": 13065},
        ])
    r = client.get("/portfolio/accounts", headers=auth_headers)
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) == 1
    # 用 raw_balance 13,065 — 不是 txn 隨便挑的 72,377
    assert rows[0]["balance"] == 13065
    assert rows[0]["snapshot_date"] == "2026-06-14"


def test_portfolio_accounts_same_day_raw_balance_beats_ambiguous_txn(
    temp_data_root, client, auth_headers
):
    """同日交易順序不可靠時，保留 crawler 直接帳戶快照。"""
    _seed_accounts_db(temp_data_root, "scsb",
        accounts=[
            {"account_no": "S1", "currency": "TWD",
             "product_type": "deposit",
             "raw_balance": 13065.0, "raw_balance_date": "2026-06-14"},
        ],
        txns=[
            {"account_no": "S1", "txn_datetime": "2026-06-14T00:00:00", "balance": 72377},
        ])

    r = client.get("/portfolio/accounts", headers=auth_headers)

    assert r.status_code == 200
    assert r.json()[0]["balance"] == 13065
    assert r.json()[0]["snapshot_date"] == "2026-06-14"


def test_portfolio_accounts_newer_txn_balance_beats_older_raw_balance(
    temp_data_root, client, auth_headers
):
    """較新的交易餘額不得被前一日的 crawler 帳戶快照壓回去。"""
    _seed_accounts_db(temp_data_root, "cathay",
        accounts=[
            {"account_no": "C1", "currency": "TWD",
             "product_type": "deposit",
             "raw_balance": 1808044.0, "raw_balance_date": "2026-08-10"},
        ],
        txns=[
            {"account_no": "C1", "txn_datetime": "2026-08-11T04:17:46", "balance": 608386},
        ])

    r = client.get("/portfolio/accounts", headers=auth_headers)

    assert r.status_code == 200
    assert r.json()[0]["balance"] == 608386
    assert r.json()[0]["snapshot_date"] == "2026-08-11"


def test_portfolio_accounts_newer_null_txn_balance_keeps_valid_raw_balance(
    temp_data_root, client, auth_headers, monkeypatch
):
    """較新交易日期不能讓解析失敗的 NULL 餘額清掉有效帳戶快照。"""
    from backend.server import bank_account_projection as projection
    from backend.server.db_facade import AccountTxnBalance

    _seed_accounts_db(temp_data_root, "cathay",
        accounts=[
            {"account_no": "C1", "currency": "TWD",
             "product_type": "deposit",
             "raw_balance": 1808044.0, "raw_balance_date": "2026-08-10"},
        ])
    original = projection.db_api.list_latest_account_txn_balances
    monkeypatch.setattr(
        projection.db_api,
        "list_latest_account_txn_balances",
        lambda **kwargs: (
            {"C1": AccountTxnBalance(
                account_no="C1", txn_datetime="2026-08-11T04:17:46", balance=None,
            )}
            if kwargs["bank"] == "cathay"
            else original(**kwargs)
        ),
    )

    r = client.get("/portfolio/accounts", headers=auth_headers)

    assert r.status_code == 200
    assert r.json()[0]["balance"] == 1808044
    assert r.json()[0]["snapshot_date"] == "2026-08-10"


def test_portfolio_accounts_raw_balance_zero_shown_as_zero_not_dash(
    temp_data_root, client, auth_headers
):
    """raw_balance=0 必須回 0（frontend 顯示 $0），不能 fall through 變 None。

    回歸 SCSB 43203 bug：真實 0 餘額帳戶在舊邏輯下沒對應 txn → balance=None →
    frontend 顯示「—」+「無交易紀錄」。實際是「$0」才對。
    """
    _seed_accounts_db(temp_data_root, "scsb",
        accounts=[
            {"account_no": "43203", "currency": "TWD",
             "product_type": "deposit",
             "raw_balance": 0.0, "raw_balance_date": "2026-06-14"},
        ])
    r = client.get("/portfolio/accounts", headers=auth_headers)
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) == 1
    assert rows[0]["balance"] == 0          # 真實 0 — 不是 None
    assert rows[0]["snapshot_date"] == "2026-06-14"


def test_portfolio_accounts_raw_balance_foreign_currency_keeps_decimal(
    temp_data_root, client, auth_headers, monkeypatch
):
    """外幣 raw_balance=1.55 必須保留小數，不能截成 1。

    回歸 SCSB 26108 bug：USD 1.55 帳戶 _num 截成 1，正確要走 _num_real 保留小數。
    這 test 只 assert balance=1.55（_num_real 是 persist 層的事，
    fixture 跳過 persist 直接塞）。
    """
    from backend.server import fx_service
    monkeypatch.setattr(fx_service, "get_rate", lambda c: 31.7 if c.upper() == "USD" else None)
    monkeypatch.setattr(fx_service, "convert_to_twd",
                        lambda a, c: round(a * 31.7) if c.upper() == "USD" else None)
    from backend.server.routers import portfolio as portfolio_mod
    monkeypatch.setattr(portfolio_mod.fx_service, "get_rate", fx_service.get_rate)
    monkeypatch.setattr(portfolio_mod.fx_service, "convert_to_twd", fx_service.convert_to_twd)

    _seed_accounts_db(temp_data_root, "scsb",
        accounts=[
            {"account_no": "26108", "currency": "USD",
             "product_type": "fx_deposit",
             "raw_balance": 1.55, "raw_balance_date": "2026-06-14"},
        ])
    r = client.get("/portfolio/accounts", headers=auth_headers)
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) == 1
    assert rows[0]["balance"] == 1.55       # 保留小數
    assert rows[0]["currency"] == "USD"
    assert rows[0]["twd_estimate"] == 49    # 1.55 * 31.7 = 49.135 → round 49


def test_portfolio_accounts_loan_fallback_when_no_raw_balance(
    temp_data_root, client, auth_headers
):
    """貸款帳戶沒 raw_balance 但 balance_history.loan_balance 有 → 用後者。

    保留原 loan_fallback 路徑（給沒升級 raw_balance 的銀行繼續工作）。
    """
    # 需要 balance_history 表 — _seed_accounts_db 只建 accounts/twd_txn,
    # 補插 balance_history
    path = _seed_accounts_db(temp_data_root, "linebank",
        accounts=[
            {"account_no": "L1", "currency": "TWD", "product_type": "loan"},
        ])
    import sqlite3
    con = sqlite3.connect(str(path))
    con.execute("""CREATE TABLE IF NOT EXISTS balance_history (
        snapshot_date TEXT PRIMARY KEY,
        twd_balance INTEGER, fx_balance INTEGER, loan_balance INTEGER,
        updated_at TEXT NOT NULL
    )""")
    con.execute("INSERT INTO balance_history (snapshot_date, loan_balance, updated_at) "
                "VALUES (?, ?, ?)", ("2026-06-13", 500_000, _utcnow_iso()))
    con.commit()
    con.close()

    r = client.get("/portfolio/accounts", headers=auth_headers)
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) == 1
    assert rows[0]["balance"] == -500_000
    assert rows[0]["product_type"] == "loan"


def test_portfolio_accounts_raw_balance_beats_loan_fallback(
    temp_data_root, client, auth_headers
):
    """raw_balance 比 loan_balance fallback 優先 — SCSB 多貸款帳戶各自精確顯示."""
    path = _seed_accounts_db(temp_data_root, "scsb",
        accounts=[
            {"account_no": "57263", "currency": "TWD", "product_type": "loan",
             "raw_balance": 20589800.0, "raw_balance_date": "2026-06-14"},
        ])
    import sqlite3
    con = sqlite3.connect(str(path))
    con.execute("""CREATE TABLE IF NOT EXISTS balance_history (
        snapshot_date TEXT PRIMARY KEY,
        twd_balance INTEGER, fx_balance INTEGER, loan_balance INTEGER,
        updated_at TEXT NOT NULL
    )""")
    # 故意給不同值看 raw_balance 是否真的優先
    con.execute("INSERT INTO balance_history (snapshot_date, loan_balance, updated_at) "
                "VALUES (?, ?, ?)", ("2026-06-13", 99999999, _utcnow_iso()))
    con.commit()
    con.close()

    r = client.get("/portfolio/accounts", headers=auth_headers)
    rows = r.json()
    # 用 raw_balance 不是 balance_history
    assert rows[0]["balance"] == -20589800
    assert rows[0]["snapshot_date"] == "2026-06-14"


def test_portfolio_accounts_raw_balance_multi_bank_integration(
    temp_data_root, client, auth_headers, monkeypatch
):
    """跨銀行 raw_balance integration — 模擬 ubot/ctbc/sinopac/scsb 全接 raw_balance 後
    portfolio API 對各家正確 lookup 該帳號餘額（不再 fallback twd_txn）。"""
    from backend.server import fx_service
    rates = {"USD": 31.7, "JPY": 0.2}
    monkeypatch.setattr(fx_service, "get_rate",
                        lambda c: rates.get(c.upper()) if c else None)
    monkeypatch.setattr(fx_service, "convert_to_twd",
                        lambda a, c: round(a * rates.get(c.upper(), 0)) if rates.get(c.upper()) else None)
    from backend.server.routers import portfolio as portfolio_mod
    monkeypatch.setattr(portfolio_mod.fx_service, "get_rate", fx_service.get_rate)
    monkeypatch.setattr(portfolio_mod.fx_service, "convert_to_twd", fx_service.convert_to_twd)

    # 5 家銀行各塞一個帳戶，全用 raw_balance（不放 twd_txn）
    _seed_accounts_db(temp_data_root, "ubot",
        accounts=[{"account_no": "U1", "currency": "TWD", "product_type": "deposit",
                   "raw_balance": 15.0, "raw_balance_date": "2026-06-14"}])
    _seed_accounts_db(temp_data_root, "sinopac",
        accounts=[
            {"account_no": "S1", "currency": "TWD", "product_type": "deposit",
             "raw_balance": 1088367.0, "raw_balance_date": "2026-06-14"},
            {"account_no": "S2", "currency": "JPY", "product_type": "fx_deposit",
             "raw_balance": 1201387.0, "raw_balance_date": "2026-06-14"},
        ])
    _seed_accounts_db(temp_data_root, "scsb",
        accounts=[
            {"account_no": "11101", "currency": "TWD", "product_type": "deposit",
             "raw_balance": 13065.0, "raw_balance_date": "2026-06-14"},
            {"account_no": "57263", "currency": "TWD", "product_type": "loan",
             "raw_balance": 20589800.0, "raw_balance_date": "2026-06-14"},
        ])
    r = client.get("/portfolio/accounts", headers=auth_headers)
    rows = r.json()
    # 5 個帳戶都應出現；貸款在共用 canonical seam 統一為負值
    by_acct = {f"{r['bank']}|{r['account_no']}": r for r in rows}
    assert by_acct["ubot|U1"]["balance"] == 15
    assert by_acct["sinopac|S1"]["balance"] == 1088367
    assert by_acct["sinopac|S2"]["balance"] == 1201387
    assert by_acct["sinopac|S2"]["currency"] == "JPY"
    assert by_acct["sinopac|S2"]["twd_estimate"] == 240277  # 1201387 * 0.2
    assert by_acct["scsb|11101"]["balance"] == 13065
    assert by_acct["scsb|57263"]["balance"] == -20589800
    assert by_acct["scsb|57263"]["product_type"] == "loan"
