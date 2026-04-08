"""API gateway: RBAC authentication, routing, and idempotency.

Single entry point for all external requests. Enforces:
- API key authentication
- Role-based route authorization
- Idempotency key deduplication
- Request/trace ID propagation
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
import psycopg2.extras

psycopg2.extras.register_uuid()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
)
logger = logging.getLogger("api-gateway")

DB_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://defi_user:defi_pass@postgres:5432/defi_db",
)
PORT = int(os.environ.get("PORT", "8000"))

# Service URLs
COMPLIANCE_URL = os.environ.get("COMPLIANCE_URL", "http://compliance-engine:8001")
LEDGER_URL = os.environ.get("LEDGER_URL", "http://ledger-service:8002")
SETTLEMENT_URL = os.environ.get("SETTLEMENT_URL", "http://settlement-service:8003")
SIGNING_URL = os.environ.get("SIGNING_URL", "http://signing-gateway:8004")

# API key -> role mapping (demo; production uses DB lookup with hashed keys)
API_KEYS = {
    "admin-key-demo-001": {
        "actor": "ADMIN/demo", "role": "ADMIN",
    },
    "trader-key-demo-001": {
        "actor": "TRADER/demo", "role": "TRADER",
    },
    "signer-key-demo-001": {
        "actor": "SIGNER/demo", "role": "SIGNER",
    },
    "compliance-key-demo-001": {
        "actor": "COMPLIANCE/demo", "role": "COMPLIANCE_OFFICER",
    },
    "auditor-key-demo-001": {
        "actor": "AUDITOR/demo", "role": "AUDITOR",
    },
    "system-key-demo-001": {
        "actor": "SYSTEM/api-gateway", "role": "SYSTEM",
    },
}

# Route -> required role mapping
ROUTE_PERMISSIONS = {
    "POST /v1/compliance/screen": {"ADMIN", "COMPLIANCE_OFFICER", "SYSTEM"},
    "POST /v1/ledger/journal": {"ADMIN", "SYSTEM", "TRADER"},
    "GET /v1/ledger/balance": {"ADMIN", "SYSTEM", "TRADER", "AUDITOR"},
    "POST /v1/settlement": {"ADMIN", "SYSTEM", "SETTLEMENT_AGENT"},
    "POST /v1/settlement/transition": {"ADMIN", "SYSTEM", "SETTLEMENT_AGENT"},
    "GET /v1/settlement": {"ADMIN", "SYSTEM", "TRADER", "AUDITOR"},
    "POST /v1/sign": {"ADMIN", "SYSTEM", "SIGNER"},
}


def get_conn():
    return psycopg2.connect(DB_URL)


def proxy_request(
    target_url: str, method: str, body: bytes = None
) -> tuple[int, dict]:
    """Forward a request to a backend service."""
    try:
        req = Request(
            target_url,
            data=body,
            headers={"Content-Type": "application/json"} if body else {},
            method=method,
        )
        with urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read())
    except URLError as e:
        if hasattr(e, "code"):
            try:
                return e.code, json.loads(e.read())
            except Exception:
                return e.code, {"error": str(e)}
        return 502, {"error": f"Backend unavailable: {e}"}


class GatewayHandler(BaseHTTPRequestHandler):

    def _send_json(self, code: int, body: dict):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(body).encode())

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length", 0))
        return self.rfile.read(length) if length else b""

    def _authenticate(self) -> dict | None:
        """Authenticate via API key header."""
        api_key = self.headers.get("X-API-Key", "")
        if api_key in API_KEYS:
            return API_KEYS[api_key]
        return None

    def _authorize(self, method: str, path: str, role: str) -> bool:
        """Check route-level authorization."""
        # Normalize path for matching
        route_key = f"{method} {path}"
        for pattern, allowed_roles in ROUTE_PERMISSIONS.items():
            pat_method, pat_path = pattern.split(" ", 1)
            if pat_method == method and path.startswith(pat_path):
                return role in allowed_roles
        # Default: allow ADMIN and SYSTEM
        return role in ("ADMIN", "SYSTEM")

    def _inject_context(self, body_bytes: bytes, auth: dict) -> bytes:
        """Inject request_id, trace_id, actor metadata into request body."""
        if not body_bytes:
            body_bytes = b"{}"
        body = json.loads(body_bytes)
        body.setdefault("request_id", str(uuid.uuid4()))
        body.setdefault("trace_id", str(uuid.uuid4()))
        body.setdefault("actor", auth["actor"])
        body.setdefault("actor_role", auth["role"])
        return json.dumps(body).encode()

    def _check_idempotency(self, key: str) -> dict | None:
        """Check if request was already processed."""
        if not key:
            return None
        conn = get_conn()
        try:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute(
                "SELECT response_code, response_body FROM idempotency_keys WHERE key = %s",
                (key,),
            )
            row = cur.fetchone()
            if row:
                return {"code": row["response_code"], "body": row["response_body"]}
            return None
        finally:
            conn.close()

    def _store_idempotency(self, key: str, code: int, body: dict):
        """Store idempotency result."""
        if not key:
            return
        conn = get_conn()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO idempotency_keys (key, response_code, response_body)
                VALUES (%s, %s, %s) ON CONFLICT (key) DO NOTHING
                """,
                (key, code, json.dumps(body)),
            )
            conn.commit()
        finally:
            conn.close()

    def do_GET(self):
        if self.path == "/health":
            # Aggregate health from all services
            services = {}
            for name, url in [
                ("compliance", COMPLIANCE_URL),
                ("ledger", LEDGER_URL),
                ("settlement", SETTLEMENT_URL),
                ("signing", SIGNING_URL),
            ]:
                try:
                    code, _ = proxy_request(f"{url}/health", "GET")
                    services[name] = "ok" if code == 200 else "degraded"
                except Exception:
                    services[name] = "down"

            self._send_json(200, {"gateway": "ok", "services": services})
            return

        # Authenticated routes
        auth = self._authenticate()
        if not auth:
            self._send_json(401, {"error": "Invalid or missing API key"})
            return

        if not self._authorize("GET", self.path, auth["role"]):
            self._send_json(403, {"error": "Insufficient permissions"})
            return

        if self.path.startswith("/v1/ledger/balance/"):
            account_id = self.path.split("/v1/ledger/balance/")[1]
            code, body = proxy_request(f"{LEDGER_URL}/balance/{account_id}", "GET")
            self._send_json(code, body)
            return

        if self.path.startswith("/v1/settlement/"):
            sid = self.path.split("/v1/settlement/")[1]
            code, body = proxy_request(f"{SETTLEMENT_URL}/settlement/{sid}", "GET")
            self._send_json(code, body)
            return

        self._send_json(404, {"error": "not found"})

    def do_POST(self):
        auth = self._authenticate()
        if not auth:
            self._send_json(401, {"error": "Invalid or missing API key"})
            return

        if not self._authorize("POST", self.path, auth["role"]):
            self._send_json(403, {"error": "Insufficient permissions"})
            return

        raw_body = self._read_body()

        # Idempotency check
        idempotency_key = self.headers.get("Idempotency-Key", "")
        if idempotency_key:
            cached = self._check_idempotency(idempotency_key)
            if cached:
                self._send_json(cached["code"], cached["body"])
                return

        body = self._inject_context(raw_body, auth)

        if self.path == "/v1/compliance/screen":
            code, resp = proxy_request(f"{COMPLIANCE_URL}/screen", "POST", body)
        elif self.path == "/v1/ledger/journal":
            code, resp = proxy_request(f"{LEDGER_URL}/journal", "POST", body)
        elif self.path == "/v1/settlement":
            code, resp = proxy_request(f"{SETTLEMENT_URL}/settlement", "POST", body)
        elif self.path.startswith("/v1/settlement/") and self.path.endswith("/transition"):
            sid = self.path.split("/v1/settlement/")[1].split("/transition")[0]
            code, resp = proxy_request(
                f"{SETTLEMENT_URL}/settlement/{sid}/transition", "POST", body
            )
        elif self.path == "/v1/sign":
            code, resp = proxy_request(f"{SIGNING_URL}/sign", "POST", body)
        else:
            self._send_json(404, {"error": "not found"})
            return

        if idempotency_key:
            self._store_idempotency(idempotency_key, code, resp)

        self._send_json(code, resp)

    def log_message(self, format, *args):
        logger.info(format, *args)


def main():
    logger.info("API gateway starting on port %d", PORT)
    server = HTTPServer(("0.0.0.0", PORT), GatewayHandler)
    server.serve_forever()


if __name__ == "__main__":
    main()
