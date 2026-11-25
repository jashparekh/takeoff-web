"""Projects API, read from the database. Every route under /api/projects/{pid} checks the caller's role.

Sheet drawings and the 3D model of the pilot still live in files (`app/data/<slug>/drawings`, `model.json`) until
uploads store them (M1); a project without those files simply has no drawings or model yet.
"""
from __future__ import annotations
import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select

from ..auth.deps import CurrentUser, Db, project_role, role_on
from ..db.models import Building, Floor, Issue, Project, ProjectMember, Run, SiteTakeoff
from ..services.inputs_builder import NotReady, assumptions_list, build_inputs, measured_floors
from ..engine import Calc
from ..services.model_builder import build_model
from .compare import auto_issues, notebook_compare

DATA = Path(__file__).parents[1] / 'data'
META_COLS = ('name', 'scope', 'client', 'city', 'consultant', 'revision', 'status')

router = APIRouter()
Viewer = Annotated[Project, Depends(project_role('viewer'))]


@lru_cache
def model_file(slug: str) -> dict | None:
    f = DATA / slug / 'model.json'
    return json.loads(f.read_text()) if f.exists() else None


def latest_run(db: Db, p: Project) -> Run | None:
    return db.scalar(select(Run).where(Run.project_id == p.id).order_by(Run.created_at.desc()).limit(1))


def meta(p: Project) -> dict:
    return {**(p.meta_json or {}), **{k: getattr(p, k) for k in META_COLS}, 'id': p.slug}


def issue_dict(i: Issue) -> dict:
    return dict(id=i.code, severity=i.severity, kind=i.kind, title=i.title, detail=i.detail, affects=i.affects,
                assumption=i.assumption, ask=i.ask, beams=i.beams, where=i.where_json, status=i.status)


def buildings(p: Project) -> list[dict]:
    return [dict(id=str(b.id), name=b.name, copies=b.copies,
                 floors=[dict(id=str(f.id), name=f.name, level_m=f.level_m, typical_count=f.typical_count,
                              measured=f.measured) for f in b.floors]) for b in p.buildings]


@router.get('/api/projects')
def list_projects(user: CurrentUser, db: Db):
    """Projects the signed-in user can open, with totals from the latest saved run (if any)."""
    member = select(ProjectMember.project_id).where(ProjectMember.user_id == user.id)
    ps = db.scalars(select(Project).where((Project.owner_id == user.id) | Project.id.in_(member))
                    .order_by(Project.created_at.desc(), Project.name)).all()
    out = []
    for p in ps:
        run = latest_run(db, p)
        s = run.results_json['summary'] if run else None
        open_issues = db.scalar(select(func.count()).select_from(Issue)
                                .where(Issue.project_id == p.id, Issue.status == 'open'))
        out.append(dict(id=p.slug, name=p.name, scope=p.scope, client=p.client, city=p.city, status=p.status,
                        role=role_on(db, p, user), issues=open_issues,
                        floors=sum(len(b.floors) for b in p.buildings),
                        measured=len(measured_floors(p)),
                        steel_t=s['steel_with_wastage_kg'] / 1000 if s else None,
                        concrete_m3=s['concrete_m3'] if s else None,
                        per_sqft=s['total_per_sqft'] if s else None))
    return out


class FloorIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    level_m: float | None = Field(None, ge=-20, le=500)
    typical_count: int = Field(1, ge=1, le=100)


class BuildingIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    copies: int = Field(1, ge=1, le=50)
    floors: list[FloorIn] = Field(min_length=1, max_length=100)


class ProjectIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    client: str | None = Field(None, max_length=200)
    city: str | None = Field(None, max_length=100)
    scope: str | None = Field(None, max_length=500)
    buildings: list[BuildingIn] = Field(min_length=1, max_length=20)

    @field_validator('name', 'client', 'city', 'scope', mode='before')
    @classmethod
    def strip(cls, v):
        return v.strip() or None if isinstance(v, str) else v


def new_slug(db: Db, name: str) -> str:
    base = re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-')[:56] or 'project'
    taken = set(db.scalars(select(Project.slug).where(Project.slug.like(f'{base}%'))))
    if base not in taken:
        return base
    return next(f'{base}-{i}' for i in range(2, 10_000) if f'{base}-{i}' not in taken)


@router.post('/api/projects', status_code=201)
def create_project(req: ProjectIn, user: CurrentUser, db: Db):
    """Create a project with its buildings and floors. The creator becomes its owner."""
    if not req.name:
        raise HTTPException(422, 'Project name is required')
    p = Project(slug=new_slug(db, req.name), owner_id=user.id, name=req.name, client=req.client, city=req.city,
                scope=req.scope, status='Waiting for drawings', meta_json={})
    for i, b in enumerate(req.buildings):
        bb = Building(name=b.name.strip(), copies=b.copies, sort=i)
        bb.floors = [Floor(name=f.name.strip(), level_m=f.level_m, typical_count=f.typical_count, sort=j)
                     for j, f in enumerate(b.floors)]
        p.buildings.append(bb)
    db.add(p)
    db.flush()
    db.add(ProjectMember(project_id=p.id, user_id=user.id, role='owner'))
    return dict(id=p.slug)


@router.get('/api/projects/{pid}')
def get_project(p: Viewer, user: CurrentUser, db: Db):
    try:
        beams, ready, not_ready = build_inputs(p)['beams'], True, None
    except NotReady as e:
        beams, ready, not_ready = [], False, str(e)
    return dict(meta=meta(p), role=role_on(db, p, user), ready=ready, not_ready=not_ready, buildings=buildings(p),
                assumptions=assumptions_list(p), issues=[issue_dict(i) for i in p.issues],
                model=build_model(db, p) or model_file(p.slug), beams_input=beams)


class CalcRequest(BaseModel):
    overrides: dict[str, float] = {}


@router.post('/api/projects/{pid}/calculate')
def calculate(p: Viewer, req: CalcRequest, db: Db):
    """Run the engine on the project's current data, with what-if overrides. Nothing is saved."""
    try:
        r = Calc(build_inputs(p), req.overrides).run()
    except NotReady as e:
        raise HTTPException(409, str(e))
    nb = db.scalar(select(SiteTakeoff).where(SiteTakeoff.project_id == p.id)
                   .order_by(SiteTakeoff.created_at.desc()).limit(1))
    cmp = notebook_compare(nb.data_json if nb else None, r)
    r['notebook'] = cmp
    r['auto_issues'] = auto_issues(r, cmp)
    return r


@router.get('/drawings/{pid}/{name}')
def drawing(p: Viewer, name: str):
    f = DATA / p.slug / 'drawings' / name
    if not re.fullmatch(r'[\w\-.]+\.svg', name) or '..' in name or not f.is_file():
        raise HTTPException(404)
    return FileResponse(f, media_type='image/svg+xml', headers={'Cache-Control': 'private, max-age=86400'})
