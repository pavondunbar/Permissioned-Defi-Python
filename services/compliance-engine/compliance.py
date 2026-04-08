"""Compliance engine: KYC/AML screening with audit trails.

Screens entities against simulated sanctions/PEP lists.
All decisions are append-only and immutable.
"""

import hashlib
import json
import logging
import os
import uuid
from http.server import HTTPServer, BaseHTTPRequestHandler

import psycopg2
import psycopg2.extras

psycopg2.extras.register_uuid()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
)
logger = logging.getLogger("compliance-engine")

DB_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://defi_user:defi_pass@postgres:5432/defi_db",
)
PORT = int(os.environ.get("PORT", "8001"))

# Simulated sanctions list
SANCTIONED_ENTITIES = {"SANCTIONED_ENTITY_1", "BLOCKED_CORP", "TERROR_ORG"}
PEP_ENTITIES = {"PEP_PERSON_1", "PEP_OFFICIAL_2"}


def get_conn():
    return psycopg2.connect(DB_URL)


def screen_entity(
    conn,
    entity_id: str,
    screening_type: str,
    ctx: dict,
) -> dict:
    """Screen an entity against compliance lists.

    Returns screening decision with full audit trail.
    """
    # Simulated screening logic
    is_sanctioned = entity_id.upper() in SANCTIONED_ENTITIES
    is_pep = entity_id.upper() in PEP_ENTITIES

    if screening_type == "SANCTIONS" or screening_type == "FULL":
        is_cleared = not is_sanctioned
        reason = (
            "Entity cleared all sanctions lists"
            if is_cleared
            else f"Entity {entity_id} found on sanctions list"
        )
    elif screening_type == "PEP":
        is_cleared = not is_pep
        reason = (
            "Entity not a PEP"
            if is_cleared
            else f"Entity {entity_id} identified as PEP"
        )
    elif screening_type == "AML":
        # Simulated AML: hash-based deterministic check
        risk_hash = hashlib.sha256(entity_id.encode()).hexdigest()
        is_cleared = not risk_hash.startswith("00")
        reason = (
            "AML screening passed"
            if is_cleared
            else "AML risk threshold exceeded"
        )
    else:
        is_cleared = not is_sanctioned and not is_pep
        reason = "Full screening completed"

    screening_ref = f"SCR-{uuid.uuid4().hex[:12].upper()}"

    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO compliance_screenings
            (entity_id, screening_type, is_cleared, reason,
             screening_reference,
             request_id, trace_id, actor, actor_role)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (
            entity_id,
            screening_type,
            is_cleared,
            reason,
            screening_ref,
            ctx["request_id"],
            ctx["trace_id"],
            ctx["actor"],
            ctx["actor_role"],
        ),
    )
    screening_id = str(cur.fetchone()[0])

    # Audit trail
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
            "compliance.screen",
            "compliance_screening",
            screening_id,
            json.dumps({
                "entity_id": entity_id,
                "screening_type": screening_type,
                "is_cleared": is_cleared,
            }),
        ),
    )

    return {
        "screening_id": screening_id,
        "entity_id": entity_id,
        "screening_type": screening_type,
        "is_cleared": is_cleared,
        "reason": reason,
        "screening_reference": screening_ref,
    }


class ComplianceHandler(BaseHTTPRequestHandler):

    def _send_json(self, code: int, body: dict):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(body).encode())

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(length)) if length else {}

    def do_GET(self):
        if self.path == "/health":
            self._send_json(200, {"status": "ok", "service": "compliance"})
            return
        self._send_json(404, {"error": "not found"})

    def do_POST(self):
        if self.path == "/screen":
            body = self._read_body()
            conn = get_conn()
            try:
                ctx = {
                    "request_id": body.get(
                        "request_id", str(uuid.uuid4())
                    ),
                    "trace_id": body.get(
                        "trace_id", str(uuid.uuid4())
                    ),
                    "actor": body.get(
                        "actor", "SYSTEM/compliance-engine"
                    ),
                    "actor_role": body.get("actor_role", "SYSTEM"),
                }
                result = screen_entity(
                    conn,
                    entity_id=body["entity_id"],
                    screening_type=body.get("screening_type", "FULL"),
                    ctx=ctx,
                )
                conn.commit()
                self._send_json(200, result)
            except Exception as e:
                conn.rollback()
                logger.error("Screening failed: %s", e)
                self._send_json(400, {"error": str(e)})
            finally:
                conn.close()
            return

        self._send_json(404, {"error": "not found"})

    def log_message(self, format, *args):
        logger.info(format, *args)


def main():
    logger.info("Compliance engine starting on port %d", PORT)
    server = HTTPServer(("0.0.0.0", PORT), ComplianceHandler)
    server.serve_forever()


if __name__ == "__main__":
    main()
