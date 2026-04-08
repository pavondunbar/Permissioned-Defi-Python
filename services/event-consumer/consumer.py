"""Kafka event consumer with idempotent processing and audit logging.

Subscribes to settlement events, records audit trails, and handles
consumer-side deduplication via the processed_events table.
"""

import json
import logging
import os
import time
import uuid

import psycopg2
import psycopg2.extras
from kafka import KafkaConsumer
from kafka.errors import KafkaError

psycopg2.extras.register_uuid()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
)
logger = logging.getLogger("event-consumer")

DB_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://defi_user:defi_pass@postgres:5432/defi_db",
)
KAFKA_BOOTSTRAP = os.environ.get("KAFKA_BOOTSTRAP", "kafka:9092")
CONSUMER_GROUP = os.environ.get("CONSUMER_GROUP", "defi-event-consumer")
TOPICS = os.environ.get(
    "TOPICS",
    "settlement.created,settlement.approved,settlement.signed,"
    "settlement.broadcasted,settlement.confirmed,settlement.failed",
).split(",")


def get_conn():
    return psycopg2.connect(DB_URL)


def create_consumer() -> KafkaConsumer:
    """Create a Kafka consumer with manual offset commits."""
    for attempt in range(30):
        try:
            consumer = KafkaConsumer(
                *TOPICS,
                bootstrap_servers=KAFKA_BOOTSTRAP,
                group_id=CONSUMER_GROUP,
                auto_offset_reset="earliest",
                enable_auto_commit=False,
                value_deserializer=lambda m: json.loads(m.decode()),
                consumer_timeout_ms=5000,
            )
            logger.info("Kafka consumer connected, topics=%s", TOPICS)
            return consumer
        except KafkaError as e:
            logger.warning(
                "Kafka not ready (attempt %d/30): %s", attempt + 1, e
            )
            time.sleep(2)
    raise RuntimeError("Could not connect to Kafka")


def is_already_processed(conn, event_id: str) -> bool:
    """Check if this event was already processed (idempotency)."""
    cur = conn.cursor()
    cur.execute(
        """
        SELECT 1 FROM processed_events
        WHERE event_id = %s AND consumer_group = %s
        """,
        (event_id, CONSUMER_GROUP),
    )
    return cur.fetchone() is not None


def mark_processed(conn, event_id: str):
    """Mark an event as processed for this consumer group."""
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO processed_events (event_id, consumer_group)
        VALUES (%s, %s)
        ON CONFLICT (event_id, consumer_group) DO NOTHING
        """,
        (event_id, CONSUMER_GROUP),
    )


def process_event(conn, event: dict):
    """Process a single event: write audit trail."""
    event_id = event.get("event_id", str(uuid.uuid4()))
    event_type = event.get("event_type", "unknown")
    payload = event.get("payload", {})

    if is_already_processed(conn, event_id):
        logger.debug("Skipping duplicate event: %s", event_id)
        return

    cur = conn.cursor()

    # Write audit event
    cur.execute(
        """
        INSERT INTO audit_events
            (request_id, trace_id, actor, actor_role,
             action, resource_type, resource_id, details)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            event.get("request_id", str(uuid.uuid4())),
            event.get("trace_id", str(uuid.uuid4())),
            event.get("actor", "SYSTEM/event-consumer"),
            event.get("actor_role", "SYSTEM"),
            f"event.consumed.{event_type}",
            "event",
            event_id,
            json.dumps(payload) if isinstance(payload, dict) else payload,
        ),
    )

    mark_processed(conn, event_id)
    conn.commit()

    logger.info("Processed event: type=%s id=%s", event_type, event_id)


def main():
    logger.info("Event consumer starting (group=%s)", CONSUMER_GROUP)

    # Wait for DB
    for attempt in range(30):
        try:
            conn = get_conn()
            conn.close()
            break
        except Exception:
            logger.info("Waiting for database (attempt %d/30)...", attempt + 1)
            time.sleep(2)

    consumer = create_consumer()

    while True:
        try:
            for message in consumer:
                conn = get_conn()
                try:
                    process_event(conn, message.value)
                    consumer.commit()
                except Exception as e:
                    logger.error(
                        "Failed to process message from %s: %s",
                        message.topic,
                        e,
                    )
                    conn.rollback()
                finally:
                    conn.close()
        except Exception as e:
            logger.error("Consumer loop error: %s", e)
            time.sleep(5)
            consumer = create_consumer()


if __name__ == "__main__":
    main()
