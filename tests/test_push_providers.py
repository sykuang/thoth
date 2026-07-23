"""Phase L11 — push provider 單元測試 (NoOp / Webhook / Multi / Apns config).

APNs HTTP/2 真實 round-trip 不在這層測 (要真 .p8 + 真 device token + 真連 Apple),
這層只測 config validation + body/header formatting + JWT signing logic.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from backend.server.push.base import NotificationPayload, NotifyResult, PushTarget
from backend.server.push.providers.none import NoOpNotifier
from backend.server.push.providers.webhook import WebhookNotifier
from backend.server.push.registry import get_notifier, reset_notifier_cache


@pytest.fixture(autouse=True)
def reset_cache():
    reset_notifier_cache()
    yield
    reset_notifier_cache()


# ---------------------------------------------------------------------------
# registry env-driven factory
# ---------------------------------------------------------------------------

def test_default_env_returns_noop(monkeypatch):
    monkeypatch.delenv("PUSH_PROVIDER", raising=False)
    n = get_notifier()
    assert n.name == "none"


def test_env_noop_explicit(monkeypatch):
    monkeypatch.setenv("PUSH_PROVIDER", "none")
    assert get_notifier().name == "none"


def test_env_webhook(monkeypatch):
    monkeypatch.setenv("PUSH_PROVIDER", "webhook")
    assert get_notifier().name == "webhook"


def test_env_unknown_raises(monkeypatch):
    monkeypatch.setenv("PUSH_PROVIDER", "bogus")
    with pytest.raises(ValueError, match="未知的 PUSH_PROVIDER"):
        get_notifier()


def test_env_apns_missing_config_raises(monkeypatch):
    monkeypatch.setenv("PUSH_PROVIDER", "apns")
    for var in ("APNS_KEY_ID", "APNS_TEAM_ID", "APNS_BUNDLE_ID", "APNS_KEY_PATH"):
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(RuntimeError) as ei:
        get_notifier()
    # 訊息含「缺少」字眼,給 user 看得懂
    assert "缺少" in str(ei.value) or "ApnsConfigError" in repr(ei.value)


def test_env_multi_requires_provider_list(monkeypatch):
    monkeypatch.setenv("PUSH_PROVIDER", "multi")
    monkeypatch.delenv("PUSH_MULTI_PROVIDERS", raising=False)
    with pytest.raises(RuntimeError, match="PUSH_MULTI_PROVIDERS"):
        get_notifier()


def test_env_multi_skips_failed_children(monkeypatch):
    """multi children 缺 dep / 缺 env 不該擋整體 (log warning + skip)."""
    monkeypatch.setenv("PUSH_PROVIDER", "multi")
    monkeypatch.setenv("PUSH_MULTI_PROVIDERS", "webhook,none")
    n = get_notifier()
    assert n.name == "multi"
    assert sorted([c.name for c in n._children]) == ["none", "webhook"]


def test_env_multi_cant_contain_self(monkeypatch):
    monkeypatch.setenv("PUSH_PROVIDER", "multi")
    monkeypatch.setenv("PUSH_MULTI_PROVIDERS", "multi,webhook")
    with pytest.raises(RuntimeError, match="無限迴圈"):
        get_notifier()


# ---------------------------------------------------------------------------
# NoOpNotifier
# ---------------------------------------------------------------------------

def test_noop_returns_empty_result():
    n = NoOpNotifier()
    payload = NotificationPayload(title="x", body="y")
    r = n.send_to_user(user_id=1, payload=payload)
    assert isinstance(r, NotifyResult)
    assert r.delivered_count == 0
    assert r.failed_count == 0


def test_noop_send_to_token_returns_empty():
    n = NoOpNotifier()
    payload = NotificationPayload(title="x", body="y")
    target = PushTarget(user_id=1, provider="apns", token="x", platform="ios")
    r = n.send_to_token(target=target, payload=payload)
    assert r.delivered_count == 0
    assert r.failed_count == 0


# ---------------------------------------------------------------------------
# WebhookNotifier
# ---------------------------------------------------------------------------

def test_webhook_formats_discord_payload():
    n = WebhookNotifier()
    body = n._format(
        "https://discord.com/api/webhooks/123/abc",
        NotificationPayload(title="同步完成", body="國泰 23 筆"),
    )
    assert "content" in body
    assert "同步完成" in body["content"]
    assert "國泰 23 筆" in body["content"]
    assert body["username"] == "Thoth"


def test_webhook_formats_slack_payload():
    n = WebhookNotifier()
    body = n._format(
        "https://hooks.slack.com/services/T/B/X",
        NotificationPayload(title="同步完成", body="國泰 23 筆"),
    )
    assert "text" in body
    assert "同步完成" in body["text"]


def test_webhook_formats_generic_payload():
    n = WebhookNotifier()
    body = n._format(
        "https://example.com/my-webhook",
        NotificationPayload(
            title="同步完成", body="國泰 23 筆", data={"deep_link": "/x"},
        ),
    )
    assert body["title"] == "同步完成"
    assert body["body"] == "國泰 23 筆"
    assert body["data"]["deep_link"] == "/x"


def test_webhook_post_success_delivers(monkeypatch):
    """Mock httpx.Client.post 回 200 → delivered_count=1."""
    n = WebhookNotifier()
    target = PushTarget(
        user_id=1, provider="webhook",
        token="https://example.com/wh", platform="web",
    )
    payload = NotificationPayload(title="x", body="y")
    with patch.object(n, "_post_one") as mock_post:
        mock_post.return_value = NotifyResult(delivered_count=1)
        r = n.send_to_token(target, payload)
    assert r.delivered_count == 1
    assert r.failed_count == 0


def test_webhook_post_404_invalidates_token():
    """Webhook 回 404 → invalid_tokens 含該 URL (webhook 被刪了)."""
    n = WebhookNotifier()

    class FakeClient:
        def __init__(self, *a, **kw):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def post(self, url, json=None):
            return MagicMock(status_code=404)

    target = PushTarget(
        user_id=1, provider="webhook",
        token="https://example.com/dead-webhook", platform="web",
    )
    with patch("backend.server.push.providers.webhook.httpx.Client", FakeClient):
        r = n.send_to_token(target, NotificationPayload(title="x", body="y"))
    assert r.failed_count == 1
    assert "https://example.com/dead-webhook" in r.invalid_tokens


def test_webhook_short_url_log_truncation():
    from backend.server.push.providers.webhook import _short
    short = _short("https://example.com/a")
    assert short == "https://example.com/a"
    long_url = "https://discord.com/api/webhooks/123456/" + "a" * 60
    shortened = _short(long_url)
    assert "…" in shortened
    assert len(shortened) < len(long_url)


# ---------------------------------------------------------------------------
# APNs body / header formatting (without real network)
# ---------------------------------------------------------------------------

def test_apns_body_format_minimal(tmp_path, monkeypatch):
    """APNs body 結構正確 — aps wrap + custom data 平鋪 root."""
    # APNs provider 需要 pyjwt (push-apns optional extras), dev 預設不裝
    pytest.importorskip("jwt", reason="needs `pip install thoth[push-apns]`")
    # 寫一個假 .p8 (內容不重要 — 這層測不簽 JWT)
    fake_key = tmp_path / "fake.p8"
    fake_key.write_text(
        "-----BEGIN PRIVATE KEY-----\nfakefake\n-----END PRIVATE KEY-----\n",
    )
    monkeypatch.setenv("APNS_KEY_ID", "ABC1234567")
    monkeypatch.setenv("APNS_TEAM_ID", "ABCDE12345")
    monkeypatch.setenv("APNS_BUNDLE_ID", "com.example.thoth")
    monkeypatch.setenv("APNS_KEY_PATH", str(fake_key))

    from backend.server.push.providers.apns import ApnsNotifier
    n = ApnsNotifier()

    payload = NotificationPayload(
        title="同步完成",
        body="國泰 23 筆",
        data={"deep_link": "/sync", "bank": "cathay"},
        badge=5,
        category="sync_done",
    )
    body = n._build_body(payload)
    assert body["aps"]["alert"]["title"] == "同步完成"
    assert body["aps"]["alert"]["body"] == "國泰 23 筆"
    assert body["aps"]["badge"] == 5
    assert body["aps"]["sound"] == "default"
    assert body["aps"]["category"] == "sync_done"
    # custom data 在 root, 不在 aps 內
    assert body["deep_link"] == "/sync"
    assert body["bank"] == "cathay"
    assert "deep_link" not in body["aps"]


def test_apns_jwt_signing_produces_valid_token(tmp_path, monkeypatch):
    """ES256 JWT 簽出來能用 pyjwt 反解 (基本 sanity)."""
    pytest.importorskip("jwt")
    pytest.importorskip("cryptography")
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    # 真產一把 P-256 key
    private_key = ec.generate_private_key(ec.SECP256R1())
    pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    key_path = tmp_path / "real.p8"
    key_path.write_bytes(pem)

    monkeypatch.setenv("APNS_KEY_ID", "ABC1234567")
    monkeypatch.setenv("APNS_TEAM_ID", "ABCDE12345")
    monkeypatch.setenv("APNS_BUNDLE_ID", "com.example.thoth")
    monkeypatch.setenv("APNS_KEY_PATH", str(key_path))

    from backend.server.push.providers.apns import ApnsNotifier
    n = ApnsNotifier()
    token = n._get_jwt()
    assert token  # non-empty

    # 第二次拿應該回 cache
    token2 = n._get_jwt()
    assert token == token2

    # invalidate 之後重新簽 — iat 可能不同, 但 algorithm/kid 都該對
    n._invalidate_jwt()
    token3 = n._get_jwt()
    # 用 cryptography 反解驗 (我們有 private 自然有 public)
    public_key = private_key.public_key()
    import jwt as pyjwt
    decoded = pyjwt.decode(token3, public_key, algorithms=["ES256"])
    assert decoded["iss"] == "ABCDE12345"
    assert "iat" in decoded


def test_apns_config_error_when_p8_missing(tmp_path, monkeypatch):
    pytest.importorskip("jwt", reason="needs `pip install thoth[push-apns]`")
    monkeypatch.setenv("APNS_KEY_ID", "ABC1234567")
    monkeypatch.setenv("APNS_TEAM_ID", "ABCDE12345")
    monkeypatch.setenv("APNS_BUNDLE_ID", "com.example.thoth")
    monkeypatch.setenv("APNS_KEY_PATH", str(tmp_path / "does-not-exist.p8"))
    from backend.server.push.providers.apns import ApnsConfigError, ApnsNotifier
    with pytest.raises(ApnsConfigError, match="找不到檔案"):
        ApnsNotifier()


def test_apns_sandbox_env_switches_host(tmp_path, monkeypatch):
    pytest.importorskip("jwt", reason="needs `pip install thoth[push-apns]`")
    fake_key = tmp_path / "fake.p8"
    fake_key.write_text("-----BEGIN PRIVATE KEY-----\nfake\n-----END PRIVATE KEY-----\n")
    monkeypatch.setenv("APNS_KEY_ID", "ABC1234567")
    monkeypatch.setenv("APNS_TEAM_ID", "ABCDE12345")
    monkeypatch.setenv("APNS_BUNDLE_ID", "com.example.thoth")
    monkeypatch.setenv("APNS_KEY_PATH", str(fake_key))
    monkeypatch.setenv("APNS_USE_SANDBOX", "1")

    from backend.server.push.providers.apns import ApnsNotifier, APNS_SANDBOX_HOST
    n = ApnsNotifier()
    assert n._host == APNS_SANDBOX_HOST
