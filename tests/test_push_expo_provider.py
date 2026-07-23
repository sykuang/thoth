"""Phase L11.5 — ExpoPushProvider unit tests (no real Expo network).

Expo Push Service relay tests — mock httpx, focus on:
  * message body shape (to/title/body/data/sound/priority)
  * per-message status='ok' → delivered_count
  * per-message status='error' + DeviceNotRegistered → invalid_tokens
  * per-message status='error' + other reason → failed_count but not invalid
  * batch (3 messages, mixed results)
  * HTTP 401 / 5xx → all failed,no invalidate
"""
from __future__ import annotations

import json
from unittest.mock import patch

import httpx
import pytest

from backend.server.push.base import NotificationPayload, PushTarget
from backend.server.push.providers.expo import ExpoPushProvider
from backend.server.push.registry import get_notifier, reset_notifier_cache


@pytest.fixture(autouse=True)
def reset_cache():
    reset_notifier_cache()
    yield
    reset_notifier_cache()


@pytest.fixture
def provider():
    return ExpoPushProvider()


def _target(token: str = "ExponentPushToken[xxxxxxxxxxxxxxxxxxxxxx]") -> PushTarget:
    return PushTarget(user_id=1, provider="expo", token=token, platform="ios")


def _payload(**kw) -> NotificationPayload:
    defaults: dict = {
        "title": "同步完成",
        "body": "國泰 23 筆",
        "data": {"deep_link": "/sync"},
    }
    defaults.update(kw)
    return NotificationPayload(**defaults)


# ---------------------------------------------------------------------------
# registry env
# ---------------------------------------------------------------------------

def test_env_expo_returns_provider(monkeypatch):
    monkeypatch.setenv("PUSH_PROVIDER", "expo")
    n = get_notifier()
    assert n.name == "expo"


def test_unknown_provider_error_msg_lists_expo(monkeypatch):
    monkeypatch.setenv("PUSH_PROVIDER", "bogus")
    with pytest.raises(ValueError, match="expo"):
        get_notifier()


# ---------------------------------------------------------------------------
# message body formatting
# ---------------------------------------------------------------------------

def test_build_message_minimal(provider):
    msg = provider._build_message(_target(), _payload())
    assert msg["to"] == "ExponentPushToken[xxxxxxxxxxxxxxxxxxxxxx]"
    assert msg["title"] == "同步完成"
    assert msg["body"] == "國泰 23 筆"
    assert msg["data"] == {"deep_link": "/sync"}
    assert msg["sound"] == "default"
    assert msg["priority"] == "high"


def test_build_message_with_badge_and_category(provider):
    msg = provider._build_message(_target(), _payload(badge=5, category="sync_done"))
    assert msg["badge"] == 5
    assert msg["categoryId"] == "sync_done"


def test_build_message_omits_data_when_empty(provider):
    msg = provider._build_message(_target(), _payload(data={}))
    assert "data" not in msg


def test_headers_with_access_token(monkeypatch):
    monkeypatch.setenv("EXPO_ACCESS_TOKEN", "test-bearer")
    n = ExpoPushProvider()
    h = n._build_headers()
    assert h["authorization"] == "Bearer test-bearer"


def test_headers_without_access_token(monkeypatch):
    monkeypatch.delenv("EXPO_ACCESS_TOKEN", raising=False)
    n = ExpoPushProvider()
    h = n._build_headers()
    assert "authorization" not in h


# ---------------------------------------------------------------------------
# response handling (mock httpx.Client.post)
# ---------------------------------------------------------------------------

class _FakeResp:
    def __init__(self, status_code: int, body_data=None, body_text: str = ""):
        self.status_code = status_code
        self._body_data = body_data
        self.text = body_text or (json.dumps(body_data) if body_data else "")

    def json(self):
        if self._body_data is None:
            raise ValueError("no body")
        return self._body_data


class _FakeClient:
    def __init__(self, resp: _FakeResp, *args, **kw):
        self._resp = resp
        self.calls: list[tuple[str, dict, list]] = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def post(self, url, headers=None, json=None):
        self.calls.append((url, headers or {}, json or []))
        return self._resp


def test_send_single_ok_delivers(provider):
    """Expo 回 {data: [{status: ok}]} → delivered_count=1."""
    fake = _FakeResp(200, {"data": [{"status": "ok", "id": "abc"}]})
    with patch("backend.server.push.providers.expo.httpx.Client",
               lambda **kw: _FakeClient(fake)):
        r = provider.send_to_token(_target(), _payload())
    assert r.delivered_count == 1
    assert r.failed_count == 0
    assert r.invalid_tokens == []


def test_send_single_device_not_registered_invalidates(provider):
    """DeviceNotRegistered → invalid_tokens 含該 token."""
    fake = _FakeResp(200, {"data": [{
        "status": "error",
        "message": "expo token not registered",
        "details": {"error": "DeviceNotRegistered"},
    }]})
    bad_token = "ExponentPushToken[DEAD_TOKEN_xxxxxxxxxxxx]"
    # mock repo.deactivate 避免真打 DB
    with patch("backend.server.push.providers.expo.httpx.Client",
               lambda **kw: _FakeClient(fake)), \
         patch("backend.server.push.providers.expo.repo.deactivate"):
        r = provider.send_to_token(_target(bad_token), _payload())
    assert r.failed_count == 1
    assert r.delivered_count == 0
    assert bad_token in r.invalid_tokens


def test_send_single_other_error_not_invalidated(provider):
    """MessageRateExceeded 等暫時錯誤 → failed_count++ 但 invalid_tokens 空."""
    fake = _FakeResp(200, {"data": [{
        "status": "error",
        "message": "rate exceeded",
        "details": {"error": "MessageRateExceeded"},
    }]})
    with patch("backend.server.push.providers.expo.httpx.Client",
               lambda **kw: _FakeClient(fake)):
        r = provider.send_to_token(_target(), _payload())
    assert r.failed_count == 1
    assert r.invalid_tokens == []


def test_send_http_401_all_failed(provider):
    """Bad EXPO_ACCESS_TOKEN → 401 → 全 batch failed (不 invalidate)."""
    fake = _FakeResp(401, body_text='{"error":"bad token"}')
    with patch("backend.server.push.providers.expo.httpx.Client",
               lambda **kw: _FakeClient(fake)):
        r = provider.send_to_token(_target(), _payload())
    assert r.failed_count == 1
    assert r.invalid_tokens == []
    assert "HTTP 401" in r.errors[0][1]


def test_send_http_500_all_failed_not_invalidated(provider):
    fake = _FakeResp(500)
    with patch("backend.server.push.providers.expo.httpx.Client",
               lambda **kw: _FakeClient(fake)):
        r = provider.send_to_token(_target(), _payload())
    assert r.failed_count == 1
    assert r.invalid_tokens == []
    assert "Expo server" in r.errors[0][1]


def test_send_transport_error_all_failed(provider):
    """httpx RequestError (network) → 全 batch failed (不 invalidate)."""
    class _ErrClient:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def post(self, url, headers=None, json=None):
            raise httpx.ConnectError("network down")

    with patch("backend.server.push.providers.expo.httpx.Client",
               lambda **kw: _ErrClient()):
        r = provider.send_to_token(_target(), _payload())
    assert r.failed_count == 1
    assert r.invalid_tokens == []
    assert "ConnectError" in r.errors[0][1]


def test_response_shape_mismatch_all_failed(provider):
    """Expo 回 200 但 data array 長度不對 → 全 batch failed."""
    fake = _FakeResp(200, {"data": []})  # 0 != 1
    with patch("backend.server.push.providers.expo.httpx.Client",
               lambda **kw: _FakeClient(fake)):
        r = provider.send_to_token(_target(), _payload())
    assert r.failed_count == 1


def test_batch_mixed_results(provider):
    """Batch 3 message: 第1筆 ok / 第2筆 DeviceNotRegistered / 第3筆 ok."""
    fake = _FakeResp(200, {"data": [
        {"status": "ok", "id": "1"},
        {"status": "error", "message": "gone",
         "details": {"error": "DeviceNotRegistered"}},
        {"status": "ok", "id": "3"},
    ]})

    # Mock repo.list_active_for_user 回 3 個 target
    targets = [
        _target("ExponentPushToken[AAA_xxxxxxxxxxxxxxxx_AAA]"),
        _target("ExponentPushToken[BBB_dead_xxxxxxxxxxx_BBB]"),
        _target("ExponentPushToken[CCC_xxxxxxxxxxxxxxxx_CCC]"),
    ]
    with patch("backend.server.push.providers.expo.repo.list_active_for_user",
               return_value=targets), \
         patch("backend.server.push.providers.expo.repo.touch"), \
         patch("backend.server.push.providers.expo.repo.deactivate"), \
         patch("backend.server.push.providers.expo.httpx.Client",
               lambda **kw: _FakeClient(fake)):
        r = provider.send_to_user(user_id=1, payload=_payload())

    assert r.delivered_count == 2
    assert r.failed_count == 1
    assert r.invalid_tokens == ["ExponentPushToken[BBB_dead_xxxxxxxxxxx_BBB]"]


def test_short_token_truncation():
    from backend.server.push.providers.expo import _short
    assert _short("ExponentPushToken[short]") == "ExponentPushToken[short]"
    long = "ExponentPushToken[" + "a" * 40 + "]"
    short = _short(long)
    assert "…" in short
    assert len(short) < len(long)
