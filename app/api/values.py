"""Values we need (plan M4 step 2). Every value the calculation uses, which still need a person's confirmation, and
editing / confirming them. A confirmed value records who confirmed it and when; changing a value clears its
confirmation unless the same request confirms it again. Every change is recorded in corrections."""
from __future__ import annotations
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ..auth.deps import CurrentUser, Db, project_role
from ..db.models import Correction, Project, User
from ..services.values import BY_KEY, ensure_values, needed

router = APIRouter()
Viewer = Annotated[Project, Depends(project_role('viewer'))]
Editor = Annotated[Project, Depends(project_role('editor'))]


def value_dict(a, emails: dict) -> dict:
    v = BY_KEY.get(a.key)
    return dict(key=a.key, label=a.label, value=a.value, unit=a.unit, group=a.group, source=a.source,
                needs_confirm=a.needs_confirm, confirmed=a.confirmed_at is not None,
                confirmed_at=a.confirmed_at.isoformat() if a.confirmed_at else None,
                confirmed_by=emails.get(a.confirmed_by), default=v.default if v else None,
                min=v.lo if v else None, max=v.hi if v else None, why=v.why if v else None)


def values_payload(db, p: Project) -> dict:
    ids = {a.confirmed_by for a in p.assumptions if a.confirmed_by}
    emails = {u.id: u.email for u in db.query(User).filter(User.id.in_(ids))} if ids else {}
    vals = [value_dict(a, emails) for a in p.assumptions]
    return dict(values=vals, needed=len(needed(p)), groups=list(dict.fromkeys(v['group'] for v in vals)))


@router.get('/api/projects/{pid}/values-needed')
def values_needed(p: Viewer, db: Db):
    """All values, grouped, with `needed` = how many still need a person's confirmation."""
    if ensure_values(p):
        db.flush()
    return values_payload(db, p)


class ValueIn(BaseModel):
    key: str = Field(max_length=64)
    value: float | None = None                    # omitted: keep the value
    confirm: bool = False                         # tick: "I have checked this"


class ValuesIn(BaseModel):
    values: list[ValueIn] = Field(min_length=1, max_length=200)


@router.put('/api/projects/{pid}/assumptions')
def put_values(p: Editor, req: ValuesIn, user: CurrentUser, db: Db):
    """Set and/or confirm values. All or nothing: an unknown key or a value out of range rejects the request."""
    ensure_values(p)
    by = {a.key: a for a in p.assumptions}
    for x in req.values:
        a = by.get(x.key)
        if a is None:
            raise HTTPException(422, f'Unknown value: {x.key}')
        reg = BY_KEY.get(x.key)
        if x.value is not None and reg and not (reg.lo <= x.value <= reg.hi):
            raise HTTPException(422, f'{a.label}: {x.value:g} is outside {reg.lo:g}–{reg.hi:g} {reg.unit}.')
    now = datetime.now(timezone.utc)
    for x in req.values:
        a = by[x.key]
        if x.value is not None and x.value != a.value:
            db.add(Correction(project_id=p.id, field=f'value:{a.key}', old_value=f'{a.value:g}',
                              new_value=f'{x.value:g}', user_id=user.id))
            a.value, a.source = x.value, f'Entered by {user.email}.'
            a.confirmed_at = a.confirmed_by = None
        if x.confirm and a.confirmed_at is None:
            a.confirmed_at, a.confirmed_by = now, user.id
            db.add(Correction(project_id=p.id, field=f'value:{a.key}:confirmed', new_value=f'{a.value:g}',
                              user_id=user.id))
    db.flush()
    return values_payload(db, p)
