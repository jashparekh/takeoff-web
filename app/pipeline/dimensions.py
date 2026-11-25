"""Clear spans from the dimensions drawn on a beam layout (a follow-up to M3 PR 3).

The consultant writes each panel's clear span as a dimension string (16'-2") on a dimension line between the beam
faces. That is the number a QS takes off, and what the pilot's hand-entered spans follow; the geometric span from
beam strips is only within 1" of it on about a fifth of the pilot's beams.

  1. read dimension strings from the lettering-only render (three sizes, upright and turned for vertical ones);
  2. find each one's dimension line: the longest straight segment just beside the text, running past it;
  3. keep it only if the line's length agrees with the written value at the sheet's scale (so a misread is dropped);
  4. give it to the nearest parallel beam whose span covers it.
"""
from __future__ import annotations
from dataclasses import dataclass

import pypdfium2.raw as R
import pytesseract

from . import geometry as g
from .calibrate import parse_dim
from .layout import LABEL_DPIS, render_only

AGREE_IN, AGREE_FRAC = 3.0, 0.04       # written value vs measured line length
NEAR_BEAM_IN = 75                      # a span's dimension sits within ~6 ft of its beam (inside the panel)
GLYPH_PT = 12                          # paths larger than this are lines, not lettering: left out of the OCR render
SUPPORTS_IN = 30                       # clear span = centre-to-centre less half of each support: at most ~30" less


@dataclass
class Dim:
    value_in: float
    vertical: bool                     # dimension runs up/down the sheet
    c: float                           # dimension line position across, pt
    a: float                           # extent along, pt
    b: float
    text: str
    reads: int = 1
    conflict: bool = False             # read as two different values: dropped


def ocr_dims(img, dpi: int) -> list[tuple[str, bool, tuple]]:
    """Dimension strings with their boxes in sheet points; `vertical` for text read bottom to top."""
    out, k = [], 72 / dpi
    for turned in (False, True):
        im = img.rotate(-90, expand=True) if turned else img
        d = pytesseract.image_to_data(im, config='--psm 11', output_type=pytesseract.Output.DICT)
        for i, text in enumerate(d['text']):
            if parse_dim(text) is None:
                continue
            l, t, w, h = d['left'][i], d['top'][i], d['width'][i], d['height'][i]
            if turned:
                x0, y0, x1, y1 = t, im.width - (l + w), t + h, im.width - l
            else:
                x0, y0, x1, y1 = l, t, l + w, t + h
            out.append((text, turned, (x0 * k, y0 * k, x1 * k, y1 * k)))
    return out


def segments(vec: g.Vectors) -> tuple[list, list]:
    """Every straight horizontal (y, x0, x1) and vertical (x, y0, y1) segment, any stroke width."""
    H, V = [], []
    for p in vec.paths:
        for (_, x1, y1), (t, x2, y2) in zip(p.points, p.points[1:]):
            if t != R.FPDF_SEGMENT_LINETO:
                continue
            if abs(y1 - y2) < 0.3 and abs(x1 - x2) > 2:
                H.append(((y1 + y2) / 2, min(x1, x2), max(x1, x2)))
            elif abs(x1 - x2) < 0.3 and abs(y1 - y2) > 2:
                V.append(((x1 + x2) / 2, min(y1, y2), max(y1, y2)))
    return H, V


def dim_line(vertical: bool, box: tuple, H: list, V: list) -> tuple | None:
    """The dimension line for a string: the longest segment parallel to it, just beside it (≤ 6 pt off the text's
    side), that runs past both ends of the text."""
    x0, y0, x1, y1 = box
    best = None
    if not vertical:
        cx = (x0 + x1) / 2
        for y, a, b in H:
            if (y1 - 1.5 <= y <= y1 + 6 or y0 - 6 <= y <= y0 + 1.5) and a <= x0 + 1 and b >= x1 - 1 and a <= cx <= b:
                if best is None or b - a > best[2] - best[1]:
                    best = (y, a, b)
    else:
        cy = (y0 + y1) / 2
        for x, a, b in V:
            if (x1 - 1.5 <= x <= x1 + 6 or x0 - 6 <= x <= x0 + 1.5) and a <= y0 + 1 and b >= y1 - 1 and a <= cy <= b:
                if best is None or b - a > best[2] - best[1]:
                    best = (x, a, b)
    return best


def line_offset(cands: list[tuple]) -> float:
    """How much shorter the drawn dimension lines are than their written values on this sheet. Draughtsmen often stop
    the line short of the extension lines by a fixed amount (≈ 8" on the pilot), so the gap is an offset, not a scale
    error. The median gap of the plausible pairs; 0 when there are too few to tell."""
    gaps = sorted(v - m for v, m in cands if -AGREE_IN <= v - m <= 15)
    return gaps[len(gaps) // 2] if len(gaps) >= 3 else 0.0


def read_dims(path: str, page_no: int, vec: g.Vectors, lettering_w: float, pt_per_in: float) -> list[Dim]:
    H, V = segments(vec)
    raw = []
    for dpi in LABEL_DPIS:
        img = render_only(path, page_no, lettering_w, dpi, max_size=GLYPH_PT)
        for text, vertical, box in ocr_dims(img, dpi):
            line = dim_line(vertical, box, H, V)
            if line is not None:
                raw.append((parse_dim(text), (line[2] - line[1]) / pt_per_in, vertical, line, text))
    off = line_offset([(v, m) for v, m, *_ in raw])
    found: list[Dim] = []
    for value, measured, vertical, line, text in raw:
        if abs(value - measured) > AGREE_IN and abs(value - measured - off) > max(AGREE_IN, AGREE_FRAC * value):
            continue                                      # misread, or not this line: drop rather than guess
        same = next((d for d in found if d.vertical == vertical and abs(d.c - line[0]) < 1
                     and abs(d.a - line[1]) < 1.5 and abs(d.b - line[2]) < 1.5), None)
        if same:
            same.reads += 1
            if same.value_in != value:
                same.conflict = True
        else:
            found.append(Dim(value, vertical, line[0], line[1], line[2], text))
    return [d for d in found if not d.conflict]


def assign(dims: list[Dim], beams: list, pt_per_in: float) -> dict[int, Dim]:
    """Beam index → its span's dimension. A panel's dimension gives the clear span of the parallel beams on both of
    its sides, so each beam takes the nearest parallel dimension that lies within its span (allowing for the line
    stopping short) and is at most two half-supports less than its centre-to-centre length."""
    near = NEAR_BEAM_IN * pt_per_in
    out: dict[int, Dim] = {}
    for i, b in enumerate(beams):
        if b.cantilever:
            continue
        c = b.p1[0] if b.vertical else b.p1[1]
        e1, e2 = sorted((b.p1[1], b.p2[1]) if b.vertical else (b.p1[0], b.p2[0]))
        c2c = (e2 - e1) / pt_per_in
        best = None
        for d in dims:
            if d.vertical != b.vertical:
                continue
            dist = abs(d.c - c)
            if dist > near or d.a < e1 - 2 or d.b > e2 + 2 or not (c2c - SUPPORTS_IN <= d.value_in <= c2c + 1):
                continue
            key = (dist, -d.reads)
            if best is None or key < best[0]:
                best = (key, d)
        if best:
            out[i] = best[1]
    return out
