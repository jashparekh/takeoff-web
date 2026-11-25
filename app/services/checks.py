"""Cross-checks between a floor's layout and its schedules (plan M3 step 6). Each finding is an Issue with a stable
code, so re-running updates it, and an Issue that no longer applies is removed. Run after the layout, schedule and
vision stages, and after a change in Review.

  size     layout label and schedule disagree on a beam's size (pilot: B5 9"X24" on the layout, 9"X18" in the schedule)
  no-sched a beam on the layout has no schedule reading
  no-layout a beam in the schedule is not on the layout
  cant     the schedule settles cantilevers: a layout cantilever with no schedule part is marked doubtful
"""
from __future__ import annotations
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db.models import BeamSpec, Issue, Member

RULES = ('check_size', 'check_no_schedule', 'check_not_on_layout', 'check_cantilever')


def natural(name: str):
    return [int(t) if t.isdigit() else t for t in re.split(r'(\d+)', name)]


def listing(names, n=12) -> str:
    names = sorted(names, key=natural)
    return ', '.join(names[:n]) + (f' and {len(names) - n} more' if len(names) > n else '')


def run(db: Session, project_id) -> list[Issue]:
    members = db.scalars(select(Member).where(Member.project_id == project_id, Member.kind == 'beam',
                                              Member.status != 'rejected')).all()
    specs = db.scalars(select(BeamSpec).where(BeamSpec.project_id == project_id, BeamSpec.status != 'rejected',
                                              BeamSpec.duplicate_of.is_(None))).all()
    if not members or not specs:                    # need both a layout and a schedule to compare
        return []
    by_m = {}
    for m in members:
        by_m.setdefault(m.name, m)
    by_s = {s.name: s for s in specs}
    found: dict[str, dict] = {}

    for name, m in by_m.items():
        s = by_s.get(name)
        if s and m.label_b_in and m.label_d_in and s.b_in and s.D_in and \
                (float(m.label_b_in), float(m.label_d_in)) != (float(s.b_in), float(s.D_in)):
            found[f'X-SIZE-{name}'] = dict(
                rule='check_size', severity='medium', kind='Drawings disagree', beams=[name],
                title=f'{name}: layout says {m.label_b_in:g}"×{m.label_d_in:g}", schedule says {s.b_in:g}"×{s.D_in:g}"',
                detail='The beam layout label and the beam schedule give different sizes for the same beam.',
                affects='Concrete, stirrups', assumption=f'Schedule size {s.b_in:g}"×{s.D_in:g}" used',
                ask=f'Confirm the size of {name}.')

    cants_m = {n for n in by_m if '-cant' in n}
    plain_m = set(by_m) - cants_m
    no_sched = sorted(n for n in plain_m if n not in by_s)
    if no_sched:
        found['X-NOSCHED'] = dict(
            rule='check_no_schedule', severity='high', kind='Missing in schedule', beams=no_sched,
            title=f'{len(no_sched)} beam{"s" if len(no_sched) != 1 else ""} on the layout with no schedule reading',
            detail=f'On the beam layout but not read from any beam schedule: {listing(no_sched)}.',
            affects='Steel quantity', assumption='Not counted until a schedule reading exists',
            ask='Check the schedule sheets for these beams, or confirm they are not beams.')
    no_layout = sorted(n for n in by_s if n not in by_m)
    if no_layout:
        found['X-NOLAYOUT'] = dict(
            rule='check_not_on_layout', severity='high', kind='Missing on layout', beams=sorted({n.split('-')[0] for n in no_layout}),
            title=f'{len(no_layout)} schedule beam{"s" if len(no_layout) != 1 else ""} not found on the layout',
            detail=f'In the beam schedule but not placed on the layout: {listing(no_layout)}.',
            affects='Steel quantity, spans', assumption='Not counted until placed on the layout',
            ask='Find these on the layout (Review → add a missed beam), or confirm they are not on this floor.')

    # The schedule says which beams have cantilever parts; a layout cantilever without one is doubtful.
    doubtful = []
    for n in cants_m:
        base = n.split('-')[0]
        m = by_m[n]
        has_part = any(s.startswith(base + '-cant') for s in by_s)
        if base in by_s and not has_part and m.status == 'auto':
            m.confidence = min(m.confidence, 0.2)
            note = 'The schedule shows no cantilever part for this beam.'
            if note not in (m.note or ''):
                m.note = f'{m.note} {note}'.strip() if m.note else note
            doubtful.append(n)
    if doubtful:
        found['X-CANT'] = dict(
            rule='check_cantilever', severity='low', kind='Layout vs schedule', beams=sorted({n.split('-')[0] for n in doubtful}),
            title=f'{len(doubtful)} overhang{"s" if len(doubtful) != 1 else ""} on the layout with no cantilever in the schedule',
            detail=f'Read as cantilevers from the layout, but their beams have no cantilever part in the schedule: {listing(doubtful)}.',
            affects='Steel quantity', assumption='Shown in Review as doubtful',
            ask='Mark them not on drawing in Review, or confirm the overhang.')

    out = []
    existing = {i.code: i for i in db.scalars(select(Issue).where(Issue.project_id == project_id, Issue.rule.in_(RULES)))}
    for code, f in found.items():
        i = existing.pop(code, None)
        if i is None:
            i = Issue(project_id=project_id, code=code)
            db.add(i)
        for k, v in f.items():
            setattr(i, k, v)
        out.append(i)
    for i in existing.values():                       # no longer applies
        if i.status == 'open':
            db.delete(i)
    db.flush()
    return out
