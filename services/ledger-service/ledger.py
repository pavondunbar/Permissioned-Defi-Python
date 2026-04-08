"""Double-entry ledger service.

Append-only journal entries with balance derivation.
No UPDATE or DELETE — balances are always computed from the full journal.
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
logger = logging.getLogger("ledger-service")

DB_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://defi_user:defi_pass@postgres:5432/defi_db",
)
PORT = int(os.environ.get("PORT", "8002"))


def get_conn():
    return psycopg2.connect(DB_URL)


def create_journal_entry(
    conn,
    account_id: str,
    counterparty_id: str,
    coa_code_debit: str,
    coa_code_credit: str,
    amount: Decimal,
    currency: str,
    entry_type: str,
    reference_id: str,
    narrative: str,
    ctx: dict,
) -> str:
    """Create a balanced double-entry journal pair.

    Atomically inserts a debit and credit leg linked by journal_id.
    The deferred trigger ensures SUM(debit) = SUM(credit) at commit.
    """
    journal_id = str(uuid.uuid4())
    cur = conn.cursor()

    # Debit leg
    cur.execute(
        """
        INSERT INTO journal_entries
            (journal_id, account_id, coa_code, currency,
             debit, credit, entry_type, reference_id, narrative,
             request_id, trace_id, actor, actor_role)
        VALUES (%s, %s, %s, %s, %s, 0, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            journal_id,
            account_id,
            coa_code_debit,
            currency,
            amount,
            entry_type,
            reference_id,
            narrative,
            ctx["request_id"],
            ctx["trace_id"],
            ctx["actor"],
            ctx["actor_role"],
        ),
    )

    # Credit leg
    cur.execute(
        """
        INSERT INTO journal_entries
            (journal_id, account_id, coa_code, currency,
             debit, credit, entry_type, reference_id, narrative,
             request_id, trace_id, actor, actor_role)
        VALUES (%s, %s, %s, %s, 0, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            journal_id,
            counterparty_id,
            coa_code_credit,
            currency,
            amount,
            entry_type,
            reference_id,
            narrative,
            ctx["request_id"],
            ctx["trace_id"],
            ctx["actor"],
            ctx["actor_role"],
        ),
    )

    # Audit event
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
            "journal.create",
            "journal",
            journal_id,
            json.dumps(
                {
                    "entry_type": entry_type,
                    "amount": str(amount),
                    "currency": currency,
                }
            ),
        ),
    )

    return journal_id


def get_balance(conn, account_id: str, currency: str = "USD") -> dict:
    """Derive balance from journal entries (no stored balance)."""
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(
        """
        SELECT coa_code, balance
        FROM account_balances
        WHERE account_id = %s AND currency = %s
        """,
        (account_id, currency),
    )
    rows = cur.fetchall()
    return {row["coa_code"]: str(row["balance"]) for row in rows}


class LedgerHandler(BaseHTTPRequestHandler):
    """HTTP handler for the ledger service."""

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
            self._send_json(200, {"status": "ok", "service": "ledger"})
            return

        if self.path.startswith("/balance/"):
            account_id = self.path.split("/balance/")[1].split("?")[0]
            conn = get_conn()
            try:
                balances = get_balance(conn, account_id)
                self._send_json(200, {
                    "account_id": account_id,
                    "balances": balances,
                })
            finally:
                conn.close()
            return

        self._send_json(404, {"error": "not found"})

    def do_POST(self):
        if self.path == "/journal":
            body = self._read_body()
            conn = get_conn()
            try:
                ctx = {
                    "request_id": body.get("request_id", str(uuid.uuid4())),
                    "trace_id": body.get("trace_id", str(uuid.uuid4())),
                    "actor": body.get("actor", "SYSTEM/ledger-service"),
                    "actor_role": body.get("actor_role", "SYSTEM"),
                }
                journal_id = create_journal_entry(
                    conn,
                    account_id=body["account_id"],
                    counterparty_id=body["counterparty_id"],
                    coa_code_debit=body["coa_code_debit"],
                    coa_code_credit=body["coa_code_credit"],
                    amount=Decimal(body["amount"]),
                    currency=body.get("currency", "USD"),
                    entry_type=body["entry_type"],
                    reference_id=body.get("reference_id"),
                    narrative=body.get("narrative", ""),
                    ctx=ctx,
                )
                conn.commit()
                self._send_json(201, {
                    "journal_id": journal_id,
                    "status": "created",
                })
            except Exception as e:
                conn.rollback()
                logger.error("Journal creation failed: %s", e)
                self._send_json(400, {"error": str(e)})
            finally:
                conn.close()
            return

        self._send_json(404, {"error": "not found"})

    def log_message(self, format, *args):
        logger.info(format, *args)


def main():
    logger.info("Ledger service starting on port %d", PORT)
    server = HTTPServer(("0.0.0.0", PORT), LedgerHandler)
    server.serve_forever()


if __name__ == "__main__":
    main()
