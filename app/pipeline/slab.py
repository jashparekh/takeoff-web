"""Bottom slab reinforcement, panel by panel (plan M5 step 1).

On a bottom-slab sheet each panel carries bar callouts (`K8@9"C/C`) written along a hairline that spans the panel in
the bar's direction; a note gives the distribution steel (`DISTRIBUTION STEEL K8@9"C/C`) for a direction a panel
does not call out, and a boxed note marks a panel of other thickness (`6"THK`).

  1. callouts: OCR of the lettering-only render with lines removed (three sizes, upright and turned), then each
     hairline still without a callout is read again from a small crop at its start;
  2. each callout's bar line: the longest hairline at or just under the text that covers where the text starts;
  3. the panel: from the middle of the bar line, the nearest beam edges on either side across it; callouts whose
     panels overlap are one panel;
  4. a direction a panel does not call out takes the distribution steel from the note (stated on the drawing, so
     not an assumption); thickness notes go to the panel they sit in.

Lengths are the drawn lines (face to face); counts are ⌊width / spacing⌋ + 1 across the other direction.
"""
from __future__ import annotations
import math
import re
from dataclasses import dataclass, field

import pypdfium2.raw as R
import pytesseract

from . import geometry as g
from .layout import LABEL_DPIS, lettering_width, render_only

DIAS = {8, 10, 12, 16, 20}
GLYPH_PT = 12
TARGET_DPI = 400
DIG = str.maketrans({'s': '8', 'S': '8', 'g': '8', 'B': '8', 'l': '1', 'I': '1', 'i': '1', 'o': '0', 'O': '0',
                     'D': '0', 'Z': '2', 'z': '2'})
CALLOUT = re.compile(r'[Kk«R]\s*([0-9sSgBlIi]{1,2})\s*[@eoO0©®]\s*([0-9sSgBlIioOD]{1,2})\s*["°\'’”]?\s*[cC¢(]')
THK = re.compile(r'(\d{1,2})\s*["”°\']?\s*TH[KX]', re.I)
MAX_PANEL_IN = 400


def parse_callout(t: str) -> tuple[int, int] | None:
    """`K8@9"C/C` as OCR reads it, forgivingly (8 read as s/g/B, @ as e/o/0, 1 as l/I); the C/C anchors it.
    Returns (bar dia mm, spacing in)."""
    m = CALLOUT.search(t.replace(' ', ''))
    if not m:
        return None
    d, sp = int(m.group(1).translate(DIG)), int(m.group(2).translate(DIG))
    return (d, sp) if d in DIAS and 3 <= sp <= 12 else None


@dataclass
class Callout:
    dia: int
    sp: int
    vertical: bool                 # bars run up/down the sheet
    box: tuple
    line: tuple | None = None      # (across, a, b) in pt
    reads: int = 1


@dataclass
class PanelBar:
    vertical: bool
    dia: int
    sp: float
    length_in: float               # bar length (panel span this way)
    width_in: float                # distributed across
    source: str                    # 'callout' or 'distribution note'

    @property
    def nos(self) -> int:
        return math.floor(self.width_in / self.sp) + 1

    def json(self) -> dict:
        return dict(vertical=self.vertical, dia=self.dia, sp=self.sp, length_in=round(self.length_in, 1),
                    width_in=round(self.width_in, 1), nos=self.nos, source=self.source)


@dataclass
class Panel:
    rect: tuple                    # x0, y0, x1, y1 pt
    callouts: list = field(default_factory=list)
    thk_in: float | None = None
    bars: list = field(default_factory=list)

    def size_in(self, ppi: float) -> tuple[float, float]:
        return (self.rect[2] - self.rect[0]) / ppi, (self.rect[3] - self.rect[1]) / ppi


@dataclass
class SlabReading:
    panels: list = field(default_factory=list)
    distribution: tuple | None = None      # (dia, spacing) from the note
    callouts: int = 0
    hairlines: int = 0


def hairlines(vec: g.Vectors, thin: float = 0.3, min_len: float = 4) -> tuple[list, list]:
    H, V = [], []
    for p in vec.paths:
        if p.width > thin:
            continue
        for (_, x1, y1), (t, x2, y2) in zip(p.points, p.points[1:]):
            if t != R.FPDF_SEGMENT_LINETO:
                continue
            if abs(y1 - y2) < 0.3 and abs(x1 - x2) > min_len:
                H.append(((y1 + y2) / 2, min(x1, x2), max(x1, x2)))
            elif abs(x1 - x2) < 0.3 and abs(y1 - y2) > min_len:
                V.append(((x1 + x2) / 2, min(y1, y2), max(y1, y2)))
    return H, V


def bar_line(vertical: bool, box: tuple, H: list, V: list) -> tuple | None:
    """The bar line for a callout: the longest hairline at or just under the text that covers where it starts (a
    narrow panel's line can be shorter than its text). Turned text reads bottom to top: it starts at the bottom."""
    x0, y0, x1, y1 = box
    best = None
    for c, a, b in (V if vertical else H):
        near = (x0 - 2 <= c <= x1 + 6) if vertical else (y0 - 2 <= c <= y1 + 6)
        start = (y1 - 4) if vertical else (x0 + 4)
        if near and a <= start <= b and (best is None or b - a > best[2] - best[1]):
            best = (c, a, b)
    return best


def ocr_tokens(img, dpi: int):
    """(text, turned, box in pt) for every word, upright and turned a quarter."""
    k = 72 / dpi
    for turned in (False, True):
        im = img.rotate(-90, expand=True) if turned else img
        d = pytesseract.image_to_data(im, config='--psm 11', output_type=pytesseract.Output.DICT)
        for i, t in enumerate(d['text']):
            if not t.strip():
                continue
            l, tp, w, h = d['left'][i], d['top'][i], d['width'][i], d['height'][i]
            box = (tp * k, (im.width - (l + w)) * k, (tp + h) * k, (im.width - l) * k) if turned else \
                (l * k, tp * k, (l + w) * k, (tp + h) * k)
            yield t, turned, box, d['line_num'][i], d['block_num'][i]


def read_callouts(path: str, page_no: int, lw: float, H: list, V: list) -> tuple[list, list]:
    found: list[Callout] = []
    words = []
    for dpi in LABEL_DPIS:
        img = render_only(path, page_no, lw, dpi, max_size=GLYPH_PT)
        for t, turned, box, line, block in ocr_tokens(img, dpi):
            if dpi == LABEL_DPIS[0]:
                words.append((t, turned, box, line, block))
            r = parse_callout(t)
            if not r:
                continue
            same = next((f for f in found if f.vertical == turned and abs(f.box[0] - box[0]) < 4
                         and abs(f.box[1] - box[1]) < 4), None)
            if same:
                same.reads += 1
                continue
            found.append(Callout(r[0], r[1], turned, box, bar_line(turned, box, H, V)))
    # Hairlines still without a callout: read the start of each again, bigger, as one line of text.
    used = {f.line for f in found if f.line}
    free = [(l, False) for l in H if l[2] - l[1] > 15 and l not in used] + \
           [(l, True) for l in V if l[2] - l[1] > 15 and l not in used]
    if free:
        big, k = render_only(path, page_no, lw, TARGET_DPI, max_size=GLYPH_PT), TARGET_DPI / 72
        for (c, a, b), vert in free:
            for side in (-1, 1):
                if vert:
                    box = (c - 12, max(a, b - 70) - 30, c + 3, b + 4) if side < 0 else (c - 3, max(a, b - 70) - 30, c + 12, b + 4)
                else:
                    box = (a - 4, c - 12, min(b, a + 70) + 30, c + 3) if side < 0 else (a - 4, c - 3, min(b, a + 70) + 30, c + 12)
                crop = big.crop(tuple(int(v * k) for v in box))
                r = parse_callout(pytesseract.image_to_string(crop.rotate(-90, expand=True) if vert else crop,
                                                              config='--psm 7'))
                if r:
                    found.append(Callout(r[0], r[1], vert, box, (c, a, b)))
                    break
    return found, words


def distribution_note(words: list) -> tuple | None:
    """`DISTRIBUTION STEEL K8@9"C/C`: the callout on the same text line as the word DISTRIBUTION."""
    for t, turned, box, line, block in words:
        if 'DISTRIB' in t.upper() and not turned:
            same = [w for w in words if not w[1] and w[3] == line and w[4] == block]
            r = parse_callout(''.join(w[0] for w in sorted(same, key=lambda w: w[2][0])))
            if r:
                return r
    return None


def panel_rect(f: Callout, mH: dict, mV: dict) -> tuple | None:
    """From the middle of the bar line, the nearest beam edges either side across it."""
    across, a, b = f.line
    mid = (a + b) / 2
    lo = hi = None
    for c, runs in (mV if f.vertical else mH).items():
        if not any(ra - 1 <= mid <= rb + 1 for ra, rb in runs):
            continue
        if c < across - 1 and (lo is None or c > lo):
            lo = c
        if c > across + 1 and (hi is None or c < hi):
            hi = c
    if lo is None or hi is None:
        return None
    return (lo, a, hi, b) if f.vertical else (a, lo, b, hi)


def overlap(p: tuple, q: tuple) -> float:
    ix = max(0, min(p[2], q[2]) - max(p[0], q[0]))
    iy = max(0, min(p[3], q[3]) - max(p[1], q[1]))
    smaller = min((p[2] - p[0]) * (p[3] - p[1]), (q[2] - q[0]) * (q[3] - q[1]))
    return ix * iy / smaller if smaller > 0 else 0


def read_slab(path: str, page_no: int, pt_per_in: float) -> SlabReading:
    vec = g.read_vectors(path, page_no)
    edges = g.beam_edges(vec, pt_per_in)
    if edges is None:
        return SlabReading()
    lw = lettering_width(vec, edges.width)
    if lw is None:
        return SlabReading()
    H, V = hairlines(vec)
    callouts, words = read_callouts(path, page_no, lw, H, V)
    out = SlabReading(distribution=distribution_note(words), callouts=len(callouts),
                      hairlines=sum(1 for l in H + V if l[2] - l[1] > 15))
    eH, eV = vec.axis_lines(edges.width)
    mH, mV = g.merge_lines(eH, 16 * pt_per_in), g.merge_lines(eV, 16 * pt_per_in)
    for f in callouts:
        if not f.line:
            continue
        r = panel_rect(f, mH, mV)
        if not r or (r[2] - r[0]) / pt_per_in > MAX_PANEL_IN or (r[3] - r[1]) / pt_per_in > MAX_PANEL_IN:
            continue
        p = next((p for p in out.panels if overlap(p.rect, r) > 0.6), None)
        if p:
            p.callouts.append(f)
        else:
            out.panels.append(Panel(r, [f]))
    for t, turned, box, line, block in words:
        m = THK.search(t.replace(' ', ''))
        if not m and re.match(r'^TH[KX]', t, re.I):         # `6"` and `THK` read as two words: join them
            prev = [w for w in words if w[1] == turned and w[3] == line and w[4] == block and w[2][2] <= box[0] + 1
                    and box[0] - w[2][2] < 6 and re.search(r'\d{1,2}\s*["”°\']?$', w[0])]
            if prev:
                m = THK.search(prev[-1][0] + t)
                box = (prev[-1][2][0], box[1], box[2], box[3])
        if m:
            cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
            p = next((p for p in out.panels if p.rect[0] <= cx <= p.rect[2] and p.rect[1] <= cy <= p.rect[3]), None)
            if p is None and out.panels:      # boxed notes often sit just outside the panel line: nearest panel
                p = min(out.panels, key=lambda p: math.dist((cx, cy), ((p.rect[0] + p.rect[2]) / 2, (p.rect[1] + p.rect[3]) / 2)))
            if p:
                p.thk_in = float(m.group(1))
    for p in out.panels:
        w, h = p.size_in(pt_per_in)
        for vert in (False, True):
            f = max((c for c in p.callouts if c.vertical == vert), key=lambda c: c.reads, default=None)
            length, width = (h, w) if vert else (w, h)
            if f:
                p.bars.append(PanelBar(vert, f.dia, f.sp, length, width, 'callout'))
            elif out.distribution:
                p.bars.append(PanelBar(vert, out.distribution[0], out.distribution[1], length, width, 'distribution note'))
    out.panels.sort(key=lambda p: (round(p.rect[1] / 20), p.rect[0]))
    return out
