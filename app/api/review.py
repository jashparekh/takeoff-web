"""Review: a person checks what the layout reader found (plan M2 step 3).

Queue = members still `auto`, least confident first (labels with no beam found come first, at confidence 0).
Accept, edit (name, label size, or redraw the centreline on the sheet), mark "not on drawing", or add a beam the
reader missed. Every change writes `corrections`, so the reader can be measured against what people decided.
"""
from __future__ import annotations
import logging
import uuid
from typing import Annotated, Callable, Literal

import anyio
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from ..auth.deps import CurrentUser, Db, project_role
from ..db.models import BAR_ROLES, Bar, BeamSpec, Correction, Member, Project, Sheet
from ..services import checks
from ..services.revisions import live
from ..settings import get_settings
from .uploads import member_dict, own_sheet, spec_dict

router = APIRouter()
Viewer = Annotated[Project, Depends(project_role('viewer'))]
Editor = Annotated[Project, Depends(project_role('editor'))]
NAME = r'^(?:H?B|CB)\d{1,3}[A-Z]?(?:-[A-Za-z]+)?$'
log = logging.getLogger(__name__)
NOT_REVIEWED = ('column',)                 # read and shown on the sheet, but not counted until M6


def get_relayout() -> Callable:
    """Queues layout re-reads (sheet ids, reason). Overridden in tests. Without REDIS_URL nothing is queued; the
    worker's minute sweep finds layouts read with an older schedule anyway."""
    if not get_settings().redis_url:
        return lambda ids, reason: log.warning('REDIS_URL not set: layouts %s not queued', ids)
    from ..pipeline.jobs import enqueue_layouts
    return lambda ids, reason: anyio.from_thread.run(enqueue_layouts, ids, reason)


Relayout = Annotated[Callable, Depends(get_relayout)]


def relayout_if_needed(db, p: Project, floor_id, relayout: Callable, tasks: BackgroundTasks) -> int:
    """A schedule change in Review can change which overhangs on the floor's layouts are cantilevers (the schedule
    settles them). Layouts read with an older schedule are queued to be read again, after this change is committed
    (people's decisions on members are kept). Returns how many."""
    from ..pipeline import stages
    stale = [sid for sid in stages.layouts_on_floor(db, p.id, floor_id)
             if stages.stale_schedule(db, db.get(Sheet, sid))]
    if stale:
        db.commit()                                  # the worker must see this change
        tasks.add_task(relayout, stale, f'review:{uuid.uuid4().hex[:8]}')
    return len(stale)


def item(m: Member, s: Sheet, slug: str) -> dict:
    return dict(member_dict(m, s.pt_per_in), item='member', sheet=dict(
        id=str(s.id), drg_no=s.drg_no, title=s.title, width_pt=s.width_pt, height_pt=s.height_pt, pt_per_in=s.pt_per_in,
        image_url=f'/api/projects/{slug}/sheets/{s.id}/display.png'))


def spec_item(b: BeamSpec, s: Sheet, slug: str) -> dict:
    return dict(spec_dict(b), item='spec', sheet=dict(
        id=str(s.id), drg_no=s.drg_no, title=s.title, width_pt=s.width_pt, height_pt=s.height_pt, pt_per_in=s.pt_per_in,
        image_url=f'/api/projects/{slug}/sheets/{s.id}/display.png'))


@router.get('/api/projects/{pid}/review')
def review_queue(p: Viewer, db: Db, limit: int = 500):
    """Layout members and schedule readings not yet checked, least confident first; plus progress counts.
    Repeats of another sheet are not listed (they are counted once, from the other sheet). Columns are left out
    until they are counted (M6): checking them now would change no quantity."""
    rows = db.execute(select(Member, Sheet).join(Sheet, Member.sheet_id == Sheet.id)
                      .where(Member.project_id == p.id, Member.status == 'auto', Member.kind.notin_(NOT_REVIEWED),
                             Member.sheet_id.in_(live()))
                      .order_by(Member.confidence, Sheet.drg_no, Member.kind, Member.name).limit(limit)).all()
    srows = db.execute(select(BeamSpec, Sheet).join(Sheet, BeamSpec.sheet_id == Sheet.id)
                       .where(BeamSpec.project_id == p.id, BeamSpec.status == 'auto', BeamSpec.duplicate_of.is_(None),
                              BeamSpec.sheet_id.in_(live()))
                       .order_by(BeamSpec.confidence, BeamSpec.name).limit(limit)).all()
    items = sorted([item(m, s, p.slug) for m, s in rows] + [spec_item(b, s, p.slug) for b, s in srows],
                   key=lambda i: (i['confidence'], i['item'], checks.natural(i['name'] or '')))
    counts = dict(db.execute(select(Member.status, func.count()).where(Member.project_id == p.id,
                                                                       Member.kind.notin_(NOT_REVIEWED),
                                                                       Member.sheet_id.in_(live()))
                             .group_by(Member.status)).all())
    for st, n in db.execute(select(BeamSpec.status, func.count()).where(
            BeamSpec.project_id == p.id, BeamSpec.duplicate_of.is_(None),
            BeamSpec.sheet_id.in_(live())).group_by(BeamSpec.status)).all():
        counts[st] = counts.get(st, 0) + n
    total = sum(counts.values())
    return dict(total=total, checked=total - counts.get('auto', 0), counts=counts, items=items[:limit])


def own_member(db, p: Project, member_id: str) -> Member:
    try:
        m = db.get(Member, uuid.UUID(member_id))
    except ValueError:
        m = None
    if m is None or m.project_id != p.id:
        raise HTTPException(404)
    return m


class MemberPatch(BaseModel):
    action: Literal['accept', 'reject', 'reopen'] | None = None
    name: str | None = Field(None, pattern=NAME)
    label_b_in: float | None = Field(None, gt=0, le=120)
    label_d_in: float | None = Field(None, gt=0, le=120)
    x1: float | None = None                        # redraw: all four, in sheet points
    y1: float | None = None
    x2: float | None = None
    y2: float | None = None
    clear_in: float | None = Field(None, gt=0, le=2400)   # span face to face, inches (e.g. from the drawn dimension)


def snap(x1, y1, x2, y2) -> tuple[bool, float, float, float, float]:
    """A drawn beam is horizontal or vertical; snap the two clicks to the axis they are closer to."""
    if abs(x2 - x1) >= abs(y2 - y1):
        y = (y1 + y2) / 2
        return False, min(x1, x2), y, max(x1, x2), y
    x = (x1 + x2) / 2
    return True, x, min(y1, y2), x, max(y1, y2)


@router.patch('/api/projects/{pid}/members/{member_id}')
def change_member(p: Editor, member_id: str, req: MemberPatch, user: CurrentUser, db: Db):
    m = own_member(db, p, member_id)
    sent = req.model_fields_set

    def record(field, old, new):
        db.add(Correction(project_id=p.id, sheet_id=m.sheet_id, member_id=m.id, field=field, user_id=user.id,
                          old_value=None if old is None else str(old), new_value=None if new is None else str(new)))
    edited = False
    for f in ('name', 'label_b_in', 'label_d_in'):
        if f in sent and getattr(req, f) is not None and getattr(m, f) != getattr(req, f):
            record(f, getattr(m, f), getattr(req, f))
            setattr(m, f, getattr(req, f))
            edited = True
    xy = ('x1', 'y1', 'x2', 'y2')
    if any(f in sent for f in xy):
        if any(getattr(req, f) is None for f in xy):
            raise HTTPException(422, 'Send both ends: x1, y1, x2, y2')
        if m.kind != 'beam':
            raise HTTPException(422, 'Only beams can be redrawn')
        vertical, *pts = snap(req.x1, req.y1, req.x2, req.y2)
        if pts[0] == pts[2] and pts[1] == pts[3]:
            raise HTTPException(422, 'The two ends are the same point')
        old = [round(v, 1) for v in (m.x1, m.y1, m.x2, m.y2)]
        m.vertical, (m.x1, m.y1, m.x2, m.y2) = vertical, pts
        record('geometry', old, [round(v, 1) for v in pts])
        edited = True
    if 'clear_in' in sent and req.clear_in is not None and m.clear_in != req.clear_in:
        record('clear_in', m.clear_in, req.clear_in)
        m.clear_in, m.clear_source = req.clear_in, 'person'
        edited = True
    if edited:
        m.status, m.note = 'edited', None
    if req.action:
        new = {'accept': 'accepted', 'reject': 'rejected', 'reopen': 'auto'}[req.action]
        if req.action == 'accept' and m.kind == 'beam' and (m.x1, m.y1) == (m.x2, m.y2):
            raise HTTPException(422, 'No beam is drawn for this label yet: redraw it on the sheet, or mark it not on the drawing.')
        if not (edited and req.action == 'accept'):
            record('status', m.status, new)
            m.status = new
    db.flush()
    checks.run(db, p.id)
    return item(m, db.get(Sheet, m.sheet_id), p.slug)


class BarIn(BaseModel):
    role: Literal[BAR_ROLES]
    nos: int = Field(ge=1, le=30)
    dia: int = Field(ge=6, le=40)
    extra_top_in: float | None = Field(None, gt=0, le=600)


class SpecPatch(BaseModel):
    action: Literal['accept', 'reject', 'reopen'] | None = None
    kind: Literal['S', 'C'] | None = None                   # span or cantilever
    b_in: float | None = Field(None, gt=0, le=120)
    D_in: float | None = Field(None, gt=0, le=120)
    stirrup_dia: int | None = Field(None, ge=6, le=16)
    stirrup_end_in: float | None = Field(None, gt=0, le=24)
    stirrup_mid_in: float | None = Field(None, gt=0, le=24)
    bars: list[BarIn] | None = None                        # replaces the whole bar list


@router.patch('/api/projects/{pid}/specs/{spec_id}')
def change_spec(p: Editor, spec_id: str, req: SpecPatch, user: CurrentUser, db: Db, relayout: Relayout,
                tasks: BackgroundTasks):
    """Accept, fix or reject a schedule reading. Every change is recorded in corrections."""
    try:
        b = db.get(BeamSpec, uuid.UUID(spec_id))
    except ValueError:
        b = None
    if b is None or b.project_id != p.id:
        raise HTTPException(404)
    sent = req.model_fields_set

    def record(field, old, new):
        db.add(Correction(project_id=p.id, sheet_id=b.sheet_id, field=f'spec:{b.name}:{field}', user_id=user.id,
                          old_value=None if old is None else str(old), new_value=None if new is None else str(new)))
    edited = False
    for f in ('kind', 'b_in', 'D_in', 'stirrup_dia', 'stirrup_end_in', 'stirrup_mid_in'):
        if f in sent and getattr(req, f) is not None and getattr(b, f) != getattr(req, f):
            record(f, getattr(b, f), getattr(req, f))
            setattr(b, f, getattr(req, f))
            edited = True
    if 'bars' in sent and req.bars is not None:
        key = lambda x: (x.role, x.nos, x.dia, x.extra_top_in or 0)          # noqa: E731 (None = no length drawn)
        old = sorted(map(key, b.bars))
        new = sorted(map(key, req.bars))
        if old != new:
            record('bars', old, new)
            b.bars = [Bar(role=x.role, nos=x.nos, dia=x.dia, extra_top_in=x.extra_top_in, sort=i)
                      for i, x in enumerate(req.bars)]
            edited = True
    if edited:
        b.status, b.source, b.notes = 'edited', 'person', None
    if req.action:
        new_status = {'accept': 'accepted', 'reject': 'rejected', 'reopen': 'auto'}[req.action]
        if req.action == 'accept' and (not b.b_in or not b.D_in or not any(x.role == 'Bottom through' for x in b.bars)):
            raise HTTPException(422, 'Size and bottom bars are needed before this reading can be accepted.')
        if not (edited and req.action == 'accept'):
            record('status', b.status, new_status)
            b.status = new_status
    db.flush()
    checks.run(db, p.id)
    n = relayout_if_needed(db, p, b.floor_id, relayout, tasks)
    return {**spec_item(b, db.get(Sheet, b.sheet_id), p.slug), 'layouts_rereading': n}


class SpecNew(BaseModel):
    name: str = Field(pattern=NAME)
    kind: Literal['S', 'C'] | None = None                   # default: C for -cant names
    b_in: float = Field(gt=0, le=120)
    D_in: float = Field(gt=0, le=120)
    stirrup_dia: int | None = Field(None, ge=6, le=16)
    stirrup_end_in: float | None = Field(None, gt=0, le=24)
    stirrup_mid_in: float | None = Field(None, gt=0, le=24)
    bars: list[BarIn] = Field(min_length=1)
    box: list[float] | None = Field(None, min_length=4, max_length=4)   # where it is on the sheet, if marked


@router.post('/api/projects/{pid}/sheets/{sheet_id}/specs', status_code=201)
def add_spec(p: Editor, sheet_id: str, req: SpecNew, user: CurrentUser, db: Db, relayout: Relayout,
             tasks: BackgroundTasks):
    """A schedule reading the reader missed, entered by a person from the elevation."""
    s = own_sheet(db, p, sheet_id)
    if s.sheet_type != 'beam_schedule':
        raise HTTPException(422, 'That sheet is not a beam schedule.')
    if db.scalar(select(BeamSpec.id).where(BeamSpec.project_id == p.id, BeamSpec.name == req.name, BeamSpec.sheet_id.in_(live()),
                                           BeamSpec.status != 'rejected', BeamSpec.duplicate_of.is_(None))):
        raise HTTPException(409, f'{req.name} already has a schedule reading; edit that one.')
    b = BeamSpec(project_id=p.id, sheet_id=s.id, floor_id=s.floor_id, name=req.name,
                 kind=req.kind or ('C' if '-cant' in req.name else 'S'), b_in=req.b_in, D_in=req.D_in, stirrup_dia=req.stirrup_dia,
                 stirrup_end_in=req.stirrup_end_in, stirrup_mid_in=req.stirrup_mid_in, box=req.box, tokens=[],
                 source='person', confidence=1, status='added')
    b.bars = [Bar(role=x.role, nos=x.nos, dia=x.dia, extra_top_in=x.extra_top_in, sort=i) for i, x in enumerate(req.bars)]
    db.add(b)
    db.flush()
    db.add(Correction(project_id=p.id, sheet_id=s.id, field=f'spec:{req.name}:added', user_id=user.id, new_value=req.name))
    checks.run(db, p.id)
    n = relayout_if_needed(db, p, b.floor_id, relayout, tasks)
    return {**spec_item(b, s, p.slug), 'layouts_rereading': n}


class MemberNew(BaseModel):
    kind: Literal['beam'] = 'beam'
    name: str = Field(pattern=NAME)
    x1: float
    y1: float
    x2: float
    y2: float
    label_b_in: float | None = Field(None, gt=0, le=120)
    label_d_in: float | None = Field(None, gt=0, le=120)


@router.post('/api/projects/{pid}/sheets/{sheet_id}/members', status_code=201)
def add_member(p: Editor, sheet_id: str, req: MemberNew, user: CurrentUser, db: Db):
    """A beam the reader missed, drawn on the sheet by a person."""
    s = own_sheet(db, p, sheet_id)
    vertical, x1, y1, x2, y2 = snap(req.x1, req.y1, req.x2, req.y2)
    if (x1, y1) == (x2, y2):
        raise HTTPException(422, 'The two ends are the same point')
    m = Member(project_id=p.id, sheet_id=s.id, floor_id=s.floor_id, kind='beam', name=req.name, vertical=vertical,
               x1=x1, y1=y1, x2=x2, y2=y2, label_b_in=req.label_b_in, label_d_in=req.label_d_in,
               confidence=1, status='added')
    db.add(m)
    db.flush()
    db.add(Correction(project_id=p.id, sheet_id=s.id, member_id=m.id, field='added', user_id=user.id,
                      new_value=req.name))
    return item(m, s, p.slug)
