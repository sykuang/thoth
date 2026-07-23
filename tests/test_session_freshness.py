"""Regression tests for BankCrawler session age enforcement.

History (2026-06-17): SCSB job 43 (2026-06-16) 證實——Chromium user_data_dir
持久化 session 在 server-side timeout 後會進入「pseudo logged-in」灰色狀態：
cookies 還在、URL 跳對、JS sentinel 看不到登入 form，但實際頁面 stub
（text len=165），所有後續 navigation 全 fail。修法是 base 層加
SESSION_MAX_AGE_SECONDS（預設 180 秒，使用者指示）—— age 超過就整個砍 session_dir
強制重 login，從根源杜絕 stale session 問題。
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

from backend.core.base import BankCrawler, ResponseCollector


@dataclass
class _StubCrawler(BankCrawler):
    """Minimal concrete subclass for testing — login/collect 不會被叫到。"""

    def login(self, page) -> bool:  # pragma: no cover - 不會被測試呼叫
        return True

    def collect(self, page, collector: ResponseCollector) -> dict:  # pragma: no cover
        return {}

    def _host_filter(self) -> str:
        return "example.com"


@pytest.fixture
def crawler(tmp_path, monkeypatch):
    """Build a stub crawler with a tmp session_dir."""
    import backend.core.base as base_mod
    monkeypatch.setattr(base_mod, "DATA_ROOT", tmp_path)
    c = _StubCrawler(name="teststub")
    assert c.session_dir.exists()
    return c


def _touch(path: Path, age_seconds: float) -> None:
    """建一個檔並把 mtime 設為 N 秒前。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x")
    past = time.time() - age_seconds
    os.utime(path, (past, past))


def test_age_none_when_no_state_files(crawler):
    """空 session_dir 回 None（首次啟動場景）。"""
    assert crawler._session_age_seconds() is None


def test_age_reflects_newest_state_file(crawler):
    """有多個 state 檔時應回最新的 age."""
    _touch(crawler.session_dir / "Cookies", age_seconds=600)
    _touch(crawler.session_dir / "Local State", age_seconds=30)
    _touch(crawler.session_dir / "Default" / "Cookies", age_seconds=100)
    age = crawler._session_age_seconds()
    assert age is not None
    # 最新的是 30 秒前；允許 ±5 秒 slack 避免 CI flake
    assert 25 < age < 60, f"expected ~30s, got {age}"


def test_freshness_keeps_dir_when_under_max_age(crawler):
    """age < SESSION_MAX_AGE_SECONDS：session_dir 不該被砍。"""
    crawler.SESSION_MAX_AGE_SECONDS = 180
    _touch(crawler.session_dir / "Cookies", age_seconds=60)
    sentinel = crawler.session_dir / "sentinel.txt"
    sentinel.write_text("keep me")
    crawler._enforce_session_freshness()
    assert sentinel.exists(), "session_dir 應該被保留"
    assert (crawler.session_dir / "Cookies").exists()


def test_freshness_wipes_dir_when_over_max_age(crawler):
    """age > SESSION_MAX_AGE_SECONDS：session_dir 整個被砍重建（修 SCSB stale session bug）。"""
    crawler.SESSION_MAX_AGE_SECONDS = 180
    _touch(crawler.session_dir / "Cookies", age_seconds=600)  # 10 分鐘前
    sentinel = crawler.session_dir / "sentinel.txt"
    sentinel.write_text("should be deleted")

    crawler._enforce_session_freshness()

    # dir 應該還在（會被 mkdir 重建），但內容全部沒了
    assert crawler.session_dir.exists()
    assert not sentinel.exists(), "過期 session 應該被砍"
    assert not (crawler.session_dir / "Cookies").exists()


def test_freshness_no_op_when_no_state_files(crawler):
    """空 session_dir：什麼都不做（不該炸）。"""
    crawler.SESSION_MAX_AGE_SECONDS = 180
    crawler._enforce_session_freshness()
    assert crawler.session_dir.exists()


def test_subclass_can_override_max_age():
    """子類可 override SESSION_MAX_AGE_SECONDS（例如 HSBC OTP 場景）。"""

    @dataclass
    class _LongLived(_StubCrawler):
        SESSION_MAX_AGE_SECONDS: int = 600

    c = _LongLived(name="longlived")
    assert c.SESSION_MAX_AGE_SECONDS == 600
    # 基類預設不被污染
    assert BankCrawler.SESSION_MAX_AGE_SECONDS == 180
