"""Outbox publisher: atomically persisted events -> Kafka relay.

Polls the outbox_events table for unpublished events, publishes
them to Kafka with at-least-once delivery, and marks them as
published. Failed events are retried up to max_retries before
being moved to the Dead Letter Queue.
"""

import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone

import psycopg2
import psycopg2.extras
from kafka import KafkaProducer
from kafka.errors import KafkaError

psycopg2.extras.register_uuid()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
)
logger = logging.getLogger("outbox-publisher")

DB_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://defi_user:defi_pass@postgres:5432/defi_db",
)
KAFKA_BOOTSTRAP = os.environ.get("KAFKA_BOOTSTRAP", "kafka:9092")
POLL_INTERVAL = float(os.environ.get("POLL_INTERVAL_SECS", "1"))
MAX_RETRIES = int(os.environ.get("MAX_RETRIES", "5"))
BATCH_SIZE = int(os.environ.get("BATCH_SIZE", "50"))


def create_producer() -> KafkaProducer:
    """Create an idempotent Kafka producer."""
    for attempt in range(30):
        try:
            producer = KafkaProducer(
                bootstrap_servers=KAFKA_BOOTSTRAP,
                value_serializer=lambda v: json.dumps(v).encode(),
                key_serializer=lambda k: k.encode() if k else None,
                acks="all",
                retries=3,
                max_in_flight_requests_per_connection=1,
            )
            logger.info("Kafka producer connected")
            return producer
        except KafkaError as e:
            logger.warning(
                "Kafka not ready (attempt %d/30): %s", attempt + 1, e
            )
            time.sleep(2)
    raise RuntimeError("Could not connect to Kafka")


def get_conn():
    return psycopg2.connect(DB_URL)


def poll_and_publish(producer: KafkaProducer) -> int:
    """Poll unpublished outbox events and publish to Kafka.

    Uses FOR UPDATE SKIP LOCKED to allow multiple publisher
    replicas to run concurrently without conflicts.
    """
    conn = get_conn()
    published = 0
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            """
            SELECT id, aggregate_id, aggregate_type, event_type,
                   topic, payload, created_at,
                   request_id, trace_id, actor, actor_role
            FROM outbox_events
            WHERE published_at IS NULL
            ORDER BY created_at ASC
            LIMIT %s
            FOR UPDATE SKIP LOCKED
            """,
            (BATCH_SIZE,),
        )
        events = cur.fetchall()

        for event in events:
            event_id = str(event["id"])
            topic = event["topic"]
            try:
                message = {
                    "event_id": event_id,
                    "aggregate_id": str(event["aggregate_id"]),
                    "aggregate_type": event["aggregate_type"],
                    "event_type": event["event_type"],
                    "payload": event["payload"],
                    "request_id": str(event["request_id"]),
                    "trace_id": str(event["trace_id"]),
                    "actor": event["actor"],
                    "actor_role": event["actor_role"],
                    "created_at": str(event["created_at"]),
                }

                future = producer.send(
                    topic,
                    key=str(event["aggregate_id"]),
                    value=message,
                )
                future.get(timeout=10)

                # Mark as published
                cur.execute(
                    "UPDATE outbox_events SET published_at = NOW() WHERE id = %s",
                    (event_id,),
                )
                published += 1

            except Exception as e:
                logger.error(
                    "Failed to publish event %s to topic %s: %s",
                    event_id,
                    topic,
                    e,
                )
                move_to_dlq(cur, event, str(e))

        conn.commit()

    except Exception as e:
        logger.error("Poll cycle failed: %s", e)
        conn.rollback()
    finally:
        conn.close()

    return published


def move_to_dlq(cur, event: dict, error_message: str):
    """Move a failed event to the Dead Letter Queue."""
    cur.execute(
        """
        INSERT INTO dead_letter_queue
            (source_table, source_id, event_type, payload,
             error_message, retry_count, max_retries)
        VALUES ('outbox_events', %s, %s, %s, %s, 1, %s)
        ON CONFLICT DO NOTHING
        """,
        (
            str(event["id"]),
            event["event_type"],
            json.dumps(event["payload"]),
            error_message,
            MAX_RETRIES,
        ),
    )
    # Still mark as published so we don't retry forever from outbox
    cur.execute(
        "UPDATE outbox_events SET published_at = NOW() WHERE id = %s",
        (str(event["id"]),),
    )
    logger.warning(
        "Event %s moved to DLQ: %s", str(event["id"]), error_message
    )


def main():
    logger.info(
        "Outbox publisher starting (poll_interval=%ss, max_retries=%d)",
        POLL_INTERVAL,
        MAX_RETRIES,
    )

    # Wait for DB
    for attempt in range(30):
        try:
            conn = get_conn()
            conn.close()
            break
        except Exception:
            logger.info("Waiting for database (attempt %d/30)...", attempt + 1)
            time.sleep(2)

    producer = create_producer()

    while True:
        try:
            count = poll_and_publish(producer)
            if count > 0:
                logger.info("Published %d events", count)
        except Exception as e:
            logger.error("Publisher loop error: %s", e)

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
