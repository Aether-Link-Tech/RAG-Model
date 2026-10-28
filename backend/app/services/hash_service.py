import hashlib
from typing import BinaryIO
from fastapi import UploadFile

async def compute_sha256_stream(file: UploadFile, chunk_size: int = 65536) -> str:
    """
    Computes SHA-256 hash by streaming chunks from UploadFile.
    Keeps memory usage low regardless of file size and resets the file pointer to 0.
    """
    hasher = hashlib.sha256()
    while chunk := await file.read(chunk_size):
        hasher.update(chunk)
    # Reset read pointer so subsequent handlers can save/read the file
    await file.seek(0)
    return hasher.hexdigest()

def compute_sha256_bytes(data: bytes) -> str:
    """Computes SHA-256 for in-memory byte buffer."""
    return hashlib.sha256(data).hexdigest()
