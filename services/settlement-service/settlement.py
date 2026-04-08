"""Settlement service with deterministic state machine.

State transitions: PENDING -> APPROVED -> SIGNED -> BROADCASTED -> CONFIRMED
Each transition is recorded as an append-only event.
MPC signing requires 2-of-3 quorum before SIGNED state.
"""

import json
import logging
import os
import uuid
from decimal import Decimal
from http.server import HTTPServer, BaseHTTPRequestHandler

import psycopg2
import psycopg2.extras

psycopg2.extras.register_uuid()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
)
logger = logging.getLogger("settlement-service")

DB_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://defi_user:defi_pass@postgres:5432/defi_db",
)
PORT = int(os.environ.get("PORT", "8003"))


def get_conn():
    return psycopg2.connect(DB_URL)


def create_settlement(conn, body: dict, ctx: dict) -> dict:
    """Create a new settlement and its initial PENDING event.

    Also creates the parent transaction record atomically.
    """
    settlement_id = str(uuid.uuid4())
    transaction_id = body.get("transaction_id", str(uuid.uuid4()))
    idempotency_key = body.get(
        "idempotency_key", f"stl-{settlement_id}"
    )

    cur = conn.cursor()

    # Create parent transaction record
    cur.execute(
        """
        INSERT INTO transactions
            (id, account_id, tx_type, amount, currency,
             counterparty_id, idempotency_key,
             request_id, trace_id, actor, actor_role)
        VALUES (%s, %s, 'settlement', %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (idempotency_key) DO NOTHING
        """,
        (
            transaction_id,
            body["sender_account_id"],
            Decimal(body["amount"]),
            body.get("currency", "USD"),
            body["receiver_account_id"],
            idempotency_key,
            ctx["request_id"],
            ctx["trace_id"],
            ctx["actor"],
            ctx["actor_role"],
        ),
    )

    # Create settlement record
    cur.execute(
        """
        INSERT INTO settlements
            (id, transaction_id, settlement_type,
             sender_account_id, receiver_account_id,
             amount, currency,
             request_id, trace_id, actor, actor_role)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            settlement_id,
            transaction_id,
            body.get("settlement_type", "standard"),
            body["sender_account_id"],
            body["receiver_account_id"],
            Decimal(body["amount"]),
            body.get("currency", "USD"),
            ctx["request_id"],
            ctx["trace_id"],
            ctx["actor"],
            ctx["actor_role"],
        ),
    )

    # Initial PENDING event
    cur.execute(
        """
        INSERT INTO settlement_events
            (settlement_id, status,
             request_id, trace_id, actor, actor_role)
        VALUES (%s, 'PENDING', %s, %s, %s, %s)
        """,
        (
            settlement_id,
            ctx["request_id"],
            ctx["trace_id"],
            ctx["actor"],
            ctx["actor_role"],
        ),
    )

    # Outbox event (same transaction)
    cur.execute(
        """
        INSERT INTO outbox_events
            (aggregate_id, aggregate_type, event_type, topic, payload,
             request_id, trace_id, actor, actor_role)
        VALUES (%s, 'settlement', 'settlement.created', 'settlement.created',
                %s, %s, %s, %s, %s)
        """,
        (
            settlement_id,
            json.dumps({
                "settlement_id": settlement_id,
                "transaction_id": transaction_id,
                "amount": body["amount"],
                "currency": body.get("currency", "USD"),
                "status": "PENDING",
            }),
            ctx["request_id"],
            ctx["trace_id"],
            ctx["actor"],
            ctx["actor_role"],
        ),
    )

    # Audit
    cur.execute(
        """
        INSERT INTO audit_events
            (request_id, trace_id, actor, actor_role,
             action, resource_type, resource_id, details)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            ctx["request_id"],
            ctx["trace_id"],
            ctx["actor"],
            ctx["actor_role"],
            "settlement.create",
            "settlement",
            settlement_id,
            json.dumps({"status": "PENDING", "amount": body["amount"]}),
        ),
    )

    return {"settlement_id": settlement_id, "status": "PENDING"}


def transition_settlement(
    conn, settlement_id: str, target_status: str, ctx: dict, extra: dict = None
) -> dict:
    """Transition a settlement to a new state.

    The DB trigger enforces valid transitions. This function also
    writes an outbox event and audit trail atomically.
    """
    cur = conn.cursor()

    # Enforce MPC quorum before APPROVED -> SIGNED transition
    if target_status == "SIGNED":
        cur.execute(
            """
            SELECT quorum_reached
            FROM mpc_quorum_status
            WHERE settlement_id = %s
            """,
            (settlement_id,),
        )
        row = cur.fetchone()
        if not row or not row[0]:
            raise ValueError(
                f"MPC quorum not reached for settlement"
                f" {settlement_id}: cannot transition to SIGNED"
            )

    # The DB trigger will validate the transition
    cur.execute(
        """
        INSERT INTO settlement_events
            (settlement_id, status, reason, tx_hash, block_number,
             request_id, trace_id, actor, actor_role)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            settlement_id,
            target_status,
            (extra or {}).get("reason"),
            (extra or {}).get("tx_hash"),
            (extra or {}).get("block_number"),
            ctx["request_id"],
            ctx["trace_id"],
            ctx["actor"],
            ctx["actor_role"],
        ),
    )

    # Outbox event
    event_type = f"settlement.{target_status.lower()}"
    cur.execute(
        """
        INSERT INTO outbox_events
            (aggregate_id, aggregate_type, event_type, topic, payload,
             request_id, trace_id, actor, actor_role)
        VALUES (%s, 'settlement', %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            settlement_id,
            event_type,
            event_type,
            json.dumps({
                "settlement_id": settlement_id,
                "status": target_status,
                **(extra or {}),
            }),
            ctx["request_id"],
            ctx["trace_id"],
            ctx["actor"],
            ctx["actor_role"],
        ),
    )

    # Audit
    cur.execute(
        """
        INSERT INTO audit_events
            (request_id, trace_id, actor, actor_role,
             action, resource_type, resource_id, details)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            ctx["request_id"],
            ctx["trace_id"],
            ctx["actor"],
            ctx["actor_role"],
            f"settlement.transition.{target_status.lower()}",
            "settlement",
            settlement_id,
            json.dumps({"status": target_status, **(extra or {})}),
        ),
    )

    return {"settlement_id": settlement_id, "status": target_status}


def get_settlement_status(conn, settlement_id: str) -> dict:
    """Get settlement with its current status and quorum info."""
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    cur.execute(
        """
        SELECT s.id, s.transaction_id, s.settlement_type,
               s.amount, s.currency, s.created_at,
               scs.status, scs.tx_hash, scs.block_number
        FROM settlements s
        LEFT JOIN settlement_current_status scs
            ON scs.settlement_id = s.id
        WHERE s.id = %s
        """,
        (settlement_id,),
    )
    row = cur.fetchone()
    if not row:
        return None

    cur.execute(
        """
        SELECT signature_count, quorum_reached
        FROM mpc_quorum_status
        WHERE settlement_id = %s
        """,
        (settlement_id,),
    )
    quorum = cur.fetchone()

    result = dict(row)
    result["id"] = str(result["id"])
    result["transaction_id"] = str(result["transaction_id"])
    result["amount"] = str(result["amount"])
    result["created_at"] = str(result["created_at"])
    if quorum:
        result["signature_count"] = quorum["signature_count"]
        result["quorum_reached"] = quorum["quorum_reached"]

    return result


class SettlementHandler(BaseHTTPRequestHandler):

    def _send_json(self, code: int, body: dict):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(body).encode())

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(length)) if length else {}

    def _build_ctx(self, body: dict) -> dict:
        return {
            "request_id": body.get("request_id", str(uuid.uuid4())),
            "trace_id": body.get("trace_id", str(uuid.uuid4())),
            "actor": body.get("actor", "SYSTEM/settlement-service"),
            "actor_role": body.get("actor_role", "SYSTEM"),
        }

    def do_GET(self):
        if self.path == "/health":
            self._send_json(200, {"status": "ok", "service": "settlement"})
            return

        if self.path.startswith("/settlement/"):
            sid = self.path.split("/settlement/")[1]
            conn = get_conn()
            try:
                result = get_settlement_status(conn, sid)
                if result:
                    self._send_json(200, result)
                else:
                    self._send_json(404, {"error": "not found"})
            finally:
                conn.close()
            return

        self._send_json(404, {"error": "not found"})

    def do_POST(self):
        body = self._read_body()
        ctx = self._build_ctx(body)

        if self.path == "/settlement":
            conn = get_conn()
            try:
                result = create_settlement(conn, body, ctx)
                conn.commit()
                self._send_json(201, result)
            except Exception as e:
                conn.rollback()
                logger.error("Settlement creation failed: %s", e)
                self._send_json(400, {"error": str(e)})
            finally:
                conn.close()
            return

        if self.path.startswith("/settlement/") and "/transition" in self.path:
            parts = self.path.split("/")
            sid = parts[2]
            conn = get_conn()
            try:
                result = transition_settlement(
                    conn, sid, body["status"], ctx,
                    extra={
                        k: body[k]
                        for k in ("reason", "tx_hash", "block_number")
                        if k in body
                    },
                )
                conn.commit()
                self._send_json(200, result)
            except Exception as e:
                conn.rollback()
                logger.error("Settlement transition failed: %s", e)
                self._send_json(
                    409 if "transition" in str(e).lower() else 400,
                    {"error": str(e)},
                )
            finally:
                conn.close()
            return

        self._send_json(404, {"error": "not found"})

    def log_message(self, format, *args):
        logger.info(format, *args)


def main():
    logger.info("Settlement service starting on port %d", PORT)
    server = HTTPServer(("0.0.0.0", PORT), SettlementHandler)
    server.serve_forever()


if __name__ == "__main__":
    main()
