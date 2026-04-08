"""MPC signing node: produces deterministic partial signatures.

Each node holds a simulated key share and produces a partial
signature from the payload hash and its node ID. In production,
this would be threshold-ECDSA/EdDSA running inside an HSM.
"""

import hashlib
import json
import logging
import os
from http.server import HTTPServer, BaseHTTPRequestHandler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
)

NODE_ID = os.environ.get("NODE_ID", "node-unknown")
PORT = int(os.environ.get("PORT", "8001"))

logger = logging.getLogger(f"mpc-{NODE_ID}")

# Simulated key share (deterministic per node)
KEY_SHARE = hashlib.sha256(f"key-share-{NODE_ID}".encode()).hexdigest()


def compute_partial_signature(payload_hash: str) -> str:
    """Compute a deterministic partial signature.

    Production: threshold-ECDSA partial using the node's key share.
    Demo: HMAC-like hash of payload_hash with the key share.
    """
    return hashlib.sha256(
        f"{KEY_SHARE}:{payload_hash}".encode()
    ).hexdigest()


class NodeHandler(BaseHTTPRequestHandler):

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
            self._send_json(200, {
                "status": "ok",
                "node_id": NODE_ID,
                "service": "mpc-node",
            })
            return
        self._send_json(404, {"error": "not found"})

    def do_POST(self):
        if self.path == "/sign":
            body = self._read_body()
            payload_hash = body.get("payload_hash", "")
            tx_id = body.get("tx_id", "unknown")

            partial = compute_partial_signature(payload_hash)
            logger.info(
                "Partial signature for tx_id=%s: %s...",
                tx_id,
                partial[:16],
            )

            self._send_json(200, {
                "node_id": NODE_ID,
                "tx_id": tx_id,
                "partial_signature": partial,
                "payload_hash": payload_hash,
            })
            return

        self._send_json(404, {"error": "not found"})

    def log_message(self, format, *args):
        logger.info(format, *args)


def main():
    logger.info("MPC node %s starting on port %d", NODE_ID, PORT)
    server = HTTPServer(("0.0.0.0", PORT), NodeHandler)
    server.serve_forever()


if __name__ == "__main__":
    main()
