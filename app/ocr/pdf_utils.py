from __future__ import annotations

import threading
import time
from pathlib import Path

from PIL import Image, ImageEnhance, ImageFilter, ImageOps

from app.utils.logger import get_logger
from config.settings import settings

logger = get_logger(__name__)

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


_DESKEW_MAX_ANGLE = 3.0        # degrees each way - scanner tilt, not sideways pages
_DESKEW_STEP = 0.25
_DESKEW_MIN_CORRECTION = 0.3   # below this, rotating costs more (resample blur) than it fixes
_DESKEW_WORKING_WIDTH = 600    # angle estimation runs on a copy this wide


def _estimate_skew_angle(img: Image.Image) -> float:
    """Angle (degrees, PIL counterclockwise convention) that best aligns the
    page's ink into horizontal bands - i.e. the rotation that FIXES the
    skew. 0.0 when numpy is unavailable or the page has no usable signal."""
    try:
        import numpy as np
    except ImportError:  # numpy comes with pandas; belt-and-braces only
        return 0.0

    gray = ImageOps.grayscale(img)
    if gray.width > _DESKEW_WORKING_WIDTH:
        gray = gray.resize(
            (_DESKEW_WORKING_WIDTH, max(1, round(gray.height * _DESKEW_WORKING_WIDTH / gray.width)))
        )

    best_angle, best_score = 0.0, -1.0
    for angle in np.arange(-_DESKEW_MAX_ANGLE, _DESKEW_MAX_ANGLE + _DESKEW_STEP / 2, _DESKEW_STEP):
        rotated = gray.rotate(float(angle), fillcolor=255)
        ink = 255.0 - np.asarray(rotated, dtype=np.float32)
        profile = ink.sum(axis=1)
        # When text lines / form rules are horizontal, the row profile is
        # spiky (dense rows next to empty ones); when tilted, ink smears
        # across rows and the profile flattens. Sum of squared differences
        # between adjacent rows peaks at the correct alignment.
        score = float(np.square(np.diff(profile)).sum())
        if score > best_score:
            best_score, best_angle = score, float(angle)
    return best_angle


def _deskew(img: Image.Image) -> Image.Image:
    if not getattr(settings, "ocr_deskew", True):
        return img
    try:
        angle = _estimate_skew_angle(img)
    except Exception:  # noqa: BLE001 - deskew must never take rendering down
        logger.warning("Deskew estimation failed - using the page as scanned")
        return img
    if abs(angle) < _DESKEW_MIN_CORRECTION:
        return img
    logger.info("Deskewing page by %.2f degrees", angle)
    return img.rotate(angle, resample=Image.BICUBIC, fillcolor="white")


# ---------------------------------------------------------------------------
# Image enhancement
# ---------------------------------------------------------------------------

def _enhance_for_handwriting(img: Image.Image) -> Image.Image:
    """
    Lightweight preprocessing to help the vision model read handwriting:
    - Upscale small renders so faint/small handwriting has enough pixels.
    - Grayscale removes colour noise; autocontrast uses the full tonal range.
    - Median despeckle kills the salt-and-pepper dots scanners add, which
      otherwise sharpen into marks that read as stray punctuation/diacritics.
    - Unsharp mask (edge-aware, thresholded) crisps stroke edges without
      amplifying the flat background the way plain global sharpening does.
    """
    long_edge = max(img.size)
    if long_edge < settings.ocr_min_long_edge_px:
        scale = settings.ocr_min_long_edge_px / long_edge
        img = img.resize(
            (int(img.width * scale), int(img.height * scale)),
            Image.LANCZOS,
        )

    gray = ImageOps.grayscale(img)
    gray = gray.filter(ImageFilter.MedianFilter(3))
    gray = ImageOps.autocontrast(gray, cutoff=1)
    gray = gray.filter(ImageFilter.UnsharpMask(radius=2, percent=110, threshold=2))
    gray = ImageEnhance.Contrast(gray).enhance(1.15)
    return gray.convert("RGB")  # vision model expects RGB


# ---------------------------------------------------------------------------
# Rendering back-ends
# ---------------------------------------------------------------------------

def _save_enhanced(pil_img: Image.Image, page_num: int, target_dir: Path) -> Path:
    """Shared save step for both render backends: deskews, enhances (unless
    disabled) and writes one PNG per page, keyed by real 1-indexed page
    number so callers can look pages up out of order."""
    img_path = target_dir / f"page_{page_num:03d}.png"
    pil_img = _deskew(pil_img)
    out = _enhance_for_handwriting(pil_img) if settings.ocr_enhance_images else pil_img.convert("RGB")
    out.save(img_path, "PNG")
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
    off. Returns {page_number: image_path} - keyed by real page number (not
    list position) so a caller asking for pages [1, 10] can't misread the
    second entry as "page 2".

    Rendering only the pages a document's form type actually needs (rather
    than every page in the source PDF) matters in practice: a real APPFORM
    submission commonly runs to 10+ pages, but only a handful are ever read
    by app.services.extractor - rasterizing and enhancing the rest for
    every such document was pure wasted work repeated on every single
    submission. Called twice per document in practice (page 1 alone for
    classification, then whatever else that form type needs once
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

    # Retried once: the intake/output folders in practice live inside a
    # OneDrive-synced directory, and a brief sync-lock on the source PDF
    # (or a similar one-off I/O hiccup) can make a single render attempt
    # come back empty even though the file is perfectly fine - confirmed
    # by re-running the exact same call moments later with no other change
    # and getting a normal result. A short pause before the retry gives
    # whatever briefly held the file time to let go.
    last_exc: Exception | None = None
    image_paths: dict[int, Path] = {}
    for attempt in range(2):
        try:
            if _RENDERER == "pypdfium2":
                image_paths = _render_with_pypdfium2(pdf_path, target_dir, wanted)
            else:
                image_paths = _render_with_pdf2image(pdf_path, target_dir, wanted)
            if image_paths:
                break
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
        if attempt == 0:
            logger.warning("Render attempt 1 for %s produced nothing usable - retrying once", pdf_path.name)
            time.sleep(1.0)

    if not image_paths and last_exc is not None:
        raise ValueError(
            f"Could not render {pdf_path.name} — file may be corrupt or password-protected: {last_exc}"
        ) from last_exc
    if not image_paths:
        raise ValueError(f"No pages rendered from {pdf_path.name}")

    logger.info("Rendered %d page(s) from %s", len(image_paths), pdf_path.name)
    return image_paths