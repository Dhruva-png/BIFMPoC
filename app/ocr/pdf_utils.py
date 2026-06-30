"""
Converts PDF pages to images for the vision model (llava / qwen-vl).

Renderer: pypdfium2 (ships its own PDF engine in the wheel — no system
dependency, unlike pdf2image which wraps Poppler CLI tools). Falls back to
pdf2image + Poppler if pypdfium2 is unavailable.

Also provides an optional Tesseract raw-text pass used only as a lightweight
cross-check for classification keyword hints — not the primary extraction path.
"""
from __future__ import annotations

import shutil
from pathlib import Path

from PIL import Image, ImageEnhance, ImageOps

from app.utils.logger import get_logger
from config.settings import settings

logger = get_logger(__name__)

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

def _render_with_pypdfium2(pdf_path: Path, target_dir: Path) -> list[Path]:
    doc = pdfium.PdfDocument(str(pdf_path))
    image_paths: list[Path] = []
    scale = settings.pdf_render_dpi / 72.0
    for i, page in enumerate(doc, start=1):
        bitmap = page.render(scale=scale, rotation=0)
        pil_img = bitmap.to_pil()
        img_path = target_dir / f"page_{i:03d}.png"
        if settings.ocr_enhance_images:
            pil_img = _enhance_for_handwriting(pil_img)
        pil_img.save(img_path, "PNG")
        image_paths.append(img_path)
    doc.close()
    return image_paths


def _render_with_pdf2image(pdf_path: Path, target_dir: Path) -> list[Path]:
    from pdf2image import convert_from_path  # type: ignore[import]
    pages = convert_from_path(str(pdf_path), dpi=settings.pdf_render_dpi)
    image_paths: list[Path] = []
    for i, pil_img in enumerate(pages, start=1):
        img_path = target_dir / f"page_{i:03d}.png"
        if settings.ocr_enhance_images:
            pil_img = _enhance_for_handwriting(pil_img)
        pil_img.save(img_path, "PNG")
        image_paths.append(img_path)
    return image_paths


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def render_pdf_to_images(pdf_path: Path, output_subdir: str | None = None) -> list[Path]:
    """
    Renders every page of a PDF to a PNG under settings.paths.temp_dir,
    applying handwriting-friendly enhancement unless ocr_enhance_images is off.
    Returns image paths in page order.
    """
    if not pdf_path.exists():
        raise FileNotFoundError(f"PDF not found: {pdf_path}")

    target_dir = settings.paths.temp_dir / (output_subdir or pdf_path.stem)
    target_dir.mkdir(parents=True, exist_ok=True)

    logger.info("Rendering %s at %d DPI via %s", pdf_path.name, settings.pdf_render_dpi, _RENDERER)

    try:
        if _RENDERER == "pypdfium2":
            image_paths = _render_with_pypdfium2(pdf_path, target_dir)
        else:
            image_paths = _render_with_pdf2image(pdf_path, target_dir)
    except Exception as exc:
        raise ValueError(
            f"Could not render {pdf_path.name} — file may be corrupt or password-protected: {exc}"
        ) from exc

    if not image_paths:
        raise ValueError(f"No pages rendered from {pdf_path.name}")

    logger.info("Rendered %d page(s) from %s", len(image_paths), pdf_path.name)
    return image_paths


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
