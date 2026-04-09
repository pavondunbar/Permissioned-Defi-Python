"""Core unit tests for the Permissioned DeFi Compliance Engine.

Covers critical invariants without requiring Docker or PostgreSQL:
  - Settlement state machine (valid/invalid transitions, terminal states)
  - AuditContext immutability and serialization
  - RBAC permission enforcement (grant, deny, wildcard)
  - Idempotency key deduplication (API and consumer layers)
"""

import json
import uuid
from dataclasses import FrozenInstanceError
from unittest.mock import MagicMock

import pytest

from shared.state_machine import (
    ALL_STATES,
    TERMINAL_STATES,
    VALID_TRANSITIONS,
    InvalidTransitionError,
    validate_transition,
)
from shared.audit import AuditContext
from shared.rbac import (
    _ACTOR_ROLES,
    _ROLE_PERMISSIONS,
    check_permission,
    get_actor_roles,
)
from shared.idempotency import (
    check_event_processed,
    check_idempotency_key,
    mark_event_processed,
    store_idempotency_key,
)


# ---------------------------------------------------------------------------
# 1. Settlement state machine — valid transitions (happy path)
# ---------------------------------------------------------------------------

class TestStateMachineHappyPath:

    def test_full_settlement_lifecycle(self):
        """PENDING -> APPROVED -> SIGNED -> BROADCASTED -> CONFIRMED."""
        path = ["PENDING", "APPROVED", "SIGNED", "BROADCASTED", "CONFIRMED"]
        for current, target in zip(path, path[1:]):
            assert validate_transition(current, target) is True

    def test_fail_from_every_non_terminal_state(self):
        """Any non-terminal state can transition to FAILED."""
        non_terminal = ALL_STATES - TERMINAL_STATES
        for state in non_terminal:
            assert validate_transition(state, "FAILED") is True


# ---------------------------------------------------------------------------
# 2. Settlement state machine — invalid transitions
# ---------------------------------------------------------------------------

class TestStateMachineInvalid:

    def test_skip_state_raises(self):
        """Cannot skip from PENDING directly to SIGNED."""
        with pytest.raises(InvalidTransitionError) as exc:
            validate_transition("PENDING", "SIGNED")
        assert "PENDING" in str(exc.value)
        assert "SIGNED" in str(exc.value)

    def test_backward_transition_raises(self):
        """Cannot go backwards from APPROVED to PENDING."""
        with pytest.raises(InvalidTransitionError):
            validate_transition("APPROVED", "PENDING")

    def test_terminal_confirmed_rejects_all(self):
        """CONFIRMED is terminal — no further transitions allowed."""
        for target in ALL_STATES:
            with pytest.raises(InvalidTransitionError):
                validate_transition("CONFIRMED", target)

    def test_terminal_failed_rejects_all(self):
        """FAILED is terminal — no further transitions allowed."""
        for target in ALL_STATES:
            with pytest.raises(InvalidTransitionError):
                validate_transition("FAILED", target)

    def test_error_attributes(self):
        """InvalidTransitionError exposes current and target states."""
        with pytest.raises(InvalidTransitionError) as exc:
            validate_transition("CONFIRMED", "APPROVED")
        assert exc.value.current == "CONFIRMED"
        assert exc.value.target == "APPROVED"


# ---------------------------------------------------------------------------
# 3. AuditContext immutability and serialization
# ---------------------------------------------------------------------------

class TestAuditContext:

    def test_frozen_rejects_mutation(self):
        """AuditContext is a frozen dataclass — assignment raises."""
        ctx = AuditContext()
        with pytest.raises(FrozenInstanceError):
            ctx.actor = "ATTACKER"

    def test_as_dict_keys(self):
        ctx = AuditContext(actor="SYSTEM/test", actor_role="SYSTEM")
        d = ctx.as_dict()
        assert set(d.keys()) == {
            "request_id", "trace_id", "actor", "actor_role",
        }
        assert d["actor"] == "SYSTEM/test"

    def test_as_sql_args_length(self):
        ctx = AuditContext()
        args = ctx.as_sql_args()
        assert len(args) == 4
        assert args[2] == ctx.actor


# ---------------------------------------------------------------------------
# 4. RBAC permission checks
# ---------------------------------------------------------------------------

class TestRBAC:

    @pytest.fixture(autouse=True)
    def _seed_rbac(self):
        """Populate the in-memory RBAC caches for each test."""
        _ROLE_PERMISSIONS.clear()
        _ACTOR_ROLES.clear()

        _ROLE_PERMISSIONS["ADMIN"] = {"*"}
        _ROLE_PERMISSIONS["TRADER"] = {
            "transaction.create", "transaction.read",
        }
        _ROLE_PERMISSIONS["AUDITOR"] = {
            "reconciliation.run", "audit.read",
        }

        _ACTOR_ROLES["alice"] = {"ADMIN"}
        _ACTOR_ROLES["bob"] = {"TRADER"}
        _ACTOR_ROLES["carol"] = {"AUDITOR"}

        yield

        _ROLE_PERMISSIONS.clear()
        _ACTOR_ROLES.clear()

    def test_wildcard_grants_any_permission(self):
        assert check_permission("alice", "settlement.approve") is True
        assert check_permission("alice", "anything.at.all") is True

    def test_explicit_grant(self):
        assert check_permission("bob", "transaction.create") is True

    def test_explicit_deny(self):
        assert check_permission("bob", "settlement.approve") is False

    def test_unknown_actor_denied(self):
        assert check_permission("unknown", "transaction.create") is False

    def test_get_actor_roles(self):
        assert get_actor_roles("carol") == {"AUDITOR"}
        assert get_actor_roles("nonexistent") == set()


# ---------------------------------------------------------------------------
# 5. Idempotency — API-layer and consumer-layer deduplication
# ---------------------------------------------------------------------------

class TestIdempotency:

    @staticmethod
    def _mock_conn(rows=None):
        """Build a mock DB connection returning preset rows."""
        conn = MagicMock()
        cursor = MagicMock()
        cursor.fetchone.return_value = rows
        conn.execute.return_value = cursor
        return conn

    def test_idempotency_key_miss_returns_none(self):
        conn = self._mock_conn(rows=None)
        assert check_idempotency_key(conn, "new-key") is None

    def test_idempotency_key_hit_returns_cached(self):
        cached_body = '{"id": "abc"}'
        conn = self._mock_conn(rows=(201, cached_body))
        result = check_idempotency_key(conn, "used-key")
        assert result == {"code": 201, "body": cached_body}

    def test_store_idempotency_key_executes_insert(self):
        conn = self._mock_conn()
        store_idempotency_key(conn, "key-1", 200, {"ok": True})
        conn.execute.assert_called_once()
        sql = conn.execute.call_args[0][0]
        assert "INSERT INTO idempotency_keys" in sql
        assert "ON CONFLICT" in sql

    def test_event_not_processed(self):
        conn = self._mock_conn(rows=None)
        assert check_event_processed(conn, str(uuid.uuid4()), "grp") is False

    def test_event_already_processed(self):
        conn = self._mock_conn(rows=(1,))
        assert check_event_processed(conn, str(uuid.uuid4()), "grp") is True

    def test_mark_event_processed_is_idempotent(self):
        conn = self._mock_conn()
        eid = str(uuid.uuid4())
        mark_event_processed(conn, eid, "grp")
        sql = conn.execute.call_args[0][0]
        assert "ON CONFLICT" in sql
