"""Persist package — 12 家銀行 sync 後 raw → normalized DB write 邏輯。

從 persist.py 拆分而來 (2026-06-17, W persist.py split refactor)。
"""
from __future__ import annotations

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

__all__ = [
    "persist_cathay",
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
