# Manual API check: POST /api/v1/documents/{id}/publish (AL-185)

Ready-to-paste commands. **Not run by the agent.** They create real rows and files in `rag_db`,
so use throw-away users and files.

Facts taken from the repo:

| Item | Value | Source |
|---|---|---|
| API base | `http://localhost:8000/api/v1` | `docker-compose.yml` (`127.0.0.1:8000:8000`), `main.py` (`/api/v1`) |
| Login | `POST /api/v1/auth/login` with `{"email","password"}` returns `access_token` | `auth.py`, `schemas/auth.py` |
| Upload | `POST /api/v1/documents`, multipart `file` and `visibility` | `documents.py` |
| Publish | `POST /api/v1/documents/{id}/publish`, no body, **201** | `documents.py` |
| File layout | `<UPLOAD_DIR>/users/<owner_id>/documents/<doc_id><ext>` | `document_ingest.build_storage_path` |
| Chroma | host port `8001`; public collection `public_knowledge`; private `user_<owner_id_hex>_private` | `docker-compose.yml`, `config.py`, `db/chroma.py` |
| `UPLOAD_DIR` | `uploads` (local run, relative to the repo root) or `/data/uploads` (compose volume) | `config.py`, `docker-compose.yml` |

The accounts must exist and be email-verified. Admin accounts come from your own seeding.

## 0. Setup

```bash
export API=http://localhost:8000/api/v1
export OWNER_EMAIL='owner@example.com';  export OWNER_PW='change-me-123'
export OTHER_EMAIL='other@example.com';  export OTHER_PW='change-me-123'
export ADMIN_EMAIL='admin@example.com';  export ADMIN_PW='change-me-123'

login() {  # login EMAIL PASSWORD -> prints the access token
  curl -s -X POST "$API/auth/login" -H 'Content-Type: application/json' \
    -d "{\"email\":\"$1\",\"password\":\"$2\"}" | python3 -c 'import sys,json; print(json.load(sys.stdin)["access_token"])'
}
export OWNER_TOKEN=$(login "$OWNER_EMAIL" "$OWNER_PW")
export OTHER_TOKEN=$(login "$OTHER_EMAIL" "$OTHER_PW")
export ADMIN_TOKEN=$(login "$ADMIN_EMAIL" "$ADMIN_PW")

# Two unique files so the hash is new each time.
echo "publish check $(date +%s) alpha confidential figures" > /tmp/pub_a.txt
echo "publish check $(date +%s) already public sample"      > /tmp/pub_b.txt
```

Helpers used below (`psql` runs as the superuser, so it bypasses RLS and sees every row):

```bash
sql() { docker exec rag_postgres psql -U postgres -d rag_db -c "$1"; }
chunks() {  # chunks COLLECTION_NAME DOCUMENT_ID -> number of chunks stored for that document
  backend/.venv/bin/python - "$1" "$2" <<'PY'
import sys, chromadb
c = chromadb.HttpClient(host="localhost", port=8001)
try:
    col = c.get_collection(sys.argv[1])
    print(len(col.get(where={"document_id": sys.argv[2]})["ids"]))
except Exception as exc:
    print("collection missing:", exc)
PY
}
```

## 1. Owner publishes own private document: expect 201

```bash
curl -s -X POST "$API/documents" -H "Authorization: Bearer $OWNER_TOKEN" \
  -F "file=@/tmp/pub_a.txt;type=text/plain" -F visibility=private | tee /tmp/pub_a.json
export OLD_ID=$(python3 -c 'import json; print(json.load(open("/tmp/pub_a.json"))["id"])')
export OWNER_ID=$(docker exec rag_postgres psql -U postgres -d rag_db -t -A -c "SELECT user_id FROM documents WHERE id='$OLD_ID'")
export OWNER_HEX=$(echo "$OWNER_ID" | tr -d '-')

# BEFORE: private row, file, private chunks exist
sql "SELECT id, user_id, visibility, status, file_path FROM documents WHERE id='$OLD_ID';"
ls -l uploads/users/$OWNER_ID/documents/            # use docker exec ... ls /data/uploads/... if running in compose
chunks "user_${OWNER_HEX}_private" "$OLD_ID"       # expect > 0

# PUBLISH (empty JSON body)
curl -i -s -X POST "$API/documents/$OLD_ID/publish" \
  -H "Authorization: Bearer $OWNER_TOKEN" -H 'Content-Type: application/json' -d '{}' | tee /tmp/pub_resp.txt
export NEW_ID=$(grep -o '"id":"[^"]*"' /tmp/pub_resp.txt | head -1 | cut -d'"' -f4)
```

Expect `HTTP/1.1 201`; the body has a new `id`, `"visibility":"public"`, `user_id` equal to the owner,
`"is_owner":true`, `"warning":null`.

```bash
# AFTER
sql "SELECT id, user_id, visibility, status, file_path FROM documents WHERE id IN ('$OLD_ID','$NEW_ID');"
#  -> only NEW_ID remains: visibility=public, same user_id, status=indexed
ls -l uploads/users/$OWNER_ID/documents/            # NEW_ID's file present, OLD_ID's file gone
chunks "user_${OWNER_HEX}_private" "$OLD_ID"       # expect 0
chunks public_knowledge "$NEW_ID"                  # expect the same count as before
chunks public_knowledge "$OLD_ID"                  # expect 0
sql "SELECT event_type, reason FROM security_audit_log ORDER BY created_at DESC LIMIT 3;"   # document_published
```

## 2. Another normal user publishes the same document: expect 404

Upload a fresh private document as the owner first (step 1 consumed the earlier one):

```bash
curl -s -X POST "$API/documents" -H "Authorization: Bearer $OWNER_TOKEN" \
  -F "file=@/tmp/pub_b.txt;type=text/plain" -F visibility=private > /tmp/pub_c.json
export PRIV_ID=$(python3 -c 'import json; print(json.load(open("/tmp/pub_c.json"))["id"])')

curl -i -s -X POST "$API/documents/$PRIV_ID/publish" -H "Authorization: Bearer $OTHER_TOKEN"
```

Expect `404` with `{"detail":"Document not found"}` and no `user_id` anywhere in the body.

```bash
sql "SELECT id, visibility, status FROM documents WHERE id='$PRIV_ID';"   # still private, unchanged
ls -l uploads/users/$OWNER_ID/documents/                                   # no new file
ls -l uploads/users/ | head                                                # the other user got no folder/file
chunks "user_${OWNER_HEX}_private" "$PRIV_ID"                              # unchanged (> 0)
```

## 3. Admin publishes another user's private document: expect 404

```bash
curl -i -s -X POST "$API/documents/$PRIV_ID/publish" -H "Authorization: Bearer $ADMIN_TOKEN"
```

Expect `404` `{"detail":"Document not found"}`. Same checks as step 2: the row is still private, no new
file, and the private chunks are unchanged.

```bash
sql "SELECT id, visibility, status FROM documents WHERE id='$PRIV_ID';"
ls -l uploads/users/$OWNER_ID/documents/
chunks "user_${OWNER_HEX}_private" "$PRIV_ID"
sql "SELECT event_type, reason FROM security_audit_log ORDER BY created_at DESC LIMIT 3;"   # access_denied entries
```

## 4. Publish an already-public document: expect 409 (owner) / 404 (anyone else)

Per Jira AL-114/AL-185 the owner gets 409; non-owners get 404 so nothing leaks.

```bash
curl -i -s -X POST "$API/documents/$NEW_ID/publish" -H "Authorization: Bearer $OWNER_TOKEN"   # 409
curl -i -s -X POST "$API/documents/$NEW_ID/publish" -H "Authorization: Bearer $OTHER_TOKEN"   # 404
```

Expect `409` `{"detail":"This document is already published."}` for the owner, and `404` for the other user.

```bash
sql "SELECT id, visibility, status FROM documents WHERE id='$NEW_ID';"   # unchanged: public
ls -l uploads/users/$OWNER_ID/documents/                                  # no extra file
chunks public_knowledge "$NEW_ID"                                         # unchanged
```

## 5. Optional: not indexed gives 409, duplicate public content gives 409

```bash
# Not indexed: force a pending status, publish, then restore it.
sql "UPDATE documents SET status='pending' WHERE id='$PRIV_ID';"
curl -i -s -X POST "$API/documents/$PRIV_ID/publish" -H "Authorization: Bearer $OWNER_TOKEN"
#   -> 409 {"detail":"Document must be indexed before it can be published"}, no file copied
sql "UPDATE documents SET status='indexed' WHERE id='$PRIV_ID';"
```

For a duplicate, upload the same bytes as a private document (the owner) and as a public document
(another user), then publish the private one. Expect `409` with
`"This document is already published."`, the private row, file and chunks unchanged, and no leftover
copied file.

## Clean-up

```bash
curl -s -X DELETE "$API/documents/$NEW_ID"  -H "Authorization: Bearer $OWNER_TOKEN"
curl -s -X DELETE "$API/documents/$PRIV_ID" -H "Authorization: Bearer $OWNER_TOKEN"
rm -f /tmp/pub_a.txt /tmp/pub_b.txt /tmp/pub_*.json /tmp/pub_resp.txt
```
