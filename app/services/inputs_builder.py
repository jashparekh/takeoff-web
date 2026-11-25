"""Build the engine's `inputs` dict from database rows.

This is the seam that keeps `app.engine.Calc` unchanged while data moves from `app/data/<project>/inputs.json`
into Postgres: the dict returned here has exactly the shape of `inputs.json`
(`{assumptions[], beams[], chajja{}, slab{}}`).
"""
from __future__ import annotations
import hashlib
from functools import lru_cache
from pathlib import Path

from sqlalchemy.orm import Session

from ..db.models import Floor, Project
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
                 confirm=a.needs_confirm) for a in project.assumptions]


def measured_floors(project: Project) -> list[Floor]:
    return [f for b in project.buildings for f in b.floors if f.measured and f.structure_json]


def build_inputs(project: Project) -> dict:
    floors = measured_floors(project)
    if not floors:
        raise NotReady('No measured floor yet: upload and review drawings first.')
    if len(floors) > 1:
        # Several floors, typical × n and building copies arrive in M6.
        raise NotReady('More than one measured floor is not supported yet.')
    s = floors[0].structure_json
    return dict(assumptions=assumptions_list(project), beams=s['beams'], chajja=s.get('chajja'), slab=s['slab'])


def calculate(project: Project, overrides: dict | None = None) -> dict:
    return Calc(build_inputs(project), overrides).run()


def get_project(session: Session, slug: str) -> Project | None:
    return session.query(Project).filter_by(slug=slug).one_or_none()
