from backend.app.core.config import settings


def split_text(
    text: str,
    chunk_size: int | None = None,
    overlap: int | None = None,
) -> list[str]:
    size = chunk_size or settings.CHUNK_SIZE
    gap = overlap if overlap is not None else settings.CHUNK_OVERLAP
    cleaned = " ".join(text.split())
    if not cleaned:
        return []
    if size <= 0:
        raise ValueError("chunk_size must be positive")
    if gap >= size:
        raise ValueError("chunk overlap must be smaller than chunk_size")

    chunks: list[str] = []
    start = 0
    length = len(cleaned)
    while start < length:
        end = min(start + size, length)
        chunk = cleaned[start:end].strip()
        if chunk:
            chunks.append(chunk)
        if end >= length:
            break
        start = end - gap
    return chunks
