"""Vector geometry of a drawing page, read with pypdfium2 (plan D1), in PDF points as displayed (origin top-left).

Generalises `tools/extract_model.py`: nothing here assumes the Work Lounge sheet's 0.84 pt beam edges or 2.04 pt
columns. Beam edges are found as the stroke width whose lines pair up most at beam-width spacing; columns as small
closed rectangles drawn heavier than beam edges. Widths in real inches come from the sheet's scale (`pt_per_in`).
"""
from __future__ import annotations
import ctypes
import math
from collections import defaultdict
from dataclasses import dataclass, field

import pypdfium2 as pdfium
import pypdfium2.raw as R

from .pdf import PDFIUM, open_pdf

AXIS_TOL = 0.3          # pt: a segment this close to horizontal/vertical counts as such
MERGE_TOL = 0.35        # pt: collinear segments within this of each other share a line
BEAM_IN = (4.5, 15.0)   # plausible beam widths, inches
COLUMN_IN = (6.0, 48.0) # plausible column sides, inches


@dataclass
class Path:
    width: float                                   # stroke width, pt
    points: list[tuple[int, float, float]]         # (segment type, x, y)
    closed: bool
    filled: bool


@dataclass
class Vectors:
    width: float
    height: float
    paths: list[Path]

    def axis_lines(self, width: float) -> tuple[list, list]:
        """Horizontal and vertical straight segments of one stroke width: H = (y, x0, x1), V = (x, y0, y1)."""
        H, V = [], []
        for p in self.paths:
            if abs(p.width - width) > 0.005:
                continue
            for (_, x1, y1), (t2, x2, y2) in zip(p.points, p.points[1:]):
                if t2 != R.FPDF_SEGMENT_LINETO:
                    continue
                if abs(y1 - y2) < AXIS_TOL and abs(x1 - x2) > AXIS_TOL:
                    H.append((round((y1 + y2) / 2, 2), min(x1, x2), max(x1, x2)))
                elif abs(x1 - x2) < AXIS_TOL and abs(y1 - y2) > AXIS_TOL:
                    V.append((round((x1 + x2) / 2, 2), min(y1, y2), max(y1, y2)))
        return H, V

    def widths(self) -> dict[float, int]:
        """Stroke widths and how many axis-aligned straight segments each has."""
        n: dict[float, int] = defaultdict(int)
        for p in self.paths:
            for (_, x1, y1), (t2, x2, y2) in zip(p.points, p.points[1:]):
                if t2 == R.FPDF_SEGMENT_LINETO and (abs(y1 - y2) < AXIS_TOL or abs(x1 - x2) < AXIS_TOL):
                    n[p.width] += 1
        return dict(n)


def read_vectors(path: str, page_no: int) -> Vectors:
    """Every path on the page with its stroke width in points. Form XObjects are flattened (max_depth)."""
    with PDFIUM:
        pdf = open_pdf(path)
        try:
            page = pdf[page_no - 1]
            W, H = page.get_size()
            rot = page.get_rotation()
            m, x, y, w = R.FS_MATRIX(), ctypes.c_float(), ctypes.c_float(), ctypes.c_float()
            fill, stroke = ctypes.c_int(), ctypes.c_int()
            out = []
            for o in page.get_objects(max_depth=15):
                if o.type != R.FPDF_PAGEOBJ_PATH:
                    continue
                R.FPDFPageObj_GetMatrix(o.raw, m)
                R.FPDFPageObj_GetStrokeWidth(o.raw, w)
                R.FPDFPath_GetDrawMode(o.raw, fill, stroke)
                scale = math.sqrt(abs(m.a * m.d - m.b * m.c))
                pts, closed = [], False
                for i in range(R.FPDFPath_CountSegments(o.raw)):
                    s = R.FPDFPath_GetPathSegment(o.raw, i)
                    R.FPDFPathSegment_GetPoint(s, x, y)
                    px, py = m.a * x.value + m.c * y.value + m.e, m.b * x.value + m.d * y.value + m.f
                    pts.append((R.FPDFPathSegment_GetType(s), *to_display(px, py, W, H, rot)))
                    closed = closed or bool(R.FPDFPathSegment_GetClose(s))
                out.append(Path(round(w.value * scale, 2) if stroke.value else 0.0, pts, closed, fill.value != 0))
            page.close()
        finally:
            pdf.close()
    dw, dh = (H, W) if rot in (90, 270) else (W, H)
    return Vectors(dw, dh, out)


def to_display(x: float, y: float, W: float, H: float, rot: int) -> tuple[float, float]:
    """PDF user space (origin bottom-left) → displayed page, origin top-left, rotation applied (clockwise)."""
    X, Y = x, H - y
    if rot == 90:
        return H - Y, X
    if rot == 180:
        return W - X, H - Y
    if rot == 270:
        return Y, W - X
    return X, Y


# ---------- lines and strips ----------

def merge_lines(segs: list[tuple[float, float, float]], gap: float) -> dict[float, list[list[float]]]:
    """Collinear segments → runs. Dashed or broken edges are joined across gaps up to `gap` pt."""
    by: dict[float, list] = {}
    for c, a, b in sorted(segs):
        k = next((kk for kk in reversed(list(by)) if abs(kk - c) <= MERGE_TOL), c)
        by.setdefault(k, []).append((a, b))
    out = {}
    for c, iv in by.items():
        iv.sort()
        runs = [list(iv[0])]
        for a, b in iv[1:]:
            if a <= runs[-1][1] + gap:
                runs[-1][1] = max(runs[-1][1], b)
            else:
                runs.append([a, b])
        out[c] = runs
    return out


@dataclass
class Strip:
    """Two parallel edge lines at beam-width spacing: a beam (or part of one) seen in plan."""
    vertical: bool
    c: float           # centreline, pt (x for vertical strips, y for horizontal)
    a: float           # start along the strip, pt
    b: float           # end
    width_pt: float    # edge-to-edge spacing

    def width_in(self, pt_per_in: float) -> float:
        return self.width_pt / pt_per_in


def strips(segs, vertical: bool, lo: float, hi: float, gap: float, min_len: float = 6) -> list[Strip]:
    """Pairs of runs `lo`–`hi` pt apart that overlap for at least `min_len` pt. Nested duplicates are dropped."""
    L = merge_lines(segs, gap)
    keys = sorted(L)
    found = []
    for i, c1 in enumerate(keys):
        for c2 in keys[i + 1:]:
            if c2 - c1 > hi:
                break
            if c2 - c1 < lo:
                continue
            for a1, b1 in L[c1]:
                for a2, b2 in L[c2]:
                    a, b = max(a1, a2), min(b1, b2)
                    if b - a > min_len:
                        found.append(Strip(vertical, (c1 + c2) / 2, a, b, c2 - c1))
    found.sort(key=lambda s: -(s.b - s.a))
    keep: list[Strip] = []
    for s in found:
        if not any(abs(s.c - k.c) < 2 and s.a >= k.a - 2 and s.b <= k.b + 2 for k in keep):
            keep.append(s)
    return keep


@dataclass
class BeamEdges:
    width: float                  # stroke width of beam edges, pt
    H: list = field(default_factory=list)
    V: list = field(default_factory=list)
    strips: list[Strip] = field(default_factory=list)


def beam_edges(vec: Vectors, pt_per_in: float) -> BeamEdges | None:
    """The stroke width whose lines pair up into the most strip length at plausible beam widths."""
    lo, hi = BEAM_IN[0] * pt_per_in, BEAM_IN[1] * pt_per_in
    gap = 16 * pt_per_in                           # join dashes and gaps up to ~16 inches
    best = None
    for w, n in sorted(vec.widths().items(), key=lambda kv: -kv[1])[:8]:
        if w <= 0 or n < 50:
            continue
        H, V = vec.axis_lines(w)
        ss = strips(H, False, lo, hi, gap) + strips(V, True, lo, hi, gap)
        total = sum(s.b - s.a for s in ss)
        if best is None or total > best[0]:
            best = (total, BeamEdges(w, H, V, ss))
    return best[1] if best else None


# ---------- columns ----------

@dataclass
class Rect:
    x0: float
    y0: float
    x1: float
    y1: float
    width: float                  # stroke width, pt
    filled: bool

    @property
    def cx(self):
        return (self.x0 + self.x1) / 2

    @property
    def cy(self):
        return (self.y0 + self.y1) / 2


def rectangles(vec: Vectors) -> list[Rect]:
    """Closed paths whose points form an axis-aligned rectangle."""
    out = []
    for p in vec.paths:
        pts = [(x, y) for _, x, y in p.points]
        if len(pts) not in (4, 5) or any(t == R.FPDF_SEGMENT_BEZIERTO for t, _, _ in p.points):
            continue
        if len(pts) == 5 and math.dist(pts[0], pts[4]) > AXIS_TOL:
            continue
        if not (p.closed or len(pts) == 5):
            continue
        xs, ys = sorted({round(x, 1) for x, _ in pts[:4]}), sorted({round(y, 1) for _, y in pts[:4]})
        if len(xs) != 2 or len(ys) != 2:
            continue
        out.append(Rect(xs[0], ys[0], xs[1], ys[1], p.width, p.filled))
    return out


def columns(vec: Vectors, pt_per_in: float, edge_width: float) -> list[Rect]:
    """Small rectangles drawn heavier than beam edges (or filled), with sides of a plausible column size.
    Duplicates (the same column drawn twice) are merged."""
    lo, hi = COLUMN_IN[0] * pt_per_in, COLUMN_IN[1] * pt_per_in
    cand = [r for r in rectangles(vec)
            if lo <= min(r.x1 - r.x0, r.y1 - r.y0) and max(r.x1 - r.x0, r.y1 - r.y0) <= hi
            and (r.width >= edge_width * 1.5 or r.filled)]
    if not cand:
        return []
    # The column class is the stroke width (or fill) with the most such rectangles.
    by: dict = defaultdict(list)
    for r in cand:
        by[(r.width, r.filled)].append(r)
    best = max(by.values(), key=len)
    keep: list[Rect] = []
    for r in best:
        if not any(abs(r.cx - k.cx) < 1 and abs(r.cy - k.cy) < 1 for k in keep):
            keep.append(r)
    return keep
