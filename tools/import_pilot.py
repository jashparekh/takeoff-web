"""Load a file-backed pilot project (app/data/<slug>/) into the database.

    python -m tools.import_pilot worklounge            # fails if the project already exists
    python -m tools.import_pilot worklounge --replace  # delete and re-import

Needs DATABASE_URL (see .env.example) and migrations applied (`alembic upgrade head`).
"""
from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path

from sqlalchemy.orm import Session

from app.db.models import Assumption, Building, Floor, Issue, Project, Run, SiteTakeoff
from app.db.session import session_factory
from app.engine import Calc
from app.services.inputs_builder import engine_version
from app.services.values import ensure_values

DATA = Path(__file__).parents[1] / 'app' / 'data'
META_COLS = ('name', 'scope', 'client', 'consultant', 'revision', 'status')
ISSUE_COLS = ('severity', 'kind', 'title', 'detail', 'affects', 'assumption', 'ask', 'beams')

# Work Lounge: only the second floor's drawings were received; the 3D view shows floors 1–4 at 3.0 m
# (a display assumption, see CLAUDE.md). Other pilots default to one measured floor.
FLOORS = {'worklounge': dict(city='Hubballi', building='Main building', measured=2,
                             floors=['First floor', 'Second floor', 'Third floor', 'Fourth floor'], floor_h=3.0)}


def read(d: Path, name: str):
    f = d / name
    return json.loads(f.read_text()) if f.exists() else None


def import_pilot(session: Session, slug: str, replace: bool = False, data_dir: Path = DATA) -> Project:
    d = data_dir / slug
    meta, inputs = read(d, 'project.json'), read(d, 'inputs.json')
    if not meta or not inputs:
        raise FileNotFoundError(f'{d} needs project.json and inputs.json')
    old = session.query(Project).filter_by(slug=slug).one_or_none()
    if old:
        if not replace:
            raise SystemExit(f'Project {slug!r} already exists; pass --replace to re-import.')
        session.delete(old)
        session.flush()

    cfg = FLOORS.get(slug, dict(city=None, building='Main building', measured=1, floors=['Floor'], floor_h=None))
    p = Project(slug=slug, city=cfg['city'], **{k: meta.get(k) for k in META_COLS},
                meta_json={k: v for k, v in meta.items() if k not in META_COLS + ('id',)})
    session.add(p)

    b = Building(name=cfg['building'], copies=1, sort=0)
    p.buildings.append(b)
    structure = dict(beams=inputs['beams'], chajja=inputs.get('chajja'), slab=inputs['slab'])
    for i, name in enumerate(cfg['floors'], start=1):
        measured = i == cfg['measured']
        b.floors.append(Floor(name=name, sort=i, typical_count=1, measured=measured,
                              level_m=i * cfg['floor_h'] if cfg['floor_h'] else None,
                              structure_json=structure if measured else None))

    for i, a in enumerate(inputs['assumptions']):
        p.assumptions.append(Assumption(key=a['key'], label=a['label'], value=float(a['value']), unit=a.get('unit'),
                                        group=a.get('group'), source=a.get('source'),
                                        needs_confirm=bool(a.get('confirm')), sort=i))
    ensure_values(p)                     # values the workbook does not hold (grades, conventions): proposed, unconfirmed
    for x in read(d, 'issues.json') or []:
        p.issues.append(Issue(code=x['id'], where_json=x.get('where'), **{k: x.get(k) for k in ISSUE_COLS}))

    session.flush()
    nb = read(d, 'notebook.json')
    if nb:
        session.add(SiteTakeoff(project_id=p.id, source=nb['source'], data_json=nb))
    session.add(Run(project_id=p.id, engine_version=engine_version(), inputs_json=inputs,
                    results_json=Calc(inputs).run(), note=f'Imported from app/data/{slug}'))
    return p


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('slug')
    ap.add_argument('--replace', action='store_true')
    args = ap.parse_args(argv)
    with session_factory()() as s, s.begin():
        p = import_pilot(s, args.slug, args.replace)
    print(f'Imported {p.slug}: {len(p.assumptions)} assumptions, {len(p.issues)} issues, '
          f'{sum(len(b.floors) for b in p.buildings)} floors')


if __name__ == '__main__':
    sys.exit(main())
