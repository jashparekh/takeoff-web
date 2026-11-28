"""Build the engine's `inputs` dict from database rows.

This is the seam that keeps `app.engine.Calc` unchanged while data moves from `app/data/<project>/inputs.json`
into Postgres: the dict returned here has exactly the shape of `inputs.json`
(`{assumptions[], beams[], chajja{}, slab{}}`).
"""
from __future__ import annotations
import hashlib
from functools import lru_cache
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session, object_session

from ..db.models import BeamSpec, Floor, Member, Project
from ..engine import Calc

ENGINE_PY = Path(__file__).parents[1] / 'engine.py'


class NotReady(Exception):
    """The project can't be calculated yet (no measured floor, or a case the engine doesn't cover)."""


@lru_cache
def engine_version() -> str:
    """Content hash of engine.py, so a run records exactly which engine produced it."""
    return 'engine-' + hashlib.sha256(ENGINE_PY.read_bytes()).hexdigest()[:12]


def assumptions_list(project: Project) -> list[dict]:
    return [dict(key=a.key, label=a.label, value=a.value, unit=a.unit, group=a.group, source=a.source,
                 confirm=a.needs_confirm, confirmed=a.confirmed_at is not None) for a in project.assumptions]


CHECKED = ('accepted', 'edited', 'added')          # a person has looked at it in Review


def floor_members(db: Session, floor: Floor) -> list[Member]:
    return db.scalars(select(Member).where(Member.floor_id == floor.id, Member.kind == 'beam',
                                           Member.status != 'rejected')).all()


def measured_floors(project: Project) -> list[Floor]:
    """Floors with something to calculate: the pilot's stored structure, or beams read off an uploaded layout."""
    db = object_session(project)
    return [f for b in project.buildings for f in b.floors
            if (f.measured and f.structure_json) or (db is not None and floor_members(db, f))]


def natural(name: str):
    import re
    return [int(t) if t.isdigit() else t for t in re.split(r'(\d+)', name)]


def beams_from_drawings(db: Session, floor: Floor) -> list[dict]:
    """The engine's `beams` from the floor's layout members (span) and schedule readings (size, bars, stirrups).
    Every beam must have both, and both must have been checked in Review; otherwise NotReady says what is left."""
    members = [m for m in floor_members(db, floor) if (m.x1, m.y1) != (m.x2, m.y2)]
    specs = {s.name: s for s in db.scalars(select(BeamSpec).where(
        BeamSpec.floor_id == floor.id, BeamSpec.status != 'rejected', BeamSpec.duplicate_of.is_(None)))}
    todo_m = [m.name for m in members if m.status not in CHECKED]
    todo_s = [n for n in {m.name for m in members} if n in specs and specs[n].status not in CHECKED]
    no_spec = sorted({m.name for m in members} - set(specs), key=natural)
    no_span = sorted({m.name for m in members if not m.clear_in}, key=natural)
    problems = []
    if todo_m:
        problems.append(f'{len(todo_m)} beams on the layout still to check in Review')
    if todo_s:
        problems.append(f'{len(todo_s)} schedule readings still to check in Review')
    if no_spec:
        problems.append(f'no schedule reading for {", ".join(no_spec[:8])}{"…" if len(no_spec) > 8 else ""}')
    if no_span:
        problems.append(f'no span for {", ".join(no_span[:8])}')
    if problems:
        raise NotReady('Not ready to calculate: ' + '; '.join(problems) + '.')
    out = []
    for m in sorted(members, key=lambda m: natural(m.name)):
        sp = specs[m.name]
        out.append(dict(
            name=m.name, b_in=sp.b_in, D_in=sp.D_in, clear_in=m.clear_in,
            kind=sp.kind,
            stirrup=dict(dia=sp.stirrup_dia, end_in=sp.stirrup_end_in, mid_in=sp.stirrup_mid_in),
            bars=[dict(role=b.role, nos=b.nos, dia=b.dia, **({'extra_top_in': b.extra_top_in} if b.extra_top_in else {}))
                  for b in sp.bars]))
    return out


def build_inputs(project: Project) -> dict:
    floors = measured_floors(project)
    if not floors:
        raise NotReady('No measured floor yet: upload and review drawings first.')
    if len(floors) > 1:
        # Several floors, typical × n and building copies arrive in M6.
        raise NotReady('More than one measured floor is not supported yet.')
    f = floors[0]
    db = object_session(project)
    s = f.structure_json or {}
    beams = beams_from_drawings(db, f) if db is not None and floor_members(db, f) else s.get('beams')
    if not s.get('slab'):
        # Slab openings and chajjas come from the slab layouts, read in M5.
        raise NotReady('Not ready to calculate: the slab layouts have not been read yet.')
    slab = dict(s['slab'])
    if db is not None:
        slab |= slab_panels(db, f)
    return dict(assumptions=assumptions_list(project), beams=beams, chajja=s.get('chajja'), slab=slab)


def slab_panels(db: Session, floor: Floor) -> dict:
    """The floor's slab readings as the engine takes them: `panels` (bottom bars, per panel) and `top` (bars over
    beams, per group, with the sheet's `top_distribution`). Each is given only once every one of its items has been
    checked in Review; until then that part is measured by average spacing, shown as an assumption."""
    ps = db.scalars(select(Member).where(Member.floor_id == floor.id, Member.kind == 'panel',
                                         Member.status != 'rejected')).all()
    bottom = [p for p in ps if 'bars' in (p.data or {})]
    top = [p for p in ps if 'top' in (p.data or {})]
    out = {}
    if bottom and all(p.status in CHECKED for p in bottom):
        out['panels'] = [dict(name=p.name, area_sqft=p.area_sqft, thk_in=p.data.get('thk_in'), bars=p.data['bars'])
                         for p in sorted(bottom, key=lambda p: natural(p.name))]
    if top and all(p.status in CHECKED for p in top):
        out['top'] = [dict(name=p.name, **p.data['top']) for p in sorted(top, key=lambda p: natural(p.name))]
        out['top_distribution'] = next((p.data.get('distribution') for p in top if p.data.get('distribution')), None)
    return out


def panels_waiting(db: Session, floor: Floor) -> int:
    """Panels read but not yet checked in Review."""
    return len(db.scalars(select(Member.id).where(Member.floor_id == floor.id, Member.kind == 'panel',
                                                  Member.status == 'auto')).all())


def calculate(project: Project, overrides: dict | None = None) -> dict:
    return Calc(build_inputs(project), overrides).run()


def get_project(session: Session, slug: str) -> Project | None:
    return session.query(Project).filter_by(slug=slug).one_or_none()
