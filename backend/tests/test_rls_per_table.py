"""FR-072 / AL-260: direct-SQL isolation tests, one set per user table.

Everything runs as the application role (`rag_app`) with raw SQL, bypassing the API, to
prove PostgreSQL itself enforces isolation:
  * user B sees 0 of user A's rows and cannot update, delete or insert rows as A
  * a connection without `app.user_id` (unauthenticated) sees nothing
  * an admin sees only what the policies allow (role assignments, audit log) and never
    another user's private documents, collections or conversations
"""
import os
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from backend.app.models.user import User
from backend.tests.conftest import _cleanup_engine, test_engine

pytestmark = pytest.mark.isolation


@pytest_asyncio.fixture(autouse=True)
async def require_postgres():
    try:
        async with test_engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception as exc:  # pragma: no cover - environment dependent
        if os.getenv("CI"):
            pytest.fail(f"PostgreSQL required in CI but unreachable: {exc}")
        pytest.skip(f"PostgreSQL unreachable: {exc}")


async def _as(user_id, role, sql, params=None, *, result="scalar"):
    """Run one statement as rag_app with app.user_id / app.user_role bound for the transaction."""
    async with test_engine.connect() as conn:
        async with conn.begin():
            if user_id is not None:
                await conn.execute(
                    text("SELECT set_config('app.user_id', :uid, true), set_config('app.user_role', :role, true)"),
                    {"uid": str(user_id), "role": role},
                )
            res = await conn.execute(text(sql), params or {})
            if result == "rowcount":
                return res.rowcount
            return res.scalar_one()


@pytest_asyncio.fixture
async def seeded(test_user: User):
    """One row of user A's data in every per-user table, written as A through RLS."""
    a = test_user.id
    ids = {"doc": uuid.uuid4(), "collection": uuid.uuid4(), "conversation": uuid.uuid4(),
           "message": uuid.uuid4(), "audit": uuid.uuid4()}
    async with test_engine.connect() as conn:
        async with conn.begin():
            await conn.execute(
                text("SELECT set_config('app.user_id', :uid, true), set_config('app.user_role', 'user', true)"),
                {"uid": str(a)},
            )
            await conn.execute(text(
                "INSERT INTO collections (id, user_id, name) VALUES (:id, :u, 'A private folder')"),
                {"id": ids["collection"], "u": a})
            await conn.execute(text(
                "INSERT INTO documents (id, user_id, collection_id, filename, file_path, mime_type, file_size, "
                "file_hash, status, visibility) VALUES (:id, :u, :c, 'a-secret.txt', '/tmp/a-secret.txt', "
                "'text/plain', 10, :h, 'indexed', 'private')"),
                {"id": ids["doc"], "u": a, "c": ids["collection"], "h": uuid.uuid4().hex * 2})
            await conn.execute(text(
                "INSERT INTO conversations (id, user_id, title) VALUES (:id, :u, 'A secret chat')"),
                {"id": ids["conversation"], "u": a})
            await conn.execute(text(
                "INSERT INTO messages (id, conversation_id, user_id, role, content) "
                "VALUES (:id, :c, :u, 'user', 'A secret question')"),
                {"id": ids["message"], "c": ids["conversation"], "u": a})
            await conn.execute(text(
                "INSERT INTO security_audit_log (id, user_id, event_type, filename, reason) "
                "VALUES (:id, :u, 'upload_rejected', 'a.exe', 'test')"),
                {"id": ids["audit"], "u": a})
    yield ids
    async with _cleanup_engine.begin() as conn:
        await conn.execute(text("DELETE FROM security_audit_log WHERE id = :id"), {"id": ids["audit"]})


# table -> SQL counting A's rows (":a" = A's user id)
COUNT_A = {
    "documents": "SELECT count(*) FROM documents WHERE user_id = :a",
    "collections": "SELECT count(*) FROM collections WHERE user_id = :a",
    "conversations": "SELECT count(*) FROM conversations WHERE user_id = :a",
    "messages": "SELECT count(*) FROM messages WHERE user_id = :a",
    "user_roles": "SELECT count(*) FROM user_roles WHERE user_id = :a",
    "security_audit_log": "SELECT count(*) FROM security_audit_log WHERE user_id = :a",
}
# What each actor may see of A's rows: (owner A, other user B, admin)
EXPECTED_VISIBLE = {
    "documents": (True, False, False),          # private documents: owner only, admins too are blocked
    "collections": (True, False, False),
    "conversations": (True, False, False),
    "messages": (True, False, False),
    "user_roles": (True, False, True),          # admins manage roles
    "security_audit_log": (False, False, True),  # audit log is admin-only, even for its subject
}


@pytest.mark.parametrize("table", list(COUNT_A))
async def test_select_isolation_per_table(table, seeded, test_user, test_user_2, admin_user):
    a, b, admin = test_user.id, test_user_2.id, admin_user.id
    owner_sees, other_sees, admin_sees = EXPECTED_VISIBLE[table]
    sql, params = COUNT_A[table], {"a": a}

    assert (await _as(a, "user", sql, params) > 0) is owner_sees, f"{table}: owner visibility"
    assert await _as(b, "user", sql, params) == 0 or other_sees, f"{table}: user B saw user A's rows"
    assert await _as(None, None, sql, params) == 0, f"{table}: unauthenticated connection saw rows"
    assert (await _as(admin, "admin", sql, params) > 0) is admin_sees, f"{table}: admin visibility"


@pytest.mark.parametrize("table", list(COUNT_A))
async def test_unauthenticated_sees_nothing_at_all(table, seeded):
    assert await _as(None, None, f"SELECT count(*) FROM {table}") == 0


@pytest.mark.parametrize("table,column", [
    ("documents", "filename"), ("collections", "name"), ("conversations", "title"), ("messages", "content"),
])
async def test_other_user_cannot_update_or_delete(table, column, seeded, test_user, test_user_2):
    a, b = test_user.id, test_user_2.id
    changed = await _as(b, "user", f"UPDATE {table} SET {column} = 'pwned' WHERE user_id = :a", {"a": a}, result="rowcount")
    removed = await _as(b, "user", f"DELETE FROM {table} WHERE user_id = :a", {"a": a}, result="rowcount")
    assert (changed, removed) == (0, 0)
    assert await _as(a, "user", COUNT_A[table], {"a": a}) == 1  # A's row is untouched


async def test_admin_cannot_modify_private_rows(seeded, test_user, admin_user):
    a = test_user.id
    for table, column in (("documents", "filename"), ("collections", "name"), ("conversations", "title")):
        assert await _as(admin_user.id, "admin", f"UPDATE {table} SET {column} = 'x' WHERE user_id = :a",
                         {"a": a}, result="rowcount") == 0, table


@pytest.mark.parametrize("sql", [
    "INSERT INTO documents (id, user_id, filename, file_path, mime_type, file_size, file_hash, status, visibility) "
    "VALUES (gen_random_uuid(), :a, 'planted.txt', '/tmp/p', 'text/plain', 1, md5(random()::text) || md5(random()::text), "
    "'indexed', 'private')",
    "INSERT INTO collections (id, user_id, name) VALUES (gen_random_uuid(), :a, 'planted')",
    "INSERT INTO conversations (id, user_id, title) VALUES (gen_random_uuid(), :a, 'planted')",
    "INSERT INTO messages (id, conversation_id, user_id, role, content) VALUES (gen_random_uuid(), :conv, :a, 'user', 'planted')",
], ids=["documents", "collections", "conversations", "messages"])
async def test_other_user_cannot_insert_rows_owned_by_victim(sql, seeded, test_user, test_user_2):
    with pytest.raises(DBAPIError):
        await _as(test_user_2.id, "user", sql, {"a": test_user.id, "conv": seeded["conversation"]}, result="rowcount")


async def test_role_assignments_are_read_only_for_the_app(seeded, test_user, admin_user):
    """user_roles is maintained by a trigger; the app role may only read it."""
    for actor, role in ((test_user.id, "user"), (admin_user.id, "admin")):
        with pytest.raises(DBAPIError, match="permission denied"):
            await _as(actor, role, "DELETE FROM user_roles WHERE user_id = :a", {"a": test_user.id}, result="rowcount")


async def test_forged_identity_setting_does_not_widen_access(seeded, test_user, test_user_2):
    """A malformed or empty app.user_id must never match anyone's rows."""
    for value in ("", "not-a-uuid"):
        with pytest.raises(DBAPIError) if value else _noop():
            seen = await _as_raw_setting(value, "SELECT count(*) FROM documents WHERE user_id = :a", {"a": test_user.id})
            assert seen == 0


class _noop:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


async def _as_raw_setting(value, sql, params):
    async with test_engine.connect() as conn:
        async with conn.begin():
            await conn.execute(text("SELECT set_config('app.user_id', :v, true)"), {"v": value})
            return (await conn.execute(text(sql), params)).scalar_one()
