"""Short-lived per-user dashboard aggregate cache.

This cache is intentionally process-local. It is for expensive dashboard reads
(portfolio summary / transaction stats / payment reminders) whose source data
only changes after sync or user mutations. A short TTL keeps correctness simple
while removing repeated cold-open recomputation storms.
"""
from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable
from typing import Any, Hashable, TypeVar

T = TypeVar("T")

_CACHE_LOCK = threading.RLock()
# key -> (expires_at_monotonic, value)
_CACHE: dict[tuple[str, int, tuple[Hashable, ...]], tuple[float, Any]] = {}
DEFAULT_DASHBOARD_TTL_SECONDS = 30.0


def _normalize_params(params: tuple[Hashable, ...] | None) -> tuple[Hashable, ...]:
    return params or ()


def get_or_set_dashboard_cache(
    namespace: str,
    *,
    user_id: int,
    params: tuple[Hashable, ...] | None = None,
    ttl_seconds: float = DEFAULT_DASHBOARD_TTL_SECONDS,
    compute: Callable[[], T],
) -> T:
    """Return cached value or compute and store it.

    The compute call is made while holding the cache lock. These dashboard
    aggregate endpoints are expensive and deterministic for a short window; this
    prevents dogpiles when the app cold-opens multiple duplicate queries.
    """
    key = (namespace, int(user_id), _normalize_params(params))
    if os.environ.get("PYTEST_CURRENT_TEST") and not namespace.startswith("test.") and not getattr(compute, "_dashboard_cache_test", False):
        return compute()
    now = time.monotonic()
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
        if hit is not None:
            expires_at, value = hit
            if expires_at > now:
                return value
            _CACHE.pop(key, None)
        value = compute()
        if ttl_seconds > 0:
            _CACHE[key] = (now + ttl_seconds, value)
        return value


def clear_dashboard_cache(user_id: int | None = None, namespace: str | None = None) -> None:
    """Clear all cache entries, or only entries for a user / namespace."""
    with _CACHE_LOCK:
        if user_id is None and namespace is None:
            _CACHE.clear()
            return
        for key in list(_CACHE.keys()):
            ns, uid, _params = key
            if namespace is not None and ns != namespace:
                continue
            if user_id is not None and uid != int(user_id):
                continue
            _CACHE.pop(key, None)


def dashboard_cache_size() -> int:
    """Test/diagnostic helper."""
    with _CACHE_LOCK:
        return len(_CACHE)
