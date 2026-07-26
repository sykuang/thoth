from __future__ import annotations

from pathlib import Path

from backend.core.bank_data import KNOWN_BANKS
from backend.server.routers.rules import SUPPORTED_BANKS as RULE_BANKS
from backend.server.sync_runner import SUPPORTED_BANKS as SYNC_BANKS
from cli.cli import BANKS as CLI_BANKS

ROOT = Path(__file__).resolve().parents[1]


def test_rakuten_is_registered_across_backend_dispatchers() -> None:
    assert "rakuten" in KNOWN_BANKS
    assert "rakuten" in RULE_BANKS
    assert "rakuten" in SYNC_BANKS
    assert "rakuten" in CLI_BANKS


def test_rakuten_is_registered_in_frontend_metadata() -> None:
    api = (ROOT / "frontend/src/types/api.ts").read_text()
    brands = (ROOT / "frontend/src/lib/banks.ts").read_text()
    auto_sync = (ROOT / "frontend/src/app/(tabs)/settings/auto-sync.tsx").read_text()

    assert "'rakuten'," in api
    assert "rakuten: ['national_id', 'user_code', 'password']" in api
    assert "rakuten: '樂天國際銀行'" in api
    assert "rakuten:" in brands
    assert "rakuten: '樂天國際銀行'" in auto_sync
