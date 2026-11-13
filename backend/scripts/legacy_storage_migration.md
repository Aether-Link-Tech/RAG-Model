# Legacy Storage Layout Migration (AL-253)

This guide describes how to safely migrate legacy per-user document files into the canonical owner-scoped storage layout.

- **Legacy layout:** `<UPLOAD_DIR>/<user_id>/...`
- **Canonical layout:** `<UPLOAD_DIR>/users/<user_id>/documents/<document_id>[.<ext>]`

The migration tool (`backend/scripts/migrate_legacy_storage.py`, backed by `backend/app/services/legacy_storage_migration.py`) validates ownership against PostgreSQL, rejects symlinks and cross-user paths, never overwrites existing destination files, and preserves original files until the corresponding database update commits.

---

## 1. Pre-Flight Checklist

1. Take a PostgreSQL backup (`pg_dump`) before running `--apply`.
2. Take a filesystem snapshot or backup of `<UPLOAD_DIR>`.
3. Ensure no bulk upload/ingest jobs are actively modifying legacy paths during the migration window.
4. Ensure `DATABASE_URL` points to a role that can read and update `documents` across all users (the migration/owner role, not the RLS-restricted runtime `rag_app` role).

---

## 2. Run Dry-Run (Default)

Dry-run is the default mode and makes **no** filesystem or database changes:

```bash
cd /path/to/RAG-Model
backend/.venv/bin/python -m backend.scripts.migrate_legacy_storage
```

For machine-readable output:

```bash
backend/.venv/bin/python -m backend.scripts.migrate_legacy_storage --json > migration_plan.json
```

Optional overrides:

- `--upload-dir /path/to/uploads` — override `settings.UPLOAD_DIR`
- `--database-url postgresql+asyncpg://...` — override `settings.DATABASE_URL`

---

## 3. Review the Dry-Run Report

Inspect the summary counts and per-document statuses:

| Status                         | Meaning                                                                                 | Action Required                                                 |
| ------------------------------ | --------------------------------------------------------------------------------------- | --------------------------------------------------------------- |
| `already_canonical`            | `documents.file_path` already resolves inside `<UPLOAD_DIR>/users/<user_id>/documents/` | None                                                            |
| `planned`                      | Validated legacy file ready to migrate                                                  | Will be migrated on `--apply`                                   |
| `destination_exists`           | Target canonical file already exists on disk                                            | Inspect both files manually; tool will never overwrite          |
| `duplicate_destination`        | Two DB records map to the same canonical path                                           | Resolve duplicate DB record before migrating                    |
| `missing_source`               | Legacy file does not exist on disk                                                      | Restore from backup or mark document failed                     |
| `symlink_source`               | Source path or legacy directory contains a symlink                                      | Inspect and remove symlink if unauthorized                      |
| `cross_user_path`              | `file_path` points inside another user's directory                                      | Security incident / DB corruption — investigate before touching |
| `outside_owner_dir`            | `file_path` is outside `<UPLOAD_DIR>/<user_id>/`                                        | Investigate path origin manually                                |
| `invalid_uuid` / `unsafe_path` | Malformed UUID, empty path, or NUL byte                                                 | Fix or quarantine invalid DB row                                |

The CLI exits with code `0` if `skipped_or_failed == 0`, or `1` if any document requires manual attention.

---

## 4. Execute the Migration (`--apply`)

Once the dry-run report is reviewed and any conflicts are resolved:

```bash
backend/.venv/bin/python -m backend.scripts.migrate_legacy_storage --apply
```

For each `planned` document, `--apply`:

1. Re-verifies destination containment and checks that no symlink was introduced.
2. Copies the legacy file to an exclusive temporary `.migrating` staging file in `<UPLOAD_DIR>/users/<user_id>/documents/`, fsyncs it, and atomically links it to the canonical destination via `os.link` (which fails with `FileExistsError` rather than overwriting if the destination exists).
3. Updates `documents.file_path` inside a PostgreSQL transaction and commits.
4. Removes the legacy source file **only after** the database commit succeeds.

Unrelated files and directories are never deleted.

---

## 5. Verify Results

1. Re-run the dry-run command and confirm `planned == 0` and all migrated documents show `already_canonical`:
   ```bash
   backend/.venv/bin/python -m backend.scripts.migrate_legacy_storage
   ```
2. Run the project's storage integrity check (`backend/app/services/integrity_check.py`) against the database and `<UPLOAD_DIR>` to confirm zero `file_outside_owner_folder` or `missing_file` issues for migrated documents.
3. Spot-check document download and ingestion endpoints for a migrated document.

---

## 6. Rollback and Recovery Strategy

### Scenario A: Database update fails during `--apply`

- The migration automatically rolls back the PostgreSQL transaction, deletes the newly created canonical destination file, and leaves the original legacy file untouched.
- Status is reported as `db_update_failed`.
- **Recovery:** Resolve the database issue (e.g., connectivity/lock), then safely re-run `--apply`.

### Scenario B: Process crashes after DB commit, before legacy source unlink

- The database already points to the canonical file, which is intact on disk, while an identical copy remains in `<UPLOAD_DIR>/<user_id>/`.
- **Recovery:** Re-running dry-run detects the leftover file as `orphaned_legacy_source` (only when its SHA-256 and byte size match the canonical destination). Running `--apply` verifies the match again and safely removes the leftover legacy file (`recovered_legacy_cleanup`), leaving unrelated files untouched.

### Scenario C: Full rollback after `--apply`

- If an application regression requires reverting to the pre-migration state:
  1. Restore the PostgreSQL backup taken in Step 1 (or restore `documents.file_path` values from the saved `migration_plan.json` / `--apply --json` output).
  2. Restore `<UPLOAD_DIR>` from the pre-migration snapshot taken in Step 1.
