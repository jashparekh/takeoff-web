"""Database tables (see docs/plans/upload-pipeline-implementation.md §3 M0–M1).

Members, bars and the later pipeline tables arrive in M2–M3. Until members/bars exist, a floor's
beams, chajja and slab openings live in `floors.structure_json`, in the same shape `app.engine.Calc`
reads from `inputs.json`; M2/M3 replace that column with rows.
"""
from __future__ import annotations
import uuid
from datetime import datetime

from sqlalchemy import (JSON, Boolean, CheckConstraint, DateTime, Float, ForeignKey, Index, Integer, String,
                        Text, UniqueConstraint, func)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

Json = JSON().with_variant(JSONB(), 'postgresql')
ROLES = ('owner', 'editor', 'viewer')
SEVERITIES = ('high', 'medium', 'low')
ISSUE_STATUSES = ('open', 'answered', 'resolved')
UPLOAD_STATUSES = ('queued', 'processing', 'done', 'failed')
SHEET_STATUSES = ('queued', 'rendering', 'ready', 'needs_attention')
JOB_STATUSES = ('queued', 'running', 'done', 'failed')


class Base(DeclarativeBase):
    pass


def pk() -> Mapped[uuid.UUID]:
    return mapped_column(primary_key=True, default=uuid.uuid4)


def created() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now())


class User(Base):
    __tablename__ = 'users'
    id: Mapped[uuid.UUID] = pk()
    email: Mapped[str] = mapped_column(String(320))
    name: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = created()
    __table_args__ = (Index('uq_users_email_lower', func.lower(email), unique=True),)


class Project(Base):
    __tablename__ = 'projects'
    id: Mapped[uuid.UUID] = pk()
    slug: Mapped[str] = mapped_column(String(64), unique=True)          # used in URLs: #p/<slug>/...
    owner_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey('users.id', ondelete='SET NULL'))
    name: Mapped[str] = mapped_column(String(200))
    scope: Mapped[str | None] = mapped_column(Text)
    client: Mapped[str | None] = mapped_column(Text)
    city: Mapped[str | None] = mapped_column(String(100))
    consultant: Mapped[str | None] = mapped_column(String(200))
    revision: Mapped[str | None] = mapped_column(String(100))
    status: Mapped[str | None] = mapped_column(Text)
    meta_json: Mapped[dict] = mapped_column(Json, default=dict)        # other project.json fields (grade, sheets…)
    created_at: Mapped[datetime] = created()

    buildings: Mapped[list[Building]] = relationship(back_populates='project', cascade='all, delete-orphan',
                                                     order_by='Building.sort')
    assumptions: Mapped[list[Assumption]] = relationship(cascade='all, delete-orphan', order_by='Assumption.sort')
    issues: Mapped[list[Issue]] = relationship(cascade='all, delete-orphan', order_by='Issue.code')


class ProjectMember(Base):
    __tablename__ = 'project_members'
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey('projects.id', ondelete='CASCADE'), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey('users.id', ondelete='CASCADE'), primary_key=True)
    role: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = created()
    __table_args__ = (CheckConstraint(f"role IN {ROLES}", name='role_valid'),)


class Building(Base):
    __tablename__ = 'buildings'
    id: Mapped[uuid.UUID] = pk()
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey('projects.id', ondelete='CASCADE'), index=True)
    name: Mapped[str] = mapped_column(String(200))
    copies: Mapped[int] = mapped_column(Integer, default=1)            # identical buildings on site
    sort: Mapped[int] = mapped_column(Integer, default=0)
    project: Mapped[Project] = relationship(back_populates='buildings')
    floors: Mapped[list[Floor]] = relationship(back_populates='building', cascade='all, delete-orphan',
                                               order_by='Floor.sort')
    __table_args__ = (CheckConstraint('copies >= 1', name='copies_positive'),)


class Floor(Base):
    __tablename__ = 'floors'
    id: Mapped[uuid.UUID] = pk()
    building_id: Mapped[uuid.UUID] = mapped_column(ForeignKey('buildings.id', ondelete='CASCADE'), index=True)
    name: Mapped[str] = mapped_column(String(200))
    level_m: Mapped[float | None] = mapped_column(Float)              # top of slab; None = not given yet
    typical_count: Mapped[int] = mapped_column(Integer, default=1)    # "typical floor × n"
    sort: Mapped[int] = mapped_column(Integer, default=0)
    measured: Mapped[bool] = mapped_column(Boolean, default=False)    # drawings received and read
    # Interim: {beams[], chajja{}, slab{}} as in inputs.json. Replaced by members/bars in M2–M3.
    structure_json: Mapped[dict | None] = mapped_column(Json)
    building: Mapped[Building] = relationship(back_populates='floors')
    __table_args__ = (CheckConstraint('typical_count >= 1', name='typical_count_positive'),)


class Assumption(Base):
    """A value the calculation needs. `needs_confirm` marks values the drawings don't give; in the upload
    flow nothing is calculated until every such value has `confirmed_at` set."""
    __tablename__ = 'assumptions'
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey('projects.id', ondelete='CASCADE'), primary_key=True)
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    label: Mapped[str] = mapped_column(Text)
    value: Mapped[float] = mapped_column(Float)
    unit: Mapped[str | None] = mapped_column(String(32))
    group: Mapped[str | None] = mapped_column(String(64))
    source: Mapped[str | None] = mapped_column(Text)                   # where the value came from
    needs_confirm: Mapped[bool] = mapped_column(Boolean, default=False)
    confirmed_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey('users.id', ondelete='SET NULL'))
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sort: Mapped[int] = mapped_column(Integer, default=0)


class Issue(Base):
    __tablename__ = 'issues'
    id: Mapped[uuid.UUID] = pk()
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey('projects.id', ondelete='CASCADE'), index=True)
    code: Mapped[str] = mapped_column(String(32))                      # I1, I2… (stable per project)
    rule: Mapped[str | None] = mapped_column(String(64))               # check that raised it (M4); None = entered by hand
    severity: Mapped[str] = mapped_column(String(16))
    kind: Mapped[str | None] = mapped_column(String(64))
    title: Mapped[str] = mapped_column(Text)
    detail: Mapped[str | None] = mapped_column(Text)
    affects: Mapped[str | None] = mapped_column(Text)
    assumption: Mapped[str | None] = mapped_column(Text)               # value used meanwhile
    ask: Mapped[str | None] = mapped_column(Text)                      # question for the consultant
    beams: Mapped[list | None] = mapped_column(Json)
    where_json: Mapped[dict | None] = mapped_column(Json)              # {sheet, x, y, zoom}
    status: Mapped[str] = mapped_column(String(16), default='open')
    created_at: Mapped[datetime] = created()
    __table_args__ = (UniqueConstraint('project_id', 'code'),
                      CheckConstraint(f"severity IN {SEVERITIES}", name='severity_valid'),
                      CheckConstraint(f"status IN {ISSUE_STATUSES}", name='status_valid'))


class Run(Base):
    """One calculation, frozen: inputs, assumptions, engine version and results (reproducible, comparable)."""
    __tablename__ = 'runs'
    id: Mapped[uuid.UUID] = pk()
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey('projects.id', ondelete='CASCADE'))
    engine_version: Mapped[str] = mapped_column(String(64))
    inputs_json: Mapped[dict] = mapped_column(Json)
    results_json: Mapped[dict] = mapped_column(Json)
    note: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey('users.id', ondelete='SET NULL'))
    created_at: Mapped[datetime] = created()
    __table_args__ = (Index('ix_runs_project_created', 'project_id', 'created_at'),)


class SiteTakeoff(Base):
    """A site count to check against (pilot: the site QS's handwritten notebook)."""
    __tablename__ = 'site_takeoffs'
    id: Mapped[uuid.UUID] = pk()
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey('projects.id', ondelete='CASCADE'), index=True)
    source: Mapped[str] = mapped_column(Text)
    data_json: Mapped[dict] = mapped_column(Json)                      # notebook.json shape
    created_at: Mapped[datetime] = created()


class LoginToken(Base):
    """A one-time sign-in link. Only the SHA-256 of the token is stored."""
    __tablename__ = 'login_tokens'
    id: Mapped[uuid.UUID] = pk()
    email: Mapped[str] = mapped_column(String(320), index=True)       # lower-cased
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = created()


class AuthSession(Base):
    """A signed-in browser. The cookie holds a random token; only its SHA-256 is stored, so sign-out and
    revoking access take effect immediately."""
    __tablename__ = 'auth_sessions'
    id: Mapped[uuid.UUID] = pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey('users.id', ondelete='CASCADE'), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = created()
    user: Mapped[User] = relationship()


class Upload(Base):
    """A PDF someone uploaded. Stored as-is at `storage_key`; the worker splits it into sheets."""
    __tablename__ = 'uploads'
    id: Mapped[uuid.UUID] = pk()
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey('projects.id', ondelete='CASCADE'), index=True)
    filename: Mapped[str] = mapped_column(String(255))
    sha256: Mapped[str] = mapped_column(String(64))                    # of the whole file; same file → same upload
    size_bytes: Mapped[int] = mapped_column(Integer)
    storage_key: Mapped[str] = mapped_column(String(300))
    status: Mapped[str] = mapped_column(String(16), default='queued')
    error: Mapped[str | None] = mapped_column(Text)                    # shown to the user as is
    pages: Mapped[int | None] = mapped_column(Integer)
    new_sheets: Mapped[int | None] = mapped_column(Integer)            # pages not already in the project
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey('users.id', ondelete='SET NULL'))
    created_at: Mapped[datetime] = created()
    sheets: Mapped[list[Sheet]] = relationship(back_populates='upload', order_by='Sheet.page_no')
    __table_args__ = (UniqueConstraint('project_id', 'sha256'), CheckConstraint(f"status IN {UPLOAD_STATUSES}", name='status_valid'))


class Sheet(Base):
    """One page of an upload. Images are stored under `p/{project_id}/s/{sheet_id}/`."""
    __tablename__ = 'sheets'
    id: Mapped[uuid.UUID] = pk()
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey('projects.id', ondelete='CASCADE'), index=True)
    upload_id: Mapped[uuid.UUID] = mapped_column(ForeignKey('uploads.id', ondelete='CASCADE'), index=True)
    page_no: Mapped[int] = mapped_column(Integer)                       # 1-based, within the upload
    page_hash: Mapped[str] = mapped_column(String(64))                  # same page in another upload → not redone
    width_pt: Mapped[float] = mapped_column(Float)
    height_pt: Mapped[float] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(16), default='queued')
    error: Mapped[str | None] = mapped_column(Text)
    display_key: Mapped[str | None] = mapped_column(String(300))       # PNG for the viewer
    display_dpi: Mapped[int | None] = mapped_column(Integer)           # px = pt × dpi / 72
    thumb_key: Mapped[str | None] = mapped_column(String(300))
    ocr_key: Mapped[str | None] = mapped_column(String(300))           # 300 dpi grayscale (M1 classify, M3 OCR)
    sheet_type: Mapped[str | None] = mapped_column(String(32))         # layout, schedule, slab… (classify, later)
    floor_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey('floors.id', ondelete='SET NULL'))
    drg_no: Mapped[str | None] = mapped_column(String(100))
    title: Mapped[str | None] = mapped_column(Text)
    revision: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = created()
    upload: Mapped[Upload] = relationship(back_populates='sheets')
    __table_args__ = (UniqueConstraint('project_id', 'page_hash'), CheckConstraint(f"status IN {SHEET_STATUSES}", name='status_valid'))


class Job(Base):
    """One pipeline stage run. `key` = stage:subject:input_hash, so the same work is never done twice."""
    __tablename__ = 'jobs'
    id: Mapped[uuid.UUID] = pk()
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey('projects.id', ondelete='CASCADE'), index=True)
    stage: Mapped[str] = mapped_column(String(32))                      # ingest, render, …
    key: Mapped[str] = mapped_column(String(200), unique=True)
    upload_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey('uploads.id', ondelete='CASCADE'), index=True)
    sheet_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey('sheets.id', ondelete='CASCADE'), index=True)
    status: Mapped[str] = mapped_column(String(16), default='queued')
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = created()
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (CheckConstraint(f"status IN {JOB_STATUSES}", name='status_valid'),)
