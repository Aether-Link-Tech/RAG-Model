from fastapi import APIRouter

from backend.app.api.v1 import admin, auth, collections, documents, retrieval, users

api_router = APIRouter()
api_router.include_router(auth.router)
api_router.include_router(users.router)
api_router.include_router(admin.router)
api_router.include_router(documents.router)
api_router.include_router(retrieval.router)
api_router.include_router(collections.router, prefix="/collections", tags=["collections"])