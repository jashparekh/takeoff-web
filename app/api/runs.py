"""Runs (plan M4 step 4): a calculation frozen with its inputs, assumptions, engine version and results.

`POST /runs` refuses (409, with the list) while any value still needs a person's check or anything is left in
Review; a person must also tick "I have checked these". Results shown in the app come from the latest run; the
`calculate` endpoint stays as the what-if, never stored. A run is `current` while calculating now would give the
same quantities (drawings, Review and values unchanged since)."""
from __future__ import annotations
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select

from ..auth.deps import CurrentUser, Db, project_role
from ..db.models import Project, Run, SiteTakeoff, User
from ..engine import Calc
from ..services.inputs_builder import NotReady, build_inputs, engine_version
from ..services.values import ensure_values, needed
from .compare import auto_issues, notebook_compare

router = APIRouter()
Viewer = Annotated[Project, Depends(project_role('viewer'))]
Editor = Annotated[Project, Depends(project_role('editor'))]


def blockers(p: Project) -> list[str]:
    """What stops a run, in plain words. Empty when a run can be made."""
    out = []
    try:
        build_inputs(p)
    except NotReady as e:
        out.append(str(e).removeprefix('Not ready to calculate: ').rstrip('.'))
    left = needed(p)
    if left:
        names = ', '.join(a.label for a in left[:6]) + (f' and {len(left) - 6} more' if len(left) > 6 else '')
        out.append(f'{len(left)} value{"s" if len(left) != 1 else ""} still to confirm in Values we need: {names}')
    return out


def latest(db, p: Project) -> Run | None:
    return db.scalar(select(Run).where(Run.project_id == p.id).order_by(Run.created_at.desc()).limit(1))


def fingerprint(results: dict) -> tuple:
    """What a person would see change: totals and every beam's steel."""
    s = results['summary']
    return (round(s['steel_with_wastage_kg'], 1), round(s['concrete_m3'], 3), round(s.get('total_per_sqft', 0), 1),
            tuple((b['name'], round(b['kg'], 2)) for b in results['beams']))


def is_current(p: Project, run: Run) -> bool:
    try:
        return fingerprint(Calc(build_inputs(p)).run()) == fingerprint(run.results_json)
    except NotReady:
        return False


def run_meta(db, p: Project, run: Run | None) -> dict | None:
    if run is None:
        return None
    by = db.get(User, run.created_by).email if run.created_by else None
    s = run.results_json['summary']
    return dict(id=str(run.id), created_at=run.created_at.isoformat(), by=by, note=run.note,
                engine_version=run.engine_version, current=is_current(p, run),
                steel_t=s['steel_with_wastage_kg'] / 1000, concrete_m3=s['concrete_m3'],
                per_sqft=s.get('total_per_sqft'))


def with_site_check(db, p: Project, results: dict) -> dict:
    """Run results plus the site comparison (from the latest site takeoff, which can arrive after the run)."""
    nb = db.scalar(select(SiteTakeoff).where(SiteTakeoff.project_id == p.id)
                   .order_by(SiteTakeoff.created_at.desc()).limit(1))
    r = dict(results)
    r['notebook'] = notebook_compare(nb.data_json if nb else None, r)
    r['auto_issues'] = auto_issues(r, r['notebook'])
    return r


@router.get('/api/projects/{pid}/runs')
def list_runs(p: Viewer, db: Db):
    runs = db.scalars(select(Run).where(Run.project_id == p.id).order_by(Run.created_at.desc())).all()
    return [run_meta(db, p, r) | dict(current=None) for r in runs]


@router.get('/api/projects/{pid}/runs/latest')
def latest_run(p: Viewer, db: Db):
    run = latest(db, p)
    if run is None:
        raise HTTPException(404, 'No calculation yet.')
    return dict(run=run_meta(db, p, run), results=with_site_check(db, p, run.results_json))


class RunIn(BaseModel):
    checked: bool = False                      # "I have checked these"
    note: str | None = Field(None, max_length=500)


@router.post('/api/projects/{pid}/runs', status_code=201)
def create_run(p: Editor, req: RunIn, user: CurrentUser, db: Db):
    """Calculate and keep the result. Refused while anything is unconfirmed or left in Review."""
    if ensure_values(p):
        db.flush()
    missing = blockers(p)
    if missing:
        raise HTTPException(409, dict(message='Not ready to calculate.', missing=missing))
    if not req.checked:
        raise HTTPException(422, 'Tick "I have checked these" to calculate.')
    inputs = build_inputs(p)
    run = Run(project_id=p.id, engine_version=engine_version(), inputs_json=inputs, results_json=Calc(inputs).run(),
              note=req.note, created_by=user.id)
    db.add(run)
    db.flush()
    return dict(run=run_meta(db, p, run), results=with_site_check(db, p, run.results_json))
