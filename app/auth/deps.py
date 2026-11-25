"""FastAPI dependencies: who is signed in, and what they may do on a project.

A project someone can't see answers 404 (same as one that doesn't exist), so project names don't leak.
"""
from __future__ import annotations
from typing import Annotated

from fastapi import Cookie, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db.models import ROLES, Project, ProjectMember, User
from ..db.session import get_db
from .core import COOKIE, session_user

RANK = {r: len(ROLES) - i for i, r in enumerate(ROLES)}       # owner 3 > editor 2 > viewer 1
Db = Annotated[Session, Depends(get_db)]


def optional_user(db: Db, token: Annotated[str | None, Cookie(alias=COOKIE)] = None) -> User | None:
    return session_user(db, token)


def current_user(user: Annotated[User | None, Depends(optional_user)]) -> User:
    if not user:
        raise HTTPException(401, 'Sign in required')
    return user


CurrentUser = Annotated[User, Depends(current_user)]


def role_on(db: Session, project: Project, user: User) -> str | None:
    if project.owner_id == user.id:
        return 'owner'
    return db.scalar(select(ProjectMember.role).where(ProjectMember.project_id == project.id,
                                                      ProjectMember.user_id == user.id))


def project_role(min_role: str = 'viewer'):
    """Dependency factory for routes with a `{pid}` (project slug) path parameter:
    `project: Project = Depends(project_role('editor'))`."""
    if min_role not in RANK:
        raise ValueError(min_role)

    def dep(pid: str, db: Db, user: CurrentUser) -> Project:
        p = db.scalar(select(Project).where(Project.slug == pid))
        role = role_on(db, p, user) if p else None
        if not role:
            raise HTTPException(404, 'Project not found')
        if RANK[role] < RANK[min_role]:
            raise HTTPException(403, f'Needs {min_role} access')
        return p
    return dep
