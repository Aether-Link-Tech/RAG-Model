"""Public landing page: guest Q&A over showcase documents, ask-only access.

- Guests (no token) can ask only about documents an admin marked `showcase` (public + indexed).
- Other public documents and every private document stay invisible to guests (PostgreSQL RLS).
- Guests get short excerpts with a highlighted sentence, never whole passages, files or owner ids.
- Only admins can change the showcase flag (API and database trigger).
- Per-IP rate limit; question length limit; feature switch.
"""
from __future__ import annotations

import json
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from backend.app.core.config import settings
from backend.app.services import source_highlight
from backend.tests.conftest import test_engine
from backend.tests.test_chat_qa import (  # noqa: F401  (fixtures)
    _auth,
    _upload,
    fake_claim_embeddings,
    qa_env,
    require_postgres,
)

pytestmark = pytest.mark.isolation

SHOWCASE = (b"Library opening hours. The university library opens at 8 am and closes at 10 pm on weekdays. "
            b"On weekends the library opens at 10 am. Students must show their ID card at the entrance.")
PUBLIC_OTHER = b"Parking permits cost 40 euros per semester and are sold at the security office near gate two."
PRIVATE = b"Confidential salary review: the falcon team receives a nine percent raise next March."


async def _doc_id(client, user, name: str) -> str:
    r = await client.get("/api/v1/documents", headers=_auth(user))
    return next(d["id"] for d in r.json() if d["filename"] == name)


async def _showcase(client, admin, doc_id: str, on: bool = True):
    return await client.patch(f"/api/v1/admin/documents/{doc_id}/showcase", headers=_auth(admin),
                              json={"showcase": on})


async def _guest_ask(client, question: str):
    return await client.post("/api/v1/public/ask", json={"question": question})


@pytest_asyncio.fixture
async def library(async_client, test_user, admin_user, qa_env):
    await _upload(async_client, test_user, SHOWCASE, "library.txt", visibility="public")
    doc_id = await _doc_id(async_client, test_user, "library.txt")
    r = await _showcase(async_client, admin_user, doc_id)
    assert r.status_code == 200 and r.json()["showcase"] is True
    return doc_id


# ---------------------------------------------------------------- guest answers
async def test_guest_answers_from_showcase_with_highlighted_excerpt(async_client, library, qa_env, monkeypatch):
    monkeypatch.setattr(settings, "PUBLIC_SOURCE_MAX_CHARS", 120)
    r = await _guest_ask(async_client, "when does the library open on weekends")
    assert r.status_code == 200, r.text
    body = r.json()
    assert not body["insufficient"] and body["sources"]
    source = body["sources"][0]
    assert source["document_name"] == "library.txt"
    assert len(source["text"]) <= 122  # excerpt (+ ellipses), never the whole passage
    assert source["highlight"] and source["highlight"][:60] in source["text"]
    dumped = json.dumps(body)
    assert "user_id" not in dumped and "file_path" not in dumped
    assert all(c["chunk_id"] is None for c in body["claims"])


async def test_guest_sees_nothing_without_showcase_documents(async_client, test_user, qa_env):
    await _upload(async_client, test_user, PUBLIC_OTHER, "parking.txt", visibility="public")
    r = await _guest_ask(async_client, "how much does a parking permit cost")
    assert r.status_code == 200
    assert r.json()["insufficient"] is True and r.json()["sources"] == []
    assert qa_env == []  # the model is not even called


async def test_guest_never_reaches_non_showcase_or_private_text(async_client, test_user, test_user_2, library, qa_env):
    await _upload(async_client, test_user, PUBLIC_OTHER, "parking.txt", visibility="public")
    await _upload(async_client, test_user_2, PRIVATE, "salary.txt")
    for question in ("parking permit cost euros semester", "falcon team salary raise March"):
        r = await _guest_ask(async_client, question)
        dumped = json.dumps(r.json()).lower()
        assert "parking.txt" not in dumped and "salary.txt" not in dumped
        assert "falcon" not in dumped and "40 euros" not in dumped
    for messages in qa_env:  # nor the passages in the prompt
        context = messages[1]["content"].split("</context>")[0].lower()
        assert "falcon" not in context and "parking" not in context


async def test_removing_showcase_hides_document_from_guests(async_client, admin_user, library, qa_env):
    assert (await _showcase(async_client, admin_user, library, False)).json()["showcase"] is False
    r = await _guest_ask(async_client, "when does the library open on weekends")
    assert r.json()["insufficient"] is True


async def test_showcase_list_names_only_showcase_documents(async_client, test_user, library, qa_env):
    await _upload(async_client, test_user, PUBLIC_OTHER, "parking.txt", visibility="public")
    r = await async_client.get("/api/v1/public/documents")
    assert r.status_code == 200
    names = [d["name"] for d in r.json()]
    assert "library.txt" in names and "parking.txt" not in names  # other rows may exist in a dev DB


# ---------------------------------------------------------------- who can set showcase
async def test_regular_user_cannot_set_showcase(async_client, test_user, qa_env):
    await _upload(async_client, test_user, PUBLIC_OTHER, "parking.txt", visibility="public")
    doc_id = await _doc_id(async_client, test_user, "parking.txt")
    assert (await _showcase(async_client, test_user, doc_id)).status_code == 403
    assert (await async_client.patch(f"/api/v1/admin/documents/{doc_id}/showcase",
                                     json={"showcase": True})).status_code == 401


async def test_admin_cannot_showcase_a_private_document(async_client, test_user, admin_user, qa_env):
    await _upload(async_client, test_user, PRIVATE, "salary.txt")
    doc_id = await _doc_id(async_client, test_user, "salary.txt")
    assert (await _showcase(async_client, admin_user, doc_id)).status_code == 404
    assert (await _showcase(async_client, admin_user, str(uuid.uuid4()))).status_code == 404


async def _as(identity: tuple[str, str], sql: str, params=None, result="scalar"):
    async with test_engine.connect() as conn:
        async with conn.begin():
            await conn.execute(text("SET LOCAL ROLE rag_app"))
            await conn.execute(text("SELECT set_config('app.user_id', :u, true), set_config('app.user_role', :r, true)"),
                               {"u": identity[0], "r": identity[1]})
            res = await conn.execute(text(sql), params or {})
            return res.rowcount if result == "rowcount" else res.scalar_one()


async def test_database_blocks_owner_from_setting_showcase(async_client, test_user, qa_env):
    await _upload(async_client, test_user, PUBLIC_OTHER, "parking.txt", visibility="public")
    doc_id = await _doc_id(async_client, test_user, "parking.txt")
    with pytest.raises(DBAPIError):
        await _as((str(test_user.id), "user"), "UPDATE documents SET showcase = true WHERE id = :d",
                  {"d": doc_id}, result="rowcount")


async def test_database_guest_context_sees_only_showcase_and_cannot_write(async_client, test_user, library, qa_env):
    await _upload(async_client, test_user, PUBLIC_OTHER, "parking.txt", visibility="public")
    await _upload(async_client, test_user, PRIVATE, "salary.txt")
    guest = ("", "guest")
    visible = await _as(guest, "SELECT string_agg(filename, ',') FROM documents WHERE user_id = :u",
                        {"u": test_user.id})
    assert visible == "library.txt"
    assert await _as(guest, "UPDATE documents SET filename = 'x' WHERE id = :d", {"d": library},
                     result="rowcount") == 0
    assert await _as(guest, "DELETE FROM documents WHERE id = :d", {"d": library}, result="rowcount") == 0
    # Anonymous without the guest role still sees nothing (existing rule unchanged).
    assert await _as(("", ""), "SELECT count(*) FROM documents WHERE id = :d", {"d": library}) == 0


# ---------------------------------------------------------------- limits
async def test_burst_limit_per_minute(async_client, library, qa_env, monkeypatch):
    monkeypatch.setattr(settings, "PUBLIC_ASK_PER_MINUTE", 2)
    codes = [(await _guest_ask(async_client, "library weekend opening")).status_code for _ in range(3)]
    assert codes == [200, 200, 429]
    r = await _guest_ask(async_client, "library weekend opening")
    assert int(r.headers["retry-after"]) >= 1 and "sign in" in r.json()["detail"]


async def test_free_questions_per_day_then_sign_in(async_client, library, qa_env, monkeypatch):
    monkeypatch.setattr(settings, "PUBLIC_ASK_PER_MINUTE", 100)
    monkeypatch.setattr(settings, "PUBLIC_ASK_PER_DAY", 2)
    assert (await async_client.get("/api/v1/public/limits")).json() ==         {"free_questions_left": 2, "free_questions_limit": 2}
    first = await _guest_ask(async_client, "library weekend opening")
    second = await _guest_ask(async_client, "library weekend opening")
    assert [first.json()["free_questions_left"], second.json()["free_questions_left"]] == [1, 0]
    third = await _guest_ask(async_client, "library weekend opening")
    assert third.status_code == 429
    assert "2 free questions" in third.json()["detail"] and "Sign in" in third.json()["detail"]
    # Refused questions are not counted, and checking the limit never uses one.
    assert (await async_client.get("/api/v1/public/limits")).json()["free_questions_left"] == 0


async def test_failed_question_is_not_counted(async_client, library, qa_env, monkeypatch):
    from backend.app.services import llm

    monkeypatch.setattr(settings, "PUBLIC_ASK_PER_DAY", 3)

    async def down(messages, retries=2):
        raise llm.LLMError(llm.UNAVAILABLE)

    monkeypatch.setattr(llm, "complete", down)
    r = await _guest_ask(async_client, "library weekend opening")
    assert r.status_code == 503
    assert (await async_client.get("/api/v1/public/limits")).json()["free_questions_left"] == 3


async def test_long_question_is_rejected_before_any_work(async_client, qa_env, monkeypatch):
    monkeypatch.setattr(settings, "PUBLIC_QUESTION_MAX_CHARS", 20)
    r = await _guest_ask(async_client, "x" * 21)
    assert r.status_code == 422 and qa_env == []


async def test_feature_switch_off_hides_endpoints(async_client, qa_env, monkeypatch):
    monkeypatch.setattr(settings, "PUBLIC_ASK_ENABLED", False)
    assert (await _guest_ask(async_client, "anything")).status_code == 404
    assert (await async_client.get("/api/v1/public/documents")).status_code == 404
    assert (await async_client.get("/api/v1/public/limits")).status_code == 404


# ---------------------------------------------------------------- highlight / excerpt units
def test_best_sentence_prefers_the_supporting_sentence():
    passage = "The office opens at 9 am. Weekend hours: the library opens at 10 am. Parking is free."
    assert source_highlight.best_sentence(passage, ["The library opens at 10 am on weekends."]) == \
        "Weekend hours: the library opens at 10 am."
    assert source_highlight.best_sentence(passage, ["zebra"]) is None


def test_excerpt_is_capped_and_centred_on_the_highlight():
    text_ = " ".join(f"Filler sentence number {i} here." for i in range(40)) + " Key rule: report within 24 hours. " + \
        " ".join(f"More filler {i} text." for i in range(40))
    out = source_highlight.excerpt(text_, "Key rule: report within 24 hours.", 120)
    assert "Key rule: report within 24 hours." in out
    assert len(out) <= 122 and out.startswith("…") and out.endswith("…")
    assert source_highlight.excerpt("Short text.", None, 120) == "Short text."
