from fastapi import APIRouter, Depends, HTTPException
from typing import List

router = APIRouter()

@router.get("/", response_model=List[str])
async def get_collections():
    # Aap yahan apni collections ki list return kar sakte hain
    return ["default_collection", "research_papers"]

@router.post("/")
async def create_collection(name: str):
    return {"message": f"Collection '{name}' created successfully!"}