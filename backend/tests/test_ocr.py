"""OCR pipeline tests (FR-058 – FR-063): extraction, storage, OCR service, merge, fallback.

The OCR model is replaced by a fake client (AL-242); the real Florence-2 path is covered
by tests/test_ocr_model.py, which only runs with RUN_OCR_MODEL_TESTS=1.
"""
import base64
import hashlib
import importlib.util
import io
import json
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import fitz
import httpx
import pytest
from sqlalchemy import select, text

from backend.app.api.v1 import documents as documents_api
from backend.app.api.v1.retrieval import _source_fields
from backend.app.core.config import settings
from backend.app.db.rls import configure_rls_context
from backend.app.models.document_image import DocumentImage
from backend.app.services import document_ingest, ocr
from backend.app.services.document_images import ImageOCR, image_folder
from backend.app.services.document_parser import extract_layout
from backend.app.services.ocr import HttpOCRClient, OCRFailure, OCRText
from backend.app.services.ocr_merge import chunks_from_segments, merge_layout
from backend.tests.conftest import TestingSessionLocal, _CleanupSessionLocal, bearer

FIXTURES = Path(__file__).parent / "fixtures" / "ocr"
EXTRACTION = FIXTURES / "extraction"
PDF_MIME = "application/pdf"
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

def _load(name: str, path: Path):
    """Import a module from a file path (registered first: dataclasses need sys.modules)."""
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


generator = _load("ocr_fixture_generator", FIXTURES / "generate.py")
from backend.app.services import ocr_engines as engines  # noqa: E402


def fake_text(data: bytes) -> str:
    return f"ocr text {hashlib.sha256(data).hexdigest()[:8]}"


class FakeOCR:
    """Deterministic text per image; optionally fails or reports errors."""

    def __init__(self, fail_with: str | None = None, raise_exc: bool = False):
        self.calls = 0
        self.fail_with, self.raise_exc = fail_with, raise_exc

    async def recognize(self, images, timeout):
        self.calls += 1
        if self.raise_exc:
            raise RuntimeError("worker crashed")
        if self.fail_with:
            return [OCRFailure(self.fail_with)] * len(images)
        return [OCRText(fake_text(d), 0.93, "fake") for d in images]


@pytest.fixture
def fake_ocr():
    client = FakeOCR()
    ocr.set_ocr_client(client)
    yield client
    ocr.set_ocr_client(None)


@pytest.fixture
def ocr_pipeline(monkeypatch, chroma_client, fake_embeddings, fake_ocr):
    """Real indexing with OCR enabled, in-process parsing and the fake OCR client."""
    monkeypatch.setattr(documents_api, "index_document", document_ingest.index_document)
    monkeypatch.setattr(document_ingest, "AsyncSessionLocal", TestingSessionLocal)
    monkeypatch.setattr(settings, "PARSER_SANDBOX_ENABLED", False)
    monkeypatch.setattr(settings, "OCR_ENABLED", True)
    return fake_ocr


def scanned_pdf(*lines: str) -> bytes:
    """A PDF whose pages contain only images (no text layer), like a scan."""
    doc = fitz.open()
    for line in lines:
        page = doc.new_page(width=595, height=842)
        page.insert_image(fitz.Rect(60, 80, 530, 140), stream=generator.render(line))
    data = doc.tobytes()
    doc.close()
    return data


async def upload(client, user, name, data: bytes, mime, visibility="private"):
    return await client.post("/api/v1/documents", headers=bearer(user), data={"visibility": visibility},
                             files={"file": (name, io.BytesIO(data), mime)})


# --- FR-058 / AL-236, AL-238: image extraction ----------------------------------------


def _layout(name, mime):
    return extract_layout(str(EXTRACTION / name), mime, name)


def test_pdf_images_with_pages_and_bboxes(monkeypatch):
    monkeypatch.setattr(settings, "PARSER_SANDBOX_ENABLED", False)
    layout = _layout("images_3_on_2_pages.pdf", PDF_MIME)
    assert [img["page"] for img in layout["images"]] == [1, 1, 2]
    assert all(len(img["bbox"]) == 4 and img["width"] > 0 for img in layout["images"])
    order = [(p["page"], b["type"]) for p in layout["pages"] for b in p["blocks"]]
    assert order == [(1, "text"), (1, "image"), (1, "text"), (1, "image"), (2, "text"), (2, "image")]


def test_docx_images_in_paragraph_order(monkeypatch):
    monkeypatch.setattr(settings, "PARSER_SANDBOX_ENABLED", False)
    layout = _layout("images_2.docx", DOCX_MIME)
    assert len(layout["images"]) == 2
    kinds = [b["type"] for b in layout["pages"][0]["blocks"]]
    assert kinds == ["text", "image", "text", "image", "text"]


@pytest.mark.parametrize("name,mime", [("no_images.pdf", PDF_MIME), ("no_images.docx", DOCX_MIME)])
def test_documents_without_images_are_fine(monkeypatch, name, mime):
    monkeypatch.setattr(settings, "PARSER_SANDBOX_ENABLED", False)
    layout = _layout(name, mime)
    assert layout["images"] == [] and layout["pages"][0]["blocks"][0]["type"] == "text"


def test_layout_images_come_out_of_the_real_sandbox(monkeypatch, tmp_path):
    if shutil.which("docker") is None or subprocess.run(["docker", "image", "inspect", settings.PARSER_DOCKER_IMAGE],
                                                       capture_output=True).returncode != 0:
        pytest.skip("Docker parser sandbox image not available")
    monkeypatch.setattr(settings, "PARSER_SANDBOX_ENABLED", True)
    monkeypatch.setattr(settings, "PARSER_INPUT_MODE", "stdin")
    target = tmp_path / "doc.pdf"
    shutil.copy(EXTRACTION / "images_3_on_2_pages.pdf", target)
    layout = extract_layout(str(target), PDF_MIME, "doc.pdf")
    assert [img["page"] for img in layout["images"]] == [1, 1, 2]


# --- FR-060 / AL-243, AL-244: merge ---------------------------------------------------


def _ocr(index, page, text_, status="done"):
    return ImageOCR(index, uuid.uuid4(), page, [1, 2, 3, 4], status, text_, 0.9, "fake")


def test_merge_inserts_ocr_text_at_image_positions():
    layout = {"pages": [
        {"page": 1, "blocks": [{"type": "text", "text": "Intro"}, {"type": "image", "image": 0, "bbox": [1, 2, 3, 4]},
                               {"type": "text", "text": "Middle"}, {"type": "image", "image": 1, "bbox": [5, 6, 7, 8]}]},
        {"page": 2, "blocks": [{"type": "text", "text": "Second page"}, {"type": "image", "image": 2, "bbox": None}]},
    ]}
    ocr_results = {0: _ocr(0, 1, "IMG A"), 1: _ocr(1, 1, "", "skipped"), 2: _ocr(2, 2, "IMG C")}
    segments = merge_layout(layout, ocr_results)
    assert [(s.source_type, s.text, s.page) for s in segments] == [
        ("text", "Intro", 1), ("ocr", "IMG A", 1), ("text", "Middle", 1),
        ("text", "Second page", 2), ("ocr", "IMG C", 2),
    ]


def test_chunks_are_marked_with_source_type_and_ocr_metadata():
    layout = {"pages": [{"page": 3, "blocks": [{"type": "text", "text": "Body"},
                                               {"type": "image", "image": 0, "bbox": [10.0, 20.0, 30.0, 40.0]}]}]}
    chunks = chunks_from_segments(merge_layout(layout, {0: _ocr(0, 3, "Chart: sales up")}))
    (t_text, t_meta), (o_text, o_meta) = chunks
    assert t_meta == {"source_type": "text", "page": 3}
    assert o_meta["source_type"] == "ocr" and o_meta["page"] == 3 and o_meta["ocr_confidence"] == 0.9
    assert json.loads(o_meta["bbox"]) == [10.0, 20.0, 30.0, 40.0] and o_meta["image_id"]


def test_retrieval_source_fields():
    assert _source_fields({"source_type": "ocr", "page": 2, "bbox": "[1, 2, 3, 4]", "image_id": "x",
                           "ocr_confidence": 0.8}) == {
        "source_type": "ocr", "page": 2, "bbox": [1.0, 2.0, 3.0, 4.0], "image_id": "x", "ocr_confidence": 0.8}
    assert _source_fields({})["source_type"] == "text"


# --- FR-059 / AL-242: OCR worker logic (fake engines) and HTTP client ----------------


class _Engine:
    def __init__(self, name, fail=False):
        self.name, self.fail = name, fail

    def recognize(self, images):
        if self.fail:
            raise RuntimeError("model crashed")
        return [(f"{self.name}:{img.size[0]}", 0.9) for img in images]


def _png(width=120):
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (width, 40), "white").save(buf, "PNG")
    return buf.getvalue()


def test_worker_uses_primary_engine_in_batch():
    service = engines.OCRService(lambda: _Engine("primary"), lambda: _Engine("fallback"))
    results = service.recognize([_png(100), _png(140)], timeout_seconds=10)
    assert [r.text for r in results] == ["primary:100", "primary:140"]
    assert all(r.engine == "primary" and r.error is None for r in results)


def test_worker_falls_back_when_primary_fails():
    service = engines.OCRService(lambda: _Engine("primary", fail=True), lambda: _Engine("fallback"))
    assert [r.engine for r in service.recognize([_png()], timeout_seconds=10)] == ["fallback"]


def test_worker_falls_back_when_primary_cannot_load():
    def broken():
        raise OSError("model files missing")

    service = engines.OCRService(broken, lambda: _Engine("fallback"))
    result = service.recognize([_png()], timeout_seconds=10)[0]
    assert result.engine == "fallback" and "model files missing" in service.status()["primary_error"]


def test_worker_reports_corrupt_images_and_timeouts_per_image():
    service = engines.OCRService(lambda: _Engine("primary", fail=True), lambda: _Engine("fallback"))
    results = service.recognize([b"not an image", _png()], timeout_seconds=10)
    assert results[0].error == "Corrupt or unsupported image" and results[1].engine == "fallback"
    timed_out = service.recognize([_png()], timeout_seconds=0)  # deadline already passed
    assert timed_out[0].error == "OCR timed out"


async def test_http_client_maps_results_and_outages(monkeypatch):
    def handler(request):
        body = json.loads(request.content)
        assert len(body["images"]) == 2 and base64.b64decode(body["images"][0])
        return httpx.Response(200, json={"results": [{"text": "hi", "confidence": 0.9, "engine": "florence-2-base"},
                                                     {"error": "Corrupt or unsupported image"}]})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))
    out = await HttpOCRClient("http://ocr").recognize([b"a", b"b"], timeout=5)
    assert out == [OCRText("hi", 0.9, "florence-2-base"), OCRFailure("Corrupt or unsupported image")]

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real_client(
        transport=httpx.MockTransport(lambda r: httpx.Response(503)), **kw))
    assert await HttpOCRClient("http://ocr").recognize([b"a"], timeout=5) == [OCRFailure("OCR service unavailable")]


# --- End to end: upload -> OCR -> chunks, storage, isolation, fallback ----------------


async def _images_for(user, doc_id):
    async with TestingSessionLocal() as db:
        await configure_rls_context(db, user.id, "user")
        rows = await db.execute(select(DocumentImage).where(DocumentImage.document_id == uuid.UUID(doc_id))
                                .order_by(DocumentImage.image_index))
        return rows.scalars().all()


async def test_upload_ocrs_images_and_indexes_ocr_chunks(async_client, test_user, ocr_pipeline):
    data = (EXTRACTION / "images_3_on_2_pages.pdf").read_bytes()
    res = await upload(async_client, test_user, "report.pdf", data, PDF_MIME)
    assert res.status_code == 201 and res.json()["status"] == "indexed", res.text
    doc_id = res.json()["id"]

    images = await _images_for(test_user, doc_id)
    assert [(i.page, i.ocr_status, i.ocr_engine) for i in images] == [(1, "done", "fake")] * 2 + [(2, "done", "fake")]
    folder = image_folder(test_user.id, uuid.UUID(doc_id))
    assert sorted(p.name for p in folder.iterdir()) == ["0.png", "1.png", "2.png"]
    assert all(Path(i.file_path).parent == folder for i in images)  # inside the owner's folder

    from backend.app.db.chroma import get_private_collection

    stored = get_private_collection(test_user.id).get(include=["metadatas", "documents"])
    ocr_chunks = [(d, m) for d, m in zip(stored["documents"], stored["metadatas"]) if m["source_type"] == "ocr"]
    assert len(ocr_chunks) == 3 and [m["page"] for _, m in ocr_chunks] == [1, 1, 2]
    assert ocr_chunks[0][0] == fake_text(folder.joinpath("0.png").read_bytes())

    # Search results say where the text came from
    hits = (await async_client.post("/api/v1/retrieval/query", headers=bearer(test_user),
                                    json={"query": ocr_chunks[0][0], "n_results": 10})).json()["results"]
    ocr_hits = [h for h in hits if h["source_type"] == "ocr"]
    assert ocr_hits and ocr_hits[0]["page"] in (1, 2) and len(ocr_hits[0]["bbox"]) == 4

    # Deleting the document removes images (files and rows)
    assert (await async_client.delete(f"/api/v1/documents/{doc_id}", headers=bearer(test_user))).status_code == 204
    assert not folder.exists()
    async with _CleanupSessionLocal() as s:
        left = (await s.execute(text("SELECT count(*) FROM document_images WHERE document_id = :d"),
                                {"d": uuid.UUID(doc_id)})).scalar_one()
    assert left == 0


async def test_scanned_pdf_is_indexed_from_ocr_text(async_client, test_user, ocr_pipeline):
    res = await upload(async_client, test_user, "scan.pdf", scanned_pdf("Annual safety training starts in March"), PDF_MIME)
    assert res.status_code == 201 and res.json()["status"] == "indexed", res.text


async def test_different_scans_are_not_near_duplicates_of_each_other(async_client, test_user, monkeypatch, ocr_pipeline):
    """Regression: scans without a text layer must not all share one 'empty' fingerprint."""
    ocr.set_ocr_client(FakeOCR(fail_with="OCR service unavailable"))
    first = await upload(async_client, test_user, "scan1.pdf", scanned_pdf("First scan text"), PDF_MIME)
    second = await upload(async_client, test_user, "scan2.pdf", scanned_pdf("Completely different scan"), PDF_MIME)
    assert first.status_code == 201 and second.status_code == 201, second.text
    assert first.json()["status"] == second.json()["status"] == "failed"  # nothing readable, but no false duplicate
    await _purge_audit(test_user)


async def test_other_users_cannot_see_document_images(async_client, test_user, test_user_2, ocr_pipeline):
    data = (EXTRACTION / "images_2.docx").read_bytes()
    doc_id = (await upload(async_client, test_user, "a.docx", data, DOCX_MIME)).json()["id"]
    assert len(await _images_for(test_user, doc_id)) == 2
    assert await _images_for(test_user_2, doc_id) == []


async def _audit_reasons(user):
    async with _CleanupSessionLocal() as s:
        rows = await s.execute(text("SELECT event_type, reason FROM security_audit_log WHERE user_id = :u"),
                               {"u": user.id})
        return rows.all()


async def _purge_audit(user):
    async with _CleanupSessionLocal() as s:
        await s.execute(text("DELETE FROM security_audit_log WHERE user_id = :u"), {"u": user.id})
        await s.commit()


# --- FR-063 / AL-250 – AL-252: failures skip the image, never the document -------------


@pytest.mark.parametrize("client,expected", [
    (FakeOCR(fail_with="OCR timed out"), "OCR timed out"),
    (FakeOCR(raise_exc=True), "OCR failed: RuntimeError"),
    (FakeOCR(fail_with="OCR service unavailable"), "OCR service unavailable"),
], ids=["timeout", "worker-crash", "model-unavailable"])
async def test_ocr_failures_skip_images_but_document_is_indexed(async_client, test_user, ocr_pipeline, client, expected):
    ocr.set_ocr_client(client)
    data = (EXTRACTION / "images_3_on_2_pages.pdf").read_bytes()
    res = await upload(async_client, test_user, "report.pdf", data, PDF_MIME)
    try:
        assert res.status_code == 201 and res.json()["status"] == "indexed", res.text  # text still indexed
        images = await _images_for(test_user, res.json()["id"])
        assert {i.ocr_status for i in images} == {"skipped"} and all(i.ocr_error == expected for i in images)
        audit = await _audit_reasons(test_user)
        assert sum(1 for e, _ in audit if e == "ocr_image_skipped") == 3
        assert all(expected in r and "image" in r for e, r in audit if e == "ocr_image_skipped")
    finally:
        await _purge_audit(test_user)


async def test_corrupt_image_is_skipped_and_others_are_processed(async_client, test_user, ocr_pipeline, monkeypatch):
    real = extract_layout

    def with_corrupt_image(*args, **kwargs):
        layout = real(*args, **kwargs)
        layout["images"][1]["data"] = "!!!not base64!!!"
        return layout

    monkeypatch.setattr(document_ingest, "extract_layout", with_corrupt_image)
    res = await upload(async_client, test_user, "report.pdf", (EXTRACTION / "images_3_on_2_pages.pdf").read_bytes(), PDF_MIME)
    try:
        assert res.json()["status"] == "indexed"
        statuses = [i.ocr_status for i in await _images_for(test_user, res.json()["id"])]
        assert statuses == ["done", "skipped", "done"]
    finally:
        await _purge_audit(test_user)


async def test_time_budget_skips_remaining_images(async_client, test_user, ocr_pipeline, monkeypatch):
    monkeypatch.setattr(settings, "OCR_BATCH_SIZE", 1)
    monkeypatch.setattr(settings, "OCR_DOCUMENT_BUDGET_SECONDS", 0.0)
    res = await upload(async_client, test_user, "report.pdf", (EXTRACTION / "images_3_on_2_pages.pdf").read_bytes(), PDF_MIME)
    try:
        assert res.json()["status"] == "indexed"
        images = await _images_for(test_user, res.json()["id"])
        assert all(i.ocr_error == "OCR time budget for the document was used up" for i in images)
    finally:
        await _purge_audit(test_user)


async def test_skipped_images_appear_in_admin_audit_log(async_client, test_user, admin_user, ocr_pipeline):
    ocr.set_ocr_client(FakeOCR(fail_with="OCR service unavailable"))
    await upload(async_client, test_user, "report.docx", (EXTRACTION / "images_2.docx").read_bytes(), DOCX_MIME)
    try:
        page = (await async_client.get("/api/v1/admin/audit-log", headers=bearer(admin_user),
                                       params={"event_type": "ocr_image_skipped", "user_id": str(test_user.id)})).json()
        assert page["total"] == 2 and "OCR service unavailable" in page["items"][0]["reason"]
    finally:
        await _purge_audit(test_user)


async def test_ocr_disabled_keeps_the_old_behaviour(async_client, test_user, ocr_pipeline, monkeypatch):
    monkeypatch.setattr(settings, "OCR_ENABLED", False)
    res = await upload(async_client, test_user, "report.pdf", (EXTRACTION / "images_3_on_2_pages.pdf").read_bytes(), PDF_MIME)
    assert res.json()["status"] == "indexed" and ocr_pipeline.calls == 0
    assert await _images_for(test_user, res.json()["id"]) == []


# --- OCR_BACKEND / OCR_ENGINE choices (local in-process vs worker, florence vs easyocr) ----


def test_engine_choices():
    florence = engines.build_service("florence")
    assert florence._primary_factory is engines.Florence2Engine and florence._fallback_factory is engines.EasyOCREngine
    easy = engines.build_service("easyocr")
    assert easy._primary_factory is engines.EasyOCREngine and easy._fallback_factory is None
    with pytest.raises(ValueError):
        engines.build_service("cloud-api")


def test_easyocr_only_has_no_fallback():
    service = engines.OCRService(lambda: _Engine("easyocr", fail=True), None)
    assert service.recognize([_png()], timeout_seconds=10)[0].error == "No OCR engine available"


def test_client_choice_follows_settings(monkeypatch):
    monkeypatch.setattr(settings, "OCR_BACKEND", "worker")
    assert isinstance(ocr.get_ocr_client(), HttpOCRClient)
    monkeypatch.setattr(settings, "OCR_BACKEND", "local")
    monkeypatch.setattr(settings, "OCR_ENGINE", "easyocr")
    local = ocr.get_ocr_client()
    assert isinstance(local, ocr.LocalOCRClient) and ocr.get_ocr_client() is local  # model loaded once


async def test_local_client_maps_results():
    client = ocr.LocalOCRClient.__new__(ocr.LocalOCRClient)
    client.service = engines.OCRService(lambda: _Engine("primary"), None)
    out = await client.recognize([_png(90), b"broken"], timeout=10)
    assert out == [OCRText("primary:90", 0.9, "primary"), OCRFailure("Corrupt or unsupported image")]


async def test_misconfigured_engine_skips_images_but_indexes(async_client, test_user, ocr_pipeline, monkeypatch):
    ocr.set_ocr_client(None)
    monkeypatch.setattr(settings, "OCR_BACKEND", "local")
    monkeypatch.setattr(settings, "OCR_ENGINE", "no-such-engine")
    res = await upload(async_client, test_user, "report.pdf", (EXTRACTION / "images_3_on_2_pages.pdf").read_bytes(), PDF_MIME)
    try:
        assert res.json()["status"] == "indexed"
        images = await _images_for(test_user, res.json()["id"])
        assert {i.ocr_error for i in images} == {"OCR is not configured correctly"}
    finally:
        await _purge_audit(test_user)
