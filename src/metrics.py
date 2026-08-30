"""Prometheus metrics for the wiki (ROADMAP P4-T3).

HTTP request counters/latency (path normalized so per-resource ids don't blow
up label cardinality) and a Celery queue-length gauge. Exposed at /metrics.
"""
from __future__ import annotations

import re

from prometheus_client import Counter, Gauge, Histogram, generate_latest

HTTP_REQUESTS = Counter(
    "wiki_http_requests_total", "HTTP requests", ["method", "path", "status"]
)
HTTP_LATENCY = Histogram(
    "wiki_http_request_duration_seconds", "HTTP latency", ["method", "path"]
)
CELERY_QUEUE_LEN = Gauge("wiki_celery_queue_length", "pending tasks in default 'celery' queue")

_HEX_ID = re.compile(r"/[0-9a-f]{32}")


def metric_path(path: str) -> str:
    """Normalize per-resource ids out of the path for bounded label cardinality."""
    return _HEX_ID.sub("/{id}", path)


def record(method: str, path: str, status: int, duration: float) -> None:
    p = metric_path(path)
    HTTP_REQUESTS.labels(method, p, str(status)).inc()
    HTTP_LATENCY.labels(method, p).observe(duration)


def refresh_celery_queue(redis_url: str) -> None:
    """Gauge = number of pending tasks in the default 'celery' queue."""
    import redis as redis_lib

    try:
        r = redis_lib.from_url(redis_url, socket_connect_timeout=2)
        CELERY_QUEUE_LEN.set(int(r.llen("celery") or 0))
    except Exception:
        CELERY_QUEUE_LEN.set(-1)  # broker unreachable


def render() -> bytes:
    return generate_latest()
