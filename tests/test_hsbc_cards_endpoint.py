"""HSBC card-list endpoint drift regressions.

2026-07-09: HSBC frontend now loads card list from `GET /ibk-bff/api/v1/cards`
(endpoint name `cards`) instead of the older `cards/suspend` (endpoint name
`suspend`). If crawler only reads `suspend`, collect() silently sees cards=[] and
never fetches per-card posted/unposted transactions.
"""
from __future__ import annotations

from backend.banks.hsbc import HsbcCrawler
from backend.core.base import ApiHit, ResponseCollector


def _hit(endpoint: str, payload):
    return ApiHit(
        url=f"https://card.hsbc.com.tw/ibk-bff/api/v1/cards{endpoint}",
        method="GET",
        status=200,
        content_type="application/json",
        body_size=100,
        resp_json={"success": True, "payload": payload, "error": None},
    )


def test_hsbc_card_list_payload_accepts_current_cards_endpoint() -> None:
    cards = [{
        "id": "card-1", "maskedCardNumber": "9052-****-****-7002",
        "cardStatusDisplay": "ACTIVATED",
    }]
    collector = ResponseCollector()
    collector.hits = [_hit("", cards)]

    got = HsbcCrawler._card_inventory(collector)

    assert got == cards


def test_hsbc_card_list_payload_falls_back_to_legacy_suspend_endpoint() -> None:
    legacy_cards = [{
        "id": "legacy-card", "maskedCardNumber": "9058-****-****-7003",
        "cardStatusDisplay": "ACTIVATED",
    }]
    collector = ResponseCollector()
    collector.hits = [_hit("/suspend", legacy_cards)]

    got = HsbcCrawler._card_inventory(collector)

    assert got == legacy_cards
