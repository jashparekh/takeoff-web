"""Read a beam layout: beam labels, the beam strips they name, and columns (plan M2 step 2).

Labels: CAD lettering is usually drawn as thin strokes on top of beam lines, which defeats OCR on a normal render.
So the page is rendered with only the lettering's strokes (found by stroke width and glyph size), then OCR'd at
200, 250 and 300 dpi, upright and turned for vertical labels; the union is kept (on Work Lounge this reads 89 of 91
labels; any single pass reads at most 80). If the PDF has a real text layer, that is used instead.

Matching (before schedules are read in M3): a label goes to the nearest parallel strip whose extent covers it, and
to the span of that strip between the supports either side of the label (crossing beams and columns). Every
member carries a confidence; low ones go to the Review queue (M2 PR 2).
"""
from __future__ import annotations
import ctypes
import math
import re
import statistics
from collections import defaultdict
from dataclasses import dataclass, field

import pypdfium2.raw as R
import pytesseract
from PIL import Image

from . import geometry as g
from .pdf import PDFIUM, open_pdf

LABEL = re.compile(r'((?:H?B|CB)\d{1,3}[A-Z]?)(?:\(\s*(\d{1,2})\D{0,3}?[X×]\s*(\d{1,2}))?')
LABEL_DPIS = (200, 250, 300)
NEAR_PT_IN = 27          # a label sits within ~27 inches (16 pt at 1:120) of its beam's centreline


# ---------- labels ----------

@dataclass
class Label:
    name: str
    vertical: bool
    x: float                 # centre, pt
    y: float
    box: tuple               # x0, y0, x1, y1, pt
    b_in: int | None = None  # size printed with the label, e.g. B13(9"X18")
    d_in: int | None = None
    reads: int = 1           # how many OCR passes found it


def lettering_width(vec: g.Vectors, edge_width: float) -> float | None:
    """The stroke width used for lettering: most paths whose size is glyph-like (1–10 pt), not the beam edges."""
    count: dict[float, int] = defaultdict(int)
    for p in vec.paths:
        if p.width <= 0 or abs(p.width - edge_width) < 0.005 or len(p.points) < 2:
            continue
        xs, ys = [x for _, x, _ in p.points], [y for _, _, y in p.points]
        size = max(max(xs) - min(xs), max(ys) - min(ys))
        if 1 <= size <= 10:
            count[p.width] += 1
    return max(count, key=count.get) if count else None


def render_only(path: str, page_no: int, width: float, dpi: int) -> Image.Image:
    """Render the page with only the paths of one stroke width (everything else removed from the in-memory copy)."""
    with PDFIUM:
        pdf = open_pdf(path)
        try:
            page = pdf[page_no - 1]
            m, w = R.FS_MATRIX(), ctypes.c_float()
            for o in list(page.get_objects(max_depth=1)):
                if o.type == R.FPDF_PAGEOBJ_PATH:
                    R.FPDFPageObj_GetMatrix(o.raw, m)
                    R.FPDFPageObj_GetStrokeWidth(o.raw, w)
                    if abs(w.value * math.sqrt(abs(m.a * m.d - m.b * m.c)) - width) < 0.005:
                        continue
                page.remove_obj(o)
            img = page.render(scale=dpi / 72, grayscale=True).to_pil().convert('L')
            page.close()
            return img
        finally:
            pdf.close()


def ocr_labels(img: Image.Image, dpi: int) -> list[Label]:
    """Labels in one render, upright and turned a quarter clockwise (vertical labels read bottom to top)."""
    out = []
    W, H = img.size
    k = 72 / dpi
    for turned in (False, True):
        im = img.rotate(-90, expand=True) if turned else img
        d = pytesseract.image_to_data(im, config='--psm 11', output_type=pytesseract.Output.DICT)
        for i, text in enumerate(d['text']):
            for m in LABEL.finditer(text.upper().replace(' ', '')):
                l, t, w, h = d['left'][i], d['top'][i], d['width'][i], d['height'][i]
                if turned:                       # back to the upright page: (x, y) in turned → (y, W' - x)
                    x0, y0, x1, y1 = t, im.width - (l + w), t + h, im.width - l
                else:
                    x0, y0, x1, y1 = l, t, l + w, t + h
                box = (x0 * k, y0 * k, x1 * k, y1 * k)
                b, dd = (int(m.group(2)), int(m.group(3))) if m.group(2) else (None, None)
                out.append(Label(m.group(1), turned, (box[0] + box[2]) / 2, (box[1] + box[3]) / 2, box, b, dd))
    return out


def text_layer_labels(lines) -> list[Label]:
    out = []
    for t in lines:
        for m in LABEL.finditer(t.text.upper().replace(' ', '')):
            vertical = (t.y1 - t.y0) > (t.x1 - t.x0) * 1.5
            b, d = (int(m.group(2)), int(m.group(3))) if m.group(2) else (None, None)
            out.append(Label(m.group(1), vertical, 0, 0, (t.x0, t.y0, t.x1, t.y1), b, d))
    return out


def merge_labels(found: list[Label], tol: float = 10) -> list[Label]:
    """The same label read in several passes → one, at the average position. Same name far apart stays separate
    (CB1 is drawn on several chajja edges)."""
    keep: list[Label] = []
    for lb in found:
        k = next((k for k in keep if k.name == lb.name and k.vertical == lb.vertical
                  and abs(k.x - lb.x) < tol and abs(k.y - lb.y) < tol), None)
        if k is None:
            keep.append(Label(lb.name, lb.vertical, lb.x, lb.y, lb.box, lb.b_in, lb.d_in))
            continue
        n = k.reads
        k.x, k.y, k.reads = (k.x * n + lb.x) / (n + 1), (k.y * n + lb.y) / (n + 1), n + 1
        k.b_in, k.d_in = k.b_in or lb.b_in, k.d_in or lb.d_in
    return keep


def read_labels(path: str, page_no: int, vec: g.Vectors, edge_width: float) -> list[Label]:
    w = lettering_width(vec, edge_width)
    found: list[Label] = []
    for dpi in LABEL_DPIS:
        img = render_only(path, page_no, w, dpi) if w else None
        if img is None:
            from .pdf import render_gray
            img, dpi = render_gray(path, page_no, dpi)
        found += ocr_labels(img, dpi)
    return merge_labels(found)


# ---------- members ----------

@dataclass
class Beam:
    name: str
    vertical: bool
    p1: tuple                  # centreline ends, pt (support centre to support centre)
    p2: tuple
    b_in: float | None         # width measured between the edge lines
    label: Label
    confidence: float
    note: str | None = None
    label_b_in: int | None = None
    label_d_in: int | None = None


@dataclass
class Layout:
    beams: list[Beam] = field(default_factory=list)
    columns: list[g.Rect] = field(default_factory=list)
    unplaced: list[Label] = field(default_factory=list)
    strips: list[g.Strip] = field(default_factory=list)
    edge_width: float | None = None


def cuts(s: g.Strip, strips: list[g.Strip], cols: list[g.Rect]) -> tuple[list[float], list[float]]:
    """Positions along strip `s` where a span may end.
    Hard: columns, and perpendicular beams that pass right across `s` (+).
    Soft: perpendicular beams that only frame into `s` from one side (T) — usually they rest on `s` and do not
    split it, but where two different labels share a span, the span is split at a soft cut between them."""
    hard, soft = {s.a, s.b}, set()
    half = s.width_pt / 2
    for p in strips:
        if p.vertical == s.vertical or not (s.a - 3 <= p.c <= s.b + 3):
            continue
        through = p.a < s.c - half - 3 and p.b > s.c + half + 3
        touches = p.a - 3 <= s.c + half and p.b + 3 >= s.c - half
        if through:
            hard.add(p.c)
        elif touches:
            soft.add(p.c)
    for r in cols:
        across, along = (r.cx, r.cy) if s.vertical else (r.cy, r.cx)
        if abs(across - s.c) < 6 and s.a - 3 <= along <= s.b + 3:
            hard.add(along)
    return sorted(hard), sorted(soft - hard)


def match(labels: list[Label], strips: list[g.Strip], cols: list[g.Rect], pt_per_in: float) -> Layout:
    near = NEAR_PT_IN * pt_per_in
    out = Layout(columns=cols, strips=strips)
    on: dict[int, list] = defaultdict(list)          # strip index → [(along, distance, label)]
    for lb in labels:
        along, across = (lb.y, lb.x) if lb.vertical else (lb.x, lb.y)
        best = None
        for i, s in enumerate(strips):
            if s.vertical != lb.vertical:
                continue
            d = abs(s.c - across)
            if d > near or not (s.a - 10 <= along <= s.b + 10):
                continue
            if best is None or d < best[0]:
                best = (d, i)
        if best is None:
            out.unplaced.append(lb)
        else:
            on[best[1]].append((along, best[0], lb))

    for i, items in on.items():
        s = strips[i]
        hard, soft = cuts(s, strips, cols)
        spans: dict[tuple, list] = defaultdict(list)    # hard span → labels in it
        for along, d, lb in items:
            a = max((c for c in hard if c <= along), default=s.a)
            b = min((c for c in hard if c > along), default=s.b)
            spans[(a, b)].append((along, d, lb))
        for (a, b), inside in spans.items():
            inside.sort(key=lambda t: t[0])
            names = [lb.name for _, _, lb in inside]
            bounds = [a]
            for (x1, _, l1), (x2, _, l2) in zip(inside, inside[1:]):
                if l1.name == l2.name:
                    continue
                between = [c for c in soft if x1 < c < x2]
                if between:                              # split where the two labels' beams meet
                    bounds.append(min(between, key=lambda c: abs(c - (x1 + x2) / 2)))
            bounds.append(b)
            for along, d, lb in inside:
                k = max(j for j in range(len(bounds) - 1) if bounds[j] <= along or j == 0)
                lo, hi = bounds[k], bounds[k + 1]
                p1, p2 = ((s.c, lo), (s.c, hi)) if s.vertical else ((lo, s.c), (hi, s.c))
                conf = round(max(0.0, 1 - d / near) * 0.5 + min(lb.reads, 3) / 3 * 0.5, 2)
                note = None
                if lb.b_in and abs(s.width_in(pt_per_in) - lb.b_in) > 1.5:
                    conf, note = min(conf, 0.4), f'Drawn {s.width_in(pt_per_in):.1f}" wide, label says {lb.b_in}".'
                if len(set(names)) > 1 and len(bounds) - 1 < len(set(names)):
                    conf = min(conf, 0.5)
                    note = note or 'Shares a span with another label and no support between them was found.'
                out.beams.append(Beam(lb.name, s.vertical, p1, p2, round(s.width_in(pt_per_in), 1), lb, conf, note,
                                      lb.b_in, lb.d_in))
    return out


def read_layout(path: str, page_no: int, pt_per_in: float) -> Layout:
    vec = g.read_vectors(path, page_no)
    edges = g.beam_edges(vec, pt_per_in)
    if edges is None:
        return Layout()
    cols = g.columns(vec, pt_per_in, edges.width)
    labels = read_labels(path, page_no, vec, edges.width)
    lay = match(labels, edges.strips, cols, pt_per_in)
    lay.edge_width = edges.width
    return lay


def label_stats(labels: list[Label]) -> dict:
    return dict(labels=len(labels), names=len({lb.name for lb in labels}),
                median_reads=statistics.median([lb.reads for lb in labels]) if labels else 0)
