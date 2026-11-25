"""Read beam schedules (plan M3 steps 2–3): find each beam's elevation, OCR its callouts, and read them into bars and
stirrups by where they sit. This is the OCR-only reading; a vision model checks the elevations it is unsure about
in the next step (M3 PR 2), and nothing is used for quantities until it agrees or a person confirms it.

On CAD schedules the lettering comes in two stroke widths: names and dimensions (e.g. 0.48 pt) and bar callouts and
stirrup notes (bolder, e.g. 0.6 pt). Each is rendered alone and OCR'd, so beam lines don't confuse the OCR.

Reading rules (how Indian RCC beam schedules are drawn):
  top callouts (above the beam):   the one not under a dimension bracket → Top through; under an EQ or length
                                   bracket, or over a support → Extra top @ support (the bracket gives its length)
  bottom callouts (at the bottom): left to right → Bottom through, then Bottom extra
  stirrup row (below):             K8@6"C/C at an end gives dia and end spacing; @8"C/C in the middle the mid spacing
  an elevation labelled only (9"X18") next to a named one is that beam's cantilever: top → Top main
"""
from __future__ import annotations
import re
from collections import defaultdict
from dataclasses import dataclass, field

import pytesseract

from . import geometry as g
from .layout import lettering_width, render_only

DIAS = (8, 10, 12, 16, 20, 25, 32)
STIRRUP_DIAS = (6, 8, 10, 12)
NAME = re.compile(r'((?:H?B|CB)\d{1,3}[A-Z]?)?\(\s*(\d{1,2})\D{0,3}?[X×]\s*(\d{1,2})(?:\D{0,3}/\s*(\d{1,2}))?')
CALL = re.compile(r'(\d)\s*K\s*(32|25|20|16|12|10|8)')
DIM = re.compile(r"(\d{1,2})\s*['’`]\s*[-–]\s*(\d{1,2})")
OCR_DPI = 400


@dataclass
class Token:
    text: str
    x0: float
    y0: float
    x1: float
    y1: float

    @property
    def cx(self):
        return (self.x0 + self.x1) / 2

    @property
    def cy(self):
        return (self.y0 + self.y1) / 2


CALLOUT_CHARS = '0123456789K+@C/'        # bar callouts and stirrup notes use only these (quotes dropped)


def ocr_tokens(path: str, page_no: int, width: float, dpi: int = OCR_DPI, chars: str | None = None) -> list[Token]:
    img = render_only(path, page_no, width, dpi)
    cfg = '--psm 11' + (f' -c tessedit_char_whitelist={chars}' if chars else '')
    d = pytesseract.image_to_data(img, config=cfg, output_type=pytesseract.Output.DICT)
    k = 72 / dpi
    return [Token(t.strip(), d['left'][i] * k, d['top'][i] * k, (d['left'][i] + d['width'][i]) * k,
                  (d['top'][i] + d['height'][i]) * k) for i, t in enumerate(d['text']) if t.strip()]


def clean(text: str) -> str:
    """Undo common OCR swaps in callouts: I/l→1, O→0 next to digits, ‘ and stray marks."""
    t = text.upper().replace('‘', '').replace('’', "'").replace('°', '"').replace('”', '"')
    t = re.sub(r'(?<=[0-9K])[IL]|[IL](?=[0-9])', '1', t)
    t = re.sub(r'(?<=\d)O|O(?=\d)', '0', t)
    return t


def callouts(text: str) -> list[tuple[int, int]]:
    """'2K20+1K25' → [(2, 20), (1, 25)]; OCR often reads '+' as '4' or 'H' ('2K2041K25')."""
    return [(int(n), int(d)) for n, d in CALL.findall(clean(text))]


def stirrup(text: str) -> tuple[int | None, int] | None:
    """'K8@6"C/C' → (8, 6); '@8"C/C' → (None, 8). OCR reads @ as 0, o, e or a: 'K806"C/C', 'K1005"C/C', 'e8"C/C'."""
    t = clean(text).replace('C/¢', 'C/C').replace('¢', 'C')
    if 'C/C' not in t and not t.endswith('"C') and '"C/' not in t:
        return None
    head = t.split('C/C')[0].split('"C')[0].rstrip('"\' ')
    head = re.sub(r'^([^\dK]*K)[ABS](?=[@0OEA]?\d)', r'\g<1>8', head)        # K8 read as KA / KB / KS
    m = re.match(r'^[^\dK]*K?\s*(12|10|8|6)[@0OEA]+(\d{1,2})$', head.replace('K', 'K', 1)) if 'K' in head[:3] else None
    if m:
        return int(m.group(1)), int(m.group(2))
    m = re.match(r'^[^\d]*?[@0OEA]?(\d{1,2})$', head)
    if m and 3 <= int(m.group(1)) <= 12:
        return None, int(m.group(1))
    if m and len(m.group(1)) == 2 and m.group(1)[0] in '0689' and 3 <= int(m.group(1)[1]) <= 9:
        return None, int(m.group(1)[1])                  # '69"C/C': the @ read as a digit
    return None


def dim_inches(text: str) -> int | None:
    m = DIM.search(clean(text))
    return int(m.group(1)) * 12 + int(m.group(2)) if m and int(m.group(2)) < 12 else None


# ---------- elevations ----------

@dataclass
class Bar:
    role: str
    nos: int
    dia: int
    extra_top_in: int | None = None


@dataclass
class Elevation:
    name: str | None                 # None for a cantilever part labelled only by its size
    b_in: int
    D_in: int
    D2_in: int | None                # B49(9"X21"/24"): two depths
    x0: float
    x1: float
    y_top: float                     # beam top line, pt
    y_bot: float
    label: Token
    tokens: list = field(default_factory=list)
    bars: list[Bar] = field(default_factory=list)
    stirrup: dict | None = None
    notes: list[str] = field(default_factory=list)
    cant_of: str | None = None

    @property
    def box(self):
        # Top callouts sit ~3 pt above the beam and their brackets ~8 pt; the row above's stirrup notes start ~32 pt
        # up, so the box stops well short of them.
        return (self.x0, self.y_top - 16, self.x1, self.label.y1 + 2)


def beam_rows(vec: g.Vectors, width: float) -> list[tuple[float, float, list[list[float]]]]:
    """Elevation rows: pairs of long horizontal outline lines (top, bottom) with their x runs."""
    H, _ = vec.axis_lines(width)
    lines = g.merge_lines(H, gap=20)
    keys = sorted(lines)
    rows = []
    for i, yt in enumerate(keys):
        for yb in keys[i + 1:]:
            if yb - yt > 40:
                break
            if yb - yt < 4:
                continue
            runs = [[max(a1, a2), min(b1, b2)] for a1, b1 in lines[yt] for a2, b2 in lines[yb]
                    if min(b1, b2) - max(a1, a2) > 12]
            if runs:
                rows.append((yt, yb, runs))
    # Each stretch of a top line belongs to the tightest bottom line under it: a row can mix 18" and 24" deep beams,
    # drawn with different bottoms.
    out, taken = [], defaultdict(list)
    for yt, yb, runs in sorted(rows, key=lambda r: (r[0], r[1] - r[0])):
        free = [[a, b] for a, b in runs if not any(a < tb - 2 and b > ta + 2 for ta, tb in taken[yt])]
        if free:
            taken[yt] += free
            out.append((yt, yb, free))
    return sorted(out)


def supports_on(vec: g.Vectors, width: float, yt: float, yb: float) -> list[float]:
    _, V = vec.axis_lines(width)
    xs = sorted({round(x, 1) for x, a, b in V if a <= yt + 1 and b >= yb - 1})
    return xs


def find_elevations(vec: g.Vectors, labels: list[Token], outline_w: float) -> list[Elevation]:
    rows = beam_rows(vec, outline_w)
    out = []
    for lb in labels:
        text = clean(lb.text).replace(' ', '')
        text = re.sub(r'^(\d{1,3}[A-Z]?)\(', r'B\1(', text)          # '40(9"X18")': the B was not read
        m = NAME.search(text)
        if not m:
            continue
        row = None
        for yt, yb, runs in rows:
            if 0 < lb.y0 - yb < 60 and any(a - 5 <= lb.cx <= b + 5 for a, b in runs):
                if row is None or yb > row[1]:
                    row = (yt, yb, runs)
        if row is None:
            continue
        yt, yb, runs = row
        a, b = next((a, b) for a, b in runs if a - 5 <= lb.cx <= b + 5)
        out.append(Elevation(m.group(1), int(m.group(2)), int(m.group(3)), int(m.group(4)) if m.group(4) else None,
                             a, b, yt, yb, lb))
    # Several labels on one run: split at the support nearest the midpoint between neighbours.
    by_run = defaultdict(list)
    for e in out:
        by_run[(round(e.y_top, 1), round(e.x0, 1), round(e.x1, 1))].append(e)
    for (yt, _, _), es in by_run.items():
        if len(es) < 2:
            continue
        es.sort(key=lambda e: e.label.cx)
        sup = supports_on(vec, outline_w, es[0].y_top, es[0].y_bot)
        for left, right in zip(es, es[1:]):
            mid = (left.label.cx + right.label.cx) / 2
            cut = min(sup, key=lambda x: abs(x - mid)) if sup else mid
            if not (left.label.cx < cut < right.label.cx):
                cut = mid
            left.x1, right.x0 = cut, cut
    # Cantilever parts: a size-only label sharing a support with a named elevation on the same row.
    for e in out:
        if e.name:
            continue
        near = [o for o in out if o.name and abs(o.y_top - e.y_top) < 1 and
                (abs(o.x1 - e.x0) < 25 or abs(e.x1 - o.x0) < 25)]
        if near:
            o = min(near, key=lambda o: min(abs(o.x1 - e.x0), abs(e.x1 - o.x0)))
            e.cant_of = o.name
    return out


def part_name(e: Elevation, all_: list[Elevation]) -> str | None:
    """B7, or B7-cant / B74-cantL / B74-cantR for cantilever parts (L = left of the beam)."""
    if e.name:
        return e.name
    if not e.cant_of:
        return None
    sides = [o for o in all_ if o.cant_of == e.cant_of and not o.name and abs(o.y_top - e.y_top) < 1]
    if len(sides) < 2:
        return f'{e.cant_of}-cant'
    owner = next(o for o in all_ if o.name == e.cant_of and abs(o.y_top - e.y_top) < 1)
    return f'{e.cant_of}-cant' + ('L' if e.label.cx < owner.label.cx else 'R')


def read_elevation(e: Elevation, calls: list[Token], dims: list[Token], supports: list[float]) -> None:
    """Assign callouts, brackets and stirrup notes inside the elevation's box to bars by position."""
    x0, y0, x1, y1 = e.box
    inside = lambda t: x0 - 2 <= t.cx <= x1 + 2 and y0 <= t.cy <= y1        # noqa: E731
    mine = [t for t in calls if inside(t)]
    my_dims = [t for t in dims if inside(t) and (dim_inches(t.text) or 'EQ' in clean(t.text))]
    e.tokens = [t.text for t in mine]
    top = sorted((t for t in mine if t.cy < e.y_top + 1 and callouts(t.text)), key=lambda t: t.cx)
    bot = sorted((t for t in mine if e.y_top + 1 <= t.cy <= e.y_bot + 7 and callouts(t.text)), key=lambda t: t.cx)
    stirs = sorted(((t, stirrup(t.text)) for t in mine if t.cy > e.y_bot + 4), key=lambda p: p[0].cx)
    stirs = [(t, s) for t, s in stirs if s]
    cant = e.name is None
    span = max(e.x1 - e.x0, 1)

    def bracket_over(t):
        for d in my_dims:
            if d.cy < t.cy and t.cy - d.cy < 20 and d.x0 - 25 <= t.cx <= d.x1 + 25:
                return d
        return None
    through_done = False
    for t in top:
        br = bracket_over(t)
        at_support = min(t.cx - e.x0, e.x1 - t.cx) / span < 0.22
        if cant:
            role = 'Top main'
        elif br is None and not at_support and not through_done:
            role, through_done = 'Top through', True
        elif br is None and not through_done and len(top) == 1 and not at_support:
            role, through_done = 'Top through', True
        else:
            role = 'Extra top @ support'
        length = dim_inches(br.text) if br is not None and role == 'Extra top @ support' else None
        for n, d in callouts(t.text):
            e.bars.append(Bar(role, n, d, length))
    if not cant and not through_done and top:
        e.notes.append('No top bar read as running through; check the top bars.')
    for i, t in enumerate(bot):
        role = 'Bottom through' if i == 0 or cant else 'Bottom extra'
        for n, d in callouts(t.text):
            e.bars.append(Bar(role, n, d))
    if stirs:
        dia = next((s[0] for _, s in stirs if s[0]), None)
        sp = [s[1] for _, s in stirs]
        end, mid = (sp[0], sp[0]) if len(sp) == 1 else (sp[0], sp[1] if len(sp) >= 2 else sp[0])
        if len(sp) >= 3:
            end, mid = sp[0], sp[len(sp) // 2]
        e.stirrup = dict(dia=dia, end_in=end, mid_in=mid)
        if dia is None:
            e.notes.append('Stirrup bar size not read.')
    else:
        e.notes.append('No stirrup note read.')
    if not any(b.role.startswith('Bottom') for b in e.bars):
        e.notes.append('No bottom bars read.')
    if not any(b.role in ('Top through', 'Top main', 'Extra top @ support') for b in e.bars):
        e.notes.append('No top bars read.')


@dataclass
class Schedule:
    elevations: list[Elevation]
    label_width: float | None
    callout_width: float | None
    outline_width: float | None


def parsed(t: Token):
    return callouts(t.text) or stirrup(t.text)


def callout_tokens(path: str, page_no: int, w: float, first: list[Token]) -> list[Token]:
    """Callouts read several ways (plain and limited to callout characters, at 300 / 400 / 500 dpi). At each place
    on the sheet keep the reading that parses, preferring the one most passes agree on."""
    passes = [first] + [ocr_tokens(path, page_no, w, dpi, CALLOUT_CHARS) for dpi in (300, 400, 500)]
    groups: list[list[Token]] = []
    for toks in passes:
        for t in toks:
            gq = next((gq for gq in groups if abs(gq[0].cx - t.cx) < 4 and abs(gq[0].cy - t.cy) < 3), None)
            (gq.append(t) if gq else groups.append([t]))
    out = []
    for gq in groups:
        good = [t for t in gq if parsed(t)]
        if not good:
            out.append(gq[0])
            continue
        votes: dict = defaultdict(list)
        for t in good:
            votes[repr(parsed(t))].append(t)
        out.append(max(votes.values(), key=len)[0])
    return out


def long_line_length(vec: g.Vectors, w: float) -> float:
    H, _ = vec.axis_lines(w)
    return sum(b - a for _, a, b in H if b - a > 30)


def glyph_count(vec: g.Vectors, w: float) -> int:
    n = 0
    for p in vec.paths:
        if abs(p.width - w) < 0.005 and len(p.points) > 1:
            xs, ys = [x for _, x, _ in p.points], [y for _, _, y in p.points]
            n += 1 <= max(max(xs) - min(xs), max(ys) - min(ys)) <= 10
    return n


def read_schedule(path: str, page_no: int) -> Schedule:
    vec = g.read_vectors(path, page_no)
    widths = [w for w, _ in sorted(vec.widths().items(), key=lambda kv: -kv[1])[:8] if w > 0]
    # Two lettering widths: names and dimensions, and (bolder) bar callouts and stirrup notes. Tell them apart by
    # what OCR reads in each.
    glyphs = sorted(widths, key=lambda w: -glyph_count(vec, w))[:3]
    read = {w: ocr_tokens(path, page_no, w) for w in glyphs}
    score_name = lambda w: sum(bool(NAME.search(clean(t.text).replace(' ', ''))) for t in read[w])   # noqa: E731
    score_call = lambda w: sum(bool(callouts(t.text)) + bool(stirrup(t.text)) for t in read[w])    # noqa: E731
    label_w = max(glyphs, key=score_name, default=None)
    call_w = max((w for w in glyphs if w != label_w), key=score_call, default=None)
    labels = read.get(label_w, [])
    calls = callout_tokens(path, page_no, call_w, read.get(call_w, [])) if call_w else []
    # Beam outlines: the stroke width whose elevation rows sit right above the most labels (bar lines inside the
    # beam pair up too, but short beams don't draw them).
    cands = [w for w in widths if w not in (label_w, call_w) and long_line_length(vec, w) > 0]
    found = {w: find_elevations(vec, labels, w) for w in cands}
    outline = max(found, key=lambda w: len(found[w]), default=None)
    elevs = found.get(outline, [])
    for e in elevs:
        read_elevation(e, calls, labels, supports_on(vec, outline, e.y_top, e.y_bot))
    return Schedule(elevs, label_w, call_w, outline)


def confidence(e: Elevation) -> float:
    """How complete the OCR reading looks (not whether it is right; the vision pass and Review settle that)."""
    roles = {b.role for b in e.bars}
    want = ['Top main', 'Bottom through'] if e.name is None else ['Top through', 'Bottom through']
    score = sum(r in roles for r in want) / len(want) * 0.5
    st = e.stirrup or {}
    score += 0.3 * sum(st.get(k) is not None for k in ('dia', 'end_in', 'mid_in')) / 3
    unread = sum(1 for t in e.tokens if not callouts(t) and not stirrup(t) and re.search(r'\d', t))
    score += 0.2 * (1 if not unread else max(0.0, 1 - unread / 3))
    return round(score, 2)
