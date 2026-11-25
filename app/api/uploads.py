"""Uploading drawings and listing the sheets made from them.

The web process only checks size, that the file starts like a PDF, and the hash; it never opens the PDF. Reading
and drawing pages happens in the worker (plan §5).
"""
from __future__ import annotations
import hashlib
import logging
import uuid
from typing import Annotated, Callable

import anyio
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel, Field
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select

from ..auth.deps import CurrentUser, Db, project_role
from ..db.models import Building, Correction, Floor, Project, Sheet, Upload
from ..pipeline.classify import COVERAGE_TYPES, TYPE_LABELS
from ..settings import get_settings
from ..storage import Storage, get_storage, upload_key

log = logging.getLogger('takeoff.uploads')
router = APIRouter()
Viewer = Annotated[Project, Depends(project_role('viewer'))]
Editor = Annotated[Project, Depends(project_role('editor'))]
MAX_FILES_PER_REQUEST = 10
CHUNK = 1 << 20


def get_enqueue() -> Callable:
    """Queues `ingest` for an upload. Overridden in tests. Without REDIS_URL nothing is queued (local dev
    without a worker); the worker's minute sweep picks up anything left queued."""
    if not get_settings().redis_url:
        return lambda upload_id: log.warning('REDIS_URL not set: upload %s not queued', upload_id)
    from ..pipeline.jobs import enqueue_ingest
    return lambda upload_id: anyio.from_thread.run(enqueue_ingest, upload_id)


Store = Annotated[Storage, Depends(get_storage)]
Enqueue = Annotated[Callable, Depends(get_enqueue)]


def read_checked(f: UploadFile, limit: int) -> tuple[str, int]:
    """SHA-256 and size, reading in chunks; refuses oversize files and files that don't start like a PDF."""
    h, size = hashlib.sha256(), 0
    f.file.seek(0)
    head = f.file.read(1024)
    if b'%PDF-' not in head:
        raise HTTPException(415, f'{f.filename}: not a PDF. Upload the drawings as PDF files.')
    h.update(head)
    size = len(head)
    while chunk := f.file.read(CHUNK):
        size += len(chunk)
        if size > limit:
            raise HTTPException(413, f'{f.filename}: larger than {limit >> 20} MB.')
        h.update(chunk)
    f.file.seek(0)
    return h.hexdigest(), size


def upload_dict(u: Upload, slug: str) -> dict:
    return dict(id=str(u.id), filename=u.filename, size_bytes=u.size_bytes, status=u.status, error=u.error,
                pages=u.pages, new_sheets=u.new_sheets, created_at=u.created_at.isoformat() if u.created_at else None,
                sheets=[sheet_dict(s, slug) for s in u.sheets])


def sheet_dict(s: Sheet, slug: str) -> dict:
    base = f'/api/projects/{slug}/sheets/{s.id}'
    ready = s.status == 'ready'
    return dict(id=str(s.id), upload_id=str(s.upload_id), page_no=s.page_no, status=s.status, error=s.error,
                width_pt=s.width_pt, height_pt=s.height_pt, sheet_type=s.sheet_type,
                floor_id=str(s.floor_id) if s.floor_id else None, drg_no=s.drg_no, title=s.title,
                revision=s.revision, scale_text=s.scale_text, drawn_on=s.drawn_on,
                classify_status=s.classify_status, classify_note=s.classify_note, text_source=s.text_source,
                pt_per_in=s.pt_per_in, scale_ratio=round(72 / s.pt_per_in) if s.pt_per_in else None,
                scale_status=s.scale_status, scale_error=s.scale_error, scale_checks=s.scale_checks,
                scale_note=s.scale_note,
                thumb_url=f'{base}/thumb.png' if ready else None,
                image_url=f'{base}/display.png' if ready else None)


@router.post('/api/projects/{pid}/uploads', status_code=201)
def create_uploads(p: Editor, user: CurrentUser, db: Db, storage: Store, enqueue: Enqueue,
                   files: Annotated[list[UploadFile], File()]):
    """Upload one or more PDFs. The same file uploaded again returns the existing upload and does no new work."""
    st = get_settings()
    if not files:
        raise HTTPException(422, 'Choose at least one PDF.')
    if len(files) > MAX_FILES_PER_REQUEST:
        raise HTTPException(422, f'At most {MAX_FILES_PER_REQUEST} files at a time.')
    checked = [(f, *read_checked(f, st.max_upload_mb << 20)) for f in files]
    have = db.scalar(select(func.count()).select_from(Upload).where(Upload.project_id == p.id))
    out, new = [], []
    for f, sha, size in checked:
        u = db.scalar(select(Upload).where(Upload.project_id == p.id, Upload.sha256 == sha))
        if u is None:
            u = next((x for x in new if x.sha256 == sha), None)          # same file twice in one request
        if u is not None:
            out.append(dict(upload_dict(u, p.slug), duplicate=True))
            continue
        if have + len(new) >= st.max_uploads_per_project:
            raise HTTPException(409, f'This project already has {st.max_uploads_per_project} uploads, the limit.')
        name = (f.filename or 'drawing.pdf').replace('/', '_').replace('\\', '_')[-255:]
        u = Upload(project_id=p.id, filename=name, sha256=sha, size_bytes=size, storage_key='', status='queued',
                   created_by=user.id)
        db.add(u)
        db.flush()
        u.storage_key = upload_key(p.id, u.id)
        storage.put(u.storage_key, f.file, 'application/pdf')
        new.append(u)
        out.append(dict(upload_dict(u, p.slug), duplicate=False))
    db.commit()                                     # the worker must see the rows before it is told about them
    for u in new:
        try:
            enqueue(u.id)
        except Exception as e:                      # noqa: BLE001 — the worker's sweep queues it within minutes
            log.warning('could not queue upload %s: %s', u.id, e)
    return dict(uploads=out)


@router.get('/api/projects/{pid}/uploads')
def list_uploads(p: Viewer, db: Db):
    ups = db.scalars(select(Upload).where(Upload.project_id == p.id).order_by(Upload.created_at.desc())).all()
    return [upload_dict(u, p.slug) for u in ups]


@router.get('/api/projects/{pid}/sheets')
def list_sheets(p: Viewer, db: Db):
    ss = db.scalars(select(Sheet).join(Upload).where(Sheet.project_id == p.id)
                    .order_by(Upload.created_at, Sheet.page_no)).all()
    return [dict(sheet_dict(s, p.slug), filename=s.upload.filename) for s in ss]


@router.get('/api/sheet-types')
def sheet_types():
    """Sheet types in display order; `coverage` marks the ones every floor needs."""
    return [dict(key=k, label=v, coverage=k in COVERAGE_TYPES) for k, v in TYPE_LABELS.items()]


class SheetPatch(BaseModel):
    sheet_type: str | None = None
    floor_id: uuid.UUID | None = None
    confirm: bool = True               # what the person sees is now right; the reader won't change it again
    accept_scale: bool = False         # the scale shown (measured or borrowed) is right
    scale_ratio: int | None = Field(None, ge=1, le=10000)   # or: the scale is 1:N


def own_sheet(db, p: Project, sheet_id: str) -> Sheet:
    try:
        s = db.get(Sheet, uuid.UUID(sheet_id))
    except ValueError:
        s = None
    if s is None or s.project_id != p.id:
        raise HTTPException(404)
    return s


@router.patch('/api/projects/{pid}/sheets/{sheet_id}')
def correct_sheet(p: Editor, sheet_id: str, req: SheetPatch, user: CurrentUser, db: Db):
    """Set a sheet's type and floor. Every change is recorded in `corrections`."""
    s = own_sheet(db, p, sheet_id)
    sent = req.model_fields_set
    if 'sheet_type' in sent and req.sheet_type is not None and req.sheet_type not in TYPE_LABELS:
        raise HTTPException(422, 'Unknown sheet type')
    if 'floor_id' in sent and req.floor_id is not None:
        ok = db.scalar(select(Floor.id).join(Building).where(Floor.id == req.floor_id, Building.project_id == p.id))
        if not ok:
            raise HTTPException(422, "That floor is not in this project")
    def record(name, old, new):
        db.add(Correction(project_id=p.id, sheet_id=s.id, field=name, user_id=user.id,
                          old_value=None if old is None else str(old), new_value=None if new is None else str(new)))
    for name in ('sheet_type', 'floor_id'):
        if name in sent and getattr(s, name) != getattr(req, name):
            record(name, getattr(s, name), getattr(req, name))
            setattr(s, name, getattr(req, name))
            if name == 'sheet_type' and s.scale_status != 'confirmed':
                s.scale_status = None          # the worker checks the scale again for the new type
    if req.confirm and sent & {'sheet_type', 'floor_id', 'confirm'}:
        s.classify_status, s.classify_note = 'confirmed', None
    if req.scale_ratio is not None or req.accept_scale:
        if req.scale_ratio is not None:
            new = 72 / req.scale_ratio
        elif s.pt_per_in:
            new = s.pt_per_in
        else:
            raise HTTPException(422, 'There is no scale to accept; enter it as 1:N.')
        record('scale', f'1:{round(72 / s.pt_per_in)}' if s.pt_per_in else None, f'1:{round(72 / new)}')
        s.pt_per_in, s.scale_status = new, 'confirmed'
        s.scale_note = f'Scale 1:{round(72 / new)} confirmed by {user.name or user.email}.'
        db.flush()
        from ..pipeline.stages import borrow             # sister sheets with no dimensions follow a confirmed scale
        for other in db.scalars(select(Sheet).where(Sheet.project_id == p.id, Sheet.scale_status.in_(('unchecked', 'borrowed')))):
            borrow(db, other)
    db.flush()
    return dict(sheet_dict(s, p.slug), filename=s.upload.filename)


@router.get('/api/projects/{pid}/sheets/{sheet_id}/{name}')
def sheet_image(p: Viewer, sheet_id: str, name: str, db: Db, storage: Store):
    """Redirect to a short-lived link to the sheet's image (the bucket is private)."""
    keys = {'thumb.png': 'thumb_key', 'display.png': 'display_key'}
    key = getattr(own_sheet(db, p, sheet_id), keys[name]) if name in keys else None
    if not key:
        raise HTTPException(404)
    return RedirectResponse(storage.url(key), status_code=302, headers={'Cache-Control': 'private, max-age=300'})
