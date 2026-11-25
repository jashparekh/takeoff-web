"""Pipeline stages. Each one is idempotent: a `jobs` row keyed by stage, subject and input hash records that the
work was done, so running a stage again (a retry, a duplicate queue message, a re-upload) does nothing new.

These are plain functions taking a session and storage, so tests run them without Redis; `jobs.py` wraps them
for the arq worker. Stage chain in M1: ingest(upload) → render(sheet) and classify(sheet) for each new page →
calibrate(sheet) once its type is known → layout(sheet) for beam layouts with a scale (M2). Then schedule / slab
by sheet type (M3–M5).
"""
from __future__ import annotations
import hashlib
import logging
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..db.models import Building, Floor, Job, Member, Sheet, Upload
from ..settings import get_settings
from ..storage import Storage, sheet_key
import numpy as np

from . import calibrate as cal
from . import classify as cls
from . import layout as lay
from . import ocr, pdf

CLASSIFY_VERSION = 2             # bump when the title-block reader changes, so sheets are read again
CALIBRATE_VERSION = 1
LAYOUT_VERSION = 1

log = logging.getLogger('takeoff.pipeline')
now = lambda: datetime.now(timezone.utc)                    # noqa: E731


class Skip(Exception):
    """The job already ran with this input."""


@contextmanager
def job(db: Session, stage: str, subject, input_hash: str, project_id, *, final: bool, upload_id=None,
        sheet_id=None):
    """Record a stage run. Raises Skip if it already finished. On error the row is marked failed only on the final
    attempt (`final`), so the queue's retries see it as queued again."""
    key = f'{stage}:{subject}:{input_hash}'
    j = db.scalar(select(Job).where(Job.key == key).with_for_update())
    if j is None:
        j = Job(stage=stage, key=key, project_id=project_id, upload_id=upload_id, sheet_id=sheet_id, attempts=0)
        db.add(j)
        try:
            db.flush()
        except IntegrityError:                     # another worker created it first
            db.rollback()
            raise Skip(key)
    if j.status == 'done':
        raise Skip(key)
    j.status, j.attempts, j.started_at, j.error = 'running', j.attempts + 1, now(), None
    db.commit()
    try:
        yield j
    except Exception as e:
        db.rollback()
        j = db.get(Job, j.id)
        j.status, j.error, j.finished_at = ('failed' if final else 'queued'), f'{type(e).__name__}: {e}'[:2000], now()
        db.commit()
        raise
    j.status, j.finished_at = 'done', now()
    db.commit()


def fetch(storage: Storage, key: str) -> tempfile._TemporaryFileWrapper:
    f = tempfile.NamedTemporaryFile(suffix='.pdf')
    f.write(storage.get(key))
    f.flush()
    return f


def ingest(db: Session, storage: Storage, upload_id, *, final: bool = True) -> list:
    """Split an upload into sheets. Pages already in the project (same page hash) are not added again.
    Returns the ids of the new sheets, which each need `render`."""
    up = db.get(Upload, upload_id)
    if up is None:
        return []
    try:
        with job(db, 'ingest', up.id, up.sha256, up.project_id, final=final, upload_id=up.id):
            up.status, up.error = 'processing', None
            db.commit()
            with fetch(storage, up.storage_key) as f:
                try:
                    pages = pdf.inspect(f.name, get_settings().max_pages_per_upload)
                except pdf.BadPdf as e:
                    up.status, up.error = 'failed', str(e)
                    db.commit()
                    return []                     # the user's file is the problem; retrying won't help
            have = set(db.scalars(select(Sheet.page_hash).where(Sheet.project_id == up.project_id)))
            new = []
            for p in pages:
                if p.page_hash in have:
                    continue
                have.add(p.page_hash)
                s = Sheet(project_id=up.project_id, upload_id=up.id, page_no=p.page_no, page_hash=p.page_hash,
                          width_pt=p.width_pt, height_pt=p.height_pt, status='queued')
                db.add(s)
                new.append(s)
            up.pages, up.new_sheets, up.status = len(pages), len(new), 'done'
            db.flush()
            return [s.id for s in new]
    except Skip:
        return [s.id for s in db.scalars(select(Sheet).where(Sheet.upload_id == upload_id, Sheet.status == 'queued'))]
    except Exception:
        if final:
            up = db.get(Upload, upload_id)
            up.status, up.error = 'failed', 'Something went wrong while reading this file. Try uploading it again.'
            db.commit()
        raise


def render(db: Session, storage: Storage, sheet_id, *, final: bool = True) -> None:
    """Draw the sheet: viewer PNG, thumbnail, 300 dpi grayscale for OCR."""
    s = db.get(Sheet, sheet_id)
    if s is None:
        return
    try:
        with job(db, 'render', s.id, f'{s.page_hash}:dpi{pdf.DISPLAY_DPI}', s.project_id, final=final,
                 sheet_id=s.id):
            s.status, s.error = 'rendering', None
            db.commit()
            with fetch(storage, s.upload.storage_key) as f:
                r = pdf.render_page(f.name, s.page_no)
            for name, data in (('display.png', r.display), ('thumb.png', r.thumb), ('ocr300.png', r.ocr)):
                storage.put(sheet_key(s.project_id, s.id, name), data, 'image/png')
            s.display_key = sheet_key(s.project_id, s.id, 'display.png')
            s.thumb_key = sheet_key(s.project_id, s.id, 'thumb.png')
            s.ocr_key = sheet_key(s.project_id, s.id, 'ocr300.png')
            s.display_dpi, s.status = r.display_dpi, 'ready'
    except Skip:
        return
    except Exception:
        if final:
            s = db.get(Sheet, sheet_id)
            s.status, s.error = 'needs_attention', 'This page could not be drawn. It is skipped; the others go on.'
            db.commit()
        raise


def project_floors(db: Session, project_id) -> list[tuple]:
    return [(f.id, f.name) for f in db.scalars(select(Floor).join(Building).where(Building.project_id == project_id)
                                                .order_by(Building.sort, Floor.sort))]


def classify(db: Session, storage: Storage, sheet_id, *, final: bool = True) -> None:
    """Read the title block and set type, floor, drawing number, revision, scale and date. A sheet a person has
    confirmed is never changed. Runs again when the project's floors change (they are part of the input)."""
    s = db.get(Sheet, sheet_id)
    if s is None or s.classify_status == 'confirmed':
        return
    floors = project_floors(db, s.project_id)
    sig = hashlib.sha256(repr([(str(i), n) for i, n in floors]).encode()).hexdigest()[:16]
    try:
        with job(db, 'classify', s.id, f'{s.page_hash}:v{CLASSIFY_VERSION}:{sig}', s.project_id, final=final,
                 sheet_id=s.id):
            with fetch(storage, s.upload.storage_key) as f:
                lines, source = pdf.text_lines(f.name, s.page_no), 'pdf'
                if not cls.title_block_lines(lines):
                    lines, source = ocr_title_block(f.name, s.page_no), 'ocr'
            r = cls.classify(lines, floors, s.upload.filename, ocr=source == 'ocr')
            db.refresh(s)
            if s.classify_status == 'confirmed':          # a person confirmed it while we were reading
                return
            s.text_source = source if lines else None
            tb = r.title_block
            s.drg_no, s.title, s.revision, s.scale_text, s.drawn_on = tb.drg_no, tb.title, tb.revision, tb.scale, tb.date
            s.sheet_type, s.floor_id = r.sheet_type, r.floor_id
            s.classify_status, s.classify_note = r.status, r.note
    except Skip:
        return
    except Exception:
        if final:
            s = db.get(Sheet, sheet_id)
            if s.classify_status != 'confirmed':
                s.classify_status, s.classify_note = 'needs_review', 'The title block could not be read. Pick the type and floor.'
                db.commit()
        raise


def ocr_title_block(path: str, page_no: int) -> list:
    """No title block in the text layer (a scan, or text drawn as outlines): OCR the bottom-right corner, then the
    whole sheet if that finds no drawing number."""
    img, _ = pdf.render_gray(path, page_no)
    W, H = img.size
    lines = ocr.lines(img, (int(W * 0.55), int(H * 0.55), W, H))
    return lines if cls.title_block_lines(lines) else ocr.lines(img)


def calibrate(db: Session, storage: Storage, sheet_id, *, final: bool = True) -> None:
    """Check the scale against the sheet's dimension strings; see calibrate.py. Waits for classify (the type decides
    whether a scale is needed). A scale a person confirmed is never changed."""
    s = db.get(Sheet, sheet_id)
    if s is None or s.scale_status == 'confirmed' or s.sheet_type is None:
        return
    if s.sheet_type not in cal.NEEDS_SCALE:
        s.scale_status, s.pt_per_in, s.scale_error, s.scale_checks, s.scale_note = 'not_needed', None, None, None, None
        db.commit()
        return
    try:
        with job(db, 'calibrate', s.id, f'{s.page_hash}:v{CALIBRATE_VERSION}:{s.scale_text}', s.project_id,
                 final=final, sheet_id=s.id):
            with fetch(storage, s.upload.storage_key) as f:
                img, dpi = pdf.render_gray(f.name, s.page_no)
            m = cal.measure(np.asarray(img), dpi, ocr.words(img))
            del img
            d = cal.decide(m, s.scale_text)
            db.refresh(s)
            if s.scale_status == 'confirmed':
                return
            s.scale_status, s.pt_per_in, s.scale_error, s.scale_checks, s.scale_note = \
                d.status, d.pt_per_in, d.error, d.checks, d.note
            db.flush()
            # Sheets of the same set with nothing to measure take their scale from a measured sister sheet.
            for other in db.scalars(select(Sheet).where(Sheet.project_id == s.project_id,
                                                        Sheet.scale_status.in_(('unchecked', 'borrowed')))):
                borrow(db, other)
    except Skip:
        return
    except Exception:
        if final:
            s = db.get(Sheet, sheet_id)
            if s.scale_status != 'confirmed':
                s.scale_status, s.scale_note = 'unchecked', 'The scale could not be checked. Enter it by hand.'
                db.commit()
        raise


def borrow(db: Session, s: Sheet) -> None:
    """Give an unmeasured sheet the scale of a measured sheet with the same printed scale and paper size, saying so.
    Preference: a scale a person confirmed, then one that matched its printed scale, then the beam layout."""
    donors = [d for d in db.scalars(select(Sheet).where(
        Sheet.project_id == s.project_id, Sheet.id != s.id, Sheet.pt_per_in.is_not(None),
        Sheet.scale_status.in_(('confirmed', 'ok', 'differs')), Sheet.sheet_type.in_(cal.NEEDS_SCALE)))
        if d.scale_text == s.scale_text and abs(d.width_pt - s.width_pt) <= 1 and abs(d.height_pt - s.height_pt) <= 1]
    if not donors:
        return
    rank = {'confirmed': 0, 'ok': 1, 'differs': 2}
    d = min(donors, key=lambda d: (rank[d.scale_status], d.sheet_type != 'beam_layout', -(d.scale_checks or 0)))
    name = d.drg_no or f'{d.upload.filename} page {d.page_no}'
    s.scale_status, s.pt_per_in, s.scale_checks, s.scale_error = 'borrowed', d.pt_per_in, 0, None
    s.scale_note = (f"This sheet's own dimensions could not confirm a scale; using {cal.ratio(d.pt_per_in)} from {name} "
                    f'(same printed scale and sheet size). Confirm it is the same.')


def layout(db: Session, storage: Storage, sheet_id, *, final: bool = True) -> None:
    """Read beams and columns off a beam layout sheet into `members`. Needs a scale (any status: the scale only sets
    which line spacings count as beams; lengths use the settled scale later). Members a person decided on are
    kept; the pipeline's own previous reading of this sheet is replaced."""
    s = db.get(Sheet, sheet_id)
    if s is None or s.sheet_type != 'beam_layout' or not s.pt_per_in:
        return
    try:
        with job(db, 'layout', s.id, f'{s.page_hash}:v{LAYOUT_VERSION}:{s.pt_per_in:.4f}', s.project_id,
                 final=final, sheet_id=s.id):
            with fetch(storage, s.upload.storage_key) as f:
                r = lay.read_layout(f.name, s.page_no, s.pt_per_in)
            db.refresh(s)
            db.query(Member).filter(Member.sheet_id == s.id, Member.status == 'auto').delete()
            kept = {m.name for m in db.scalars(select(Member).where(Member.sheet_id == s.id, Member.kind == 'beam'))}
            for b in r.beams:
                if b.name in kept:                       # a person already decided about this beam
                    continue
                db.add(Member(project_id=s.project_id, sheet_id=s.id, floor_id=s.floor_id, kind='beam', name=b.name,
                              vertical=b.vertical, x1=b.p1[0], y1=b.p1[1], x2=b.p2[0], y2=b.p2[1], b_in=b.b_in,
                              label_b_in=b.label_b_in, label_d_in=b.label_d_in, label_box=list(b.label.box),
                              confidence=b.confidence, note=b.note))
            for c in r.columns:
                db.add(Member(project_id=s.project_id, sheet_id=s.id, floor_id=s.floor_id, kind='column',
                              x1=c.x0, y1=c.y0, x2=c.x1, y2=c.y1,
                              b_in=round(min(c.x1 - c.x0, c.y1 - c.y0) / s.pt_per_in, 1), confidence=0.8))
            for u in r.unplaced:
                db.add(Member(project_id=s.project_id, sheet_id=s.id, floor_id=s.floor_id, kind='beam', name=u.name,
                              vertical=u.vertical, x1=u.x, y1=u.y, x2=u.x, y2=u.y, label_box=list(u.box),
                              label_b_in=u.b_in, label_d_in=u.d_in, confidence=0,
                              note='Label read, but no beam found next to it.'))
            names = {b.name for b in r.beams}
            s.layout_version = LAYOUT_VERSION
            s.layout_note = (f'{len(names)} beams placed from {len(names | {u.name for u in r.unplaced})} labels read; '
                             f'{len(r.columns)} columns.')
    except Skip:
        return
