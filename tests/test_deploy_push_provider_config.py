"""Deployment config must enable the same push provider frontend registers.

Production incident (2026-06-30): Container App was deployed without
PUSH_PROVIDER, so backend defaulted to NoOpNotifier even though the iOS app
registered Expo push tokens. Scheduler jobs ran successfully but delivered 0
notifications. This test guards the Bicep template from regressing to noop.
"""
from __future__ import annotations

from pathlib import Path


def test_container_app_sets_push_provider_expo() -> None:
    """Azure Container App env must set PUSH_PROVIDER=expo for production push."""
    bicep = Path("deploy/main.bicep").read_text(encoding="utf-8")

    assert "{ name: 'PUSH_PROVIDER', value: 'expo' }" in bicep, (
        "deploy/main.bicep must set PUSH_PROVIDER=expo; otherwise backend "
        "uses NoOpNotifier and scheduled/payment reminders silently deliver 0."
    )
