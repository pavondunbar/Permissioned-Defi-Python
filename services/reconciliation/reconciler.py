"""Reconciliation engine: replay ledger, recompute balances, compare, alert.

Performs deterministic state rebuild by replaying all journal entries
and comparing derived balances against the live views.
"""

import json
import logging
import os
import time
import uuid
from decimal import Decimal

import psycopg2
import psycopg2.extras

psycopg2.extras.register_uuid()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
)
logger = logging.getLogger("reconciliation")

DB_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://defi_user:defi_pass@postgres:5432/defi_db",
)
INTERVAL_SECS = int(os.environ.get("RECONCILIATION_INTERVAL", "60"))


def get_conn():
    return psycopg2.connect(DB_URL)


def run_reconciliation(conn) -> dict:
    """Execute a full reconciliation run.

    Steps:
    1. Replay all journal entries -> recompute balances from scratch
    2. Compare against account_balances view
    3. Verify journal pair integrity (SUM debit = SUM credit per journal_id)
    4. Record mismatches and update run status
    """
    run_id = str(uuid.uuid4())
    request_id = str(uuid.uuid4())
    trace_id = str(uuid.uuid4())
    actor = "SYSTEM/reconciliation"
    actor_role = "SYSTEM"

    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    # Create reconciliation run
    cur.execute(
        """
        INSERT INTO reconciliation_runs
            (id, run_type, status,
             request_id, trace_id, actor, actor_role)
        VALUES (%s, 'FULL', 'RUNNING', %s, %s, %s, %s)
        """,
        (run_id, request_id, trace_id, actor, actor_role),
    )
    conn.commit()

    mismatches = []
    total_checked = 0

    try:
        # CHECK 1: Journal pair integrity
        cur.execute(
            """
            SELECT journal_id,
                   SUM(debit) AS total_debit,
                   SUM(credit) AS total_credit
            FROM journal_entries
            GROUP BY journal_id
            HAVING SUM(debit) <> SUM(credit)
            """
        )
        unbalanced = cur.fetchall()
        for row in unbalanced:
            mismatches.append({
                "type": "UNBALANCED_JOURNAL",
                "entity_type": "journal",
                "entity_id": str(row["journal_id"]),
                "expected": str(row["total_debit"]),
                "actual": str(row["total_credit"]),
            })
        total_checked += 1

        # CHECK 2: Replay journal -> recompute balances
        cur.execute(
            """
            SELECT
                je.account_id,
                je.currency,
                je.coa_code,
                coa.normal_balance,
                CASE
                    WHEN coa.normal_balance = 'debit'
                        THEN COALESCE(SUM(je.debit), 0) - COALESCE(SUM(je.credit), 0)
                    ELSE COALESCE(SUM(je.credit), 0) - COALESCE(SUM(je.debit), 0)
                END AS replayed_balance
            FROM journal_entries je
            JOIN chart_of_accounts coa ON coa.code = je.coa_code
            GROUP BY je.account_id, je.currency, je.coa_code, coa.normal_balance
            """
        )
        replayed = {
            (str(r["account_id"]), r["currency"], r["coa_code"]): Decimal(
                str(r["replayed_balance"])
            )
            for r in cur.fetchall()
        }

        # Compare with view
        cur.execute(
            "SELECT account_id, currency, coa_code, balance FROM account_balances"
        )
        view_balances = {
            (str(r["account_id"]), r["currency"], r["coa_code"]): Decimal(
                str(r["balance"])
            )
            for r in cur.fetchall()
        }

        all_keys = set(replayed.keys()) | set(view_balances.keys())
        for key in all_keys:
            replayed_val = replayed.get(key, Decimal("0"))
            view_val = view_balances.get(key, Decimal("0"))
            if replayed_val != view_val:
                mismatches.append({
                    "type": "BALANCE_MISMATCH",
                    "entity_type": "account_balance",
                    "entity_id": key[0],
                    "expected": str(replayed_val),
                    "actual": str(view_val),
                    "details": {
                        "currency": key[1],
                        "coa_code": key[2],
                    },
                })
            total_checked += 1

        # CHECK 3: No negative asset balances
        cur.execute(
            """
            SELECT ab.account_id, ab.currency, ab.coa_code, ab.balance
            FROM account_balances ab
            JOIN chart_of_accounts coa ON coa.code = ab.coa_code
            WHERE coa.account_type = 'asset' AND ab.balance < 0
            """
        )
        for row in cur.fetchall():
            mismatches.append({
                "type": "NEGATIVE_ASSET_BALANCE",
                "entity_type": "account_balance",
                "entity_id": str(row["account_id"]),
                "expected": "0",
                "actual": str(row["balance"]),
            })
        total_checked += 1

        # CHECK 4: Settlement state machine consistency
        cur.execute(
            """
            SELECT se.settlement_id, se.status, se.created_at,
                   LAG(se.status) OVER (
                       PARTITION BY se.settlement_id
                       ORDER BY se.created_at
                   ) AS prev_status
            FROM settlement_events se
            """
        )
        for row in cur.fetchall():
            if row["prev_status"] is None:
                if row["status"] != "PENDING":
                    mismatches.append({
                        "type": "INVALID_INITIAL_STATE",
                        "entity_type": "settlement",
                        "entity_id": str(row["settlement_id"]),
                        "expected": "PENDING",
                        "actual": row["status"],
                    })
        total_checked += 1

        # Record mismatches
        for m in mismatches:
            cur.execute(
                """
                INSERT INTO reconciliation_mismatches
                    (run_id, mismatch_type, entity_type, entity_id,
                     expected_value, actual_value, details)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    run_id,
                    m["type"],
                    m["entity_type"],
                    m.get("entity_id"),
                    (
                        Decimal(m["expected"])
                        if m.get("expected")
                        and m["expected"].replace(".", "").replace("-", "").isdigit()
                        else None
                    ),
                    (
                        Decimal(m["actual"])
                        if m.get("actual")
                        and m["actual"].replace(".", "").replace("-", "").isdigit()
                        else None
                    ),
                    json.dumps(m.get("details")) if m.get("details") else None,
                ),
            )

        status = "FAILED" if mismatches else "PASSED"
        cur.execute(
            """
            UPDATE reconciliation_runs
            SET status = %s, total_checked = %s, mismatches = %s,
                completed_at = NOW()
            WHERE id = %s
            """,
            (status, total_checked, len(mismatches), run_id),
        )

        # Audit trail
        cur.execute(
            """
            INSERT INTO audit_events
                (request_id, trace_id, actor, actor_role,
                 action, resource_type, resource_id, details)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                request_id,
                trace_id,
                actor,
                actor_role,
                "reconciliation.complete",
                "reconciliation_run",
                run_id,
                json.dumps({
                    "status": status,
                    "total_checked": total_checked,
                    "mismatches": len(mismatches),
                }),
            ),
        )

        conn.commit()

        result = {
            "run_id": run_id,
            "status": status,
            "total_checked": total_checked,
            "mismatches": len(mismatches),
            "mismatch_details": mismatches,
        }

        if mismatches:
            logger.warning(
                "Reconciliation FAILED: %d mismatches found", len(mismatches)
            )
            for m in mismatches:
                logger.warning("  %s: %s", m["type"], m)
        else:
            logger.info(
                "Reconciliation PASSED: %d checks verified", total_checked
            )

        return result

    except Exception as e:
        logger.error("Reconciliation error: %s", e)
        cur.execute(
            """
            UPDATE reconciliation_runs
            SET status = 'ERROR', completed_at = NOW()
            WHERE id = %s
            """,
            (run_id,),
        )
        conn.commit()
        return {"run_id": run_id, "status": "ERROR", "error": str(e)}


def main():
    logger.info(
        "Reconciliation engine starting (interval=%ds)", INTERVAL_SECS
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

    while True:
        try:
            conn = get_conn()
            try:
                run_reconciliation(conn)
            finally:
                conn.close()
        except Exception as e:
            logger.error("Reconciliation loop error: %s", e)

        time.sleep(INTERVAL_SECS)


if __name__ == "__main__":
    main()
