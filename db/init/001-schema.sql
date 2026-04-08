-- =====================================================================================
-- Permissioned DeFi Compliance Engine — PostgreSQL Schema
-- Append-only, double-entry, immutable ledger with deterministic state rebuild
-- =====================================================================================

CREATE EXTENSION IF NOT EXISTS "pgcrypto";

-- =====================================================================================
-- 1. IMMUTABILITY ENFORCEMENT
-- =====================================================================================

CREATE OR REPLACE FUNCTION deny_mutation()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION 'IMMUTABLE TABLE: % on % denied', TG_OP, TG_TABLE_NAME;
END;
$$ LANGUAGE plpgsql;

-- =====================================================================================
-- 2. CHART OF ACCOUNTS (double-entry reference data)
-- =====================================================================================

CREATE TABLE chart_of_accounts (
    code            VARCHAR(32) PRIMARY KEY,
    name            VARCHAR(128) NOT NULL,
    account_type    VARCHAR(16) NOT NULL CHECK (account_type IN ('asset', 'liability', 'equity', 'revenue', 'expense')),
    normal_balance  VARCHAR(8) NOT NULL CHECK (normal_balance IN ('debit', 'credit'))
);

CREATE TRIGGER trg_deny_update_coa
    BEFORE UPDATE ON chart_of_accounts FOR EACH ROW EXECUTE FUNCTION deny_mutation();
CREATE TRIGGER trg_deny_delete_coa
    BEFORE DELETE ON chart_of_accounts FOR EACH ROW EXECUTE FUNCTION deny_mutation();

INSERT INTO chart_of_accounts (code, name, account_type, normal_balance) VALUES
    ('OPERATING',           'Operating Account',        'asset',     'debit'),
    ('CUSTODY_ASSET',       'Custody Asset Hold',       'asset',     'debit'),
    ('SETTLEMENT_PENDING',  'Settlement Pending',       'liability', 'credit'),
    ('INSTITUTION_LIABILITY','Institution Liability',   'liability', 'credit'),
    ('FEE_REVENUE',         'Fee Revenue',              'revenue',   'credit'),
    ('COLLATERAL_POSTED',   'Collateral Posted',        'asset',     'debit'),
    ('ESCROW_HOLD',         'Escrow Hold',              'asset',     'debit'),
    ('TREASURY',            'Treasury Account',         'equity',    'credit');


-- =====================================================================================
-- 3. ACCOUNTS (identity only — no mutable balance columns)
-- =====================================================================================

CREATE TABLE accounts (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    account_name    VARCHAR(256) NOT NULL,
    account_type    VARCHAR(30) NOT NULL CHECK (account_type IN (
        'institutional', 'custodian', 'treasury', 'system', 'escrow'
    )),
    entity_id       VARCHAR(256),
    currency        VARCHAR(10) NOT NULL DEFAULT 'USD',
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,
    kyc_verified    BOOLEAN NOT NULL DEFAULT FALSE,
    aml_cleared     BOOLEAN NOT NULL DEFAULT FALSE,
    metadata        JSONB NOT NULL DEFAULT '{}',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_accounts_entity ON accounts (entity_id);

CREATE TRIGGER trg_deny_update_accounts
    BEFORE UPDATE ON accounts FOR EACH ROW EXECUTE FUNCTION deny_mutation();
CREATE TRIGGER trg_deny_delete_accounts
    BEFORE DELETE ON accounts FOR EACH ROW EXECUTE FUNCTION deny_mutation();


-- =====================================================================================
-- 4. JOURNAL ENTRIES (append-only double-entry ledger)
-- =====================================================================================

CREATE TABLE journal_entries (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    journal_id      UUID NOT NULL,
    account_id      UUID NOT NULL REFERENCES accounts(id),
    coa_code        VARCHAR(32) NOT NULL REFERENCES chart_of_accounts(code),
    currency        VARCHAR(10) NOT NULL DEFAULT 'USD',
    debit           NUMERIC(38, 18) NOT NULL DEFAULT 0,
    credit          NUMERIC(38, 18) NOT NULL DEFAULT 0,
    entry_type      VARCHAR(50) NOT NULL,
    reference_id    UUID,
    narrative       TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    request_id      UUID NOT NULL,
    trace_id        UUID NOT NULL,
    actor           VARCHAR(256) NOT NULL,
    actor_role      VARCHAR(50) NOT NULL,

    CONSTRAINT chk_debit_xor_credit CHECK (
        (debit > 0 AND credit = 0) OR (debit = 0 AND credit > 0)
    )
);

CREATE INDEX idx_journal_journal_id ON journal_entries (journal_id);
CREATE INDEX idx_journal_account ON journal_entries (account_id, created_at DESC);
CREATE INDEX idx_journal_reference ON journal_entries (reference_id);

CREATE TRIGGER trg_deny_update_journal
    BEFORE UPDATE ON journal_entries FOR EACH ROW EXECUTE FUNCTION deny_mutation();
CREATE TRIGGER trg_deny_delete_journal
    BEFORE DELETE ON journal_entries FOR EACH ROW EXECUTE FUNCTION deny_mutation();

-- Deferred constraint: every journal_id must balance (SUM debit = SUM credit)
CREATE OR REPLACE FUNCTION check_journal_balance()
RETURNS TRIGGER AS $$
DECLARE
    _total_debit  NUMERIC(38, 18);
    _total_credit NUMERIC(38, 18);
BEGIN
    SELECT COALESCE(SUM(debit), 0), COALESCE(SUM(credit), 0)
    INTO _total_debit, _total_credit
    FROM journal_entries
    WHERE journal_id = NEW.journal_id;

    IF _total_debit <> _total_credit THEN
        RAISE EXCEPTION
            'Unbalanced journal %: debit=% credit=%',
            NEW.journal_id, _total_debit, _total_credit;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE CONSTRAINT TRIGGER trg_check_journal_balance
    AFTER INSERT ON journal_entries
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION check_journal_balance();


-- =====================================================================================
-- 5. TRANSACTIONS (immutable transaction records)
-- =====================================================================================

CREATE TABLE transactions (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id          UUID NOT NULL REFERENCES accounts(id),
    tx_type             VARCHAR(30) NOT NULL CHECK (tx_type IN (
        'deposit', 'withdrawal', 'transfer', 'settlement',
        'collateral_lock', 'collateral_release', 'fee'
    )),
    amount              NUMERIC(38, 18) NOT NULL CHECK (amount > 0),
    currency            VARCHAR(10) NOT NULL DEFAULT 'USD',
    counterparty_id     UUID REFERENCES accounts(id),
    destination_address VARCHAR(256),
    idempotency_key     VARCHAR(256) NOT NULL UNIQUE,
    metadata            JSONB NOT NULL DEFAULT '{}',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    request_id          UUID NOT NULL,
    trace_id            UUID NOT NULL,
    actor               VARCHAR(256) NOT NULL,
    actor_role          VARCHAR(50) NOT NULL
);

CREATE INDEX idx_transactions_account ON transactions (account_id);
CREATE INDEX idx_transactions_idempotency ON transactions (idempotency_key);

CREATE TRIGGER trg_deny_update_transactions
    BEFORE UPDATE ON transactions FOR EACH ROW EXECUTE FUNCTION deny_mutation();
CREATE TRIGGER trg_deny_delete_transactions
    BEFORE DELETE ON transactions FOR EACH ROW EXECUTE FUNCTION deny_mutation();


-- =====================================================================================
-- 6. TRANSACTION STATUS HISTORY (append-only state transitions)
-- =====================================================================================

CREATE TABLE transaction_status_history (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    transaction_id  UUID NOT NULL REFERENCES transactions(id),
    status          VARCHAR(20) NOT NULL CHECK (status IN (
        'pending', 'approved', 'rejected', 'signed',
        'broadcasted', 'confirmed', 'failed'
    )),
    tx_hash         VARCHAR(256),
    block_number    BIGINT,
    reason          TEXT,
    metadata        JSONB,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    request_id      UUID NOT NULL,
    trace_id        UUID NOT NULL,
    actor           VARCHAR(256) NOT NULL,
    actor_role      VARCHAR(50) NOT NULL
);

CREATE INDEX idx_tx_status_transaction ON transaction_status_history (transaction_id, created_at DESC);

CREATE TRIGGER trg_deny_update_tx_status
    BEFORE UPDATE ON transaction_status_history FOR EACH ROW EXECUTE FUNCTION deny_mutation();
CREATE TRIGGER trg_deny_delete_tx_status
    BEFORE DELETE ON transaction_status_history FOR EACH ROW EXECUTE FUNCTION deny_mutation();

-- State machine enforcement at DB level
CREATE OR REPLACE FUNCTION enforce_tx_status_transition()
RETURNS TRIGGER AS $$
DECLARE
    _current VARCHAR(20);
BEGIN
    SELECT status INTO _current
    FROM transaction_status_history
    WHERE transaction_id = NEW.transaction_id
    ORDER BY created_at DESC
    LIMIT 1;

    IF _current IS NULL THEN
        IF NEW.status <> 'pending' THEN
            RAISE EXCEPTION 'First status must be pending, got %', NEW.status;
        END IF;
        RETURN NEW;
    END IF;

    IF _current IN ('confirmed', 'failed', 'rejected') THEN
        RAISE EXCEPTION 'Transaction in terminal state %', _current;
    END IF;

    IF NOT (
        (_current = 'pending' AND NEW.status IN ('approved', 'rejected', 'failed'))
        OR (_current = 'approved' AND NEW.status IN ('signed', 'failed'))
        OR (_current = 'signed' AND NEW.status IN ('broadcasted', 'failed'))
        OR (_current = 'broadcasted' AND NEW.status IN ('confirmed', 'failed'))
    ) THEN
        RAISE EXCEPTION 'Invalid transition: % -> %', _current, NEW.status;
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_enforce_tx_status
    BEFORE INSERT ON transaction_status_history
    FOR EACH ROW EXECUTE FUNCTION enforce_tx_status_transition();


-- =====================================================================================
-- 7. SETTLEMENTS
-- =====================================================================================

CREATE TABLE settlements (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    transaction_id      UUID NOT NULL REFERENCES transactions(id),
    settlement_type     VARCHAR(30) NOT NULL CHECK (settlement_type IN (
        'dvp', 'pvp', 'standard', 'collateral'
    )),
    sender_account_id   UUID NOT NULL REFERENCES accounts(id),
    receiver_account_id UUID NOT NULL REFERENCES accounts(id),
    amount              NUMERIC(38, 18) NOT NULL CHECK (amount > 0),
    currency            VARCHAR(10) NOT NULL DEFAULT 'USD',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    request_id          UUID NOT NULL,
    trace_id            UUID NOT NULL,
    actor               VARCHAR(256) NOT NULL,
    actor_role          VARCHAR(50) NOT NULL
);

CREATE INDEX idx_settlements_transaction ON settlements (transaction_id);

CREATE TRIGGER trg_deny_update_settlements
    BEFORE UPDATE ON settlements FOR EACH ROW EXECUTE FUNCTION deny_mutation();
CREATE TRIGGER trg_deny_delete_settlements
    BEFORE DELETE ON settlements FOR EACH ROW EXECUTE FUNCTION deny_mutation();


-- =====================================================================================
-- 8. SETTLEMENT EVENTS (deterministic state machine)
-- =====================================================================================

CREATE TABLE settlement_events (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    settlement_id   UUID NOT NULL REFERENCES settlements(id),
    status          VARCHAR(20) NOT NULL CHECK (status IN (
        'PENDING', 'APPROVED', 'SIGNED', 'BROADCASTED', 'CONFIRMED', 'FAILED'
    )),
    reason          TEXT,
    tx_hash         VARCHAR(66),
    block_number    BIGINT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    request_id      UUID NOT NULL,
    trace_id        UUID NOT NULL,
    actor           VARCHAR(256) NOT NULL,
    actor_role      VARCHAR(50) NOT NULL
);

CREATE INDEX idx_settlement_events_settlement ON settlement_events (settlement_id, created_at DESC);

CREATE TRIGGER trg_deny_update_settlement_events
    BEFORE UPDATE ON settlement_events FOR EACH ROW EXECUTE FUNCTION deny_mutation();
CREATE TRIGGER trg_deny_delete_settlement_events
    BEFORE DELETE ON settlement_events FOR EACH ROW EXECUTE FUNCTION deny_mutation();

-- Settlement state machine (trigger-enforced)
CREATE OR REPLACE FUNCTION enforce_settlement_status_transition()
RETURNS TRIGGER AS $$
DECLARE
    _current VARCHAR(20);
BEGIN
    SELECT status INTO _current
    FROM settlement_events
    WHERE settlement_id = NEW.settlement_id
    ORDER BY created_at DESC
    LIMIT 1;

    IF _current IS NULL THEN
        IF NEW.status <> 'PENDING' THEN
            RAISE EXCEPTION 'First settlement event must be PENDING, got %', NEW.status;
        END IF;
        RETURN NEW;
    END IF;

    IF _current IN ('CONFIRMED', 'FAILED') THEN
        RAISE EXCEPTION 'Settlement % in terminal state %', NEW.settlement_id, _current;
    END IF;

    IF NOT (
        (_current = 'PENDING' AND NEW.status IN ('APPROVED', 'FAILED'))
        OR (_current = 'APPROVED' AND NEW.status IN ('SIGNED', 'FAILED'))
        OR (_current = 'SIGNED' AND NEW.status IN ('BROADCASTED', 'FAILED'))
        OR (_current = 'BROADCASTED' AND NEW.status IN ('CONFIRMED', 'FAILED'))
    ) THEN
        RAISE EXCEPTION 'Invalid settlement transition: % -> %', _current, NEW.status;
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_enforce_settlement_status
    BEFORE INSERT ON settlement_events
    FOR EACH ROW EXECUTE FUNCTION enforce_settlement_status_transition();


-- =====================================================================================
-- 9. MPC KEY SHARES (signer registry)
-- =====================================================================================

CREATE TABLE mpc_key_shares (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    signer_id       VARCHAR(50) NOT NULL UNIQUE,
    signer_name     VARCHAR(256) NOT NULL,
    public_key      VARCHAR(256) NOT NULL,
    status          VARCHAR(20) NOT NULL DEFAULT 'ACTIVE'
                    CHECK (status IN ('ACTIVE', 'REVOKED')),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TRIGGER trg_deny_update_mpc_keys
    BEFORE UPDATE ON mpc_key_shares FOR EACH ROW EXECUTE FUNCTION deny_mutation();
CREATE TRIGGER trg_deny_delete_mpc_keys
    BEFORE DELETE ON mpc_key_shares FOR EACH ROW EXECUTE FUNCTION deny_mutation();


-- =====================================================================================
-- 10. MPC SIGNATURES (individual signer approvals)
-- =====================================================================================

CREATE TABLE mpc_signatures (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    settlement_id   UUID NOT NULL REFERENCES settlements(id),
    signer_id       VARCHAR(50) NOT NULL REFERENCES mpc_key_shares(signer_id),
    signature_share VARCHAR(256) NOT NULL,
    signed_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    request_id      UUID NOT NULL,
    trace_id        UUID NOT NULL,
    actor           VARCHAR(256) NOT NULL,
    actor_role      VARCHAR(50) NOT NULL,

    UNIQUE (settlement_id, signer_id)
);

CREATE INDEX idx_mpc_sigs_settlement ON mpc_signatures (settlement_id);

CREATE TRIGGER trg_deny_update_mpc_sigs
    BEFORE UPDATE ON mpc_signatures FOR EACH ROW EXECUTE FUNCTION deny_mutation();
CREATE TRIGGER trg_deny_delete_mpc_sigs
    BEFORE DELETE ON mpc_signatures FOR EACH ROW EXECUTE FUNCTION deny_mutation();


-- =====================================================================================
-- 11. IDEMPOTENCY KEYS (API-level dedup)
-- =====================================================================================

CREATE TABLE idempotency_keys (
    key             VARCHAR(256) PRIMARY KEY,
    response_code   INTEGER NOT NULL,
    response_body   JSONB NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TRIGGER trg_deny_update_idempotency
    BEFORE UPDATE ON idempotency_keys FOR EACH ROW EXECUTE FUNCTION deny_mutation();
CREATE TRIGGER trg_deny_delete_idempotency
    BEFORE DELETE ON idempotency_keys FOR EACH ROW EXECUTE FUNCTION deny_mutation();


-- =====================================================================================
-- 12. PROCESSED EVENTS (consumer-side idempotency)
-- =====================================================================================

CREATE TABLE processed_events (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    event_id        UUID NOT NULL,
    consumer_group  VARCHAR(100) NOT NULL,
    processed_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (event_id, consumer_group)
);

CREATE TRIGGER trg_deny_update_processed_events
    BEFORE UPDATE ON processed_events FOR EACH ROW EXECUTE FUNCTION deny_mutation();
CREATE TRIGGER trg_deny_delete_processed_events
    BEFORE DELETE ON processed_events FOR EACH ROW EXECUTE FUNCTION deny_mutation();


-- =====================================================================================
-- 13. OUTBOX EVENTS (transactional outbox for Kafka)
-- =====================================================================================

CREATE TABLE outbox_events (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    aggregate_id    UUID NOT NULL,
    aggregate_type  VARCHAR(50) NOT NULL,
    event_type      VARCHAR(100) NOT NULL,
    topic           VARCHAR(100) NOT NULL,
    payload         JSONB NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    published_at    TIMESTAMPTZ,

    request_id      UUID NOT NULL,
    trace_id        UUID NOT NULL,
    actor           VARCHAR(256) NOT NULL,
    actor_role      VARCHAR(50) NOT NULL
);

CREATE INDEX idx_outbox_unpublished
    ON outbox_events (created_at) WHERE published_at IS NULL;

-- Outbox: only published_at may be updated
CREATE OR REPLACE FUNCTION deny_outbox_mutation()
RETURNS TRIGGER AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'IMMUTABLE TABLE: DELETE on outbox_events denied';
    END IF;
    IF TG_OP = 'UPDATE' THEN
        IF NEW.id             IS DISTINCT FROM OLD.id
        OR NEW.aggregate_id   IS DISTINCT FROM OLD.aggregate_id
        OR NEW.aggregate_type IS DISTINCT FROM OLD.aggregate_type
        OR NEW.event_type     IS DISTINCT FROM OLD.event_type
        OR NEW.topic          IS DISTINCT FROM OLD.topic
        OR NEW.payload        IS DISTINCT FROM OLD.payload
        OR NEW.created_at     IS DISTINCT FROM OLD.created_at
        OR NEW.request_id     IS DISTINCT FROM OLD.request_id
        OR NEW.trace_id       IS DISTINCT FROM OLD.trace_id
        OR NEW.actor          IS DISTINCT FROM OLD.actor
        OR NEW.actor_role     IS DISTINCT FROM OLD.actor_role
        THEN
            RAISE EXCEPTION 'outbox_events: only published_at may be updated';
        END IF;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_deny_outbox_mutation
    BEFORE UPDATE OR DELETE ON outbox_events
    FOR EACH ROW EXECUTE FUNCTION deny_outbox_mutation();


-- =====================================================================================
-- 14. AUDIT EVENTS (append-only audit log)
-- =====================================================================================

CREATE TABLE audit_events (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    request_id      UUID,
    trace_id        UUID NOT NULL,
    actor           VARCHAR(256) NOT NULL,
    actor_role      VARCHAR(50) NOT NULL,
    action          VARCHAR(100) NOT NULL,
    resource_type   VARCHAR(50) NOT NULL,
    resource_id     UUID,
    details         JSONB,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_audit_trace ON audit_events (trace_id);
CREATE INDEX idx_audit_resource ON audit_events (resource_type, resource_id);

CREATE TRIGGER trg_deny_update_audit
    BEFORE UPDATE ON audit_events FOR EACH ROW EXECUTE FUNCTION deny_mutation();
CREATE TRIGGER trg_deny_delete_audit
    BEFORE DELETE ON audit_events FOR EACH ROW EXECUTE FUNCTION deny_mutation();


-- =====================================================================================
-- 15. RECONCILIATION TABLES
-- =====================================================================================

CREATE TABLE reconciliation_runs (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_type        VARCHAR(30) NOT NULL CHECK (run_type IN (
        'BALANCE', 'JOURNAL_INTEGRITY', 'STATE_MACHINE', 'FULL'
    )),
    status          VARCHAR(20) NOT NULL DEFAULT 'RUNNING'
                    CHECK (status IN ('RUNNING', 'PASSED', 'FAILED', 'ERROR')),
    total_checked   INTEGER NOT NULL DEFAULT 0,
    mismatches      INTEGER NOT NULL DEFAULT 0,
    started_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at    TIMESTAMPTZ,

    request_id      UUID NOT NULL,
    trace_id        UUID NOT NULL,
    actor           VARCHAR(256) NOT NULL,
    actor_role      VARCHAR(50) NOT NULL
);

-- Only allow transition from RUNNING to terminal
CREATE OR REPLACE FUNCTION guard_recon_run_update()
RETURNS TRIGGER AS $$
BEGIN
    IF OLD.status <> 'RUNNING' THEN
        RAISE EXCEPTION 'Reconciliation run % already terminal: %', OLD.id, OLD.status;
    END IF;
    IF NEW.status = 'RUNNING' THEN
        RAISE EXCEPTION 'Cannot transition recon run to RUNNING';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_guard_recon_run_update
    BEFORE UPDATE ON reconciliation_runs
    FOR EACH ROW EXECUTE FUNCTION guard_recon_run_update();

CREATE TRIGGER trg_deny_delete_recon_runs
    BEFORE DELETE ON reconciliation_runs FOR EACH ROW EXECUTE FUNCTION deny_mutation();

CREATE TABLE reconciliation_mismatches (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id          UUID NOT NULL REFERENCES reconciliation_runs(id),
    mismatch_type   VARCHAR(50) NOT NULL,
    entity_type     VARCHAR(50) NOT NULL,
    entity_id       UUID,
    expected_value  NUMERIC(38, 18),
    actual_value    NUMERIC(38, 18),
    details         JSONB,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TRIGGER trg_deny_update_recon_mismatches
    BEFORE UPDATE ON reconciliation_mismatches FOR EACH ROW EXECUTE FUNCTION deny_mutation();
CREATE TRIGGER trg_deny_delete_recon_mismatches
    BEFORE DELETE ON reconciliation_mismatches FOR EACH ROW EXECUTE FUNCTION deny_mutation();


-- =====================================================================================
-- 16. DEAD LETTER QUEUE
-- =====================================================================================

CREATE TABLE dead_letter_queue (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source_table    VARCHAR(50) NOT NULL,
    source_id       UUID NOT NULL,
    event_type      VARCHAR(100) NOT NULL,
    payload         JSONB NOT NULL,
    error_message   TEXT,
    retry_count     INTEGER NOT NULL DEFAULT 0,
    max_retries     INTEGER NOT NULL DEFAULT 5,
    last_retry_at   TIMESTAMPTZ,
    resolved_at     TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_dlq_unresolved ON dead_letter_queue (created_at) WHERE resolved_at IS NULL;

-- DLQ: only retry/resolved fields may be updated
CREATE OR REPLACE FUNCTION guard_dlq_update()
RETURNS TRIGGER AS $$
BEGIN
    IF NEW.id           IS DISTINCT FROM OLD.id
    OR NEW.source_table IS DISTINCT FROM OLD.source_table
    OR NEW.source_id    IS DISTINCT FROM OLD.source_id
    OR NEW.event_type   IS DISTINCT FROM OLD.event_type
    OR NEW.payload      IS DISTINCT FROM OLD.payload
    OR NEW.created_at   IS DISTINCT FROM OLD.created_at
    THEN
        RAISE EXCEPTION 'dead_letter_queue: only retry/resolved fields may be updated';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_guard_dlq_update
    BEFORE UPDATE ON dead_letter_queue
    FOR EACH ROW EXECUTE FUNCTION guard_dlq_update();

CREATE TRIGGER trg_deny_delete_dlq
    BEFORE DELETE ON dead_letter_queue FOR EACH ROW EXECUTE FUNCTION deny_mutation();


-- =====================================================================================
-- 17. RBAC TABLES
-- =====================================================================================

CREATE TABLE rbac_roles (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    role_name   VARCHAR(50) NOT NULL UNIQUE,
    description TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TRIGGER trg_deny_update_rbac_roles
    BEFORE UPDATE ON rbac_roles FOR EACH ROW EXECUTE FUNCTION deny_mutation();
CREATE TRIGGER trg_deny_delete_rbac_roles
    BEFORE DELETE ON rbac_roles FOR EACH ROW EXECUTE FUNCTION deny_mutation();

CREATE TABLE rbac_role_permissions (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    role_id     UUID NOT NULL REFERENCES rbac_roles(id),
    permission  VARCHAR(100) NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (role_id, permission)
);

CREATE TRIGGER trg_deny_update_rbac_perms
    BEFORE UPDATE ON rbac_role_permissions FOR EACH ROW EXECUTE FUNCTION deny_mutation();
CREATE TRIGGER trg_deny_delete_rbac_perms
    BEFORE DELETE ON rbac_role_permissions FOR EACH ROW EXECUTE FUNCTION deny_mutation();

CREATE TABLE rbac_actor_roles (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    actor       VARCHAR(256) NOT NULL,
    role_id     UUID NOT NULL REFERENCES rbac_roles(id),
    granted_by  VARCHAR(256),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (actor, role_id)
);

CREATE INDEX idx_rbac_actor ON rbac_actor_roles (actor);

CREATE TRIGGER trg_deny_update_rbac_actor_roles
    BEFORE UPDATE ON rbac_actor_roles FOR EACH ROW EXECUTE FUNCTION deny_mutation();
CREATE TRIGGER trg_deny_delete_rbac_actor_roles
    BEFORE DELETE ON rbac_actor_roles FOR EACH ROW EXECUTE FUNCTION deny_mutation();


-- =====================================================================================
-- 18. COMPLIANCE SCREENINGS
-- =====================================================================================

CREATE TABLE compliance_screenings (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    entity_id           VARCHAR(256) NOT NULL,
    screening_type      VARCHAR(30) NOT NULL CHECK (screening_type IN (
        'KYC', 'AML', 'SANCTIONS', 'PEP', 'FULL'
    )),
    is_cleared          BOOLEAN NOT NULL,
    reason              TEXT,
    screening_reference VARCHAR(256),
    screened_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    request_id          UUID NOT NULL,
    trace_id            UUID NOT NULL,
    actor               VARCHAR(256) NOT NULL,
    actor_role          VARCHAR(50) NOT NULL
);

CREATE INDEX idx_compliance_entity ON compliance_screenings (entity_id);

CREATE TRIGGER trg_deny_update_compliance
    BEFORE UPDATE ON compliance_screenings FOR EACH ROW EXECUTE FUNCTION deny_mutation();
CREATE TRIGGER trg_deny_delete_compliance
    BEFORE DELETE ON compliance_screenings FOR EACH ROW EXECUTE FUNCTION deny_mutation();


-- =====================================================================================
-- 19. DERIVED VIEWS (balances computed from ledger replay)
-- =====================================================================================

-- 19a. Account balances — derived from journal entries
CREATE VIEW account_balances AS
SELECT
    je.account_id,
    a.account_name,
    je.currency,
    je.coa_code,
    CASE
        WHEN coa.normal_balance = 'debit'
            THEN COALESCE(SUM(je.debit), 0) - COALESCE(SUM(je.credit), 0)
        ELSE COALESCE(SUM(je.credit), 0) - COALESCE(SUM(je.debit), 0)
    END AS balance
FROM journal_entries je
JOIN accounts a ON a.id = je.account_id
JOIN chart_of_accounts coa ON coa.code = je.coa_code
GROUP BY je.account_id, a.account_name, je.currency, je.coa_code, coa.normal_balance;

-- 19b. Transaction current status
CREATE VIEW transaction_current_status AS
SELECT DISTINCT ON (transaction_id)
    transaction_id, status, reason, tx_hash, block_number, created_at
FROM transaction_status_history
ORDER BY transaction_id, created_at DESC;

-- 19c. Settlement current status
CREATE VIEW settlement_current_status AS
SELECT DISTINCT ON (settlement_id)
    settlement_id, status, reason, tx_hash, block_number, created_at
FROM settlement_events
ORDER BY settlement_id, created_at DESC;

-- 19d. MPC quorum status per settlement
CREATE VIEW mpc_quorum_status AS
SELECT
    s.id AS settlement_id,
    s.transaction_id,
    COUNT(ms.id) AS signature_count,
    COUNT(ms.id) >= 2 AS quorum_reached
FROM settlements s
LEFT JOIN mpc_signatures ms ON ms.settlement_id = s.id
GROUP BY s.id, s.transaction_id;

-- 19e. Outbox delivery status
CREATE VIEW outbox_delivery_status AS
SELECT
    id, aggregate_id, aggregate_type, event_type, topic,
    created_at, published_at,
    CASE WHEN published_at IS NOT NULL THEN 'DELIVERED' ELSE 'PENDING' END AS delivery_status
FROM outbox_events;


-- =====================================================================================
-- 20. SEED DATA
-- =====================================================================================

-- RBAC Roles
INSERT INTO rbac_roles (id, role_name, description) VALUES
    ('00000000-0000-0000-0000-000000000001', 'ADMIN', 'Full system access'),
    ('00000000-0000-0000-0000-000000000002', 'COMPLIANCE_OFFICER', 'KYC/AML screening, investor approval'),
    ('00000000-0000-0000-0000-000000000003', 'SETTLEMENT_AGENT', 'Process and approve settlements'),
    ('00000000-0000-0000-0000-000000000004', 'SIGNER', 'MPC key share holder for transaction signing'),
    ('00000000-0000-0000-0000-000000000005', 'SYSTEM', 'Internal system operations'),
    ('00000000-0000-0000-0000-000000000006', 'AUDITOR', 'Read-only access, reconciliation'),
    ('00000000-0000-0000-0000-000000000007', 'TRADER', 'Submit transactions');

-- RBAC Permissions
INSERT INTO rbac_role_permissions (role_id, permission) VALUES
    ('00000000-0000-0000-0000-000000000001', '*'),
    ('00000000-0000-0000-0000-000000000002', 'compliance.screen'),
    ('00000000-0000-0000-0000-000000000002', 'account.approve'),
    ('00000000-0000-0000-0000-000000000003', 'settlement.approve'),
    ('00000000-0000-0000-0000-000000000003', 'settlement.process'),
    ('00000000-0000-0000-0000-000000000003', 'transaction.approve'),
    ('00000000-0000-0000-0000-000000000004', 'settlement.sign'),
    ('00000000-0000-0000-0000-000000000005', 'settlement.approve'),
    ('00000000-0000-0000-0000-000000000005', 'settlement.sign'),
    ('00000000-0000-0000-0000-000000000005', 'settlement.broadcast'),
    ('00000000-0000-0000-0000-000000000005', 'settlement.process'),
    ('00000000-0000-0000-0000-000000000005', 'transaction.create'),
    ('00000000-0000-0000-0000-000000000005', 'transaction.approve'),
    ('00000000-0000-0000-0000-000000000005', 'compliance.screen'),
    ('00000000-0000-0000-0000-000000000005', 'account.approve'),
    ('00000000-0000-0000-0000-000000000005', 'reconciliation.run'),
    ('00000000-0000-0000-0000-000000000005', 'outbox.publish'),
    ('00000000-0000-0000-0000-000000000006', 'reconciliation.run'),
    ('00000000-0000-0000-0000-000000000006', 'audit.read'),
    ('00000000-0000-0000-0000-000000000007', 'transaction.create'),
    ('00000000-0000-0000-0000-000000000007', 'transaction.read');

-- Actor-role assignments
INSERT INTO rbac_actor_roles (actor, role_id, granted_by) VALUES
    ('SYSTEM/compliance-engine',   '00000000-0000-0000-0000-000000000005', 'BOOTSTRAP'),
    ('SYSTEM/ledger-service',      '00000000-0000-0000-0000-000000000005', 'BOOTSTRAP'),
    ('SYSTEM/settlement-service',  '00000000-0000-0000-0000-000000000005', 'BOOTSTRAP'),
    ('SYSTEM/outbox-publisher',    '00000000-0000-0000-0000-000000000005', 'BOOTSTRAP'),
    ('SYSTEM/reconciliation',      '00000000-0000-0000-0000-000000000005', 'BOOTSTRAP'),
    ('SYSTEM/event-consumer',      '00000000-0000-0000-0000-000000000005', 'BOOTSTRAP'),
    ('SYSTEM/api-gateway',         '00000000-0000-0000-0000-000000000005', 'BOOTSTRAP'),
    ('SYSTEM/signer-1',            '00000000-0000-0000-0000-000000000004', 'BOOTSTRAP'),
    ('SYSTEM/signer-2',            '00000000-0000-0000-0000-000000000004', 'BOOTSTRAP'),
    ('SYSTEM/signer-3',            '00000000-0000-0000-0000-000000000004', 'BOOTSTRAP');

-- MPC Key Shares (3 signers for 2-of-3 quorum)
INSERT INTO mpc_key_shares (signer_id, signer_name, public_key, status) VALUES
    ('SIGNER_1', 'Primary HSM Signer',     'pk_sim_signer1_a1b2c3d4e5f6', 'ACTIVE'),
    ('SIGNER_2', 'Secondary HSM Signer',   'pk_sim_signer2_f6e5d4c3b2a1', 'ACTIVE'),
    ('SIGNER_3', 'DR Signer',              'pk_sim_signer3_1a2b3c4d5e6f', 'ACTIVE');

-- Demo accounts
INSERT INTO accounts (id, account_name, account_type, entity_id, currency, kyc_verified, aml_cleared) VALUES
    ('10000000-0000-0000-0000-000000000001', 'Treasury',        'treasury',      'TREASURY',    'USD', TRUE, TRUE),
    ('10000000-0000-0000-0000-000000000002', 'Escrow Pool',     'escrow',        'ESCROW',      'USD', TRUE, TRUE),
    ('10000000-0000-0000-0000-000000000003', 'Bank Alpha',      'institutional', 'BANK_ALPHA',  'USD', TRUE, TRUE),
    ('10000000-0000-0000-0000-000000000004', 'Bank Beta',       'institutional', 'BANK_BETA',   'USD', TRUE, TRUE),
    ('10000000-0000-0000-0000-000000000005', 'Custodian Prime', 'custodian',     'CUSTODIAN_1', 'USD', TRUE, TRUE);
