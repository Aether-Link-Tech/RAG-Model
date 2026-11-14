import asyncio
import logging
import uuid

from backend.app.core.celery_app import celery_app
from backend.app.db.rls import clear_rls_context, configure_rls_context
from backend.app.db.session import AsyncSessionLocal
from backend.app.models.document import Document
from backend.app.services.document_ingest import index_document
from backend.app.services.near_duplicate import NearDuplicateError

logger = logging.getLogger(__name__)


async def _process_document(
    document_id: uuid.UUID,
    user_id: uuid.UUID,
    user_role: str,
) -> None:
    async with AsyncSessionLocal() as db:
        try:
            await configure_rls_context(db, user_id, user_role)
            document = await db.get(Document, document_id)
            if document is None:
                logger.warning(
                    "Indexing skipped: document %s not visible to user %s",
                    document_id,
                    user_id,
                )
                return

            try:
                await index_document(db, document)
            except NearDuplicateError:
                logger.info("Indexing of %s rejected as near-duplicate", document_id)
            except Exception as exc:
                logger.exception("Unexpected indexing failure for document %s", document_id)
                await db.rollback()
                document = await db.get(Document, document_id)
                if document is not None:
                    document.status = "failed"
                    document.error_message = str(exc) or type(exc).__name__
                    await db.commit()
        finally:
            clear_rls_context(db)


@celery_app.task(name="backend.app.tasks.document_tasks.process_document")
def process_document(
    document_id: str,
    user_id: str,
    user_role: str = "user",
) -> None:
    asyncio.run(
        _process_document(
            uuid.UUID(document_id),
            uuid.UUID(user_id),
            user_role,
        )
    )
