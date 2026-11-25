"""Tesseract OCR (plan D4; Apache-2.0). Called as a subprocess by pytesseract, so it is safe to use from several
threads at once (unlike pdfium). Needs the `tesseract` program: `brew install tesseract`, or `tesseract-ocr` in the
Docker image."""
from __future__ import annotations
from dataclasses import dataclass

import pytesseract
from PIL import Image

from .pdf import TextLine

MIN_CONF = 30


@dataclass
class Word:
    text: str
    conf: float
    left: int                  # pixels in the full image
    top: int
    width: int
    height: int


def words(img: Image.Image, box: tuple[int, int, int, int] | None = None) -> list[Word]:
    """Words found anywhere in `img` (or inside `box`), with positions in the full image. Sparse-text mode, since
    drawings are mostly lines with scattered labels."""
    ox, oy = (box[0], box[1]) if box else (0, 0)
    crop = img.crop(box) if box else img
    d = pytesseract.image_to_data(crop, config='--psm 11', output_type=pytesseract.Output.DICT)
    out = []
    for i, t in enumerate(d['text']):
        t = t.strip()
        if t and float(d['conf'][i]) >= MIN_CONF:
            out.append(Word(t, float(d['conf'][i]), d['left'][i] + ox, d['top'][i] + oy, d['width'][i], d['height'][i]))
    return out


def lines(img: Image.Image, box: tuple[int, int, int, int] | None = None) -> list[TextLine]:
    """OCR text grouped into lines, as `TextLine`s in page fractions — the same shape the PDF text layer gives, so
    the title-block reader works on either."""
    ox, oy = (box[0], box[1]) if box else (0, 0)
    crop = img.crop(box) if box else img
    W, H = img.size
    d = pytesseract.image_to_data(crop, config='--psm 11', output_type=pytesseract.Output.DICT)
    groups: dict[tuple, list[int]] = {}
    for i, t in enumerate(d['text']):
        if t.strip() and float(d['conf'][i]) >= MIN_CONF:
            groups.setdefault((d['block_num'][i], d['par_num'][i], d['line_num'][i]), []).append(i)
    out = []
    for idx in groups.values():
        l = min(d['left'][i] for i in idx) + ox
        t = min(d['top'][i] for i in idx) + oy
        r = max(d['left'][i] + d['width'][i] for i in idx) + ox
        b = max(d['top'][i] + d['height'][i] for i in idx) + oy
        out.append(TextLine(' '.join(d['text'][i].strip() for i in idx), l / W, t / H, r / W, b / H))
    return out
