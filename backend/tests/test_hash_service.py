import io
import re
import pytest
from starlette.datastructures import UploadFile
from backend.app.services.hash_service import (
    FileSizeLimitExceededError,
    compute_sha256_stream,
    compute_sha256_and_size_stream,
    compute_sha256_bytes,
)

HEX_64_REGEX = re.compile(r"^[a-f0-9]{64}$")


class CountingBytesIO(io.BytesIO):
    """BytesIO wrapper tracking total bytes read to prove early stream termination."""

    def __init__(self, initial_bytes: bytes) -> None:
        super().__init__(initial_bytes)
        self.bytes_read = 0

    def read(self, size: int | None = -1) -> bytes:
        chunk = super().read(size)
        self.bytes_read += len(chunk)
        return chunk


@pytest.mark.asyncio
async def test_sha256_format_is_64_hex_chars():
    """Verify that SHA-256 output is exactly 64 lowercase hexadecimal characters."""
    sample_data = b"Enterprise RAG Model Platform - Test Content"
    upload_file = UploadFile(file=io.BytesIO(sample_data), filename="test.txt")

    hash_result = await compute_sha256_stream(upload_file)
    assert len(hash_result) == 64
    assert HEX_64_REGEX.match(hash_result) is not None


@pytest.mark.asyncio
async def test_identical_contents_different_filenames_produce_same_hash():
    """Verify that files with identical contents produce identical hashes regardless of filename."""
    content = b"%PDF-1.4 Identical Content For Deduplication Testing"
    file_a = UploadFile(file=io.BytesIO(content), filename="financial_report_2026.pdf")
    file_b = UploadFile(file=io.BytesIO(content), filename="copy_of_financial_report.pdf")

    hash_a = await compute_sha256_stream(file_a)
    hash_b = await compute_sha256_stream(file_b)

    assert hash_a == hash_b
    assert HEX_64_REGEX.match(hash_a)


@pytest.mark.asyncio
async def test_different_contents_produce_different_hashes():
    """Verify that different contents yield different SHA-256 digests."""
    file_1 = UploadFile(file=io.BytesIO(b"Document Version A"), filename="doc.txt")
    file_2 = UploadFile(file=io.BytesIO(b"Document Version B"), filename="doc.txt")

    hash_1 = await compute_sha256_stream(file_1)
    hash_2 = await compute_sha256_stream(file_2)

    assert hash_1 != hash_2


@pytest.mark.asyncio
async def test_single_pass_hash_and_size_computation_resets_pointer():
    """Verify compute_sha256_and_size_stream returns hash + size and rewinds stream pointer to 0."""
    content = b"Single-pass streaming hash and byte-size measurement" * 50
    upload_file = UploadFile(file=io.BytesIO(content), filename="stream.txt")

    file_hash, file_size = await compute_sha256_and_size_stream(upload_file, chunk_size=128)

    assert file_size == len(content)
    assert file_hash == compute_sha256_bytes(content)

    # Verify stream pointer was reset to 0 so subsequent storage writes read the full content
    remaining_data = await upload_file.read()
    assert remaining_data == content


@pytest.mark.asyncio
async def test_streaming_size_limit_aborts_immediately_mid_stream():
    """Verify max_bytes aborts streaming immediately without reading the entire oversized payload."""
    oversized_payload = b"X" * 2048  # 2048 bytes total
    tracked_stream = CountingBytesIO(oversized_payload)
    upload_file = UploadFile(file=tracked_stream, filename="large.txt")

    # Limit is 200 bytes with 64-byte chunks -> should stop right on chunk 4 (256 bytes read)
    with pytest.raises(FileSizeLimitExceededError) as exc_info:
        await compute_sha256_and_size_stream(upload_file, chunk_size=64, max_bytes=200)

    assert exc_info.value.max_bytes == 200
    assert tracked_stream.bytes_read == 256  # Aborted well before reading all 2048 bytes
    assert tracked_stream.tell() == 0  # Pointer reset cleanly via finally block


@pytest.mark.asyncio
async def test_empty_content_hash():
    """Verify empty file produces standard SHA-256 empty digest."""
    upload_file = UploadFile(file=io.BytesIO(b""), filename="empty.txt")
    file_hash, file_size = await compute_sha256_and_size_stream(upload_file)

    assert file_size == 0
    assert file_hash == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
