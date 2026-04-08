"""Idempotency layer for APIs and message consumers."""

import json
import logging

logger = logging.getLogger("permissioned_defi.idempotency")


def check_idempotency_key(conn, key: str) -> dict | None:
    """Check if an idempotency key has been used. Returns cached response or None."""
    row = conn.execute(
        "SELECT response_code, response_body FROM idempotency_keys WHERE key = %s",
        (key,),
    ).fetchone()
    if row:
        logger.debug("Idempotency hit: key=%s", key)
        return {"code": row[0], "body": row[1]}
    return None


def store_idempotency_key(conn, key: str, response_code: int, response_body: dict):
    """Store an idempotency key with its response."""
    conn.execute(
        """
        INSERT INTO idempotency_keys (key, response_code, response_body)
        VALUES (%s, %s, %s)
        ON CONFLICT (key) DO NOTHING
        """,
        (key, response_code, json.dumps(response_body)),
    )


def check_event_processed(conn, event_id: str, consumer_group: str) -> bool:
    """Check if a Kafka event has already been processed by this consumer group."""
    row = conn.execute(
        """
        SELECT 1 FROM processed_events
        WHERE event_id = %s AND consumer_group = %s
        """,
        (event_id, consumer_group),
    ).fetchone()
    return row is not None


def mark_event_processed(conn, event_id: str, consumer_group: str):
    """Mark a Kafka event as processed for this consumer group."""
    conn.execute(
        """
        INSERT INTO processed_events (event_id, consumer_group)
        VALUES (%s, %s)
        ON CONFLICT (event_id, consumer_group) DO NOTHING
        """,
        (event_id, consumer_group),
    )
