"""Audit context and trail for all state transitions."""

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass(frozen=True)
class AuditContext:
    """Immutable audit context attached to every state transition."""

    request_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    trace_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    actor: str = "SYSTEM/unknown"
    actor_role: str = "SYSTEM"

    def as_sql_args(self) -> tuple:
        return (self.request_id, self.trace_id, self.actor, self.actor_role)

    def as_dict(self) -> dict:
        return {
            "request_id": self.request_id,
            "trace_id": self.trace_id,
            "actor": self.actor,
            "actor_role": self.actor_role,
        }


def write_audit_event(
    conn,
    ctx: AuditContext,
    action: str,
    resource_type: str,
    resource_id: str = None,
    details: dict = None,
):
    """Insert an append-only audit event."""
    import json

    conn.execute(
        """
        INSERT INTO audit_events
            (request_id, trace_id, actor, actor_role,
             action, resource_type, resource_id, details)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            ctx.request_id,
            ctx.trace_id,
            ctx.actor,
            ctx.actor_role,
            action,
            resource_type,
            resource_id,
            json.dumps(details) if details else None,
        ),
    )


def new_trace() -> str:
    return str(uuid.uuid4())


def now_utc() -> datetime:
    return datetime.now(timezone.utc)
