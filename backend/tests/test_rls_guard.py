"""FR-072 / AL-259: guard test — no table may hold per-user data without RLS.

Fails CI as soon as a migration adds a table that is not protected by FORCE ROW LEVEL
SECURITY with at least one policy, unless the table is explicitly exempted below with a
reason. Also checks that the application role can never bypass RLS.
"""
import os

import pytest
import pytest_asyncio
from sqlalchemy import text

from backend.app.db.rls_policies import app_role, drop_owner_rls_sql, owner_rls_sql
from backend.tests.conftest import _cleanup_engine

pytestmark = pytest.mark.isolation

# Tables that intentionally have no RLS. Every entry needs a reason a reviewer can check.
EXEMPT = {
    "alembic_version": "migration bookkeeping, no user data, not granted to the app role",
    "users": "looked up before authentication (login, registration, email verification); "
    "access is limited by the API, and email / full name are encrypted at rest (FR-007)",
}

TABLES_SQL = text(
    """
    SELECT c.relname AS name,
           c.relrowsecurity AS rls,
           c.relforcerowsecurity AS forced,
           (SELECT count(*) FROM pg_policies p
             WHERE p.schemaname = 'public' AND p.tablename = c.relname) AS policies,
           EXISTS (SELECT 1 FROM information_schema.columns col
                    WHERE col.table_schema = 'public' AND col.table_name = c.relname
                      AND col.column_name = 'user_id') AS has_user_id
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p')
    ORDER BY c.relname
    """
)


@pytest_asyncio.fixture(autouse=True)
async def require_postgres():
    try:
        async with _cleanup_engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception as exc:  # pragma: no cover - environment dependent
        if os.getenv("CI"):
            pytest.fail(f"PostgreSQL required in CI but unreachable: {exc}")
        pytest.skip(f"PostgreSQL unreachable: {exc}")


async def _tables():
    async with _cleanup_engine.connect() as conn:
        return (await conn.execute(TABLES_SQL)).mappings().all()


async def test_every_table_has_forced_rls_and_policies():
    problems = []
    for t in await _tables():
        if t["name"] in EXEMPT:
            assert not t["has_user_id"], f"{t['name']} has a user_id column and cannot be exempt from RLS"
            continue
        if not t["rls"]:
            problems.append(f"{t['name']}: ROW LEVEL SECURITY is not enabled")
        if not t["forced"]:
            problems.append(f"{t['name']}: ROW LEVEL SECURITY is not FORCED")
        if t["policies"] == 0:
            problems.append(f"{t['name']}: no policies (every query would be denied)")
    assert not problems, (
        "Tables without row-level security (see docs/ARCHITECTURE.md §5.5, use "
        "backend/app/db/rls_policies.owner_rls_sql):\n  " + "\n  ".join(problems)
    )


async def test_every_user_id_table_is_covered():
    """Belt and braces for the Jira rule: any table with a user_id column must be protected."""
    unprotected = [
        t["name"] for t in await _tables()
        if t["has_user_id"] and not (t["rls"] and t["forced"] and t["policies"])
    ]
    assert unprotected == []


async def test_exemptions_still_exist():
    """Remove stale entries so the exemption list never silently grows."""
    names = {t["name"] for t in await _tables()}
    assert set(EXEMPT) <= names


async def test_app_role_cannot_bypass_rls():
    async with _cleanup_engine.connect() as conn:
        row = (
            await conn.execute(
                text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = :r"), {"r": app_role()}
            )
        ).one()
    assert row.rolsuper is False
    assert row.rolbypassrls is False


async def test_guard_detects_a_new_unprotected_table_and_the_helper_fixes_it():
    """Create a throw-away table: unprotected it must be reported, with owner_rls_sql it passes."""
    table = "zz_rls_guard_probe"
    async with _cleanup_engine.begin() as conn:
        await conn.execute(text(f"DROP TABLE IF EXISTS {table}"))
        await conn.execute(text(f"CREATE TABLE {table} (id serial PRIMARY KEY, user_id uuid NOT NULL)"))
    try:
        probe = next(t for t in await _tables() if t["name"] == table)
        assert not probe["rls"] and probe["has_user_id"]

        async with _cleanup_engine.begin() as conn:
            for statement in owner_rls_sql(table):
                await conn.execute(text(statement))
        probe = next(t for t in await _tables() if t["name"] == table)
        assert probe["rls"] and probe["forced"] and probe["policies"] == 1

        async with _cleanup_engine.begin() as conn:
            for statement in drop_owner_rls_sql(table):
                await conn.execute(text(statement))
        probe = next(t for t in await _tables() if t["name"] == table)
        assert not probe["rls"] and probe["policies"] == 0
    finally:
        async with _cleanup_engine.begin() as conn:
            await conn.execute(text(f"DROP TABLE IF EXISTS {table}"))


def test_helper_rejects_sql_injection_in_identifiers():
    with pytest.raises(ValueError):
        owner_rls_sql("documents; DROP TABLE users")
    with pytest.raises(ValueError):
        owner_rls_sql("documents", owner_column="user_id) OR (true")
    with pytest.raises(ValueError):
        owner_rls_sql("documents", privileges="SELECT; DROP TABLE users")
