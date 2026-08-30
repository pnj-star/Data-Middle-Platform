"""IP rate limiting + idempotency helpers (ROADMAP P4-T2).

All Redis calls are fail-lenient: if the broker is unreachable the limiter /
idempotency store degrade open (requests proceed) rather than blocking users.
"""
from __future__ import annotations

import hashlib

import redis as redis_lib

from src.config import config as app_config

IDEMPOTENCY_TTL_SECONDS = 86400  # 24h


_client_cache: dict = {}


def redis_client():
    """Reused Redis client (connection pool) per DSN — not a new one per request."""
    key = app_config.redis_url
    if key not in _client_cache:
        _client_cache[key] = redis_lib.from_url(
            key, decode_responses=True, socket_connect_timeout=2
        )
    return _client_cache[key]


def ip_rate_limit_hit(ip: str, limit_per_minute: int) -> bool:
    """True if `ip` exceeded the per-minute request budget."""
    if limit_per_minute <= 0:
        return False
    key = f"wiki:rate:{ip}"
    try:
        r = redis_client()
        count = int(r.incr(key) or 0)
        if count == 1:
            r.expire(key, 60)
        return count > limit_per_minute
    except Exception:
        return False  # redis down → degrade open


def idempotency_lookup(key: str, scope: str = "") -> str | None:
    """Stored resource id for an Idempotency-Key within a scope, or None.

    The redis key binds `scope` (e.g. the target space) so the same header used
    against a different space never collides (P4-T2 修复: cross-space isolation).
    """
    if not key:
        return None
    rkey = f"wiki:idem:{scope}:{hashlib.sha256(key.encode('utf-8')).hexdigest()}"
    try:
        return redis_client().get(rkey)
    except Exception:
        return None


def idempotency_store(key: str, scope: str, resource_id: str) -> None:
    """Remember that an Idempotency-Key in `scope` produced `resource_id`."""
    if not key:
        return
    rkey = f"wiki:idem:{scope}:{hashlib.sha256(key.encode('utf-8')).hexdigest()}"
    try:
        redis_client().setex(rkey, IDEMPOTENCY_TTL_SECONDS, resource_id)
    except Exception:
        pass
