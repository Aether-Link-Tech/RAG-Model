"""Generate the OCR fixtures (FR-058 / AL-238, FR-062 / AL-248).

    python backend/tests/fixtures/ocr/generate.py

Writes into this folder (deterministic, no personal data):
  extraction/  images_3_on_2_pages.pdf, images_2.docx, no_images.pdf, no_images.docx
  testset/     10 PDF + 10 DOCX documents with text rendered into images
  ground_truth.json  exact text, page and quality of every test-set image
"""
from __future__ import annotations

import io
import json
import random
from pathlib import Path

import docx
import fitz  # PyMuPDF
from docx.shared import Inches
from PIL import Image, ImageDraw, ImageFilter, ImageFont

HERE = Path(__file__).parent
RNG = random.Random(62)

SENTENCES = [
    "Quarterly revenue grew twelve percent",
    "Submit expense reports by Friday",
    "The server room door must stay locked",
    "Annual safety training starts in March",
    "Backup jobs run every night at two",
    "Visitors must sign in at reception",
    "The new policy applies to all teams",
    "Project deadline moved to next Monday",
    "Customer support hours are nine to five",
    "Recycle paper in the blue containers",
    "Meeting notes are shared after each call",
    "Invoices above five thousand need approval",
    "Update your password every ninety days",
    "The library closes early on holidays",
    "Lab results are reviewed within two days",
    "Parking permits renew in September",
    "All laptops must use disk encryption",
    "Board meeting is held on the first Tuesday",
    "Report broken equipment to facilities",
    "Travel requests need manager approval",
    "Cafeteria menu changes every week",
    "Emergency exits are marked in green",
    "Printer toner is stored in room four",
    "The audit covers the last fiscal year",
    "Sales targets are reviewed each quarter",
    "New staff receive badges on day one",
    "Archive old files after five years",
    "Network maintenance is planned for Sunday",
    "Fire drills happen twice a year",
    "Feedback forms close at the end of May",
]


def _font(size: int) -> ImageFont.ImageFont:
    for name in ("arial.ttf", "DejaVuSans.ttf", "LiberationSans-Regular.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default(size=size)


def render(text: str, quality: str = "clean") -> bytes:
    """PNG (clean) or degraded JPEG (low) of one line of text."""
    size = 44 if quality == "clean" else 30
    font = _font(size)
    width = int(font.getlength(text)) + 60
    img = Image.new("RGB", (width, size + 50), "white")
    ImageDraw.Draw(img).text((30, 22), text, fill="black" if quality == "clean" else (70, 70, 70), font=font)
    buf = io.BytesIO()
    if quality == "clean":
        img.save(buf, "PNG")
        return buf.getvalue()
    img = img.rotate(RNG.uniform(-2.5, 2.5), expand=True, fillcolor="white")
    img = img.filter(ImageFilter.GaussianBlur(0.9))
    noisy = img.load()
    for _ in range(img.width * img.height // 40):
        x, y = RNG.randrange(img.width), RNG.randrange(img.height)
        shade = RNG.randrange(120, 230)
        noisy[x, y] = (shade, shade, shade)
    img.save(buf, "JPEG", quality=25)
    return buf.getvalue()


def make_pdf(path: Path, pages: list[list[tuple[str, str]]]) -> None:
    """pages: [[("text", "..."), ("image", png_bytes), ...], ...]"""
    doc = fitz.open()
    for items in pages:
        page = doc.new_page(width=595, height=842)
        y = 60
        for kind, value in items:
            if kind == "text":
                page.insert_text((60, y), value, fontsize=12)
                y += 40
            else:
                img = Image.open(io.BytesIO(value))
                w = min(470, img.width * 0.6)
                h = w * img.height / img.width
                page.insert_image(fitz.Rect(60, y, 60 + w, y + h), stream=value)
                y += h + 30
    doc.save(str(path), garbage=4, deflate=True)
    doc.close()


def make_docx(path: Path, items: list[tuple[str, object]]) -> None:
    document = docx.Document()
    for kind, value in items:
        if kind == "text":
            document.add_paragraph(value)
        else:
            document.add_paragraph().add_run().add_picture(io.BytesIO(value), width=Inches(4.5))
    document.save(str(path))


def extraction_fixtures() -> None:
    out = HERE / "extraction"
    out.mkdir(exist_ok=True)
    a, b, c = (render(s) for s in SENTENCES[:3])
    make_pdf(out / "images_3_on_2_pages.pdf", [
        [("text", "Page one introduction."), ("image", a), ("text", "Between the images."), ("image", b)],
        [("text", "Page two introduction."), ("image", c)],
    ])
    make_docx(out / "images_2.docx", [
        ("text", "First paragraph."), ("image", a), ("text", "Second paragraph."), ("image", b),
        ("text", "Closing paragraph."),
    ])
    make_pdf(out / "no_images.pdf", [[("text", "A document without any images.")]])
    make_docx(out / "no_images.docx", [("text", "A document without any images.")])


def test_set() -> None:
    out = HERE / "testset"
    out.mkdir(exist_ok=True)
    truth: dict[str, list[dict]] = {}
    sentences = iter(SENTENCES)
    for kind in ("pdf", "docx"):
        for n in range(1, 11):
            quality = "clean" if n <= 5 else "low"
            name = f"ocr_{kind}_{n:02d}_{quality}.{kind}"
            lines = [next(sentences, None) or RNG.choice(SENTENCES) for _ in range(1 if n % 2 else 2)]
            images = [render(line, quality) for line in lines]
            if kind == "pdf":
                pages = [[("text", f"Test document {n}, page {p + 1}."), ("image", img)] for p, img in enumerate(images)]
                make_pdf(out / name, pages)
                entries = [{"image_index": i, "page": i + 1, "quality": quality, "text": t} for i, t in enumerate(lines)]
            else:
                items: list[tuple[str, object]] = []
                for i, img in enumerate(images):
                    items += [("text", f"Test document {n}, section {i + 1}."), ("image", img)]
                make_docx(out / name, items)
                entries = [{"image_index": i, "page": None, "quality": quality, "text": t} for i, t in enumerate(lines)]
            truth[name] = entries
    (HERE / "ground_truth.json").write_text(json.dumps(truth, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    extraction_fixtures()
    test_set()
    print("fixtures written to", HERE)
