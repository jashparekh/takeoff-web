"""Check a sheet's scale against its own dimension strings (plan M1 step 7).

Drawings are often printed "to fit" a smaller sheet, so the printed scale (1:100) can be wrong for the PDF: the
Work Lounge layout says 1:100 on an A3 sheet but measures about 1:120. Quantities read off the drawing in M2+ use
the scale this stage settles on, so it must not be taken on trust.

Method: OCR every dimension string (feet-inches, e.g. 16'-2"), measure the dimension line it sits on, and turn each
into points per real inch. The largest group of dimensions that agree (within 2%, or 2 px for short ones) is the
measured scale; at least two must agree. Nothing is applied silently: a measured scale that differs from the
printed one, a scale borrowed from another sheet, or no scale at all, all go to a person.
"""
from __future__ import annotations
import re
from dataclasses import dataclass

import numpy as np

from .ocr import Word

# Sheet types read for lengths later (M2–M6). Schedules and notes are drawn without a true scale.
NEEDS_SCALE = {'beam_layout', 'slab_bottom', 'slab_top', 'slab', 'column_layout', 'footing'}
TOLERANCE = 0.02
MIN_AGREE = 2
DIM = re.compile(r"^[{(\[|]?(\d{1,3})\s*['’`]?\s*[-–—]\s*(\d{1,2})(?:\s*(\d)/(\d))?\s*[\"”'’°]{0,2}[!|.,)]?$")


def parse_dim(text: str) -> float | None:
    """Feet-inches string → inches. 16'-2" → 194; OCR often drops the marks: 15-11 → 191."""
    m = DIM.match(text.strip())
    if not m:
        return None
    ft, inch = int(m.group(1)), int(m.group(2))
    if inch >= 12:
        return None
    frac = int(m.group(3)) / int(m.group(4)) if m.group(3) and int(m.group(4)) else 0
    total = ft * 12 + inch + frac
    return total if total >= 6 else None          # very short dims can't be measured precisely enough


def line_length(dark: np.ndarray, w: Word) -> int | None:
    """Length in px of the dimension line a horizontal dimension string sits on: the first row at or just below
    the text that is dark right across it, followed left and right until it stops (arrow tip or extension line)."""
    H, W = dark.shape
    l, t, ww, h = w.left, w.top, w.width, w.height
    row = None
    for r in range(t + h // 2, min(H - 1, t + h + max(14, h))):
        seg = dark[r, l:l + ww]
        if seg.size and seg.mean() > 0.92:
            row = r
            break
    if row is None:
        return None
    band = dark[max(0, row - 1):row + 2].any(axis=0)
    cx = l + ww // 2

    def walk(step):
        x, gap = cx, 0
        while 0 <= x + step < W and gap <= 3:
            x += step
            gap = 0 if band[x] else gap + 1
        return x - step * gap
    length = walk(1) - walk(-1)
    return length if length > ww * 1.05 else None


@dataclass
class Measured:
    pt_per_in: float
    agree: int                  # dimensions in the agreeing group
    found: int                  # dimensions measured at all
    examples: list[str]


def measure(gray: np.ndarray, dpi: int, words: list[Word]) -> Measured | None:
    dark = gray < 140
    samples = []                                       # (pt per inch, line px, inches, text)
    for w in words:
        inches = parse_dim(w.text)
        if inches is None or w.width < w.height * 1.2:   # horizontal strings only
            continue
        px = line_length(dark, w)
        if px:
            samples.append((px * 72 / dpi / inches, px, inches, w.text))
    if not samples:
        return None
    best: list = []
    for v, *_ in samples:
        group = [s for s in samples if abs(s[0] / v - 1) <= max(TOLERANCE, 2 / s[1])]
        if len(group) > len(best) or (len(group) == len(best) and sum(s[1] for s in group) > sum(s[1] for s in best)):
            best = group
    if len(best) < MIN_AGREE:
        return Measured(0, len(best), len(samples), [])
    pt_per_in = sum(s[1] for s in best) * 72 / dpi / sum(s[2] for s in best)   # total length / total inches
    longest = sorted(best, key=lambda s: -s[1])[:3]
    return Measured(pt_per_in, len(best), len(samples), [s[3] for s in longest])


def printed_pt_per_in(scale_text: str | None) -> float | None:
    """'1:100' → 0.72 pt per real inch (72 pt per paper inch ÷ 100)."""
    m = re.fullmatch(r'1:(\d{1,4})', scale_text or '')
    return 72 / int(m.group(1)) if m and int(m.group(1)) else None


def ratio(pt_per_in: float) -> str:
    return f'1:{round(72 / pt_per_in)}'


@dataclass
class Decision:
    status: str
    pt_per_in: float | None
    error: float | None
    checks: int | None
    note: str | None


def decide(m: Measured | None, scale_text: str | None) -> Decision:
    """What to record when the sheet's own dimensions are all we have. `unchecked` may later become `borrowed`."""
    printed = printed_pt_per_in(scale_text)
    if m and m.agree >= MIN_AGREE:
        err = m.pt_per_in / printed - 1 if printed else None
        eg = ', '.join(m.examples)
        if printed and abs(err) <= TOLERANCE:
            return Decision('ok', m.pt_per_in, err, m.agree,
                            f'{m.agree} of {m.found} dimensions agree with the printed scale {scale_text} (e.g. {eg}).')
        said = f'The printed scale is {scale_text}, but' if printed else 'No scale is printed;'
        return Decision('differs', m.pt_per_in, err, m.agree,
                        f'{said} {m.agree} of {m.found} dimensions measure about {ratio(m.pt_per_in)} (e.g. {eg}). '
                        'The sheet was probably printed to fit. Check and confirm the scale to use.')
    found = m.found if m else 0
    why = 'No dimensions could be read on this sheet' if not found else \
        f'Only {found} dimension{"s" if found != 1 else ""} could be measured and they do not agree'
    return Decision('unchecked', None, None, 0, f'{why}, so the scale could not be checked.')
