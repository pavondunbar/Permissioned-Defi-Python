"""MPC signing gateway: 2-of-3 quorum threshold signing.

Fans out signing requests to MPC nodes in parallel, collects
partial signatures, and combines them once quorum is reached.
Uses deterministic stubs (SHA-256) for demo purposes.
"""

import hashlib
import json
import logging
import os
import uuid
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.request import urlopen, Request
from urllib.error import URLError

import psycopg2

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
)
logger = logging.getLogger("signing-gateway")

DB_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://defi_user:defi_pass@postgres:5432/defi_db",
)
MPC_NODES = os.environ.get(
    "MPC_NODES",
    "http://mpc-node-1:8001,http://mpc-node-2:8001,http://mpc-node-3:8001",
).split(",")
QUORUM_THRESHOLD = int(os.environ.get("QUORUM_THRESHOLD", "2"))
PORT = int(os.environ.get("PORT", "8004"))


def get_conn():
    return psycopg2.connect(DB_URL)


def request_partial_signature(node_url: str, payload: dict) -> dict | None:
    """Request a partial signature from a single MPC node."""
    try:
        req = Request(
            f"{node_url}/sign",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except (URLError, Exception) as e:
        logger.warning("MPC node %s failed: %s", node_url, e)
        return None


def combine_signatures(partials: list[dict], payload_hash: str) -> str:
    """Combine partial signatures into a final combined signature.

    In production this would use threshold-ECDSA/EdDSA.
    For demo: concatenate partial hashes and hash the result.
    """
    sorted_parts = sorted(
        partials,
        key=lambda p: p.get("node_id", ""),
    )
    combined_input = "|".join(
        p.get("partial_signature", "") for p in sorted_parts
    )
    return hashlib.sha256(
        f"{payload_hash}:{combined_input}".encode()
    ).hexdigest()


def _persist_signatures(
    settlement_id: str, partials: list[dict], payload: dict
):
    """Write partial signatures to mpc_signatures table.

    Uses ON CONFLICT DO NOTHING for idempotency against the
    UNIQUE(settlement_id, signer_id) constraint.
    """
    request_id = payload.get("request_id", str(uuid.uuid4()))
    trace_id = payload.get("trace_id", str(uuid.uuid4()))
    actor = payload.get("actor", "SYSTEM/signing-gateway")
    actor_role = payload.get("actor_role", "SYSTEM")

    conn = get_conn()
    try:
        cur = conn.cursor()
        for partial in partials:
            cur.execute(
                """
                INSERT INTO mpc_signatures
                    (settlement_id, signer_id, signature_share,
                     request_id, trace_id, actor, actor_role)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (settlement_id, signer_id) DO NOTHING
                """,
                (
                    settlement_id,
                    partial.get("node_id"),
                    partial.get("partial_signature"),
                    request_id,
                    trace_id,
                    actor,
                    actor_role,
                ),
            )
        conn.commit()
        logger.info(
            "Persisted %d signatures for settlement %s",
            len(partials),
            settlement_id,
        )
    except Exception as e:
        conn.rollback()
        logger.error(
            "Failed to persist signatures for settlement %s: %s",
            settlement_id,
            e,
        )
        raise
    finally:
        conn.close()


def sign_transaction(payload: dict) -> dict:
    """Orchestrate MPC signing across all nodes.

    Fans out to all nodes, collects partials, combines once
    quorum threshold is met.
    """
    tx_id = payload.get("tx_id", str(uuid.uuid4()))
    payload_hash = hashlib.sha256(
        json.dumps(payload, sort_keys=True).encode()
    ).hexdigest()

    logger.info(
        "Signing tx_id=%s across %d nodes (quorum=%d)",
        tx_id,
        len(MPC_NODES),
        QUORUM_THRESHOLD,
    )

    sign_request = {
        "tx_id": tx_id,
        "payload_hash": payload_hash,
        "payload": payload,
    }

    # Fan out to all MPC nodes
    partials = []
    for node_url in MPC_NODES:
        result = request_partial_signature(node_url, sign_request)
        if result and result.get("partial_signature"):
            partials.append(result)
            logger.info(
                "Partial signature from %s: node_id=%s",
                node_url,
                result.get("node_id"),
            )

    if len(partials) < QUORUM_THRESHOLD:
        return {
            "tx_id": tx_id,
            "status": "FAILED",
            "error": (
                f"Quorum not reached: got {len(partials)}"
                f" of {QUORUM_THRESHOLD} required"
            ),
            "partials_received": len(partials),
        }

    combined = combine_signatures(partials, payload_hash)
    simulated_tx_hash = f"0x{combined}"

    settlement_id = payload.get("settlement_id")
    if settlement_id:
        _persist_signatures(settlement_id, partials, payload)

    signers = [p.get("node_id") for p in partials]

    return {
        "tx_id": tx_id,
        "status": "SIGNED",
        "combined_signature": combined,
        "simulated_tx_hash": simulated_tx_hash,
        "partials_received": len(partials),
        "quorum_threshold": QUORUM_THRESHOLD,
        "signers": signers,
    }


class GatewayHandler(BaseHTTPRequestHandler):

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
            self._send_json(200, {"status": "ok", "service": "signing-gateway"})
            return
        self._send_json(404, {"error": "not found"})

    def do_POST(self):
        if self.path == "/sign":
            body = self._read_body()
            result = sign_transaction(body)
            code = 200 if result["status"] == "SIGNED" else 503
            self._send_json(code, result)
            return
        self._send_json(404, {"error": "not found"})

    def log_message(self, format, *args):
        logger.info(format, *args)


def main():
    logger.info(
        "Signing gateway starting on port %d (nodes=%s, quorum=%d)",
        PORT,
        MPC_NODES,
        QUORUM_THRESHOLD,
    )
    server = HTTPServer(("0.0.0.0", PORT), GatewayHandler)
    server.serve_forever()


if __name__ == "__main__":
    main()
