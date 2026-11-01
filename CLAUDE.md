# CLAUDE.md

Secure AI-powered document intelligence + context-aware RAG system (team Aether-Link, Jira project `AL`).
FastAPI backend, PostgreSQL 17, ChromaDB, Redis/Celery, Ollama (local LLM + embeddings), React/Vite frontend.

## Layout
- `backend/app/api/v1/` — routes: `documents.py` (upload/list/get/delete), `retrieval.py`, `users.py`
- `backend/app/api/deps.py` — `CurrentUser` / `DbSession` dependencies
- `backend/app/db/chroma.py` — Chroma client + public / per-user private collections
- `backend/app/services/` — `vector_store.py` (owner-scoped search), `document_ingest.py`, `chunking.py`, `embedding.py` (Ollama), `hash_service.py`
- `backend/app/models/` — SQLAlchemy models; migrations in `backend/alembic/versions/`
- `backend/tests/` — pytest suite; shared fixtures in `conftest.py`
- `frontend/` — Vite + React + TS (`npm run dev` → port 3000)

## Commands
Python env: conda env **`rag-model`** (Python 3.11). Don't create ad-hoc venvs; add new deps to `backend/requirements.txt`.

```bash
docker compose up -d                                   # postgres:5432, redis:6379, chroma:8001
conda run -n rag-model alembic -c backend/alembic.ini upgrade head
conda run -n rag-model uvicorn backend.app.main:app --reload --port 8000
cd backend && conda run -n rag-model python -m pytest tests -q
```

Tests need PostgreSQL at `DATABASE_URL` (default `postgres:postgres@localhost:5432/rag_db`, migrated to head).
Chroma and Ollama are faked in tests via the `chroma_client` (in-memory) and `fake_embeddings` fixtures.
DB-backed isolation tests skip locally if Postgres is down, but fail in CI (`CI=true`).
CI: `.github/workflows/backend-tests.yml` runs the suite on PRs/pushes to `develop` and `main`.

## Git workflow
- Branch from **`develop`**; PR into **`develop`**, never directly into `main`.
- The lead (Muhammad Ayyan) reviews and tests, then merges `develop` → `main`.
- Branch naming for the team: `feature/fr-XXX-short-name`. The lead works on `muhammad-ayyan`.
- Commit style: Conventional Commits, e.g. `feat(privacy): ...`, `fix(documents): ...`, `chore(tests): ...`.
- Never commit uploaded user files, `.env`, `uploads/`, or `chroma_data/`.

## Security / isolation rules (do not break)
- Every document query is scoped to the current user (`Document.user_id == current_user.id`); cross-user access returns **404**, not 403, so existence isn't leaked.
- Private vectors live only in `user_{user_id}_private` (template `CHROMA_PRIVATE_COLLECTION_TEMPLATE`); public in `CHROMA_PUBLIC_COLLECTION`.
- Every private Chroma read uses `private_owner_filter()` (user_id AND visibility='private'). Never open another user's private collection.
- API responses must not expose internal ownership metadata (`user_id` in chunk metadata) or raw exception text.
- Duplicate detection is SHA-256, scoped: per-user for private, global for public (partial unique indexes).
- Auth is currently the dev-only `X-User-Id` header; JWT + PostgreSQL RLS is in progress on branch `al-127-129-rls-jwt-auth`.

## Jira
REST API with user env vars `JIRA_EMAIL` / `JIRA_TOKEN`; site `https://final-team-ho4yb920.atlassian.net`, project `AL`.
Hierarchy: Epic → Story (FR-XXX) → Subtask. Show proposed Jira changes before applying them.
