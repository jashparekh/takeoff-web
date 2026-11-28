"""Drawing revisions (plan M5 step 2).

A sheet whose drawing number matches another sheet of the project with a higher revision supersedes it (whichever
arrives first). A superseded sheet keeps its readings for comparison, but nothing counts them: every query of
members and schedule readings goes through `live()`. The new revision is read like any sheet; then `carry_over`
matches the old revision's checked items to the new reading (by name, then position):

  - reads the same as what the person checked → the person's decision carries over (accepted / rejected), noted;
  - changed → left for Review, least confident first, with a note saying what changed;
  - added by a person and not read on the new revision either → carried over as added, noted.
"""
from __future__ import annotations
import math
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db.models import BeamSpec, Member, Sheet

CHECKED = ('accepted', 'edited', 'added')
NEAR_PT = 30                       # same beam on the new revision: same name, midpoint within this
SAME_PT = 3                        # reads the same: ends within this


def live():
    """Sheets that count: not superseded by a later revision."""
    return select(Sheet.id).where(Sheet.superseded_by.is_(None))


def rev_key(rev: str | None) -> tuple | None:
    """R0 < R1 < R2…; A < B; plain numbers. None when it can't be read."""
    if not rev:
        return None
    t = rev.strip().upper()
    m = re.search(r'R\s*(\d+)', t) or re.fullmatch(r'(\d+)', t)
    if m:
        return (1, int(m.group(1)))
    m = re.fullmatch(r'(?:REV\.?\s*)?([A-Z])', t)
    return (0, ord(m.group(1))) if m else None


def norm_drg(drg: str | None) -> str | None:
    return re.sub(r'[\s_]+', '', drg).upper() if drg else None


def link(db: Session, s: Sheet) -> Sheet | None:
    """After classify: if another live sheet has the same drawing number, the higher revision supersedes the lower.
    Returns the sheet `s` supersedes, if any. Same or unreadable revisions are left alone (with a note)."""
    key = norm_drg(s.drg_no)
    if not key or s.superseded_by:
        return None
    others = [o for o in db.scalars(select(Sheet).where(Sheet.project_id == s.project_id, Sheet.id != s.id,
                                                        Sheet.superseded_by.is_(None), Sheet.drg_no.is_not(None)))
              if norm_drg(o.drg_no) == key]
    replaced = None
    for o in others:
        a, b = rev_key(s.revision), rev_key(o.revision)
        if a is None or b is None or a == b:
            note = f'Same drawing number as another sheet ({o.drg_no} {o.revision or "no revision"}); both are kept.'
            s.classify_note = f'{s.classify_note} {note}'.strip() if s.classify_note and note not in s.classify_note else note
            continue
        if a > b:
            o.superseded_by = s.id
            replaced = o
        else:
            s.superseded_by = o.id
            break
    db.flush()
    return replaced


def predecessor(db: Session, s: Sheet) -> Sheet | None:
    return db.scalar(select(Sheet).where(Sheet.superseded_by == s.id).order_by(Sheet.created_at.desc()).limit(1))


def _mid(m) -> tuple:
    return ((m.x1 + m.x2) / 2, (m.y1 + m.y2) / 2)


def _same_beam(old: Member, new: Member) -> list[str]:
    """What differs between the person's checked beam and the new reading (empty: reads the same)."""
    out = []
    ends_o = sorted([(old.x1, old.y1), (old.x2, old.y2)])
    ends_n = sorted([(new.x1, new.y1), (new.x2, new.y2)])
    if any(math.dist(a, b) > SAME_PT for a, b in zip(ends_o, ends_n)):
        out.append('position or length')
    if (old.label_b_in, old.label_d_in) != (new.label_b_in, new.label_d_in) and new.label_b_in and new.label_d_in:
        out.append(f'size {new.label_b_in:g}"×{new.label_d_in:g}" (was {old.label_b_in or 0:g}"×{old.label_d_in or 0:g}")')
    if old.clear_in and new.clear_in and abs(old.clear_in - new.clear_in) > 0.5:
        out.append(f'span {new.clear_in:g}" (was {old.clear_in:g}")')
    return out


def _bars(b: BeamSpec) -> list:
    return sorted((x.role, x.nos, x.dia, x.extra_top_in or 0) for x in b.bars)


def _same_spec(old: BeamSpec, new: BeamSpec) -> list[str]:
    out = []
    if (old.b_in, old.D_in) != (new.b_in, new.D_in):
        out.append(f'size {new.b_in or 0:g}"×{new.D_in or 0:g}" (was {old.b_in or 0:g}"×{old.D_in or 0:g}")')
    if (old.stirrup_dia, old.stirrup_end_in, old.stirrup_mid_in) != (new.stirrup_dia, new.stirrup_end_in, new.stirrup_mid_in):
        out.append('stirrups')
    if _bars(old) != _bars(new):
        out.append('bars')
    return out


def _panel_overlap(a: Member, b: Member) -> float:
    ix = max(0, min(a.x2, b.x2) - max(a.x1, b.x1))
    iy = max(0, min(a.y2, b.y2) - max(a.y1, b.y1))
    small = min((a.x2 - a.x1) * (a.y2 - a.y1), (b.x2 - b.x1) * (b.y2 - b.y1))
    return ix * iy / small if small > 0 else 0


def _settle(old, new, diffs: list[str], rev: str) -> None:
    """Carry a person's decision to the matching item on the new revision, or flag what changed."""
    if old.status == 'rejected':
        new.status = 'rejected'
        new.note = ' '.join(filter(None, [new.note, f'Marked not on drawing on {rev}.']))
    elif not diffs:
        new.status = 'accepted'
        new.note = ' '.join(filter(None, [new.note, f'Same as checked on {rev}.']))
    else:
        new.confidence = min(new.confidence or 0, 0.3)
        new.note = ' '.join(filter(None, [new.note, f'Changed since {rev}: {", ".join(diffs)}.']))


def carry_over(db: Session, s: Sheet) -> dict:
    """Bring people's decisions from the revision `s` superseded. Returns counts for the stage note."""
    old_s = predecessor(db, s)
    if old_s is None:
        return {}
    rev = old_s.revision or 'the previous revision'
    counts = dict(kept=0, changed=0, added=0)
    # Layout beams and slab panels.
    olds = db.scalars(select(Member).where(Member.sheet_id == old_s.id, Member.status.in_(CHECKED + ('rejected',)),
                                           Member.kind.in_(('beam', 'panel')))).all()
    news = db.scalars(select(Member).where(Member.sheet_id == s.id, Member.status == 'auto')).all()
    taken = set()
    for o in olds:
        if o.kind == 'beam':
            cands = [n for n in news if n.kind == 'beam' and n.id not in taken and n.name == o.name
                     and math.dist(_mid(n), _mid(o)) <= NEAR_PT]
            n = min(cands, key=lambda n: math.dist(_mid(n), _mid(o)), default=None)
            diffs = _same_beam(o, n) if n else None
        else:
            cands = [n for n in news if n.kind == 'panel' and n.id not in taken and _panel_overlap(n, o) > 0.8]
            n = max(cands, key=lambda n: _panel_overlap(n, o), default=None)
            diffs = ([] if n and (n.data or {}) == (o.data or {}) else ['bars or thickness']) if n else None
        if n is not None:
            taken.add(n.id)
            _settle(o, n, diffs, rev)
            counts['changed' if diffs and o.status != 'rejected' else 'kept'] += 1
        elif o.status == 'added':
            db.add(Member(project_id=s.project_id, sheet_id=s.id, floor_id=s.floor_id, kind=o.kind, name=o.name,
                          vertical=o.vertical, x1=o.x1, y1=o.y1, x2=o.x2, y2=o.y2, b_in=o.b_in,
                          label_b_in=o.label_b_in, label_d_in=o.label_d_in, clear_in=o.clear_in,
                          clear_source=o.clear_source, data=o.data, confidence=0.5, status='added',
                          note=f'Added by a person on {rev} and not read on this revision either; check it is still drawn.'))
            counts['added'] += 1
    # Schedule readings.
    olds = db.scalars(select(BeamSpec).where(BeamSpec.sheet_id == old_s.id,
                                             BeamSpec.status.in_(CHECKED + ('rejected',)))).all()
    news = {b.name: b for b in db.scalars(select(BeamSpec).where(BeamSpec.sheet_id == s.id, BeamSpec.status == 'auto'))}
    for o in olds:
        n = news.get(o.name)
        if n is None:
            continue
        diffs = _same_spec(o, n)
        if o.status == 'rejected':
            n.status = 'rejected'
        elif not diffs:
            n.status = 'accepted'
        else:
            n.confidence = min(n.confidence or 0, 0.3)
        n.notes = ' '.join(filter(None, [n.notes, f'Same as checked on {rev}.' if not diffs else
                                         f'Changed since {rev}: {", ".join(diffs)}.']))
        counts['changed' if diffs and o.status != 'rejected' else 'kept'] += 1
    db.flush()
    return counts


def carried_note(c: dict) -> str:
    if not c:
        return ''
    parts = [f'{c["kept"]} checked items carried over'] if c.get('kept') else []
    if c.get('changed'):
        parts.append(f'{c["changed"]} changed since the last revision (in Review)')
    if c.get('added'):
        parts.append(f'{c["added"]} added by a person carried over')
    return (' ' + '; '.join(parts) + '.') if parts else ''
