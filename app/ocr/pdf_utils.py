"""
Converts PDF pages to images for the vision model (llava / qwen-vl).

Renderer: pypdfium2 (ships its own PDF engine in the wheel — no system
dependency, unlike pdf2image which wraps Poppler CLI tools). Falls back to
pdf2image + Poppler if pypdfium2 is unavailable.

Also provides an optional Tesseract raw-text pass used only as a lightweight
cross-check for classification keyword hints — not the primary extraction path.

When ocr_enhance_images is on (the default), each page also gets a raw,
un-enhanced sibling saved alongside the enhanced version
("page_003.png" + "page_003_raw.png"). app.services.extractor uses this
raw sibling as an independent second read for multi-pass self-consistency
extraction - enhancement helps in most cases but occasionally over-
sharpens noise into a false stroke, so a raw counterpart catches what
enhancement introduces and vice versa. Skipped when enhancement is off,
since enhanced == raw in that case and there'd be nothing to gain from a
second identical read.
"""
from __future__ import annotations

import shutil
import threading
from pathlib import Path

from PIL import Image, ImageEnhance, ImageOps

from app.utils.logger import get_logger
from config.settings import settings

logger = get_logger(__name__)

# pypdfium2 wraps the PDFium C library, which is NOT thread-safe: calling
# PdfDocument()/page.render()/doc.close() concurrently from multiple threads
# (as pipeline.py's ThreadPoolExecutor does when processing a batch) corrupts
# internal PDFium state and crashes the whole process on Windows with
# "OSError: exception: access violation writing 0x...". This is not a
# corrupt/password-protected file - it's a race condition. Serializing every
# pdfium call behind this lock fixes it; the rest of each document's
# processing (page enhancement, and especially the LLM call, which is the
# slow part) still runs concurrently, so batch throughput barely changes.
_PDFIUM_LOCK = threading.Lock()

# ---------------------------------------------------------------------------
# PDF renderer (pypdfium2 preferred, pdf2image fallback)
# ---------------------------------------------------------------------------

try:
    import pypdfium2 as pdfium
    _RENDERER = "pypdfium2"
    logger.debug("PDF renderer: pypdfium2")
except ImportError:
    pdfium = None  # type: ignore[assignment]
    _RENDERER = "pdf2image"
    logger.debug("PDF renderer: pdf2image (pypdfium2 not available)")

try:
    import pytesseract
    _TESSERACT_AVAILABLE = shutil.which("tesseract") is not None
except ImportError:
    _TESSERACT_AVAILABLE = False


# ---------------------------------------------------------------------------
# Image enhancement
# ---------------------------------------------------------------------------

def _enhance_for_handwriting(img: Image.Image) -> Image.Image:
    """
    Lightweight preprocessing to help the vision model read handwriting:
    - Upscale small renders so faint/small handwriting has enough pixels.
    - Grayscale removes colour noise; autocontrast uses the full tonal range.
    - Mild unsharp mask crisps stroke edges without artefacts.
    """
    long_edge = max(img.size)
    if long_edge < settings.ocr_min_long_edge_px:
        scale = settings.ocr_min_long_edge_px / long_edge
        img = img.resize(
            (int(img.width * scale), int(img.height * scale)),
            Image.LANCZOS,
        )

    gray = ImageOps.grayscale(img)
    gray = ImageOps.autocontrast(gray, cutoff=1)
    gray = ImageEnhance.Sharpness(gray).enhance(1.6)
    gray = ImageEnhance.Contrast(gray).enhance(1.15)
    return gray.convert("RGB")  # vision model expects RGB


# ---------------------------------------------------------------------------
# Rendering back-ends
# ---------------------------------------------------------------------------

def _save_enhanced(pil_img: Image.Image, page_num: int, target_dir: Path) -> Path:
    """Shared save step for both render backends: writes the raw sibling
    (if enhancement is on) and the enhanced/primary image, keyed by real
    1-indexed page number so callers can look pages up out of order."""
    img_path = target_dir / f"page_{page_num:03d}.png"
    if settings.ocr_enhance_images:
        raw_path = target_dir / f"page_{page_num:03d}_raw.png"
        pil_img.convert("RGB").save(raw_path, "PNG")
        enhanced_img = _enhance_for_handwriting(pil_img)
        enhanced_img.save(img_path, "PNG")
    else:
        pil_img.convert("RGB").save(img_path, "PNG")
    return img_path


def _render_with_pypdfium2(pdf_path: Path, target_dir: Path, page_numbers: set[int] | None) -> dict[int, Path]:
    scale = settings.pdf_render_dpi / 72.0

    # Only the pdfium calls themselves need the lock - PDFium's C internals
    # are what's unsafe across threads, not the PIL enhancement/save step
    # that follows. Keeping the lock scoped this tightly means a slow batch
    # of many documents still overlaps their (much slower) LLM calls freely;
    # only the brief render step is ever serialized.
    #
    # Iterating still visits every page up to the highest one requested
    # (pdfium doesn't expose a cheaper "seek" than walking the page list),
    # but skips the actual rasterize() call - the expensive part - for any
    # page not in page_numbers, so a document only needs the pages a form
    # type's extraction pass genuinely reads (see app.services.extractor)
    # instead of every page in the source PDF.
    raw_pages: dict[int, Image.Image] = {}
    with _PDFIUM_LOCK:
        doc = pdfium.PdfDocument(str(pdf_path))
        for i, page in enumerate(doc, start=1):
            if page_numbers is None or i in page_numbers:
                bitmap = page.render(scale=scale, rotation=0)
                raw_pages[i] = bitmap.to_pil()
                bitmap.close()
            page.close()
        doc.close()

    return {i: _save_enhanced(pil_img, i, target_dir) for i, pil_img in raw_pages.items()}


def _render_with_pdf2image(pdf_path: Path, target_dir: Path, page_numbers: set[int] | None) -> dict[int, Path]:
    from pdf2image import convert_from_path  # type: ignore[import]
    if page_numbers is None:
        pages = enumerate(convert_from_path(str(pdf_path), dpi=settings.pdf_render_dpi), start=1)
    else:
        # pdf2image has no non-contiguous page filter - render each
        # requested page with its own first_page/last_page call instead of
        # converting the whole document.
        pages = []
        for n in sorted(page_numbers):
            rendered = convert_from_path(str(pdf_path), dpi=settings.pdf_render_dpi, first_page=n, last_page=n)
            if rendered:
                pages.append((n, rendered[0]))

    return {i: _save_enhanced(pil_img, i, target_dir) for i, pil_img in pages}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def render_pdf_to_images(
    pdf_path: Path,
    output_subdir: str | None = None,
    page_numbers: list[int] | None = None,
) -> dict[int, Path]:
    """
    Renders the given 1-indexed page_numbers of a PDF to PNGs under
    settings.paths.temp_dir (every page, if page_numbers is None),
    applying handwriting-friendly enhancement unless ocr_enhance_images is
    off. Returns {page_number: enhanced_image_path} - keyed by real page
    number (not list position) so a caller asking for pages [1, 10] can't
    misread the second entry as "page 2". A raw sibling for each rendered
    page is saved alongside on disk when enhancement is on; see
    raw_sibling_path() below for how callers can find it.

    Rendering only the pages a document's form type actually needs (rather
    than every page in the source PDF) matters in practice: a real APPFORM
    submission commonly runs to 10+ pages, but only page 1 (and, for a
    minor/joint account, page 10's guardian section) is ever read by
    app.services.extractor - rasterizing, enhancing, and double-saving
    pages 2-9 for every such document was pure wasted work repeated on
    every single submission. Called twice per document in practice (page 1
    alone for classification, then whatever else that form type needs once
    classified) rather than once for everything up front.
    """
    if not pdf_path.exists():
        raise FileNotFoundError(f"PDF not found: {pdf_path}")

    target_dir = settings.paths.temp_dir / (output_subdir or pdf_path.stem)
    target_dir.mkdir(parents=True, exist_ok=True)

    wanted = set(page_numbers) if page_numbers is not None else None
    logger.info(
        "Rendering %s%s at %d DPI via %s", pdf_path.name,
        f" page(s) {sorted(wanted)}" if wanted else "", settings.pdf_render_dpi, _RENDERER,
    )

    try:
        if _RENDERER == "pypdfium2":
            image_paths = _render_with_pypdfium2(pdf_path, target_dir, wanted)
        else:
            image_paths = _render_with_pdf2image(pdf_path, target_dir, wanted)
    except Exception as exc:
        raise ValueError(
            f"Could not render {pdf_path.name} — file may be corrupt or password-protected: {exc}"
        ) from exc

    if not image_paths:
        raise ValueError(f"No pages rendered from {pdf_path.name}")

    logger.info("Rendered %d page(s) from %s", len(image_paths), pdf_path.name)
    return image_paths


def raw_sibling_path(enhanced_image_path: Path) -> Path | None:
    """Returns the un-enhanced sibling of an enhanced page image, if one
    was saved (i.e. ocr_enhance_images was on for that render), else None."""
    candidate = enhanced_image_path.parent / f"{enhanced_image_path.stem}_raw{enhanced_image_path.suffix}"
    return candidate if candidate.exists() else None


def quick_text_scan(image_path: Path) -> str:
    """
    Optional raw-text pass via Tesseract (if installed).
    Used for classification keyword hints only — not the primary extraction path.
    Returns empty string gracefully when Tesseract is unavailable.
    """
    if not _TESSERACT_AVAILABLE:
        return ""
    try:
        return pytesseract.image_to_string(Image.open(image_path))
    except Exception as exc:  # noqa: BLE001
        logger.warning("Tesseract quick scan failed for %s: %s", image_path.name, exc)
        return ""


def find_page_for_hint(image_paths: list[Path], hint_keywords: list[str]) -> Path | None:
    """
    Scans pages with quick_text_scan to locate a page matching given keywords.
    Used e.g. to find the guardian section on page 10 of the APPFORM.
    """
    for img_path in image_paths:
        text = quick_text_scan(img_path).lower()
        if any(hint.lower() in text for hint in hint_keywords):
            return img_path
    return None