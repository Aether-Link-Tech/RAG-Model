"""AL-162 — FR-066 EICAR / ClamAV tests.

Spec (Jira AL-162): upload EICAR -> expect rejection + quarantine + audit log
entry. Add to CI.

Three layers:
1. Unit tests of scan_and_quarantine with a fake clamd client (always run).
2. API test: an infected upload is blocked (422), quarantined, and never stored
   or registered (always runs, fake clamd).
3. Real ClamAV with the real EICAR string: runs when ClamAV is reachable;
   required in CI (CLAMAV_HOST set + CI=true), skipped on dev machines.

The EICAR string is assembled at runtime so this source file itself is not
flagged by local antivirus.
"""
import io
import os
import socket
import uuid
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

clamav = pytest.importorskip("backend.app.services.clamav")
if not hasattr(clamav, "scan_and_quarantine"):
    pytest.skip("FR-066 (scan_and_quarantine) not merged yet", allow_module_level=True)

from backend.app.core.config import settings
from backend.app.core.security import create_access_token
from backend.app.models.document import Document
from backend.app.models.user import User

EICAR = ("X5O!P%@AP[4\\PZX54(P^)7CC)7}$" + "EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*").encode("ascii")
# Harmless stand-in for fake-scanner tests, so local antivirus never touches temp files.
FAKE_INFECTED = b"FAKE-MALWARE-MARKER for AL-162 tests"
SIGNATURE = "Eicar-Test-Signature"


class _FakeClamd:
    """Mimics clamd.ClamdNetworkSocket: FOUND when the marker is in the stream."""

    def __init__(self, *args, ping="PONG", fail=False, **kwargs):
        self._ping, self._fail = ping, fail

    def ping(self):
        if self._fail:
            raise ConnectionRefusedError("clamd down")
        return self._ping

    def instream(self, stream):
        data = stream.read()
        if FAKE_INFECTED in data or EICAR in data:
            return {"stream": ("FOUND", SIGNATURE)}
        return {"stream": ("OK", None)}


@pytest.fixture
def quarantine_dir(tmp_path, monkeypatch):
    qdir = tmp_path / "quarantine"
    monkeypatch.setattr(settings, "CLAMAV_QUARANTINE_DIR", str(qdir))
    return qdir


@pytest.fixture
def fake_clamd(monkeypatch):
    def install(**kwargs):
        monkeypatch.setattr(clamav.clamd, "ClamdNetworkSocket", lambda *a, **k: _FakeClamd(**kwargs))
    install()
    return install


# --- 1. Unit: scan_and_quarantine --------------------------------------------

USER_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
DOC_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")


def _quarantined_files(qdir: Path) -> list[Path]:
    return [p for p in qdir.rglob("*") if p.is_file()] if qdir.exists() else []


def test_infected_file_is_moved_to_per_user_quarantine(quarantine_dir, fake_clamd):
    from backend.app.services.storage_paths import user_storage_dir

    staged = user_storage_dir(USER_ID, create=True) / "upload.tmp"
    staged.write_bytes(FAKE_INFECTED)

    with pytest.raises(clamav.InfectedFileError) as info:
        clamav.scan_and_quarantine(str(staged), USER_ID, DOC_ID)

    assert info.value.signature == SIGNATURE
    assert not staged.exists(), "infected file must leave the upload area"
    target = quarantine_dir / str(USER_ID) / str(DOC_ID)
    assert target.is_file() and target.read_bytes() == FAKE_INFECTED
    assert _quarantined_files(quarantine_dir) == [target]


def test_clean_file_is_left_in_place(quarantine_dir, fake_clamd):
    from backend.app.services.storage_paths import user_storage_dir

    staged = user_storage_dir(USER_ID, create=True) / "upload.tmp"
    staged.write_bytes(b"clean document text")
    clamav.scan_and_quarantine(str(staged), USER_ID, DOC_ID)
    assert staged.exists()
    assert _quarantined_files(quarantine_dir) == []


@pytest.mark.parametrize("kwargs", [{"fail": True}, {"ping": "NOPE"}])
def test_scanner_unavailable_fails_closed(quarantine_dir, fake_clamd, kwargs):
    from backend.app.services.storage_paths import user_storage_dir

    fake_clamd(**kwargs)
    staged = user_storage_dir(USER_ID, create=True) / "upload.tmp"
    staged.write_bytes(b"anything")
    with pytest.raises(clamav.ClamAVUnavailableError):
        clamav.scan_and_quarantine(str(staged), USER_ID, DOC_ID)
    assert staged.exists(), "nothing is quarantined when the scanner is unavailable"


# --- 2. API: infected upload is blocked end to end ---------------------------

@pytest.mark.clamav_scan
async def test_infected_upload_is_rejected_quarantined_and_not_registered(
    async_client, test_user: User, db_session: AsyncSession, quarantine_dir, fake_clamd
):
    headers = {"Authorization": f"Bearer {create_access_token(test_user.id, role=test_user.role)}"}
    response = await async_client.post(
        "/api/v1/documents",
        headers=headers,
        files={"file": ("invoice.txt", io.BytesIO(FAKE_INFECTED), "text/plain")},
    )

    assert response.status_code == 422, response.text
    assert "malware" in response.json()["detail"].lower()

    rows = await db_session.execute(select(Document).where(Document.user_id == test_user.id))
    assert rows.scalars().all() == [], "no documents row for an infected upload"

    stored = [p for p in Path(settings.UPLOAD_DIR).rglob("*") if p.is_file()]
    assert stored == [], "nothing may remain in user storage (incl. .tmp)"
    quarantined = _quarantined_files(quarantine_dir)
    assert len(quarantined) == 1
    assert quarantined[0].parent.name == str(test_user.id), "quarantine must be per user"
    assert quarantined[0].read_bytes() == FAKE_INFECTED


# --- 3. Real ClamAV + real EICAR ---------------------------------------------

def _clamav_reachable() -> bool:
    try:
        with socket.create_connection((settings.CLAMAV_HOST, settings.CLAMAV_PORT), timeout=2):
            return True
    except OSError:
        return False


@pytest.mark.clamav_scan
def test_real_clamav_detects_eicar(quarantine_dir):
    if not _clamav_reachable():
        if os.getenv("CI") and os.getenv("CLAMAV_HOST"):
            pytest.fail("ClamAV required in CI but unreachable")
        pytest.skip("ClamAV not running locally")

    from backend.app.services.storage_paths import user_storage_dir

    staged = user_storage_dir(USER_ID, create=True) / "eicar.tmp"
    try:
        staged.write_bytes(EICAR)
        staged.read_bytes()
    except OSError:
        # The host's own antivirus (e.g. Windows Defender) blocked the EICAR file
        # before ClamAV could see it. CI runs on Linux without one.
        if os.getenv("CI"):
            raise
        pytest.skip("Local antivirus intercepted the EICAR test file")
    with pytest.raises(clamav.InfectedFileError) as info:
        clamav.scan_and_quarantine(str(staged), USER_ID, DOC_ID)
    assert "eicar" in info.value.signature.lower()
    assert not staged.exists()
    assert (quarantine_dir / str(USER_ID) / str(DOC_ID)).is_file()
