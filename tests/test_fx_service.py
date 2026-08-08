"""Tests for backend.server.fx_service (Phase 6 — FX rate service).

驗:
  - 台銀 CSV 解析正確 (USD/JPY/CNY/...)
  - 取「即期買入」(col 3) 與「即期賣出」(col 13) 的中間價
  - 主來源失敗 → fallback open.er-api
  - Cache TTL 6 小時 — 連打 N 次只觸發 1 次網路
  - convert_to_twd 正確 round int
  - 未知幣別回 None
  - TWD 走 short-circuit 不打網路, 直接回 1.0

所有測試都 mock httpx.get, 不打真實網路 (CI 無外網時仍要綠)。
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from backend.server import fx_service


# ============================================================
# 真實 BoT CSV 樣本 (2026-06-14 抓的 5 個幣別 raw)
# ============================================================
SAMPLE_BOT_CSV = """\ufeff幣別,匯率,現金,即期,遠期10天,遠期30天,遠期60天,遠期90天,遠期120天,遠期150天,遠期180天,匯率,現金,即期,遠期10天,遠期30天,遠期60天,遠期90天,遠期120天,遠期150天,遠期180天
USD,本行買入,31.22000,31.54500,31.56700,31.52500,31.46600,31.41000,31.35400,31.29600,31.24000,本行賣出,31.89000,31.69500,31.67100,31.63400,31.58300,31.53400,31.48400,31.43700,31.38400,
HKD,本行買入,3.88000,4.00100,4.00500,4.00200,3.99900,3.99600,3.99200,3.98700,3.98300,本行賣出,4.08400,4.07100,4.06600,4.06500,4.06200,4.05900,4.05700,4.05400,4.05100,
JPY,本行買入,0.19420,0.19770,0.19775,0.19770,0.19760,0.19750,0.19740,0.19730,0.19720,本行賣出,0.20460,0.20120,0.20100,0.20090,0.20080,0.20070,0.20060,0.20050,0.20040,
CNY,本行買入,4.55000,4.65800,4.65900,4.65500,4.65000,4.64500,4.64000,4.63400,4.62800,本行賣出,4.78900,4.70700,4.70300,4.69900,4.69400,4.68900,4.68400,4.67900,4.67400,
EUR,本行買入,35.32000,36.10500,36.13000,36.08000,36.01900,35.95900,35.89000,35.82200,35.75400,本行賣出,37.43000,36.62500,36.45400,36.42100,36.36200,36.30400,36.24000,36.18600,36.11300,
"""


# 真實 open.er-api.com 回應的簡化版
SAMPLE_ER_API = {
    "result": "success",
    "base_code": "TWD",
    "time_last_update_utc": "Sun, 14 Jun 2026 00:00:00 +0000",
    "rates": {
        "TWD": 1.0,
        "USD": 0.031636,    # 1 TWD = 0.031636 USD  →  1 USD = ~31.61 TWD
        "JPY": 5.067663,    # 1 TWD = 5.067663 JPY  →  1 JPY = ~0.197 TWD
        "CNY": 0.2143,      # 1 TWD = 0.2143 CNY    →  1 CNY = ~4.667 TWD
    },
}


# ============================================================
# Helpers — 每個 test 開頭強制清快取 (autouse 確保 isolation)
# ============================================================

@pytest.fixture(autouse=True)
def _clear_cache():
    fx_service._clear_cache()
    yield
    fx_service._clear_cache()


def _mk_response(*, status_code=200, content=None, json_data=None):
    """Build a MagicMock that quacks like httpx2.Response."""
    r = MagicMock()
    r.status_code = status_code
    if content is not None:
        r.content = content if isinstance(content, bytes) else content.encode("utf-8")
    r.json = MagicMock(return_value=json_data or {})
    return r


# ============================================================
# Test: 解析台銀 CSV
# ============================================================

def test_fx_service_parses_bot_csv_correctly():
    """台銀 CSV → rates dict, 取即期買入與即期賣出的中間價.

    USD = (31.54500 + 31.69500) / 2 = 31.62
    JPY = (0.19770 + 0.20120) / 2 = 0.19945
    """
    fake_resp = _mk_response(content=SAMPLE_BOT_CSV)
    with patch("backend.server.fx_service.httpx.get", return_value=fake_resp):
        bundle = fx_service.get_rates()

    assert bundle is not None
    assert bundle["source"] == "bank_of_taiwan"
    rates = bundle["rates"]
    assert rates["USD"] == 31.62
    assert rates["JPY"] == 0.19945
    assert rates["CNY"] == 4.6825
    assert rates["EUR"] == 36.365
    assert rates["HKD"] == 4.036
    # 最少 5 個幣別
    assert len(rates) >= 5


def test_fx_service_skips_malformed_bot_rows():
    """畸形 row 跳過, 不影響其他 row.

    驗:
      - 欄位不足的 row 跳過
      - 非數字的 rate 跳過
      - NaN 等非有限值跳過
      - 還是有正確 row 進結果
    """
    bad_csv = (
        "\ufeff幣別,匯率,現金,即期,...,本行賣出,現金,即期,...\n"
        "USD,本行買入,31.22000,31.54500,31.56700,31.52500,31.46600,31.41000,31.35400,31.29600,31.24000,本行賣出,31.89000,31.69500,31.67100,31.63400,31.58300,31.53400,31.48400,31.43700,31.38400,\n"
        "BAD,short,row\n"  # 欄位不夠
        "ZZZ,本行買入,1,1,1,1,1,1,1,1,1,本行賣出,1,not_a_number,1,1,1,1,1,1,1,\n"  # rate not numeric
        "NAN,本行買入,1,nan,1,1,1,1,1,1,1,本行賣出,1,1,1,1,1,1,1,1,1,\n"  # non-finite
        "XX,本行買入,1,1,1,1,1,1,1,1,1,本行賣出,1,1,1,1,1,1,1,1,1,\n"  # 幣別 < 3 字 → skip
    )
    fake_resp = _mk_response(content=bad_csv)
    with patch("backend.server.fx_service.httpx.get", return_value=fake_resp):
        bundle = fx_service.get_rates()
    assert bundle is not None
    rates = bundle["rates"]
    assert rates == {"USD": 31.62}  # 只有買賣兩側都合法的 row 進來


# ============================================================
# Test: Fallback open.er-api
# ============================================================

def test_fx_service_falls_back_to_er_api():
    """台銀失敗 (500) → fallback open.er-api, 反推 1/rate."""
    bot_resp = _mk_response(status_code=500, content=b"")
    er_resp = _mk_response(json_data=SAMPLE_ER_API)

    def _side(url, **_kw):
        if "rate.bot.com.tw" in url:
            return bot_resp
        if "er-api" in url:
            return er_resp
        raise AssertionError(f"unexpected URL: {url}")

    with patch("backend.server.fx_service.httpx.get", side_effect=_side):
        bundle = fx_service.get_rates()

    assert bundle is not None
    assert bundle["source"] == "open_er_api"
    rates = bundle["rates"]
    # 1/0.031636 ≈ 31.6094, round 6 = 31.609529 → 31.609529 or 31.609529 (allow tolerance)
    assert rates["USD"] == pytest.approx(31.6094, abs=0.01)
    # 1/5.067663 ≈ 0.197329, round 6 = 0.197329 → 約 0.1973
    assert rates["JPY"] == pytest.approx(0.1973, abs=0.001)
    # 1/0.2143 ≈ 4.6664, round 6 = 4.666356
    assert rates["CNY"] == pytest.approx(4.6664, abs=0.01)


def test_fx_service_returns_none_when_both_sources_fail():
    """台銀 + er-api 都掛 → 回 None (caller 容忍)."""
    bot_resp = _mk_response(status_code=500, content=b"")
    er_resp = _mk_response(status_code=500, json_data={})

    def _side(url, **_kw):
        if "rate.bot.com.tw" in url:
            return bot_resp
        return er_resp

    with patch("backend.server.fx_service.httpx.get", side_effect=_side):
        bundle = fx_service.get_rates()
    assert bundle is None


def test_fx_service_handles_httpx_exception_on_bot():
    """httpx.get 直接 raise → fallback 仍要試 (e.g. DNS fail / connect refuse)."""
    er_resp = _mk_response(json_data=SAMPLE_ER_API)

    def _side(url, **_kw):
        if "rate.bot.com.tw" in url:
            import httpx2
            raise httpx2.ConnectError("boom")
        return er_resp

    with patch("backend.server.fx_service.httpx.get", side_effect=_side):
        bundle = fx_service.get_rates()
    assert bundle is not None
    assert bundle["source"] == "open_er_api"


# ============================================================
# Test: Cache TTL — 連打 N 次只觸發 1 次網路
# ============================================================

def test_fx_service_caches_rates():
    """連打 get_rate 3 次 (不同幣) → 只觸發 1 次 httpx.get."""
    fake_resp = _mk_response(content=SAMPLE_BOT_CSV)
    with patch("backend.server.fx_service.httpx.get", return_value=fake_resp) as mock_get:
        r1 = fx_service.get_rate("USD")
        r2 = fx_service.get_rate("JPY")
        r3 = fx_service.get_rate("CNY")
        # 多打一次 get_rates 也應該 hit cache
        b = fx_service.get_rates()

    assert mock_get.call_count == 1
    assert r1 == 31.62
    assert r2 == 0.19945
    assert r3 == 4.6825
    assert b["source"] == "bank_of_taiwan"


def test_fx_service_cache_expires_after_ttl(monkeypatch):
    """6 小時後 cache 過期, 重打網路."""
    fake_resp = _mk_response(content=SAMPLE_BOT_CSV)
    with patch("backend.server.fx_service.httpx.get", return_value=fake_resp) as mock_get:
        fx_service.get_rate("USD")
        assert mock_get.call_count == 1

        # 假裝過了 6 小時 + 1 秒
        import time
        real_time = time.time()
        monkeypatch.setattr(
            "backend.server.fx_service.time.time",
            lambda: real_time + fx_service.CACHE_TTL_SECONDS + 1,
        )
        fx_service.get_rate("USD")
        # 應該又打了一次
        assert mock_get.call_count == 2


def test_fx_service_uses_stale_cache_when_refresh_fails():
    """有舊 cache → 6h 內仍 fresh; 過期後 refresh 失敗仍回舊 cache (穩定 > 新鮮)."""
    import httpx2
    fake_ok = _mk_response(content=SAMPLE_BOT_CSV)
    with patch("backend.server.fx_service.httpx.get", return_value=fake_ok):
        fx_service.get_rate("USD")  # 灌一筆進 cache

    # 模擬 6 小時後 refresh 兩源都掛 (用真實 httpx.ConnectError, fx_service 才會 swallow)
    import time
    real_time = time.time()
    with patch("backend.server.fx_service.time.time",
               return_value=real_time + fx_service.CACHE_TTL_SECONDS + 1), \
         patch("backend.server.fx_service.httpx.get",
               side_effect=httpx2.ConnectError("network down")):
        # 仍應回得到舊 cache
        bundle = fx_service.get_rates()
        assert bundle is not None
        assert bundle["rates"]["USD"] == 31.62


# ============================================================
# Test: convert_to_twd
# ============================================================

def test_convert_to_twd_returns_int():
    """convert_to_twd 回 int, 不是 float."""
    fake_resp = _mk_response(content=SAMPLE_BOT_CSV)
    with patch("backend.server.fx_service.httpx.get", return_value=fake_resp):
        twd = fx_service.convert_to_twd(1201387, "JPY")
    assert twd is not None
    assert isinstance(twd, int)
    # 1201387 * 0.19945 = 239617.13... → round → 239617
    assert twd == 239617


def test_convert_to_twd_uses_midpoint_rate():
    """TWD 換算使用即期買賣中間價。"""
    fake_resp = _mk_response(content=SAMPLE_BOT_CSV)
    with patch("backend.server.fx_service.httpx.get", return_value=fake_resp):
        # 100 * 31.62 = 3162
        twd_usd = fx_service.convert_to_twd(100, "USD")
    assert twd_usd == 3162


def test_convert_to_twd_returns_none_for_unknown_currency():
    """KRW 不在 CSV 裡 → 回 None."""
    fake_resp = _mk_response(content=SAMPLE_BOT_CSV)
    with patch("backend.server.fx_service.httpx.get", return_value=fake_resp):
        twd = fx_service.convert_to_twd(1000, "KRW")
    assert twd is None


def test_convert_to_twd_returns_none_for_none_amount():
    """amount=None → 回 None (不打網路)."""
    twd = fx_service.convert_to_twd(None, "JPY")
    assert twd is None


def test_convert_to_twd_returns_none_when_source_fails():
    """兩源都失敗 → convert_to_twd 回 None."""
    bot_resp = _mk_response(status_code=500, content=b"")
    er_resp = _mk_response(status_code=500, json_data={})
    with patch("backend.server.fx_service.httpx.get",
               side_effect=lambda *a, **k: bot_resp if "bot" in a[0] else er_resp):
        twd = fx_service.convert_to_twd(1000, "JPY")
    assert twd is None


# ============================================================
# Test: TWD short-circuit
# ============================================================

def test_get_rate_twd_returns_one_without_network():
    """get_rate('TWD') → 1.0, 不觸發網路."""
    with patch("backend.server.fx_service.httpx.get") as mock_get:
        rate = fx_service.get_rate("TWD")
    assert rate == 1.0
    assert mock_get.call_count == 0  # 沒打網路


def test_get_rate_empty_string_returns_none():
    """get_rate('') → None (不打網路)."""
    with patch("backend.server.fx_service.httpx.get") as mock_get:
        rate = fx_service.get_rate("")
    assert rate is None
    assert mock_get.call_count == 0


def test_get_rate_handles_lowercase_currency():
    """get_rate('jpy') 自動 upper → 對應到 JPY."""
    fake_resp = _mk_response(content=SAMPLE_BOT_CSV)
    with patch("backend.server.fx_service.httpx.get", return_value=fake_resp):
        rate = fx_service.get_rate("jpy")
    assert rate == 0.19945
