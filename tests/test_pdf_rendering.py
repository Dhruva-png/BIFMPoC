import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PIL import Image

from app.ocr import pdf_utils
from config.settings import settings


def _make_pdf(path: Path, n_pages: int) -> None:
    """Builds a tiny real multi-page PDF via Pillow so rendering tests
    exercise the actual pypdfium2 path, not a mock."""
    pages = [Image.new("RGB", (80, 120), color=(255, 255, 255)) for _ in range(n_pages)]
    pages[0].save(path, "PDF", save_all=True, append_images=pages[1:])


def test_selective_page_numbers_only_renders_those_pages(tmp_path, monkeypatch):
    monkeypatch.setattr(settings.paths, "temp_dir", tmp_path)
    pdf_path = tmp_path / "ten_pages.pdf"
    _make_pdf(pdf_path, 10)

    rendered = pdf_utils.render_pdf_to_images(pdf_path, page_numbers=[1, 10])

    assert set(rendered.keys()) == {1, 10}
    assert rendered[1].exists()
    assert rendered[10].exists()
    # Pages 2-9 must never have been rasterized/saved at all.
    target_dir = tmp_path / pdf_path.stem
    for n in range(2, 10):
        assert not (target_dir / f"page_{n:03d}.png").exists()


def test_no_page_numbers_renders_every_page(tmp_path, monkeypatch):
    monkeypatch.setattr(settings.paths, "temp_dir", tmp_path)
    pdf_path = tmp_path / "three_pages.pdf"
    _make_pdf(pdf_path, 3)

    rendered = pdf_utils.render_pdf_to_images(pdf_path)

    assert set(rendered.keys()) == {1, 2, 3}


def test_requesting_a_page_beyond_the_document_is_silently_omitted(tmp_path, monkeypatch):
    monkeypatch.setattr(settings.paths, "temp_dir", tmp_path)
    pdf_path = tmp_path / "one_page.pdf"
    _make_pdf(pdf_path, 1)

    rendered = pdf_utils.render_pdf_to_images(pdf_path, page_numbers=[1, 10])

    assert set(rendered.keys()) == {1}
