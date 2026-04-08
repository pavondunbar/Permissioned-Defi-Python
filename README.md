# Permissioned DeFi Compliance Engine

> **Sandbox / Educational Project**
> This is a sandbox environment for learning and testing. It simulates a permissioned DeFi compliance engine with institutional-grade settlement infrastructure, MPC threshold signing, and regulatory compliance enforcement. **Not for production use** — see [Production Considerations](#production-considerations) for what a real deployment would require.

---

## Table of Contents

- [Overview](#overview)
- [What is Permissioned DeFi?](#what-is-permissioned-defi)
- [Architecture](#architecture)
- [Core Services](#core-services)
- [Key Features & Design Patterns](#key-features--design-patterns)
- [Database Schema](#database-schema)
- [State Machines](#state-machines)
- [Real-World Example](#real-world-example)
- [Running in a Sandbox Environment](#running-in-a-sandbox-environment)
- [Project Structure](#project-structure)
- [Production Considerations](#production-considerations)
- [License](#license)

---

## Overview

| Component | Count |
|-----------|-------|
| Microservices | 9 (+ 3 MPC nodes) |
| Docker Networks | 3 (dmz, internal, signing) |
| Database Tables | 18 |
| Database Views | 5 |
| RBAC Roles | 7 |
| Kafka Topics | Auto-created per event type |
| State Machines | 2 (settlement + transaction) |
| MPC Signing Quorum | 2-of-3 threshold |
| Chart of Accounts | 8 COA codes |
| Demo Accounts | 5 (seeded) |

This system implements a **full-stack permissioned DeFi compliance engine** with:
- Append-only, double-entry PostgreSQL ledger with trigger-enforced immutability
- Deterministic settlement state machine (PENDING → APPROVED → SIGNED → BROADCASTED → CONFIRMED)
- MPC 2-of-3 threshold signing with isolated signing network
- Transactional outbox pattern for atomic Kafka event publishing
- 4-check reconciliation engine for continuous integrity verification
- RBAC with separation of duties across 7 roles
- Complete audit trail on every state transition

---

## What is Permissioned DeFi?

In traditional DeFi, anyone can participate — open protocols, anonymous actors, permissionless access. **Permissioned DeFi** applies the composability and transparency of decentralized finance to **regulated institutional environments** where participants must be identified, compliant, and authorized.

Think of it as the infrastructure layer where:

- **Banks and institutions** settle large-value transactions through a shared compliance framework
- **Every participant** passes KYC/AML/sanctions screening before any funds move
- **Every transaction** follows a deterministic state machine with full audit trails
- **Signing authority** is distributed across multiple key holders (MPC) — no single party can authorize a transfer alone
- **The ledger** is append-only and double-entry — balances are derived from replaying journal entries, never stored as mutable columns

Real-world parallels include institutional stablecoin settlement networks, tokenized securities platforms with regulatory gating, and cross-border payment rails that require compliance at every hop.

---

## Architecture

```
                              Internet
                                 │
                     ┌───────────┴───────────┐
                     │    API Gateway :8000   │  ← Only internet-facing service
                     │  (RBAC + Idempotency)  │
                     └─────┬─────┬─────┬─────┘
              ─ ─ ─ ─ DMZ Network ─ ─ ─ ─ ─ ─ ─
                           │     │     │
              ─ ─ ─ ─ Internal Network ─ ─ ─ ─ ─
                     ┌─────┘     │     └─────┐
                     ▼           ▼           ▼
              ┌────────────┐ ┌────────┐ ┌──────────┐
              │ Compliance │ │ Ledger │ │Settlement│
              │  Engine    │ │Service │ │ Service  │
              └─────┬──────┘ └───┬────┘ └────┬─────┘
                    │            │            │
                    ▼            ▼            ▼
              ┌──────────────────────────────────┐
              │         PostgreSQL 16             │
              │  (Append-only, Immutable Ledger)  │
              └──────────┬───────────────────────┘
                         │
              ┌──────────┴───────────┐
              │    Outbox Publisher   │──── Polls outbox_events
              └──────────┬───────────┘     (FOR UPDATE SKIP LOCKED)
                         │
                         ▼
              ┌──────────────────────┐
              │    Apache Kafka      │
              │  (acks=all, 7d ret.) │
              └──────────┬───────────┘
                         │
              ┌──────────┴───────────┐
              │   Event Consumer     │──── Dedup via processed_events
              └──────────────────────┘

              ─ ─ ─ ─ Signing Network ─ ─ ─ ─ ─
              ┌──────────────────────────────────┐
              │        Signing Gateway           │
              │   (MPC Coordination + Persist)   │
              └───┬──────────┬──────────┬────────┘
                  ▼          ▼          ▼
              ┌───────┐ ┌───────┐ ┌───────┐
              │MPC    │ │MPC    │ │MPC    │
              │Node 1 │ │Node 2 │ │Node 3 │
              └───────┘ └───────┘ └───────┘
                  2-of-3 Threshold Signing

              ┌──────────────────────────────────┐
              │     Reconciliation Engine         │
              │  (4-check continuous integrity)   │
              └──────────────────────────────────┘
```

**Network Isolation:**

| Network | Services | Purpose |
|---------|----------|---------|
| `dmz` | API Gateway | Internet-facing boundary |
| `internal` | All business services, Postgres, Kafka | Core processing |
| `signing` | Signing Gateway, MPC Nodes 1-3 | Isolated signing operations |

Only the API Gateway bridges the `dmz` and `internal` networks. Only the Signing Gateway bridges `internal` and `signing`. MPC nodes have no access to the database or Kafka.

---

## Core Services

### API Gateway (`services/api-gateway/`)
The sole internet-facing entry point. Enforces RBAC permissions on every request, handles API key authentication, manages idempotency keys, and proxies requests to internal services. Provides aggregated health checks across all services.

### Compliance Engine (`services/compliance-engine/`)
Performs KYC/AML/sanctions/PEP screening on entities before they can participate in transactions. Persists all screening results to the `compliance_screenings` table with full audit context. Blocked entities (sanctions matches) are rejected with detailed reasons.

### Ledger Service (`services/ledger-service/`)
Manages the append-only, double-entry journal. Every financial operation creates balanced debit/credit entry pairs under a shared `journal_id`. Balances are never stored — they are derived in real-time from the `account_balances` view by replaying all journal entries.

### Settlement Service (`services/settlement-service/`)
Manages the full settlement lifecycle through a deterministic state machine. Each state transition is persisted as an immutable event in `settlement_events`. Verifies MPC quorum status before allowing the SIGNED transition. Emits outbox events on every state change.

### Signing Gateway (`services/signing-gateway/`)
Coordinates MPC threshold signing. Fans out signing requests to all 3 MPC nodes, collects partial signatures, verifies quorum (2-of-3), persists individual signatures to `mpc_signatures`, and produces a combined signature hash. Bridges the `internal` and `signing` networks.

### MPC Nodes (`services/mpc-node/`)
Three independent signing nodes (SIGNER_1, SIGNER_2, SIGNER_3) that each hold a key share and produce partial signatures. Isolated on the `signing` network with no database access. Simulates HSM-backed key material in this sandbox.

### Outbox Publisher (`services/outbox-publisher/`)
Polls `outbox_events` for unpublished rows using `FOR UPDATE SKIP LOCKED` (no contention). Publishes to Kafka with `acks=all` for durability. Failed events are moved to the `dead_letter_queue` after exceeding `MAX_RETRIES` (default: 5).

### Event Consumer (`services/event-consumer/`)
Kafka consumer with manual offset commits and at-least-once delivery guarantees. Deduplicates events using the `processed_events` table. Writes audit events for all consumed messages.

### Reconciliation Engine (`services/reconciliation/`)
Runs 4 continuous integrity checks on a configurable interval (default: 60s):

| Check | What It Verifies |
|-------|-----------------|
| Journal Pair Integrity | Every `journal_id` has exactly 2 legs (debit + credit) that balance |
| Balance Replay | Recomputed balances from raw journal entries match the `account_balances` view |
| Negative Balance Detection | No account has gone below zero |
| State Machine Consistency | Every settlement's event history follows valid transitions |

---

## Key Features & Design Patterns

### Immutability & Append-Only Design
- All core tables have `deny_mutation()` triggers blocking UPDATE and DELETE
- Financial history is never modified — corrections are recorded as new journal entries
- Outbox events allow only `published_at` to be updated (delivery status)
- Dead letter queue allows only retry/resolved fields to be updated

### Double-Entry Accounting
- Every financial operation creates balanced debit + credit legs
- Deferred constraint trigger (`check_journal_balance`) ensures `SUM(debit) = SUM(credit)` per journal
- `CHECK (debit > 0 AND credit = 0) OR (debit = 0 AND credit > 0)` on every row
- 8 Chart of Accounts codes: OPERATING, CUSTODY_ASSET, SETTLEMENT_PENDING, INSTITUTION_LIABILITY, FEE_REVENUE, COLLATERAL_POSTED, ESCROW_HOLD, TREASURY
- Balances derived from views, never stored as mutable columns

### Deterministic State Machines
- Settlement and transaction state machines enforced at both application and database level
- Database triggers reject invalid transitions (e.g., PENDING → CONFIRMED)
- Terminal states (CONFIRMED, FAILED) block all further transitions
- Every transition is an immutable append-only event with full audit context

### MPC Threshold Signing (2-of-3)
- 3 independent signing nodes on an isolated network
- Signing gateway fans out to all nodes, requires 2 partial signatures
- Each partial signature persisted to `mpc_signatures` with unique constraint per (settlement, signer)
- Quorum verification via `mpc_quorum_status` database view
- Settlement cannot transition to SIGNED without quorum

### Transactional Outbox Pattern
- Business operations and outbox events are written in the same database transaction
- Outbox publisher polls and publishes asynchronously — no distributed transactions needed
- `FOR UPDATE SKIP LOCKED` prevents publisher contention
- Dead letter queue captures persistent failures with retry counts

### RBAC with Separation of Duties
- 7 roles: ADMIN, COMPLIANCE_OFFICER, SETTLEMENT_AGENT, SIGNER, SYSTEM, AUDITOR, TRADER
- Permissions checked on every API request via the gateway
- Role-permission mappings stored in the database, loaded at startup
- Service accounts use the SYSTEM role; human operators get specific roles

### Idempotency (Two Layers)
- **API Layer:** `idempotency_keys` table — duplicate API calls return cached responses
- **Consumer Layer:** `processed_events` table — duplicate Kafka events are skipped
- Both tables are immutable (UPDATE/DELETE denied by triggers)

### Audit Trail
- Every state transition records: `request_id`, `trace_id`, `actor`, `actor_role`
- Dedicated `audit_events` table with immutability triggers
- Trace IDs propagate end-to-end from API gateway through all services
- Audit context is a frozen dataclass — cannot be modified after creation

---

## Database Schema

### Core Financial Tables

#### `chart_of_accounts`
| Column | Type | Description |
|--------|------|-------------|
| `code` | VARCHAR(32) PK | Account code (OPERATING, TREASURY, etc.) |
| `name` | VARCHAR(128) | Human-readable name |
| `account_type` | VARCHAR(16) | asset, liability, equity, revenue, expense |
| `normal_balance` | VARCHAR(8) | debit or credit |

#### `accounts`
| Column | Type | Description |
|--------|------|-------------|
| `id` | UUID PK | Account identifier |
| `account_name` | VARCHAR(256) | Display name |
| `account_type` | VARCHAR(30) | institutional, custodian, treasury, system, escrow |
| `entity_id` | VARCHAR(256) | External entity reference |
| `currency` | VARCHAR(10) | Default: USD |
| `kyc_verified` | BOOLEAN | KYC screening status |
| `aml_cleared` | BOOLEAN | AML screening status |
| `metadata` | JSONB | Extensible attributes |

#### `journal_entries`
| Column | Type | Description |
|--------|------|-------------|
| `id` | UUID PK | Row identifier |
| `journal_id` | UUID | Groups debit + credit legs |
| `account_id` | UUID FK | Account reference |
| `coa_code` | VARCHAR(32) FK | Chart of accounts code |
| `debit` | NUMERIC(38,18) | Debit amount (0 if credit leg) |
| `credit` | NUMERIC(38,18) | Credit amount (0 if debit leg) |
| `entry_type` | VARCHAR(50) | deposit, withdrawal, transfer, etc. |
| `trace_id` | UUID | End-to-end trace identifier |

### Settlement Tables

#### `settlements`
| Column | Type | Description |
|--------|------|-------------|
| `id` | UUID PK | Settlement identifier |
| `transaction_id` | UUID FK | Parent transaction |
| `settlement_type` | VARCHAR(30) | dvp, pvp, standard, collateral |
| `sender_account_id` | UUID FK | Source account |
| `receiver_account_id` | UUID FK | Destination account |
| `amount` | NUMERIC(38,18) | Settlement amount |
| `currency` | VARCHAR(10) | Currency code |

#### `settlement_events`
| Column | Type | Description |
|--------|------|-------------|
| `id` | UUID PK | Event identifier |
| `settlement_id` | UUID FK | Parent settlement |
| `status` | VARCHAR(20) | PENDING, APPROVED, SIGNED, BROADCASTED, CONFIRMED, FAILED |
| `tx_hash` | VARCHAR(66) | Blockchain transaction hash |
| `block_number` | BIGINT | Confirmation block number |
| `reason` | TEXT | Transition reason |

### MPC Signing Tables

#### `mpc_key_shares`
| Column | Type | Description |
|--------|------|-------------|
| `signer_id` | VARCHAR(50) UNIQUE | Signer identifier (SIGNER_1, SIGNER_2, SIGNER_3) |
| `signer_name` | VARCHAR(256) | Human-readable name |
| `public_key` | VARCHAR(256) | Simulated public key |
| `status` | VARCHAR(20) | ACTIVE or REVOKED |

#### `mpc_signatures`
| Column | Type | Description |
|--------|------|-------------|
| `id` | UUID PK | Signature record identifier |
| `settlement_id` | UUID FK | Settlement being signed |
| `signer_id` | VARCHAR(50) FK | Which signer produced this |
| `signature_share` | VARCHAR(256) | Partial signature value |
| `signed_at` | TIMESTAMPTZ | Signature timestamp |

### Infrastructure Tables

#### `outbox_events`
| Column | Type | Description |
|--------|------|-------------|
| `id` | UUID PK | Event identifier |
| `aggregate_id` | UUID | Business entity ID |
| `aggregate_type` | VARCHAR(50) | Entity type (settlement, transaction, etc.) |
| `event_type` | VARCHAR(100) | Event name |
| `topic` | VARCHAR(100) | Target Kafka topic |
| `payload` | JSONB | Event data |
| `published_at` | TIMESTAMPTZ | NULL until published (only mutable field) |

#### `dead_letter_queue`
| Column | Type | Description |
|--------|------|-------------|
| `id` | UUID PK | DLQ entry identifier |
| `source_table` | VARCHAR(50) | Origin table |
| `event_type` | VARCHAR(100) | Failed event type |
| `error_message` | TEXT | Failure reason |
| `retry_count` | INTEGER | Number of attempts |
| `resolved_at` | TIMESTAMPTZ | NULL until resolved |

#### `reconciliation_runs`
| Column | Type | Description |
|--------|------|-------------|
| `id` | UUID PK | Run identifier |
| `run_type` | VARCHAR(30) | BALANCE, JOURNAL_INTEGRITY, STATE_MACHINE, FULL |
| `status` | VARCHAR(20) | RUNNING, PASSED, FAILED, ERROR |
| `total_checked` | INTEGER | Entities verified |
| `mismatches` | INTEGER | Discrepancies found |

### Derived Views

| View | Source | Purpose |
|------|--------|---------|
| `account_balances` | `journal_entries` | Real-time balances computed from ledger replay |
| `settlement_current_status` | `settlement_events` | Latest status per settlement |
| `transaction_current_status` | `transaction_status_history` | Latest status per transaction |
| `mpc_quorum_status` | `mpc_signatures` | Signature count and quorum boolean per settlement |
| `outbox_delivery_status` | `outbox_events` | DELIVERED / PENDING status per outbox event |

---

## State Machines

### Settlement State Machine

```
              ┌─────────┐
              │ PENDING  │
              └────┬─────┘
                   │ Compliance cleared,
                   │ funds verified
                   ▼
              ┌──────────┐
              │ APPROVED │
              └────┬─────┘
                   │ MPC 2-of-3
                   │ quorum reached
                   ▼
              ┌──────────┐
              │  SIGNED  │
              └────┬─────┘
                   │ Transaction submitted
                   │ to network
                   ▼
              ┌─────────────┐
              │ BROADCASTED │
              └────┬────────┘
                   │ Block confirmed
                   │
                   ▼
              ┌───────────┐
              │ CONFIRMED │  ← Terminal
              └───────────┘

     Any state ──→ FAILED  ← Terminal
```

**Valid Transitions (enforced by database trigger):**
| From | To |
|------|----|
| PENDING | APPROVED, FAILED |
| APPROVED | SIGNED, FAILED |
| SIGNED | BROADCASTED, FAILED |
| BROADCASTED | CONFIRMED, FAILED |

### Transaction State Machine

```
              ┌─────────┐
              │ pending  │
              └────┬─────┘
                   │
              ┌────┴────┐
              ▼         ▼
         ┌──────────┐  ┌──────────┐
         │ approved │  │ rejected │ ← Terminal
         └────┬─────┘  └──────────┘
              │
              ▼
         ┌──────────┐
         │  signed  │
         └────┬─────┘
              │
              ▼
         ┌─────────────┐
         │ broadcasted │
         └────┬────────┘
              │
              ▼
         ┌───────────┐
         │ confirmed │ ← Terminal
         └───────────┘

     Any state ──→ failed ← Terminal
```

---

## Real-World Example

**Scenario:** Bank Alpha deposits $10M and settles $5M to Bank Beta through the permissioned network.

### Step 1: Compliance Screening
Bank Alpha and Bank Beta are screened against sanctions, PEP, and AML lists. Both are **CLEARED**. A test entity `SANCTIONED_ENTITY_1` is **BLOCKED**.

### Step 2: Record Deposit (Double-Entry Journal)
Bank Alpha deposits $10,000,000. The ledger creates two balanced journal entries:
- **Debit:** OPERATING (Bank Alpha) — $10,000,000
- **Credit:** INSTITUTION_LIABILITY (Treasury) — $10,000,000

No mutable balance column is updated. The `account_balances` view recalculates in real-time.

### Step 3: Create Settlement (PENDING)
A $5,000,000 settlement is created from Bank Alpha to Bank Beta. The initial state event `PENDING` is written to `settlement_events`. An outbox event is atomically created in the same transaction.

### Step 4: Approve Settlement (APPROVED)
A compliance officer approves the settlement. The state machine validates `PENDING → APPROVED` at both the application and database level. Another immutable event is appended.

### Step 5: MPC Signing (SIGNED)
The signing gateway fans out to 3 MPC nodes. Each node produces a partial signature from its key share. The gateway collects 2 partials (quorum met), persists them to `mpc_signatures`, produces a combined hash, and the settlement transitions to `SIGNED`.

### Step 6: Broadcast (BROADCASTED)
The signed transaction is submitted to the network. The settlement transitions to `BROADCASTED` with the transaction hash recorded.

### Step 7: Confirm (CONFIRMED)
The transaction is confirmed at block 19,500,000. The settlement reaches the terminal state `CONFIRMED`. The full audit trail records every transition with trace IDs, actors, and timestamps.

### Step 8: Verify
- Balances are derived from the ledger — no mutable balance was ever updated
- The reconciliation engine verifies journal pair integrity, replays balances, checks for negatives, and validates state machine consistency
- The audit trail provides a complete chain of custody from deposit to confirmation

---

## Running in a Sandbox Environment

### Prerequisites

- Docker and Docker Compose
- Python 3.x (for the demo script)
- `curl` (for health checks)

### Quick Start

```bash
# Clone the repository
git clone https://github.com/pavondunbar/Permissioned-Defi.git
cd Permissioned-Defi

# Build and start all 12 services
make up

# Verify all services are healthy
make health

# Run the end-to-end demo
make demo
```

### Useful Commands

| Command | Description |
|---------|-------------|
| `make up` | Build and start all services |
| `make down` | Stop containers (keep volumes) |
| `make down-v` | Stop containers and remove volumes (full reset) |
| `make logs` | Follow all service logs |
| `make ps` | Show container status |
| `make health` | Check gateway health (aggregated) |
| `make demo` | Run the end-to-end demo |
| `make shell-pg` | Open psql shell |
| `make topics` | List Kafka topics |

### Inspecting the Database

| Command | Description |
|---------|-------------|
| `make db-journal` | Show recent journal entries |
| `make db-balances` | Show all account balances (derived from ledger) |
| `make db-audit` | Show recent audit events |
| `make db-settlements` | Show settlements with current status |
| `make db-recon` | Show reconciliation run history |
| `make db-outbox` | Show outbox event delivery status |
| `make db-dlq` | Show dead letter queue |
| `make db-rbac` | Show RBAC configuration |
| `make db-immutable-test` | Verify immutability (UPDATE should be blocked) |

---

## Project Structure

```
Permissioned-Defi/
├── docker-compose.yml              # 12 services, 3 networks, trust boundaries
├── Makefile                        # Developer commands (up, down, demo, db-*)
├── db/
│   └── init/
│       └── 001-schema.sql          # Full schema: 18 tables, 5 views, triggers, seed data
├── scripts/
│   └── demo.py                     # 10-step end-to-end demo script
├── services/
│   ├── api-gateway/
│   │   ├── Dockerfile
│   │   └── gateway.py              # RBAC enforcement, routing, idempotency
│   ├── compliance-engine/
│   │   ├── Dockerfile
│   │   └── compliance.py           # KYC/AML/sanctions screening
│   ├── ledger-service/
│   │   ├── Dockerfile
│   │   └── ledger.py               # Double-entry journal, balance derivation
│   ├── settlement-service/
│   │   ├── Dockerfile
│   │   └── settlement.py           # Settlement lifecycle, state transitions
│   ├── signing-gateway/
│   │   ├── Dockerfile
│   │   └── gateway.py              # MPC coordination, quorum verification
│   ├── mpc-node/
│   │   ├── Dockerfile
│   │   └── node.py                 # Partial signature generation (x3 instances)
│   ├── outbox-publisher/
│   │   ├── Dockerfile
│   │   └── publisher.py            # Outbox → Kafka publishing, DLQ
│   ├── event-consumer/
│   │   ├── Dockerfile
│   │   └── consumer.py             # Kafka consumer, deduplication, audit
│   └── reconciliation/
│       ├── Dockerfile
│       └── reconciler.py           # 4-check continuous integrity verification
└── shared/
    ├── __init__.py
    ├── audit.py                    # AuditContext + immutable audit trail
    ├── idempotency.py              # API + consumer idempotency helpers
    ├── outbox.py                   # Transactional outbox pattern
    ├── rbac.py                     # Role-based access control
    └── state_machine.py            # Settlement state machine definitions
```

---

## Production Considerations

This sandbox demonstrates architectural patterns. A production deployment would additionally require:

| Component | What's Missing |
|-----------|---------------|
| TLS / mTLS | All inter-service communication is plaintext HTTP |
| Secret Management | Database credentials are in docker-compose.yml; use Vault/KMS |
| HSM Integration | MPC nodes simulate key material; real deployment uses hardware security modules |
| Real Blockchain | Settlement broadcast/confirm is simulated; integrate actual chain RPC |
| Authentication | API keys are static strings; use OAuth 2.0 / JWT with proper rotation |
| Rate Limiting | No request throttling on the API gateway |
| Horizontal Scaling | Single-instance services; add load balancing and replica sets |
| Monitoring & Alerting | No Prometheus/Grafana/PagerDuty integration |
| Backup & DR | No automated database backups or disaster recovery procedures |
| Compliance Feeds | Sanctions/PEP screening uses a static list; integrate real data providers |
| Penetration Testing | No security audit has been performed |
| Kafka Hardening | Single-broker, no authentication, no encryption; use SASL + TLS in production |

---

## License

This project is licensed under the MIT License.

---

Built with &#9829; by [Pavon Dunbar](https://github.com/pavondunbar)
