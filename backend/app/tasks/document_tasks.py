import asyncio
import logging
import uuid

from backend.app.core.celery_app import celery_app
from backend.app.core.config import settings
from backend.app.db.rls import clear_rls_context, configure_rls_context
from backend.app.models.document import Document
from backend.app.services import audit_log
from backend.app.services.document_ingest import document_error_message, index_document
from backend.app.services.document_images import remove_image_files
from backend.app.services.embedding import EmbeddingError
from backend.app.services.near_duplicate import NearDuplicateError
from backend.app.services.storage_paths import resolve_document_file_path
from backend.app.services.vector_store import delete_document_chunks
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.pool import NullPool

logger = logging.getLogger(__name__)

worker_engine = create_async_engine(
    settings.app_database_url,
    echo=settings.DEBUG,
    future=True,
    poolclass=NullPool,
)
WorkerSessionLocal = async_sessionmaker(
    bind=worker_engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autocommit=False,
    autoflush=False,
)


async def _process_document(
    document_id: uuid.UUID,
    user_id: uuid.UUID,
    user_role: str,
) -> None:
    async with WorkerSessionLocal() as db:
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
            except NearDuplicateError as exc:
                logger.exception("Indexing of %s rejected as near-duplicate", document_id)
                owner_id = document.user_id
                document.status = "failed"
                document.error_message = document_error_message(exc)
                await db.commit()

                try:
                    file_path = resolve_document_file_path(
                        owner_id,
                        document.file_path,
                        visibility=document.visibility,
                        must_exist=False,
                    )
                    file_path.unlink()
                except FileNotFoundError:
                    pass
                except Exception:
                    logger.exception(
                        "Could not remove rejected near-duplicate file for document %s",
                        document_id,
                    )
                try:
                    remove_image_files(
                        owner_id,
                        document_id,
                        visibility=document.visibility,
                    )
                except Exception:
                    logger.exception(
                        "Could not remove images for rejected near-duplicate document %s",
                        document_id,
                    )
                try:
                    delete_document_chunks(
                        user_id=owner_id,
                        document_id=document_id,
                        visibility=document.visibility,
                    )
                except Exception:
                    logger.exception(
                        "Could not remove vectors for rejected near-duplicate document %s",
                        document_id,
                    )
                await audit_log.record_security_event(
                    event_type=audit_log.UPLOAD_REJECTED,
                    user_id=owner_id,
                    filename=document.filename,
                    reason="Near-duplicate of an existing document",
                    ip_address=None,
                )
            except (EmbeddingError, OperationalError, TimeoutError, ConnectionError):
                raise
            except Exception as exc:
                logger.exception("Unexpected indexing failure for document %s", document_id)
                await db.rollback()
                document = await db.get(Document, document_id)
                if document is not None:
                    document.status = "failed"
                    document.error_message = document_error_message(exc)
                    await db.commit()
        finally:
            clear_rls_context(db)


async def _process_and_dispose(
    document_id: uuid.UUID,
    user_id: uuid.UUID,
    user_role: str,
) -> None:
    try:
        await _process_document(document_id, user_id, user_role)
    finally:
        await worker_engine.dispose()


@celery_app.task(
    name="backend.app.tasks.document_tasks.process_document",
    acks_late=True,
    reject_on_worker_lost=True,
    autoretry_for=(EmbeddingError, OperationalError, TimeoutError, ConnectionError),
    retry_backoff=True,
    retry_backoff_max=300,
    retry_jitter=True,
    max_retries=3,
)
def process_document(
    document_id: str,
    user_id: str,
    user_role: str = "user",
) -> None:
    asyncio.run(
        _process_and_dispose(
            uuid.UUID(document_id),
            uuid.UUID(user_id),
            user_role,
        )
    )
