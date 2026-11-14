from celery import Celery

from backend.app.core.config import settings

celery_app = Celery(
    "rag_model",
    broker=settings.CELERY_BROKER_URL,
    backend=settings.CELERY_RESULT_BACKEND,
    include=["backend.app.tasks.document_tasks"],
)
celery_app.conf.task_always_eager = settings.CELERY_TASK_ALWAYS_EAGER
