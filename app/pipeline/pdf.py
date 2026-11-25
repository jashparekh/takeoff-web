"""Reading and drawing PDF pages with pypdfium2 (Apache-2.0 / BSD-3; plan decision D1).

Runs in the worker only, never in the web process (plan §5: PDF parsing is isolated from the API).
pdfium is not thread-safe: two threads inside it at once crash the process. Every call here holds `PDFIUM`, so a
worker running several jobs only overlaps their downloads and uploads, never their PDF work.
"""
from __future__ import annotations
import hashlib
import io
import threading
from dataclasses import dataclass

import pypdfium2 as pdfium
from PIL import Image

DISPLAY_DPI, OCR_DPI, THUMB_W = 300, 300, 480
DISPLAY_MAX_PX, OCR_MAX_PX = 7000, 14000      # cap the long side so an A0 sheet doesn't blow up worker memory


PDFIUM = threading.Lock()


class BadPdf(Exception):
    """The file can't be used. The message is shown to the user."""


@dataclass
class PageInfo:
    page_no: int          # 1-based
    width_pt: float       # as displayed (page rotation applied)
    height_pt: float
    page_hash: str        # fingerprint of what the page shows; same drawing in another upload → same hash


def open_pdf(path: str) -> pdfium.PdfDocument:
    try:
        return pdfium.PdfDocument(path)
    except pdfium.PdfiumError as e:
        if 'password' in str(e).lower():
            raise BadPdf('This PDF is password-protected. Save it without a password and upload it again.') from e
        raise BadPdf('This file could not be read as a PDF.') from e


def page_size(page: pdfium.PdfPage) -> tuple[float, float]:
    w, h = page.get_size()
    return (h, w) if page.get_rotation() in (90, 270) else (w, h)


def page_hash(page: pdfium.PdfPage) -> str:
    """SHA-256 of a small grayscale render plus the page size. Byte-level PDF hashes differ between exports of the
    same drawing (ids, timestamps); what the page shows doesn't."""
    w, h = page_size(page)
    img = page.render(scale=36 / 72, grayscale=True).to_pil()
    return hashlib.sha256(f'{w:.1f}x{h:.1f}:'.encode() + img.tobytes()).hexdigest()


def inspect(path: str, max_pages: int) -> list[PageInfo]:
    with PDFIUM:
        return _inspect(path, max_pages)


def _inspect(path: str, max_pages: int) -> list[PageInfo]:
    pdf = open_pdf(path)
    try:
        n = len(pdf)
        if n == 0:
            raise BadPdf('This PDF has no pages.')
        if n > max_pages:
            raise BadPdf(f'This PDF has {n} pages; the limit is {max_pages}. Split it and upload the parts.')
        out = []
        for i in range(n):
            page = pdf[i]
            w, h = page_size(page)
            out.append(PageInfo(i + 1, w, h, page_hash(page)))
            page.close()
        return out
    finally:
        pdf.close()


def dpi_for(w_pt: float, h_pt: float, want: int, max_px: int) -> int:
    return max(36, min(want, int(max_px * 72 / max(w_pt, h_pt))))


def png(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, 'PNG', optimize=False, compress_level=6)
    return buf.getvalue()


@dataclass
class Rendered:
    display: bytes
    display_dpi: int
    thumb: bytes
    ocr: bytes
    ocr_dpi: int


def render_page(path: str, page_no: int) -> Rendered:
    """Viewer image (colour, 300 dpi up to 7000 px), thumbnail, and a 300 dpi grayscale image for OCR. One page at a time."""
    with PDFIUM:
        return _render_page(path, page_no)


def _render_page(path: str, page_no: int) -> Rendered:
    pdf = open_pdf(path)
    try:
        page = pdf[page_no - 1]
        w, h = page_size(page)
        ddpi = dpi_for(w, h, DISPLAY_DPI, DISPLAY_MAX_PX)
        disp = page.render(scale=ddpi / 72).to_pil().convert('RGB')
        thumb = disp.copy()
        thumb.thumbnail((THUMB_W, THUMB_W * 4))
        display, thumb_png = png(disp), png(thumb)
        del disp, thumb
        odpi = dpi_for(w, h, OCR_DPI, OCR_MAX_PX)
        ocr = png(page.render(scale=odpi / 72, grayscale=True).to_pil().convert('L'))
        page.close()
        return Rendered(display, ddpi, thumb_png, ocr, odpi)
    finally:
        pdf.close()


@dataclass
class TextLine:
    text: str
    x0: float             # fractions of the page as displayed, origin top-left
    y0: float
    x1: float
    y1: float


def text_lines(path: str, page_no: int) -> list[TextLine]:
    """The page's text layer, one entry per text run. CAD exports often keep the title block as text while the
    drawing itself is outlines; scanned sheets have no text at all (those need OCR, a later stage)."""
    with PDFIUM:
        pdf = open_pdf(path)
        try:
            page = pdf[page_no - 1]
            w, h = page.get_size()
            tp = page.get_textpage()
            out = []
            for i in range(tp.count_rects()):
                l, b, r, t = tp.get_rect(i)
                s = ' '.join(tp.get_text_bounded(l, b, r, t).split())
                if s:
                    out.append(TextLine(s, l / w, 1 - t / h, r / w, 1 - b / h))
            tp.close()
            rot = page.get_rotation()
            page.close()
        finally:
            pdf.close()
    return [rotate(x, rot) for x in out]


def rotate(t: TextLine, rot: int) -> TextLine:
    """Positions above are in the unrotated page; map them to the page as it is shown."""
    if rot == 90:
        return TextLine(t.text, 1 - t.y1, t.x0, 1 - t.y0, t.x1)
    if rot == 180:
        return TextLine(t.text, 1 - t.x1, 1 - t.y1, 1 - t.x0, 1 - t.y0)
    if rot == 270:
        return TextLine(t.text, t.y0, 1 - t.x1, t.y1, 1 - t.x0)
    return t


def render_gray(path: str, page_no: int, dpi: int = OCR_DPI) -> tuple[Image.Image, int]:
    """Grayscale render for OCR and measuring, as displayed. Returns the image and the dpi used (capped for huge
    sheets)."""
    with PDFIUM:
        pdf = open_pdf(path)
        try:
            page = pdf[page_no - 1]
            w, h = page_size(page)
            d = dpi_for(w, h, dpi, OCR_MAX_PX)
            img = page.render(scale=d / 72, grayscale=True).to_pil().convert('L')
            page.close()
            return img, d
        finally:
            pdf.close()
