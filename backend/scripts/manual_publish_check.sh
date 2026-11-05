#!/usr/bin/env bash
# Manual verification script for AL-184: publish_document SECURITY DEFINER function
# Targets local test database (rag_db on localhost via rag_postgres container) only.

set -euo pipefail

CONTAINER="${PG_CONTAINER:-rag_postgres}"
DB_NAME="${PG_DB:-rag_db}"
DB_SUPERUSER="${PG_SUPERUSER:-postgres}"

echo "=== Running AL-184 manual publish_document verification against ${CONTAINER}/${DB_NAME} ==="

docker exec -i "${CONTAINER}" psql -v ON_ERROR_STOP=1 -U "${DB_SUPERUSER}" -d "${DB_NAME}" <<'SQL'
BEGIN;

-- 1. Create temporary test user A (as superuser before switching to rag_app)
WITH inserted_user AS (
    INSERT INTO users (id, email, hashed_password, role, is_deleted, created_at)
    VALUES (
        '11111111-1111-1111-1111-111111111111',
        'manual-al184-user-a@example.com',
        'hashed_pw',
        'user',
        false,
        now()
    )
    RETURNING id
)
SELECT id AS created_user_a_id FROM inserted_user;

-- Switch to runtime non-superuser role rag_app and set RLS session context for User A
SET LOCAL ROLE rag_app;
SELECT set_config('app.user_id', '11111111-1111-1111-1111-111111111111', true);
SELECT set_config('app.user_role', 'user', true);

-- 2. Insert a private, indexed document for User A
INSERT INTO documents (
    id, user_id, filename, file_path, mime_type, file_size,
    file_hash, status, error_message, created_at, updated_at, visibility
) VALUES (
    '22222222-2222-2222-2222-222222222222',
    '11111111-1111-1111-1111-111111111111',
    'manual_check.txt',
    '/tmp/private/11111111-1111-1111-1111-111111111111/manual_check.txt',
    'text/plain',
    64,
    'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
    'indexed',
    NULL,
    now(),
    now(),
    'private'
);

\echo '--- Before publish_document: documents rows for User A ---'
SELECT id, user_id, visibility, status, file_path FROM documents
 WHERE id = '22222222-2222-2222-2222-222222222222';

-- 3. Call publish_document(...) as User A
\echo '--- Calling publish_document(...) ---'
SELECT id, user_id, visibility, status, file_path
  FROM publish_document(
      '22222222-2222-2222-2222-222222222222'::uuid,
      '33333333-3333-3333-3333-333333333333'::uuid,
      '/tmp/public/33333333-3333-3333-3333-333333333333_manual_check.txt'
  );

-- 4. Verify private row is gone and public row exists
\echo '--- After publish_document: checking old private ID (expect 0 rows) ---'
SELECT id, visibility FROM documents WHERE id = '22222222-2222-2222-2222-222222222222';

\echo '--- After publish_document: checking new public ID (expect 1 row with visibility=public) ---'
SELECT id, user_id, visibility, status, file_path FROM documents
 WHERE id = '33333333-3333-3333-3333-333333333333';

-- Rollback so test database remains clean after verification
ROLLBACK;
SQL

echo "=== Manual verification completed successfully (transaction rolled back cleanly) ==="

