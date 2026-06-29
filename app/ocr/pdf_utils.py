"""
Converts PDF pages to images so they can be sent to llava for layout/field/
checkbox understanding. Uses PyMuPDF (fitz) — a self-contained Python binding
to the MuPDF renderer, distributed as a regular pip wheel.

Deliberately NOT using pdf2image/Poppler here: pdf2image only wraps the
Poppler command-line tools (pdftoppm/pdftocairo), which are a *separate*
binary that pip cannot install — they have to be installed on the OS and put
on PATH. That's exactly what produces the "Unable to get page count. Is
poppler installed and in PATH?" error on a fresh machine. PyMuPDF ships its
own renderer inside the wheel, so `pip install -r requirements.txt` is
sufficient and there is nothing extra to install or configure on Windows.

Also provides an optional pytesseract raw-text pass, used only as a
cross-check signal (e.g. for keyword-based form classification hints) -
not as the primary extraction path, since llava handles layout + checkboxes
that plain OCR text cannot.
"""
from __future__ import annotations

import shutil
from pathlib import Path

import fitz  # PyMuPDF

from app.utils.logger import get_logger
from config.settings import settings

logger = get_logger(__name__)

try:
    import pytesseract
    from PIL import Image
    _TESSERACT_AVAILABLE = shutil.which("tesseract") is not None
except ImportError:
    _TESSERACT_AVAILABLE = False


def render_pdf_to_images(pdf_path: Path, output_subdir: str | None = None) -> list[Path]:
    """
    Renders every page of a PDF to a PNG file under settings.paths.temp_dir.
    Returns the list of image paths in page order.
    """
    if not pdf_path.exists():
        raise FileNotFoundError(f"PDF not found: {pdf_path}")

    target_dir = settings.paths.temp_dir / (output_subdir or pdf_path.stem)
    target_dir.mkdir(parents=True, exist_ok=True)

    logger.info("Rendering %s to images at %d DPI", pdf_path.name, settings.pdf_render_dpi)

    # PyMuPDF renders at 72 DPI by default; scale the page->pixmap matrix to
    # hit the configured DPI, matching the old pdf2image(dpi=...) behaviour.
    zoom = settings.pdf_render_dpi / 72.0
    matrix = fitz.Matrix(zoom, zoom)

    image_paths: list[Path] = []
    try:
        with fitz.open(str(pdf_path)) as doc:
            for i, page in enumerate(doc, start=1):
                pixmap = page.get_pixmap(matrix=matrix)
                img_path = target_dir / f"page_{i:03d}.png"
                pixmap.save(str(img_path))
                image_paths.append(img_path)
    except fitz.FileDataError as exc:
        raise ValueError(f"Could not read {pdf_path.name} - file may be corrupt or not a valid PDF") from exc

    logger.info("Rendered %d page(s) from %s", len(image_paths), pdf_path.name)
    return image_paths


def quick_text_scan(image_path: Path) -> str:
    """
    Cheap, deterministic raw-text pass using Tesseract (if installed).
    Used only for classification keyword hints - falls back to empty string
    if pytesseract/Tesseract isn't installed, since it's optional, not required.
    """
    if not _TESSERACT_AVAILABLE:
        return ""
    try:
        return pytesseract.image_to_string(Image.open(image_path))
    except Exception as exc:  # noqa: BLE001 - degrade gracefully, this path is optional
        logger.warning("Tesseract quick scan failed for %s: %s", image_path.name, exc)
        return ""


def find_page_for_hint(image_paths: list[Path], hint_keywords: list[str]) -> Path | None:
    """Scans pages with quick_text_scan to locate a page matching given keywords (e.g. Page 10 guardian section)."""
    for img_path in image_paths:
        text = quick_text_scan(img_path).lower()
        if any(hint.lower() in text for hint in hint_keywords):
            return img_path
    return None
