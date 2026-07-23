"""Tests for /fx/rates router (Phase 6).

驗:
  - GET /fx/rates 回 {as_of, source, base, rates} dict
  - rates 包含至少 USD/JPY/CNY 5 個幣別
  - 未 auth → 401
  - fx_service 兩源都掛 → 503
"""
from __future__ import annotations

from datetime import datetime
from unittest.mock import patch

import pytest


SAMPLE_BOT_CSV = """\ufeff幣別,匯率,現金,即期,遠期10天,遠期30天,遠期60天,遠期90天,遠期120天,遠期150天,遠期180天,匯率,現金,即期,遠期10天,遠期30天,遠期60天,遠期90天,遠期120天,遠期150天,遠期180天
USD,本行買入,31.22000,31.54500,31.56700,31.52500,31.46600,31.41000,31.35400,31.29600,31.24000,本行賣出,31.89000,31.69500,31.67100,31.63400,31.58300,31.53400,31.48400,31.43700,31.38400,
HKD,本行買入,3.88000,4.00100,4.00500,4.00200,3.99900,3.99600,3.99200,3.98700,3.98300,本行賣出,4.08400,4.07100,4.06600,4.06500,4.06200,4.05900,4.05700,4.05400,4.05100,
JPY,本行買入,0.19420,0.19770,0.19775,0.19770,0.19760,0.19750,0.19740,0.19730,0.19720,本行賣出,0.20460,0.20120,0.20100,0.20090,0.20080,0.20070,0.20060,0.20050,0.20040,
CNY,本行買入,4.55000,4.65800,4.65900,4.65500,4.65000,4.64500,4.64000,4.63400,4.62800,本行賣出,4.78900,4.70700,4.70300,4.69900,4.69400,4.68900,4.68400,4.67900,4.67400,
EUR,本行買入,35.32000,36.10500,36.13000,36.08000,36.01900,35.95900,35.89000,35.82200,35.75400,本行賣出,37.43000,36.62500,36.45400,36.42100,36.36200,36.30400,36.24000,36.18600,36.11300,
"""


def _mk_response(*, status_code=200, content=None):
    from unittest.mock import MagicMock
    r = MagicMock()
    r.status_code = status_code
    r.content = content.encode("utf-8") if isinstance(content, str) else (content or b"")
    return r


@pytest.fixture(autouse=True)
def _clear_fx_cache():
    """每 test 開頭清 fx_service cache, 避免 cross-test 污染."""
    from backend.server import fx_service
    fx_service._clear_cache()
    yield
    fx_service._clear_cache()


# `client` fixture 從 conftest.py 取得 (isolated: tmp_path + JWT_SECRET +
# Fernet key + 一堆 reload), 之前在本檔自定 local fixture 只 return
# TestClient(app) 把 conftest 的 isolation shadow 掉, 導致 CI runner
# 乾淨 env 跑 register/login JWT 永遠拿不到 → 61 errors。
# 2026-06-18 修法：刪 local fixture, 改用 conftest 的, CI 全綠。


@pytest.fixture
def auth_headers(client):
    email = f"fx-router-test-{datetime.now().timestamp()}@example.com"
    resp = client.post("/auth/register",
                       json={"email": email, "password": "Password123!"})
    assert resp.status_code == 201, resp.text
    token = resp.json()["token"]
    return {"Authorization": f"Bearer {token}"}


# ============================================================
# Happy path
# ============================================================

def test_fx_rates_returns_dict(client, auth_headers):
    """GET /fx/rates → 含 base, source, rates 的 dict.

    rates 至少包含 USD/JPY/CNY (前 5 大常見幣別).
    """
    fake_resp = _mk_response(content=SAMPLE_BOT_CSV)
    with patch("backend.server.fx_service.httpx.get", return_value=fake_resp):
        r = client.get("/fx/rates", headers=auth_headers)

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["base"] == "TWD"
    assert body["source"] == "bank_of_taiwan"
    assert "as_of" in body
    rates = body["rates"]
    assert "USD" in rates
    assert "JPY" in rates
    assert "CNY" in rates
    assert "EUR" in rates
    assert "HKD" in rates
    assert rates["USD"] == 31.695
    assert rates["JPY"] == 0.2012


def test_fx_rates_falls_back_to_er_api_when_bot_fails(client, auth_headers):
    """台銀掛 → fallback open.er-api, source 改成 'open_er_api'."""
    bot_resp = _mk_response(status_code=500)

    from unittest.mock import MagicMock
    er_resp = MagicMock()
    er_resp.status_code = 200
    er_resp.json = MagicMock(return_value={
        "result": "success",
        "base_code": "TWD",
        "time_last_update_utc": "Sun, 14 Jun 2026 00:00:00 +0000",
        "rates": {"TWD": 1.0, "USD": 0.031636, "JPY": 5.067663, "CNY": 0.2143,
                  "EUR": 0.0276, "HKD": 0.2458},
    })

    def _side(url, **_kw):
        if "rate.bot.com.tw" in url:
            return bot_resp
        return er_resp

    with patch("backend.server.fx_service.httpx.get", side_effect=_side):
        r = client.get("/fx/rates", headers=auth_headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["source"] == "open_er_api"
    rates = body["rates"]
    assert "USD" in rates
    # 1/0.031636 ≈ 31.61
    assert 31.0 < rates["USD"] < 32.0


# ============================================================
# Auth
# ============================================================

def test_fx_rates_requires_auth(client):
    """沒 Bearer token → 401."""
    r = client.get("/fx/rates")
    assert r.status_code == 401


def test_fx_rates_rejects_invalid_token(client):
    """壞 token → 401."""
    r = client.get("/fx/rates", headers={"Authorization": "Bearer bogus"})
    assert r.status_code == 401


# ============================================================
# Failure modes
# ============================================================

def test_fx_rates_returns_503_when_both_sources_fail(client, auth_headers):
    """台銀 + er-api 都掛 → 503."""
    bot_resp = _mk_response(status_code=500)

    from unittest.mock import MagicMock
    er_resp = MagicMock()
    er_resp.status_code = 500
    er_resp.json = MagicMock(return_value={})

    def _side(url, **_kw):
        if "rate.bot.com.tw" in url:
            return bot_resp
        return er_resp

    with patch("backend.server.fx_service.httpx.get", side_effect=_side):
        r = client.get("/fx/rates", headers=auth_headers)
    assert r.status_code == 503
