"""Transaction-local PostgreSQL Row-Level Security (RLS) context management (AL-129)."""
from __future__ import annotations

import uuid
from sqlalchemy import event, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session, SessionTransaction

RLS_DB_ROLE = "rag_app"
VALID_USER_ROLES = {"user", "admin"}


def _normalize_rls_identity(user_id: uuid.UUID | str, user_role: str = "user") -> tuple[str, str]:
    if isinstance(user_id, uuid.UUID):
        normalized_uid = str(user_id)
    elif isinstance(user_id, str):
        normalized_uid = str(uuid.UUID(user_id))
    else:
        raise ValueError("user_id must be a valid UUID.")

    if user_role not in VALID_USER_ROLES:
        raise ValueError(f"Invalid user_role: {user_role!r}")

    return normalized_uid, user_role


def _apply_rls_on_sync_connection(
    connection: Connection,
    user_id: str,
    user_role: str,
) -> None:
    """Apply transaction-local RLS role and `app.user_id` / `app.user_role` settings."""
    connection.execute(text(f"SET LOCAL ROLE {RLS_DB_ROLE}"))
    connection.execute(
        text(
            "SELECT "
            "set_config('app.user_id', :uid, true), "
            "set_config('app.user_role', :role, true)"
        ),
        {"uid": user_id, "role": user_role},
    )


@event.listens_for(Session, "after_begin")
def _on_session_after_begin(
    session: Session,
    transaction: SessionTransaction,
    connection: Connection,
) -> None:
    """Ensure every transaction begun on an authenticated session sets `app.user_id` locally."""
    if not session.info.get("rls_enforced"):
        return

    user_id = session.info.get("rls_user_id", "")
    user_role = session.info.get("rls_user_role", "")
    _apply_rls_on_sync_connection(connection, user_id, user_role)


async def configure_rls_context(
    session: AsyncSession,
    user_id: uuid.UUID | str,
    user_role: str = "user",
) -> None:
    """Bind the authenticated user's identity to the current and subsequent transactions on `session`.

    Uses `set_config(..., is_local=true)` and `SET LOCAL ROLE rag_app` so that:
    1. All queries in the active transaction run under PostgreSQL RLS with `app.user_id` set.
    2. Any subsequent transaction started on this `AsyncSession` (e.g. after `await db.commit()`)
       automatically re-applies the transaction-local settings via the `after_begin` hook.
    3. Settings automatically expire at `COMMIT`/`ROLLBACK`, preventing connection-pool leakage.
    """
    normalized_uid, normalized_role = _normalize_rls_identity(user_id, user_role)
    session.sync_session.info["rls_enforced"] = True
    session.sync_session.info["rls_user_id"] = normalized_uid
    session.sync_session.info["rls_user_role"] = normalized_role

    if session.in_transaction():
        await session.execute(text(f"SET LOCAL ROLE {RLS_DB_ROLE}"))
        await session.execute(
            text(
                "SELECT "
                "set_config('app.user_id', :uid, true), "
                "set_config('app.user_role', :role, true)"
            ),
            {"uid": normalized_uid, "role": normalized_role},
        )


async def enforce_unauthenticated_rls_context(session: AsyncSession) -> None:
    """Activate the non-bypass `rag_app` role with an empty `app.user_id` for the transaction."""
    session.sync_session.info["rls_enforced"] = True
    session.sync_session.info["rls_user_id"] = ""
    session.sync_session.info["rls_user_role"] = ""

    await session.execute(text(f"SET LOCAL ROLE {RLS_DB_ROLE}"))
    await session.execute(
        text(
            "SELECT "
            "set_config('app.user_id', '', true), "
            "set_config('app.user_role', '', true)"
        )
    )


def clear_rls_context(session: AsyncSession) -> None:
    """Clear session-level RLS metadata before closing or returning a session."""
    session.sync_session.info.pop("rls_enforced", None)
    session.sync_session.info.pop("rls_user_id", None)
    session.sync_session.info.pop("rls_user_role", None)
