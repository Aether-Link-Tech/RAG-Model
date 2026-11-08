"""publish_document SECURITY DEFINER function (AL-184)

Revision ID: 26d21e70da5d
Revises: 9b2c3d4e5f6a
Create Date: 2026-11-06 01:10:10.172673

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '26d21e70da5d'
down_revision: Union[str, Sequence[str], None] = "9b2c3d4e5f6a"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
    CREATE OR REPLACE FUNCTION publish_document(
        p_doc_id uuid,
        p_new_id uuid,
        p_new_file_path varchar
    )
    RETURNS documents
    LANGUAGE plpgsql
    SECURITY DEFINER
    SET search_path = public, pg_temp
    AS $$
    DECLARE
        v_uid  text := NULLIF(current_setting('app.user_id', true), '');
        v_row  documents%ROWTYPE;
        v_new  documents%ROWTYPE;
    BEGIN
        IF v_uid IS NULL THEN
            RAISE EXCEPTION 'not authenticated' USING ERRCODE = '42501';
        END IF;

        SELECT * INTO v_row
          FROM documents
         WHERE id = p_doc_id AND visibility = 'private'
         FOR UPDATE;
        IF NOT FOUND THEN
            RAISE EXCEPTION 'document not found' USING ERRCODE = 'P0002';
        END IF;

        IF v_row.user_id::text <> v_uid THEN
            RAISE EXCEPTION 'not permitted' USING ERRCODE = '42501';
        END IF;

        IF v_row.status <> 'indexed' THEN
            RAISE EXCEPTION 'document not indexed' USING ERRCODE = 'P0001';
        END IF;

        INSERT INTO documents (
            id, user_id, filename, file_path, mime_type, file_size,
            file_hash, status, error_message, created_at, updated_at, visibility
        ) VALUES (
            p_new_id, v_row.user_id, v_row.filename, p_new_file_path, v_row.mime_type,
            v_row.file_size, v_row.file_hash, v_row.status, v_row.error_message,
            now(), now(), 'public'
        )
        RETURNING * INTO v_new;

        DELETE FROM documents WHERE id = v_row.id;

        RETURN v_new;
    END;
    $$;
    """)
    op.execute(
        "REVOKE ALL ON FUNCTION publish_document(uuid, uuid, varchar) FROM PUBLIC"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION publish_document(uuid, uuid, varchar) TO rag_app"
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS publish_document(uuid, uuid, varchar)")
