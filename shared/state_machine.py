"""Deterministic settlement state machine.

Transition graph:
    PENDING -> APPROVED -> SIGNED -> BROADCASTED -> CONFIRMED
    Any non-terminal state can also transition to FAILED.
"""

VALID_TRANSITIONS: dict[str, set[str]] = {
    "PENDING": {"APPROVED", "FAILED"},
    "APPROVED": {"SIGNED", "FAILED"},
    "SIGNED": {"BROADCASTED", "FAILED"},
    "BROADCASTED": {"CONFIRMED", "FAILED"},
}

TERMINAL_STATES = {"CONFIRMED", "FAILED"}

ALL_STATES = {"PENDING", "APPROVED", "SIGNED", "BROADCASTED", "CONFIRMED", "FAILED"}


class InvalidTransitionError(Exception):
    """Raised when a state transition violates the state machine."""

    def __init__(self, current: str, target: str):
        self.current = current
        self.target = target
        super().__init__(
            f"Invalid settlement transition: {current} -> {target}"
        )


def validate_transition(current: str, target: str) -> bool:
    """Validate a settlement state transition.

    Raises InvalidTransitionError if the transition is not allowed.
    Returns True if valid.
    """
    if current in TERMINAL_STATES:
        raise InvalidTransitionError(current, target)

    allowed = VALID_TRANSITIONS.get(current, set())
    if target not in allowed:
        raise InvalidTransitionError(current, target)

    return True


def get_current_status(conn, settlement_id: str) -> str | None:
    """Read the current settlement status from the event log."""
    row = conn.execute(
        """
        SELECT status FROM settlement_events
        WHERE settlement_id = %s
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (settlement_id,),
    ).fetchone()
    return row[0] if row else None
