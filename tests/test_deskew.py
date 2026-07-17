"""
Tests for the projection-profile deskew in app.ocr.pdf_utils.

A BIFM form page is full of long horizontal rules, so a synthetic page of
horizontal bars is a faithful stand-in: rotate it by a known angle and the
estimator should propose (approximately) the opposite rotation to undo it.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PIL import Image, ImageDraw

from app.ocr.pdf_utils import _DESKEW_MIN_CORRECTION, _estimate_skew_angle


def _form_like_page(width=1000, height=700):
    """White page with horizontal black rules, like a form's field lines."""
    img = Image.new("L", (width, height), 255)
    draw = ImageDraw.Draw(img)
    for y in range(60, height - 40, 50):
        draw.line([(40, y), (width - 40, y)], fill=0, width=3)
    return img


def test_straight_page_needs_no_correction():
    angle = _estimate_skew_angle(_form_like_page())
    assert abs(angle) < _DESKEW_MIN_CORRECTION


def test_tilted_page_estimate_undoes_the_tilt():
    # PIL's rotate() is counterclockwise: a page skewed by +1.5deg should
    # produce a correction estimate of about -1.5deg.
    skewed = _form_like_page().rotate(1.5, fillcolor=255)
    angle = _estimate_skew_angle(skewed)
    assert abs(angle - (-1.5)) <= 0.5


def test_tilt_in_the_other_direction():
    skewed = _form_like_page().rotate(-2.0, fillcolor=255)
    angle = _estimate_skew_angle(skewed)
    assert abs(angle - 2.0) <= 0.5
