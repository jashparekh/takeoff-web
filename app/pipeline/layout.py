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
import itertools
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
BEAM_GAP_IN = 16          # a crossing beam interrupts a beam's edge lines by about its own width
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


def render_only(path: str, page_no: int, width: float, dpi: int, max_size: float | None = None) -> Image.Image:
    """Render the page with only the paths of one stroke width (everything else removed from the in-memory copy).
    `max_size` (pt) also drops paths bigger than a glyph, e.g. the dimension lines that text sits on."""
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
                        if max_size is None:
                            continue
                        l, b, r, t = (ctypes.c_float() for _ in range(4))
                        R.FPDFPageObj_GetBounds(o.raw, l, b, r, t)
                        if max(r.value - l.value, t.value - b.value) <= max_size:
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
    cantilever: bool = False
    clear_in: float | None = None          # face-to-face span: the drawn dimension, else the geometry
    clear_source: str = 'layout'           # 'dimension' (written on the layout) or 'layout' (measured)
    geo_clear_in: float | None = None      # what the geometry measures (see clear_span_in)


@dataclass
class Layout:
    beams: list[Beam] = field(default_factory=list)
    columns: list[g.Rect] = field(default_factory=list)
    unplaced: list[Label] = field(default_factory=list)
    strips: list[g.Strip] = field(default_factory=list)
    edge_width: float | None = None
    dims: list = field(default_factory=list)          # dimension strings read (dimensions.Dim)
    outline: g.Outline | None = None


def cuts(s: g.Strip, strips: list[g.Strip], cols: list[g.Rect], ppi: float = 0.6,
         labels: list[float] | None = None) -> tuple[list[float], list[float]]:
    """Positions along strip `s` where a span may end.
    Hard: columns, and the ends of the strip.
    Soft: perpendicular beams, whether they frame in from one side (T) or pass right across (X). A beam is drawn
    continuous through another beam unless its label says otherwise (the pilot's spans run through crossings: their
    c/c is the workbook span + one beam width); so a span is split at a soft cut only where two different labels share
    it."""
    hard, soft, through = {s.a, s.b}, set(), set()
    half = s.width_pt / 2
    for p in strips:
        if p.vertical == s.vertical or not (s.a - 3 <= p.c <= s.b + 3):
            continue
        if p.a - 3 <= s.c + half and p.b + 3 >= s.c - half:      # touches or crosses
            soft.add(p.c)
            if p.a < s.c - half - 3 and p.b > s.c + half + 3:
                through.add(p.c)
    for r in cols:
        across, along = (r.cx, r.cy) if s.vertical else (r.cy, r.cx)
        if abs(across - s.c) < 6 and s.a - 3 <= along <= s.b + 3:
            hard.add(along)
    # An overhang: past the last beam that crosses the line there is no column and no label, so the line beyond is a
    # cantilever (add_cantilevers reads it) and the span stops at that beam.
    inner = sorted(c for c in hard if s.a + 3 < c < s.b - 3)
    labelled = [] if labels is None else labels
    col_at = [(r.cy if s.vertical else r.cx) for r in cols if abs((r.cx if s.vertical else r.cy) - s.c) < 6]
    for end in (s.a, s.b):
        if any(abs(x - end) <= 12 for x in col_at):     # the line ends on a column: supported, not an overhang
            continue
        xs = sorted((c for c in through if OVERHANG_IN[0] * ppi <= abs(c - end) <= OVERHANG_IN[1] * ppi),
                    key=lambda c: abs(c - end))
        if not xs:
            continue
        root = xs[0]
        lo, hi = sorted((root, end))
        if not any(lo + 3 < c < hi - 3 for c in inner) and not any(lo < x < hi for x in labelled):
            hard.add(root)
    return sorted(hard), sorted(soft - hard)


OVERHANG_IN = (12, 110)        # a cantilever past its root beam: longer than the tip's edge beam, at most ~9 ft


def centred_cuts(inside: list, soft: list[float], a: float, b: float) -> list[float]:
    """Where to split span a–b shared by different labels (`inside`, sorted along it). A label is written at the
    middle of its own span, so between each pair of neighbouring labels pick the soft cut that, over the whole span,
    puts every label nearest the centre of its piece. Labels with no cut between them stay together."""
    groups: list[list] = []                       # runs of the same name: [name, mean position]
    for along, _, lb in inside:
        if groups and groups[-1][0] == lb.name:
            groups[-1][1] = (groups[-1][1] + along) / 2
        else:
            groups.append([lb.name, along])
    if len(groups) < 2:
        return []
    choices = [[c for c in soft if x1 < c < x2] or [None] for (_, x1), (_, x2) in zip(groups, groups[1:])]
    best = None
    for picked in itertools.product(*choices):                # few labels share a span
        bounds = [a] + [c for c in picked if c is not None] + [b]
        cost, k = 0.0, 0
        for gi, (_, x) in enumerate(groups):
            cost += abs((bounds[k] + bounds[k + 1]) / 2 - x)
            if gi < len(picked) and picked[gi] is not None:
                k += 1
        if best is None or cost < best[0]:
            best = (cost, [c for c in picked if c is not None])
    return best[1]


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
        hard, soft = cuts(s, strips, cols, pt_per_in, [along for along, _, _ in items])
        spans: dict[tuple, list] = defaultdict(list)    # hard span → labels in it
        for along, d, lb in items:
            a = max((c for c in hard if c <= along), default=s.a)
            b = min((c for c in hard if c > along), default=s.b)
            spans[(a, b)].append((along, d, lb))
        for (a, b), inside in spans.items():
            inside.sort(key=lambda t: t[0])
            names = [lb.name for _, _, lb in inside]
            bounds = [a] + centred_cuts(inside, soft, a, b) + [b]
            seen = set()
            for along, d, lb in sorted(inside, key=lambda t: t[1]):        # nearest label first
                k = max(j for j in range(len(bounds) - 1) if bounds[j] <= along or j == 0)
                lo, hi = bounds[k], bounds[k + 1]
                if (lb.name, lo, hi) in seen:                             # the same label read twice on one span
                    continue
                seen.add((lb.name, lo, hi))
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


def columns_on(s: g.Strip, lo: float, hi: float, cols: list[g.Rect]) -> list[tuple[float, float]]:
    """Columns sitting on strip `s`'s line between `lo` and `hi`, as (start, end) along the line."""
    out = []
    for r in cols:
        across = r.cx if s.vertical else r.cy
        a, b = (r.y0, r.y1) if s.vertical else (r.x0, r.x1)
        if abs(across - s.c) < 6 and b > lo and a < hi:
            out.append((a, b))
    return out


def add_cantilevers(lay: Layout, strips: list[g.Strip], cols: list[g.Rect], labels: list[Label], pt_per_in: float):
    """A beam's cantilever: follow its line outward (across beams that cross it) from the end of its span. If the line
    runs out before reaching a column or another label, that stretch overhangs: it is the cantilever. A column at
    the root is the support; a column further out means an unlabelled span instead. Tips often carry an edge beam,
    so a free tip is not required. Named like the schedules: B7-cant, or B74-cantL / B74-cantR (left or top / right
    or bottom) when both ends overhang."""
    gap = BEAM_GAP_IN * pt_per_in + 6
    near = NEAR_PT_IN * pt_per_in
    by_line: dict[tuple, list[g.Strip]] = defaultdict(list)
    for s in strips:
        by_line[(s.vertical, round(s.c))].append(s)
    runs = []                                      # (vertical, c, lo, hi, a strip on it)
    for (vertical, c), ss in by_line.items():
        same = sorted([t for (v, k), lst in by_line.items() if v == vertical and abs(k - c) <= 1 for t in lst],
                      key=lambda t: t.a)
        cur = None
        for t in same:
            if cur and t.a - cur[3] <= gap:
                cur[3] = max(cur[3], t.b)
            else:
                cur = [vertical, t.c, t.a, t.b, t]
                runs.append(cur)
    seen = set()
    label_at = [(lb.vertical, lb.y if lb.vertical else lb.x, lb.x if lb.vertical else lb.y) for lb in labels]
    new = []
    for vertical, c, lo_run, hi_run, s0 in runs:
        key = (vertical, round(c, 0), round(lo_run), round(hi_run))
        if key in seen:
            continue
        seen.add(key)
        on = []
        for b in lay.beams:
            if b.vertical != vertical or b.cantilever or abs((b.p1[0] if vertical else b.p1[1]) - c) > 1.5:
                continue
            lo, hi = sorted((b.p1[1], b.p2[1]) if vertical else (b.p1[0], b.p2[0]))
            if lo >= lo_run - 2 and hi <= hi_run + 2:
                on.append((lo, hi, b))
        if not on:
            continue
        on.sort(key=lambda t: t[0])
        found = []
        for side, (lo, hi, beam) in (('L', on[0]), ('R', on[-1])):
            root, tip = (lo, lo_run) if side == 'L' else (hi, hi_run)
            x1, x2 = sorted((root, tip))
            if x2 - x1 < 12 * pt_per_in:
                continue                           # under a foot: an edge detail
            out_cols = [(a, b) for a, b in columns_on(s0, x1, x2, cols) if not (a - 3 <= root <= b + 3)]
            if out_cols:
                continue                           # something holds it up further out: not an overhang
            if any(v == vertical and x1 + 2 < al < x2 - 2 and abs(ac - c) <= near for v, al, ac in label_at):
                continue                           # another beam's label is there
            found.append((side, x1, x2, beam))
        for side, x1, x2, beam in found:
            name = f'{beam.name}-cant' + (side if len(found) == 2 else '')
            p1, p2 = ((c, x1), (c, x2)) if vertical else ((x1, c), (x2, c))
            new.append(Beam(name, vertical, p1, p2, beam.b_in, beam.label, min(0.5, round(beam.confidence * 0.8, 2)),
                            'Runs past the last support with no column under it: read as a cantilever.',
                            beam.label_b_in, beam.label_d_in, True))
    lay.beams += new


SPAN_AGREE_IN = 3              # drawn dimension vs measured clear span


def read_layout(path: str, page_no: int, pt_per_in: float) -> Layout:
    from .dimensions import assign, read_dims
    vec = g.read_vectors(path, page_no)
    edges = g.beam_edges(vec, pt_per_in)
    if edges is None:
        return Layout()
    cols = g.columns(vec, pt_per_in, edges.width)
    labels = read_labels(path, page_no, vec, edges.width)
    lay = match(labels, edges.strips, cols, pt_per_in)
    add_cantilevers(lay, edges.strips, cols, labels, pt_per_in)
    for b in lay.beams:
        b.clear_in = b.geo_clear_in = clear_span_in(b, edges.strips, cols, pt_per_in)
    lettering = lettering_width(vec, edges.width)
    lay.dims = read_dims(path, page_no, vec, lettering, pt_per_in) if lettering else []
    for i, d in assign(lay.dims, lay.beams, pt_per_in).items():
        b = lay.beams[i]
        b.clear_in, b.clear_source = d.value_in, 'dimension'
        if b.geo_clear_in and abs(d.value_in - b.geo_clear_in) > SPAN_AGREE_IN:
            b.confidence = min(b.confidence, 0.4)
            b.note = ' '.join(filter(None, [b.note, f'Drawn span {d.text} but the beam measures '
                                                    f'{b.geo_clear_in:.0f}" face to face.']))
    lay.edge_width = edges.width
    lay.outline = g.slab_outline(vec, edges.width, edges.strips, pt_per_in)
    return lay


def label_stats(labels: list[Label]) -> dict:
    return dict(labels=len(labels), names=len({lb.name for lb in labels}),
                median_reads=statistics.median([lb.reads for lb in labels]) if labels else 0)


def half_support_in(s_vertical: bool, c: float, at: float, strips: list[g.Strip], cols: list[g.Rect],
                    pt_per_in: float) -> float:
    """How far the span end `at` is inside its support, in inches. Spans end either at a support's centre line (a
    beam crossing right over, or a column) — then half the support comes off — or where the beam's edge lines stop,
    which is already the support's face (or a free tip) — then nothing does."""
    for r in cols:
        across, a, b = (r.cx, r.y0, r.y1) if s_vertical else (r.cy, r.x0, r.x1)
        if abs(across - c) < 6 and abs((a + b) / 2 - at) <= 1.5:
            return (b - a) / 2 / pt_per_in
    for p in strips:
        if p.vertical != s_vertical and abs(p.c - at) <= 1.5 and p.a - 3 <= c <= p.b + 3:
            return p.width_pt / 2 / pt_per_in
    return 0.0


def clear_span_in(b: Beam, strips: list[g.Strip], cols: list[g.Rect], pt_per_in: float) -> float:
    """Face-to-face span (what schedules and the engine call clear span): centre-to-centre less half of each
    support. A cantilever is measured from the support face to its tip."""
    c = b.p1[0] if b.vertical else b.p1[1]
    e1, e2 = sorted((b.p1[1], b.p2[1]) if b.vertical else (b.p1[0], b.p2[0]))
    c2c = (e2 - e1) / pt_per_in
    return round(c2c - half_support_in(b.vertical, c, e1, strips, cols, pt_per_in)
                 - half_support_in(b.vertical, c, e2, strips, cols, pt_per_in), 1)
