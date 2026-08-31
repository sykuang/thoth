"""Persist package — 12 家銀行 sync 後 raw → normalized DB write 邏輯。

從 persist.py 拆分而來 (2026-06-17, W persist.py split refactor)。
"""
from __future__ import annotations

from typing import Any

from backend.core.base import validate_card_bill_facts, validate_history_coverage
from backend.core.persist.cathay import persist_cathay
from backend.core.persist.ctbc import persist_ctbc
from backend.core.persist.dbs import persist_dbs
from backend.core.persist.esun import persist_esun
from backend.core.persist.fubon import persist_fubon
from backend.core.persist.generic import persist_generic_dump
from backend.core.persist.hsbc import persist_hsbc
from backend.core.persist.linebank import persist_linebank
from backend.core.persist.rakuten import persist_rakuten
from backend.core.persist.scb import persist_scb
from backend.core.persist.scsb import persist_scsb
from backend.core.persist.sinopac import persist_sinopac
from backend.core.persist.taishin import persist_taishin
from backend.core.persist.ubot import persist_ubot
from backend.core.card_bills import CardBillWriteBarrier, apply_card_bill_facts


PERSISTERS = {
    "cathay": "persist_cathay",
    "ctbc": "persist_ctbc",
    "dbs": "persist_dbs",
    "esun": "persist_esun",
    "fubon": "persist_fubon",
    "hsbc": "persist_hsbc",
    "linebank": "persist_linebank",
    "rakuten": "persist_rakuten",
    "scb": "persist_scb",
    "scsb": "persist_scsb",
    "sinopac": "persist_sinopac",
    "taishin": "persist_taishin",
    "ubot": "persist_ubot",
}


def _validate_history_coverage_before_persist(coverage):
    if coverage is None:
        return
    if not isinstance(coverage, dict):
        raise ValueError("invalid history coverage")
    domains = coverage.get("domains")
    mode = coverage.get("mode")
    if not isinstance(domains, list) or not isinstance(mode, str):
        raise ValueError("invalid history coverage")
    domain_names = frozenset(
        name
        for domain in domains
        if isinstance(domain, dict)
        if isinstance(name := domain.get("domain"), str)
    )
    validate_history_coverage(
        coverage,
        expected_mode=mode,
        expected_domains=domain_names,
    )


def persist_collected(bank, data, store, rules=None):
    """Persist one collected result, then apply its canonical card-bill facts."""
    try:
        target = PERSISTERS[bank]
    except KeyError as exc:
        raise ValueError(f"unknown bank persist: {bank!r}") from exc
    coverage = data.get("history_coverage")
    if bank in {"esun", "fubon", "hsbc", "sinopac"} and coverage is None:
        raise ValueError(f"{bank} persistence requires history coverage")
    _validate_history_coverage_before_persist(coverage)
    facts_ok = data.get("card_bill_facts_ok")
    facts = data.get("card_bill_facts") or []
    validate_card_bill_facts(facts, facts_ok=facts_ok)
    persist = globals()[target] if isinstance(target, str) else target
    barrier: Any = CardBillWriteBarrier(store)
    atomic = bank in {"hsbc", "sinopac"}
    try:
        if atomic:
            delta = persist(data, barrier, rules=rules, commit=False)
        else:
            delta = persist(data, barrier, rules=rules)
        applied = apply_card_bill_facts(
            store,
            facts_ok=facts_ok,
            facts=facts,
            commit=not atomic,
        )
        if data.get("card_bill_facts_ok") is not None:
            delta["card_bill_facts_applied"] = applied
        store.record_history_coverage_cursors(
            data.get("history_coverage"), commit=not atomic,
        )
        if atomic:
            store.commit()
        return delta
    except Exception:
        if atomic:
            store.conn.rollback()
        raise

__all__ = [
    "persist_cathay",
    "persist_collected",
    "persist_ctbc",
    "persist_dbs",
    "persist_esun",
    "persist_fubon",
    "persist_generic_dump",
    "persist_hsbc",
    "persist_linebank",
    "persist_rakuten",
    "persist_scb",
    "persist_scsb",
    "persist_sinopac",
    "persist_taishin",
    "persist_ubot",
]
