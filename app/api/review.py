"""Review: a person checks what the layout reader found (plan M2 step 3).

Queue = members still `auto`, least confident first (labels with no beam found come first, at confidence 0).
Accept, edit (name, label size, or redraw the centreline on the sheet), mark "not on drawing", or add a beam the
reader missed. Every change writes `corrections`, so the reader can be measured against what people decided.
"""
from __future__ import annotations
import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from ..auth.deps import CurrentUser, Db, project_role
from ..db.models import Correction, Member, Project, Sheet
from .uploads import member_dict, own_sheet

router = APIRouter()
Viewer = Annotated[Project, Depends(project_role('viewer'))]
Editor = Annotated[Project, Depends(project_role('editor'))]
NAME = r'^(?:H?B|CB)\d{1,3}[A-Z]?(?:-[A-Za-z]+)?$'


def item(m: Member, s: Sheet, slug: str) -> dict:
    return dict(member_dict(m, s.pt_per_in), sheet=dict(
        id=str(s.id), drg_no=s.drg_no, title=s.title, width_pt=s.width_pt, height_pt=s.height_pt, pt_per_in=s.pt_per_in,
        image_url=f'/api/projects/{slug}/sheets/{s.id}/display.png'))


@router.get('/api/projects/{pid}/review')
def review_queue(p: Viewer, db: Db, limit: int = 500):
    """Members not yet checked, least confident first, then by sheet and name; plus progress counts."""
    rows = db.execute(select(Member, Sheet).join(Sheet, Member.sheet_id == Sheet.id)
                      .where(Member.project_id == p.id, Member.status == 'auto')
                      .order_by(Member.confidence, Sheet.drg_no, Member.kind, Member.name).limit(limit)).all()
    counts = dict(db.execute(select(Member.status, func.count()).where(Member.project_id == p.id)
                             .group_by(Member.status)).all())
    total = sum(counts.values())
    return dict(total=total, checked=total - counts.get('auto', 0), counts=counts,
                items=[item(m, s, p.slug) for m, s in rows])


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
    return item(m, db.get(Sheet, m.sheet_id), p.slug)


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
