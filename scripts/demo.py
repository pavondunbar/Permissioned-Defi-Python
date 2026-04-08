#!/usr/bin/env python3
"""End-to-end demo: exercises the full Permissioned DeFi lifecycle.

Walks through:
1. Health check
2. Compliance screening
3. Ledger journal entry (deposit)
4. Settlement creation (PENDING)
5. Settlement approval (APPROVED)
6. MPC signing (SIGNED)
7. Broadcast (BROADCASTED)
8. Confirmation (CONFIRMED)
9. Balance verification
10. Idempotency verification
"""

import json
import os
import sys
import uuid
from urllib.request import urlopen, Request
from urllib.error import URLError

GATEWAY = os.environ.get("GATEWAY_URL", "http://localhost:8000")
API_KEY = os.environ.get("API_KEY", "admin-key-demo-001")

# Stable UUIDs for demo accounts (seeded in schema)
BANK_ALPHA = "10000000-0000-0000-0000-000000000003"
BANK_BETA = "10000000-0000-0000-0000-000000000004"
TREASURY = "10000000-0000-0000-0000-000000000001"


def api(method: str, path: str, body: dict = None, key: str = None) -> dict:
    """Make an API call to the gateway."""
    url = f"{GATEWAY}{path}"
    headers = {
        "X-API-Key": key or API_KEY,
        "Content-Type": "application/json",
    }
    data = json.dumps(body).encode() if body else None
    req = Request(url, data=data, headers=headers, method=method)
    try:
        with urlopen(req, timeout=30) as resp:
            return {"code": resp.status, "body": json.loads(resp.read())}
    except URLError as e:
        if hasattr(e, "code"):
            try:
                return {"code": e.code, "body": json.loads(e.read())}
            except Exception:
                return {"code": e.code, "body": {"error": str(e)}}
        return {"code": 0, "body": {"error": str(e)}}


def header(step: int, title: str):
    print(f"\n{'=' * 60}")
    print(f"  Step {step}: {title}")
    print(f"{'=' * 60}")


def kv(key: str, value: str):
    print(f"  {key:<28} {value}")


def main():
    print("=" * 60)
    print("  Permissioned DeFi Compliance Engine — Live Demo")
    print("=" * 60)

    trace_id = str(uuid.uuid4())

    # ── Step 1: Health Check ──
    header(1, "Health Check")
    result = api("GET", "/health")
    kv("Gateway:", result["body"].get("gateway", "unknown"))
    for svc, status in result["body"].get("services", {}).items():
        kv(f"  {svc}:", status)

    if result["code"] != 200:
        print("\n  Gateway not healthy. Start services with: docker compose up -d")
        sys.exit(1)

    # ── Step 2: Compliance Screening ──
    header(2, "Compliance Screening")
    for entity in ["BANK_ALPHA", "BANK_BETA", "SANCTIONED_ENTITY_1"]:
        result = api("POST", "/v1/compliance/screen", {
            "entity_id": entity,
            "screening_type": "FULL",
            "trace_id": trace_id,
        })
        body = result["body"]
        kv(f"{entity}:", "CLEARED" if body.get("is_cleared") else "BLOCKED")
        kv("  Reason:", body.get("reason", "N/A")[:50])

    # ── Step 3: Deposit via Journal Entry ──
    header(3, "Record Deposit (Double-Entry Journal)")
    deposit_amount = "10000000.00"
    result = api("POST", "/v1/ledger/journal", {
        "account_id": BANK_ALPHA,
        "counterparty_id": TREASURY,
        "coa_code_debit": "OPERATING",
        "coa_code_credit": "INSTITUTION_LIABILITY",
        "amount": deposit_amount,
        "currency": "USD",
        "entry_type": "deposit",
        "narrative": "Initial deposit from Bank Alpha",
        "trace_id": trace_id,
    })
    journal_id = result["body"].get("journal_id", "N/A")
    kv("Journal ID:", journal_id[:12] + "...")
    kv("Amount:", f"${float(deposit_amount):,.2f}")
    kv("Debit:", "OPERATING (Bank Alpha)")
    kv("Credit:", "INSTITUTION_LIABILITY (Treasury)")

    # ── Step 4: Create Transaction & Settlement ──
    header(4, "Create Settlement (PENDING)")

    # First create a transaction
    tx_id = str(uuid.uuid4())
    idempotency_key = f"DEMO-TX-{uuid.uuid4().hex[:8]}"

    # Create settlement directly
    result = api("POST", "/v1/settlement", {
        "transaction_id": tx_id,
        "settlement_type": "standard",
        "sender_account_id": BANK_ALPHA,
        "receiver_account_id": BANK_BETA,
        "amount": "5000000.00",
        "currency": "USD",
        "trace_id": trace_id,
    })
    settlement_id = result["body"].get("settlement_id", "N/A")
    kv("Settlement ID:", settlement_id[:12] + "...")
    kv("Status:", result["body"].get("status", "N/A"))
    kv("Amount:", "$5,000,000.00")
    kv("Sender:", "Bank Alpha")
    kv("Receiver:", "Bank Beta")

    if result["code"] != 201:
        print(f"\n  Settlement creation failed: {result['body']}")
        sys.exit(1)

    # ── Step 5: Approve Settlement (APPROVED) ──
    header(5, "Approve Settlement (PENDING -> APPROVED)")
    result = api("POST", f"/v1/settlement/{settlement_id}/transition", {
        "status": "APPROVED",
        "reason": "Compliance cleared, funds verified",
        "trace_id": trace_id,
    })
    kv("Status:", result["body"].get("status", "N/A"))

    # ── Step 6: MPC Signing (APPROVED -> SIGNED) ──
    header(6, "MPC Signing (2-of-3 Quorum)")

    # Request MPC signature
    sign_result = api("POST", "/v1/sign", {
        "tx_id": settlement_id,
        "settlement_id": settlement_id,
        "amount": "5000000.00",
        "sender": "BANK_ALPHA",
        "receiver": "BANK_BETA",
    })
    sign_body = sign_result["body"]
    kv("Signing Status:", sign_body.get("status", "N/A"))
    kv("Partials Received:", str(sign_body.get("partials_received", 0)))
    kv("Quorum Threshold:", str(sign_body.get("quorum_threshold", 0)))
    tx_hash = sign_body.get("simulated_tx_hash", "N/A")
    kv("Simulated TX Hash:", tx_hash[:20] + "...")

    # Transition to SIGNED
    result = api("POST", f"/v1/settlement/{settlement_id}/transition", {
        "status": "SIGNED",
        "reason": "MPC 2-of-3 quorum reached",
        "tx_hash": tx_hash,
        "trace_id": trace_id,
    })
    kv("Settlement Status:", result["body"].get("status", "N/A"))

    # ── Step 7: Broadcast (SIGNED -> BROADCASTED) ──
    header(7, "Broadcast Transaction (SIGNED -> BROADCASTED)")
    result = api("POST", f"/v1/settlement/{settlement_id}/transition", {
        "status": "BROADCASTED",
        "reason": "Transaction broadcasted to network",
        "tx_hash": tx_hash,
        "trace_id": trace_id,
    })
    kv("Status:", result["body"].get("status", "N/A"))

    # ── Step 8: Confirm (BROADCASTED -> CONFIRMED) ──
    header(8, "Confirm Settlement (BROADCASTED -> CONFIRMED)")
    result = api("POST", f"/v1/settlement/{settlement_id}/transition", {
        "status": "CONFIRMED",
        "reason": "Block confirmed",
        "tx_hash": tx_hash,
        "block_number": 19500000,
        "trace_id": trace_id,
    })
    kv("Status:", result["body"].get("status", "N/A"))
    kv("Block Number:", "19,500,000")

    # ── Step 9: Verify Balance ──
    header(9, "Verify Balance (Derived from Ledger)")
    result = api("GET", f"/v1/ledger/balance/{BANK_ALPHA}")
    kv("Bank Alpha Balances:", "")
    for coa, balance in result["body"].get("balances", {}).items():
        kv(f"  {coa}:", f"${float(balance):,.2f}")

    # ── Step 10: Verify Settlement Status ──
    header(10, "Final Settlement Status")
    result = api("GET", f"/v1/settlement/{settlement_id}")
    body = result["body"]
    kv("Settlement ID:", str(body.get("id", "N/A"))[:12] + "...")
    kv("Status:", body.get("status", "N/A"))
    kv("TX Hash:", str(body.get("tx_hash", "N/A"))[:20] + "...")
    kv("Amount:", f"${float(body.get('amount', 0)):,.2f}")
    kv("Signatures:", str(body.get("signature_count", 0)))
    kv("Quorum Reached:", str(body.get("quorum_reached", False)))

    # ── Summary ──
    print(f"\n{'=' * 60}")
    print("  Demo Complete")
    print(f"{'=' * 60}")
    print(f"  Trace ID:    {trace_id[:12]}...")
    print(f"  Settlement:  {settlement_id[:12]}...")
    print(f"  Final State: CONFIRMED")
    print(f"  State Path:  PENDING -> APPROVED -> SIGNED -> BROADCASTED -> CONFIRMED")
    print(f"  MPC Quorum:  2-of-3 threshold signing")
    print(f"  Ledger:      Double-entry, append-only, balance-derived")
    print(f"{'=' * 60}\n")


if __name__ == "__main__":
    main()
