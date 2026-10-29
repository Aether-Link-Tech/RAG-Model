import hashlib
from typing import Optional
from fastapi import UploadFile


class FileSizeLimitExceededError(ValueError):
    """Raised during streaming when an upload exceeds the configured byte limit."""

    def __init__(self, max_bytes: int) -> None:
        self.max_bytes = max_bytes
        super().__init__(f"File size exceeds maximum limit of {max_bytes} bytes.")


async def compute_sha256_and_size_stream(
    file: UploadFile,
    chunk_size: int = 65536,
    max_bytes: Optional[int] = None,
) -> tuple[str, int]:
    """
    Computes SHA-256 hash and total byte count in a single streaming pass.
    Aborts immediately if max_bytes is exceeded and resets the file pointer to 0.
    """
    hasher = hashlib.sha256()
    total_size = 0
    try:
        while chunk := await file.read(chunk_size):
            total_size += len(chunk)
            if max_bytes is not None and total_size > max_bytes:
                raise FileSizeLimitExceededError(max_bytes)
            hasher.update(chunk)
    finally:
        # Always reset read pointer so callers or cleanup handlers see offset 0
        await file.seek(0)
    return hasher.hexdigest(), total_size


async def compute_sha256_stream(
    file: UploadFile,
    chunk_size: int = 65536,
    max_bytes: Optional[int] = None,
) -> str:
    """
    Computes SHA-256 hash by streaming chunks from UploadFile.
    Keeps memory usage low regardless of file size and resets the file pointer to 0.
    """
    digest, _ = await compute_sha256_and_size_stream(
        file, chunk_size=chunk_size, max_bytes=max_bytes
    )
    return digest


def compute_sha256_bytes(data: bytes) -> str:
    """Computes SHA-256 for in-memory byte buffer."""
    return hashlib.sha256(data).hexdigest()
