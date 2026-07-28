"""OCR text reading for already-detected candidate boxes.

Deliberately narrow scope: this module never creates candidate boxes
itself (that's agents/gui/detection.py's job, from strokes/fills only).
It only reads the text *inside* boxes that detection.py already kept, so
the Vision prompt can be given "element N: <text>" hints without the
Vision model having to re-read tiny crops itself.

Requires the Tesseract OCR binary on PATH (via pytesseract). If it's not
installed, every box just gets an empty string back -- callers should
treat missing OCR text as "unknown", not as an error.
"""

from __future__ import annotations

import logging

from PIL import Image

from agents.gui.detection import BoundingBox

logger = logging.getLogger("pipeline")

_tesseract_checked = False
_tesseract_available = False


def _tesseract_ready() -> bool:
    global _tesseract_checked, _tesseract_available
    if _tesseract_checked:
        return _tesseract_available

    _tesseract_checked = True
    try:
        import pytesseract

        pytesseract.get_tesseract_version()
        _tesseract_available = True
    except Exception as exc:
        logger.warning(
            "GUI: Tesseract OCR not available (%s) -- element text descriptions will be empty", exc
        )
        _tesseract_available = False
    return _tesseract_available


def read_element_text(image: Image.Image, box: BoundingBox, padding: int = 2) -> str:
    """OCR just the crop for one box. Returns "" if OCR isn't available
    or nothing was recognized."""
    if not _tesseract_ready():
        return ""

    import pytesseract

    left = max(box.x - padding, 0)
    top = max(box.y - padding, 0)
    right = min(box.x + box.width + padding, image.width)
    bottom = min(box.y + box.height + padding, image.height)
    crop = image.crop((left, top, right, bottom))

    text = pytesseract.image_to_string(crop, lang="kor+eng")
    return text.strip()


def read_element_texts(image: Image.Image, boxes: list[BoundingBox]) -> dict[int, str]:
    return {box.index: read_element_text(image, box) for box in boxes}
