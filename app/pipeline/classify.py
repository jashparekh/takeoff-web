"""Read a sheet's title block: drawing number, title, revision, scale, date; then its type and floor.

Plan M1 step 6. This version reads the PDF's text layer. Sheets without one (scans, or CAD exports with all text as
outlines) come back as needing review; OCR of the title-block crop is the next step for them.

Nothing here guesses silently: when the type or floor isn't clear from the title, the sheet is marked
`needs_review` with a plain-words note, and a person picks it on the Coverage grid.
"""
from __future__ import annotations
import re
from dataclasses import dataclass, field

from .pdf import TextLine

# Sheet types, in the order the Coverage grid shows them. Keywords are matched against the title (upper case);
# the first type with a match wins, so more specific entries come first.
TYPES: dict[str, tuple[str, list[str]]] = {
    'beam_layout':     ('Beam layout', [r'SHUTTERING', r'BEAM\s*LAYOUT', r'FRAMING\s*PLAN', r'BEAM\s*PLAN']),
    'beam_schedule':   ('Beam schedule', [r'BEAM\s*(REINF|SCHEDULE|DETAIL|R/F)']),
    'slab_bottom':     ('Slab bottom bars', [r'BOTTOM\s*SLAB', r'SLAB\s*BOTTOM']),
    'slab_top':        ('Slab top bars', [r'TOP\s*SLAB', r'SLAB\s*TOP']),
    'slab':            ('Slab', [r'SLAB']),
    'column_schedule': ('Column schedule', [r'COLUMN\s*(SCHEDULE|DETAIL|REINF)']),
    'column_layout':   ('Column layout', [r'COLUMN\s*(LAYOUT|PLAN|MARKING)', r'CENT(RE|ER)\s*LINE']),
    'footing':         ('Footings', [r'FOOTING', r'FOUNDATION', r'PILE']),
    'staircase':       ('Staircase', [r'STAIR']),
    'notes':           ('General notes', [r'GENERAL\s*NOTES']),
    'other':           ('Other', []),
}
TYPE_LABELS = {k: v[0] for k, v in TYPES.items()}
COVERAGE_TYPES = ['beam_layout', 'beam_schedule', 'slab_bottom', 'slab_top']   # what a floor needs for M2–M5

# Floor words → a canonical key: (key, words, short forms). Words match anywhere in the text; short forms (SF, G.F.)
# only as a separate token, since two letters inside other words mean nothing.
ORD = ['FIRST', 'SECOND', 'THIRD', 'FOURTH', 'FIFTH', 'SIXTH', 'SEVENTH', 'EIGHTH', 'NINTH', 'TENTH']
SUFFIX = {1: 'ST', 2: 'ND', 3: 'RD'}
FLOOR_WORDS: list[tuple[str, list[str], list[str]]] = [
    ('basement', [r'BASEMENT', r'CELLAR'], []),
    ('stilt', [r'STILT'], []),
    ('plinth', [r'PLINTH'], []),
    ('ground', [r'GROUND'], [r'G\.?F\.?']),
    *[(str(n), [ORD[n - 1], rf'{n}\s*{SUFFIX.get(n, "TH")}'], [rf'{ORD[n - 1][0]}\.?F\.?'] if n <= 3 else [])
      for n in range(1, 11)],
    ('terrace', [r'TERRACE', r'ROOF'], []),
    ('headroom', [r'HEAD\s*ROOM', r'MUMTY'], []),
]


def floor_key(text: str) -> str | None:
    """'SECOND FLOOR', '2ND FLR', 'SF' → '2'; 'Ground floor' → 'ground'. None if no floor is named, or several."""
    u = re.sub(r'[_]+', ' ', text.upper())
    found = set()
    for key, words, short in FLOOR_WORDS:
        if any(re.search(rf'(?<![A-Z]){w}(?![A-Z])', u) for w in words) or \
                any(re.search(rf'(?<![A-Z0-9.]){p}(?![A-Z0-9])', u) for p in short):
            found.add(key)
    if not found and (m := re.search(r'\b(?:FLOOR|FLR|LEVEL)\s*(\d{1,2})\b', u)):    # "Floor 11", "Level 3"
        found.add(m.group(1).lstrip('0') or 'ground')
    return found.pop() if len(found) == 1 else None


DRG_NO = re.compile(r'\b[A-Z]{1,6}(?:[-/][A-Z0-9]{1,6}){1,5}\b')
REV = re.compile(r'\b(?:REV(?:ISION)?\.?\s*:?\s*([A-Z]?\d{1,2}|[A-Z]))\b|\b(R\d{1,2})\b')
SCALE = re.compile(r'\b1\s*:\s*\d{1,4}\b')
DATE = re.compile(r'\b\d{1,2}[./-]\d{1,2}[./-]\d{2,4}\b')
TITLE_WORDS = re.compile(r'FLOOR|LAYOUT|SCHEDULE|DETAIL|REINF|PLAN|SECTION|FOOTING|FOUNDATION|COLUMN|SLAB|BEAM|STAIR|NOTES')


@dataclass
class TitleBlock:
    drg_no: str | None = None
    title: str | None = None
    revision: str | None = None
    scale: str | None = None
    date: str | None = None
    lines: list[str] = field(default_factory=list)


def title_block_lines(lines: list[TextLine]) -> list[TextLine]:
    """Text in the usual title-block places: bottom-right corner, then the right strip, then the bottom strip."""
    for keep in (lambda t: t.x0 > 0.6 and t.y0 > 0.6, lambda t: t.x0 > 0.7, lambda t: t.y0 > 0.8):
        got = [t for t in lines if keep(t)]
        if any(DRG_NO.search(t.text.upper()) and re.search(r'\d', t.text) for t in got):
            return got
    return []


def read_title_block(lines: list[TextLine]) -> TitleBlock:
    tb_lines = sorted(title_block_lines(lines), key=lambda t: (round(t.y0, 3), t.x0))
    tb = TitleBlock(lines=[t.text for t in tb_lines])
    up = [(t, t.text.upper()) for t in tb_lines]
    for t, u in up:
        if tb.drg_no is None and (m := DRG_NO.search(u)) and re.search(r'\d', m.group()) and not DATE.fullmatch(m.group()):
            tb.drg_no = m.group()
        if tb.revision is None and (m := REV.search(u)):
            tb.revision = m.group(1) or m.group(2)
        if tb.scale is None and (m := SCALE.search(u)):
            tb.scale = m.group().replace(' ', '')
        if tb.date is None and (m := DATE.search(u)):
            tb.date = m.group()
    # Title: the first line naming a floor or a drawing kind, plus the lines just below it (titles wrap).
    anchor = next((i for i, (t, u) in enumerate(up) if TITLE_WORDS.search(u) and 'STRUCTURAL DRAWING' not in u
                   and not DRG_NO.fullmatch(u)), None)
    if anchor is not None:
        parts, last = [up[anchor][0]], up[anchor][0]
        for t, u in up[anchor + 1:]:
            if 0 < t.y0 - last.y0 <= 0.02 and abs(t.x0 - parts[0].x0) < 0.15 and not DRG_NO.fullmatch(u):
                parts.append(t)
                last = t
        tb.title = ' '.join(p.text for p in parts).strip(' ,')
    return tb


def sheet_type(title: str | None) -> str | None:
    if not title:
        return None
    u = title.upper()
    for key, (_, pats) in TYPES.items():
        if any(re.search(p, u) for p in pats):
            return key
    return None


@dataclass
class Result:
    title_block: TitleBlock
    sheet_type: str | None
    floor_id: object | None
    status: str                    # auto | needs_review
    note: str | None


def classify(lines: list[TextLine], floors: list[tuple[object, str]], filename: str = '') -> Result:
    """`floors`: (floor id, floor name) for every floor in the project, all buildings."""
    tb = read_title_block(lines)
    notes = []
    if not lines:
        notes.append('This sheet has no readable text, so its title block could not be read.')
    elif not tb.lines:
        notes.append('No title block found.')
    st = sheet_type(tb.title) if tb.title else None
    if st is None and tb.lines:
        notes.append('The title does not say what kind of drawing this is.')

    by_key: dict[str, list] = {}
    for fid, name in floors:
        if (k := floor_key(name)) is not None:
            by_key.setdefault(k, []).append(fid)
    fk = floor_key(tb.title) if tb.title else None
    source = 'title'
    if fk is None:                                       # fall back to the file name ("260919_SF_BEAM LAYOUT.pdf")
        fk, source = floor_key(filename.rsplit('.', 1)[0]), 'file name'
    floor_id = None
    if fk is None:
        if tb.lines:                                     # no title block at all is already said above
            notes.append('The title does not name a floor.')
    elif len(by_key.get(fk, [])) == 1:
        floor_id = by_key[fk][0]
        if source == 'file name':
            notes.append('The floor was taken from the file name, not the title block.')
    elif len(by_key.get(fk, [])) > 1:
        notes.append('More than one building has this floor; pick the building.')
    else:
        notes.append("The floor named on the sheet is not one of this project's floors.")
    ok = st is not None and floor_id is not None and source == 'title'
    return Result(tb, st, floor_id, 'auto' if ok else 'needs_review', ' '.join(notes) or None)
