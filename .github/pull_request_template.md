## What & why
<!-- Jira key(s), e.g. AL-138 / FR-008, and a short summary -->

## How it was tested
<!-- Commands run, results. Paste `pytest` summary line. -->

---

## Reviewer checklist — data isolation (FR-006 / FR-079)
Tick every box that applies. A PR touching documents, retrieval, chat or storage is not mergeable with an unticked item.

**Database**
- [ ] Every query on user-owned tables filters by the authenticated user (`... == current_user.id`) — or relies on RLS with `app.user_id` set for the request.
- [ ] Cross-user access returns **404** (not 403, not the object), so existence isn't leaked.
- [ ] No endpoint accepts a `user_id` from the request body/query to decide whose data to read or write.

**Vector store (ChromaDB)**
- [ ] No retrieval call without a user filter: private reads go through `query_for_user()` / `private_owner_filter()` (user_id AND visibility='private').
- [ ] Private chunks are written only to `user_{user_id}_private`; public only to the public collection.
- [ ] No code opens a private collection by a user id other than the caller's.
- [ ] Deletes remove chunks from the correct collection, scoped by owner + document_id.

**Responses & errors**
- [ ] API responses don't expose internal ownership metadata (e.g. chunk `user_id`) or other users' data.
- [ ] Errors don't return raw exception text, SQL, file paths or collection names.

**Files**
- [ ] Files are stored only under the caller's own storage directory; no path traversal (`..`) possible.
- [ ] No uploaded files, `.env`, `uploads/` or `chroma_data/` committed.

**Tests**
- [ ] New data access has a cross-user test (User B can't read/search/modify User A's data).
- [ ] `pytest` passes locally and in CI.

## Git
- [ ] Branched from `develop`, PR targets **`develop`** (not `main`).
