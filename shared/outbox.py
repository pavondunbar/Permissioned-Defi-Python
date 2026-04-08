"""Transactional outbox pattern for atomic event persistence."""

import json
import uuid


def insert_outbox_event(
    conn,
    aggregate_type: str,
    aggregate_id: str,
    event_type: str,
    topic: str,
    payload: dict,
    ctx=None,
):
    """Insert an outbox event atomically within the current transaction.

    This must be called inside the same DB transaction as the business
    write to guarantee atomic persistence. The outbox publisher will
    asynchronously relay these events to Kafka.
    """
    from shared.audit import AuditContext

    if ctx is None:
        ctx = AuditContext()

    conn.execute(
        """
        INSERT INTO outbox_events
            (id, aggregate_id, aggregate_type, event_type, topic, payload,
             request_id, trace_id, actor, actor_role)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            str(uuid.uuid4()),
            aggregate_id,
            aggregate_type,
            event_type,
            topic,
            json.dumps(payload),
            ctx.request_id,
            ctx.trace_id,
            ctx.actor,
            ctx.actor_role,
        ),
    )
