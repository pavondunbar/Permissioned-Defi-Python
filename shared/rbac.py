"""Role-Based Access Control with separation of duties."""

import functools
import logging

logger = logging.getLogger("permissioned_defi.rbac")

# Permission cache populated at startup from DB
_ROLE_PERMISSIONS: dict[str, set[str]] = {}
_ACTOR_ROLES: dict[str, set[str]] = {}


def load_rbac_from_db(conn):
    """Load RBAC tables into memory for fast permission checks."""
    global _ROLE_PERMISSIONS, _ACTOR_ROLES
    _ROLE_PERMISSIONS.clear()
    _ACTOR_ROLES.clear()

    rows = conn.execute(
        """
        SELECT r.role_name, rp.permission
        FROM rbac_roles r
        JOIN rbac_role_permissions rp ON rp.role_id = r.id
        """
    ).fetchall()
    for role_name, permission in rows:
        _ROLE_PERMISSIONS.setdefault(role_name, set()).add(permission)

    rows = conn.execute(
        """
        SELECT ar.actor, r.role_name
        FROM rbac_actor_roles ar
        JOIN rbac_roles r ON r.id = ar.role_id
        """
    ).fetchall()
    for actor, role_name in rows:
        _ACTOR_ROLES.setdefault(actor, set()).add(role_name)

    logger.info(
        "RBAC loaded: %d roles, %d actors",
        len(_ROLE_PERMISSIONS),
        len(_ACTOR_ROLES),
    )


def check_permission(actor: str, permission: str) -> bool:
    """Check if an actor has a specific permission."""
    roles = _ACTOR_ROLES.get(actor, set())
    for role in roles:
        perms = _ROLE_PERMISSIONS.get(role, set())
        if "*" in perms or permission in perms:
            return True
    return False


def get_actor_roles(actor: str) -> set[str]:
    """Get all roles for an actor."""
    return _ACTOR_ROLES.get(actor, set())


def require_permission(permission: str):
    """Decorator that enforces RBAC permission checks.

    Expects the decorated function to receive a keyword argument
    `ctx` of type AuditContext, whose `actor` field is checked.
    """

    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            ctx = kwargs.get("ctx") or (
                args[-1] if args else None
            )
            if ctx is None:
                raise PermissionError(
                    f"No audit context for permission check: {permission}"
                )
            actor = ctx.actor
            if not check_permission(actor, permission):
                raise PermissionError(
                    f"Actor {actor} lacks permission: {permission}"
                )
            return func(*args, **kwargs)

        return wrapper

    return decorator
