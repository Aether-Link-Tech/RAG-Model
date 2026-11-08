"""User roles for role-based access control (FR-004 / AL-21)."""
from enum import StrEnum


class UserRole(StrEnum):
    """Roles a user account can hold.

    Values match the `check_user_role` constraint on `users.role` and the
    `app.user_role` setting used by PostgreSQL RLS policies.
    """

    ADMIN = "admin"
    USER = "user"


VALID_USER_ROLES: frozenset[str] = frozenset(role.value for role in UserRole)
