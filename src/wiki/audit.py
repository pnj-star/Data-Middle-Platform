"""Audit logging for the wiki (ROADMAP Phase 2, P2-T3).

Append-only records into `audit_logs`: who, when, on what, result, plus source
IP. Rows are added to the caller's session and committed with the caller's own
commit — audit never commits out of band (except login failures, which must
persist before raising 401).
"""
from __future__ import annotations

from fastapi import Request

from src.wiki.models import AuditLog


def client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def write_audit(
    session,
    action: str,
    target_type: str,
    target_id: str | None = None,
    user_id: str | None = None,
    detail: dict | None = None,
    ip: str | None = None,
    result: str = "success",
) -> None:
    """Append an audit row to `session` (flushed/committed by the caller)."""
    session.add(
        AuditLog(
            user_id=user_id,
            action=action,
            target_type=target_type,
            target_id=target_id,
            detail=detail,
            ip=ip,
            result=result,
        )
    )
