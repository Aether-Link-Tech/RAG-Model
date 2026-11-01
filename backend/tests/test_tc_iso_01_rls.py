"""TC-ISO-01 (AL-130): PostgreSQL RLS isolates private documents between users.

User A uploads a private document. User B must get nothing back:
  1. through the API (GET / list), and
  2. through direct SQL on the `rag_app` role with `app.user_id` = B,
  3. and an unauthenticated transaction (no `app.user_id`) sees nothing at all.
"""
import io
import os
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import text

from backend.app.core.security import create_access_token
from backend.app.models.user import User
from backend.tests.conftest import test_engine


@pytest_asyncio.fixture(autouse=True)
async def require_postgres():
    try:
        async with test_engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception as exc:  # pragma: no cover - environment dependent
        if os.getenv("CI"):
            pytest.fail(f"PostgreSQL required in CI but unreachable: {exc}")
        pytest.skip(f"PostgreSQL unreachable: {exc}")


def _headers(user: User) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_access_token(user.id, role=user.role)}"}


async def _count_as(user_id: uuid.UUID | None, sql: str, params: dict) -> int:
    """Run `sql` on the rag_app role inside one transaction with app.user_id bound (or not)."""
    async with test_engine.connect() as conn:
        async with conn.begin():
            if user_id is not None:
                await conn.execute(
                    text("SELECT set_config('app.user_id', :uid, true), set_config('app.user_role', 'user', true)"),
                    {"uid": str(user_id)},
                )
            return (await conn.execute(text(sql), params)).scalar_one()


async def test_tc_iso_01_private_document_invisible_to_other_user_via_api_and_sql(
    async_client, test_user: User, test_user_2: User
):
    upload = await async_client.post(
        "/api/v1/documents",
        headers=_headers(test_user),
        files={"file": ("a_private.txt", io.BytesIO(b"tc-iso-01 user A private content"), "text/plain")},
        data={"visibility": "private"},
    )
    assert upload.status_code == 201, upload.text
    doc_id = upload.json()["id"]

    # API: B cannot fetch or list A's private document.
    assert (await async_client.get(f"/api/v1/documents/{doc_id}", headers=_headers(test_user_2))).status_code == 404
    b_list = await async_client.get("/api/v1/documents", headers=_headers(test_user_2))
    assert b_list.status_code == 200
    assert doc_id not in {d["id"] for d in b_list.json()}

    by_id = "SELECT count(*) FROM documents WHERE id = :id"
    by_owner = "SELECT count(*) FROM documents WHERE user_id = :owner AND visibility = 'private'"

    # Direct SQL as B: 0 rows, even when filtering explicitly for A's id / A's ownership.
    assert await _count_as(test_user_2.id, by_id, {"id": doc_id}) == 0
    assert await _count_as(test_user_2.id, by_owner, {"owner": test_user.id}) == 0

    # Unauthenticated transaction: fails closed.
    assert await _count_as(None, by_id, {"id": doc_id}) == 0

    # Control: A does see their own row, so the zeros above come from RLS, not a missing row.
    assert await _count_as(test_user.id, by_id, {"id": doc_id}) == 1
