from typing import List

from fastapi import APIRouter

from backend.app.api.deps import AdminUser, CurrentUser

router = APIRouter()


@router.get("/", response_model=List[str])
async def get_collections(_user: CurrentUser):
    # Aap yahan apni collections ki list return kar sakte hain
    return ["default_collection", "research_papers"]


@router.post("/")
async def create_collection(name: str, _admin: AdminUser):
    return {"message": f"Collection '{name}' created successfully!"}
