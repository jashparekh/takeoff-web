"""Pipeline stages. Each one is idempotent: a `jobs` row keyed by stage, subject and input hash records that the
work was done, so running a stage again (a retry, a duplicate queue message, a re-upload) does nothing new.

These are plain functions taking a session and storage, so tests run them without Redis; `jobs.py` wraps them
for the arq worker. Stage chain in M1: ingest(upload) → render(sheet) and classify(sheet) for each new page →
calibrate(sheet) once its type is known → layout(sheet) for beam layouts with a scale (M2), schedule(sheet) for
beam schedules (M3). Then slab by sheet type (M5).
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

from ..db.models import Bar, BeamSpec, Building, Floor, Issue, Job, Member, Sheet, Upload
from ..settings import get_settings
from ..storage import Storage, sheet_key
import numpy as np

from . import calibrate as cal
from . import classify as cls
from . import layout as lay
from . import reconcile as rec
from . import vision as vis
from . import schedule as sch
from . import ocr, pdf

CLASSIFY_VERSION = 2             # bump when the title-block reader changes, so sheets are read again
CALIBRATE_VERSION = 1
SCHEDULE_VERSION = 1
LAYOUT_VERSION = 2               # 2: cantilevers and slab outline

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
            if r.outline and not db.scalar(select(Member.id).where(Member.sheet_id == s.id, Member.kind == 'slab_panel')):
                xs, ys = [p[0] for p in r.outline.points], [p[1] for p in r.outline.points]
                db.add(Member(project_id=s.project_id, sheet_id=s.id, floor_id=s.floor_id, kind='slab_panel',
                              name='Slab', x1=min(xs), y1=min(ys), x2=max(xs), y2=max(ys),
                              points=[list(p) for p in r.outline.points], area_sqft=r.outline.area_sqft,
                              confidence=0.7, note='Floor plate traced from the beam edges (beams included).'))
            for u in r.unplaced:
                db.add(Member(project_id=s.project_id, sheet_id=s.id, floor_id=s.floor_id, kind='beam', name=u.name,
                              vertical=u.vertical, x1=u.x, y1=u.y, x2=u.x, y2=u.y, label_box=list(u.box),
                              label_b_in=u.b_in, label_d_in=u.d_in, confidence=0,
                              note='Label read, but no beam found next to it.'))
            names = {b.name for b in r.beams if not b.cantilever}
            cants = sum(1 for b in r.beams if b.cantilever)
            s.layout_version = LAYOUT_VERSION
            s.layout_note = (f'{len(names)} beams placed from {len(names | {u.name for u in r.unplaced})} labels read; '
                             f'{cants} cantilever parts; {len(r.columns)} columns'
                             + (f'; floor plate {r.outline.area_sqft:,.0f} sq ft.' if r.outline else '.'))
    except Skip:
        return


def schedule(db: Session, storage: Storage, sheet_id, *, final: bool = True) -> None:
    """Read a beam schedule sheet into `beam_specs` + `bars` (OCR reading; see schedule.py). Specs a person decided
    on are kept; elevations already read on another sheet of the project are kept as duplicates and raise one
    Issue, so the beam is counted once."""
    s = db.get(Sheet, sheet_id)
    if s is None or s.sheet_type != 'beam_schedule':
        return
    try:
        with job(db, 'schedule', s.id, f'{s.page_hash}:v{SCHEDULE_VERSION}', s.project_id, final=final, sheet_id=s.id):
            with fetch(storage, s.upload.storage_key) as f:
                r = sch.read_schedule(f.name, s.page_no)
            db.refresh(s)
            db.query(BeamSpec).filter(BeamSpec.sheet_id == s.id, BeamSpec.status == 'auto').delete()
            kept = set(db.scalars(select(BeamSpec.name).where(BeamSpec.sheet_id == s.id)))
            others = {b.name: b for b in db.scalars(select(BeamSpec).where(
                BeamSpec.project_id == s.project_id, BeamSpec.sheet_id != s.id, BeamSpec.duplicate_of.is_(None)))}
            repeats, n = [], 0
            for e in r.elevations:
                name = sch.part_name(e, r.elevations)
                if not name or name in kept:
                    continue
                spec = BeamSpec(project_id=s.project_id, sheet_id=s.id, floor_id=s.floor_id, name=name,
                                kind='S' if e.name else 'C', b_in=e.b_in, D_in=e.D_in, D2_in=e.D2_in,
                                stirrup_dia=(e.stirrup or {}).get('dia'), stirrup_end_in=(e.stirrup or {}).get('end_in'),
                                stirrup_mid_in=(e.stirrup or {}).get('mid_in'), box=list(e.box), tokens=e.tokens,
                                notes=' '.join(e.notes) or None, source='ocr', confidence=sch.confidence(e))
                spec.bars = [Bar(role=b.role, nos=b.nos, dia=b.dia, extra_top_in=b.extra_top_in, sort=i)
                             for i, b in enumerate(e.bars)]
                if name in others:
                    spec.duplicate_of = others[name].id
                    repeats.append(name)
                db.add(spec)
                n += 1
            if repeats:
                repeat_issue(db, s, repeats)
            s.schedule_version = SCHEDULE_VERSION
            s.layout_note = (f'{n} beam elevations read' + (f'; {len(repeats)} repeat another sheet' if repeats else '')
                             + '.')
    except Skip:
        return


def repeat_issue(db: Session, s: Sheet, names: list[str]) -> None:
    """One Issue per sheet listing the elevations it repeats (pilot: schedule sheet 2 repeats B68–B73)."""
    code = f'REP-{s.drg_no or s.id.hex[:6]}'
    i = db.scalar(select(Issue).where(Issue.project_id == s.project_id, Issue.code == code))
    if i is None:
        i = Issue(project_id=s.project_id, code=code, rule='schedule_repeat', severity='low', kind='Drawing repeats')
        db.add(i)
    i.title = f'{s.drg_no or "A schedule sheet"} repeats {len(names)} beam elevation{"s" if len(names) != 1 else ""}'
    i.detail = f'Already drawn on another schedule sheet: {", ".join(sorted(names, key=lambda n: (len(n), n)))}. Counted once.'
    i.affects, i.assumption = 'Steel quantity', 'Each beam counted once'
    i.ask = 'Confirm these are repeats, not separate beams with the same names.'
    i.beams = sorted({n.split('-')[0] for n in names})


def vision(db: Session, storage: Storage, sheet_id, *, final: bool = True, client=None) -> str:
    """Second reading of schedule elevations OCR could not fully read (M3 step 4–5). Off unless switched on with a
    key (see vision.enabled); a test or replay passes `client`. Returns what happened, for the log."""
    s = db.get(Sheet, sheet_id)
    if s is None or s.sheet_type != 'beam_schedule':
        return 'not a schedule'
    st = get_settings()
    if client is None:
        if not vis.enabled():
            return 'off'
        client = vis.default_client()
    specs = [b for b in db.scalars(select(BeamSpec).where(BeamSpec.sheet_id == s.id, BeamSpec.status == 'auto',
                                                          BeamSpec.duplicate_of.is_(None)).order_by(BeamSpec.name))
             if b.box and b.confidence < st.vision_below_confidence
             and (b.readings or {}).get('tried') != vis.PROMPT_VERSION]      # failures are not sent again either
    if not specs:
        return 'nothing to read'
    calls = 0
    try:
        pending = hashlib.sha256(','.join(sorted(str(b.id) for b in specs)).encode()).hexdigest()[:12]
        with job(db, 'vision', s.id, f'{vis.PROMPT_VERSION}:{st.vision_model}:{pending}', s.project_id,
                 final=final, sheet_id=s.id):
            with fetch(storage, s.upload.storage_key) as f:
                img, dpi = pdf.render_gray(f.name, s.page_no, vis.CROP_DPI)
            for spec in specs:
                if calls >= st.vision_max_calls_per_sheet:
                    spec.notes = ((spec.notes or '') + ' Not sent to the vision model: per-sheet limit reached.').strip()
                    continue
                png = vis.crop_from(img, dpi, spec.box)
                ocr = rec.from_spec(spec)
                first, c1 = vis.read(db, client, png, st.vision_model, project_id=s.project_id, spec_id=spec.id)
                calls += not c1.cached
                second = None
                if first is None:                    # refused or failed: try the escalation model once
                    second, c2 = vis.read(db, client, png, st.vision_escalate_model, project_id=s.project_id,
                                          spec_id=spec.id)
                    calls += not c2.cached
                    if second is None:
                        spec.notes = ((spec.notes or '') + ' The vision model could not read this elevation.').strip()
                        spec.readings = dict(spec.readings or {}, tried=vis.PROMPT_VERSION)
                        continue
                    first, second = second, None
                merged, status, conf = rec.reconcile(ocr, rec.from_model(first))
                if 'disagree' in status.values():
                    second, c3 = vis.read(db, client, png, st.vision_escalate_model, project_id=s.project_id,
                                          spec_id=spec.id)
                    calls += not c3.cached
                    if second is not None:
                        merged, status, conf = rec.reconcile(ocr, rec.from_model(first), rec.from_model(second))
                apply_reading(spec, merged, status, conf, first, second)
            db.flush()
    except Skip:
        return 'already read'
    return f'{len(specs)} elevations read, {calls} model calls'


def apply_reading(spec: BeamSpec, merged: dict, status: dict, conf: float, first, second) -> None:
    """Put the reconciled values on the spec, keeping every reading and each field's status for Review."""
    ocr_snapshot = dict(size=[spec.b_in, spec.D_in], stirrup=[spec.stirrup_dia, spec.stirrup_end_in, spec.stirrup_mid_in],
                        bars=[[b.role, b.nos, b.dia, b.extra_top_in] for b in spec.bars])
    from .vision import PROMPT_VERSION
    spec.readings = dict(ocr=ocr_snapshot, model=first.model_dump(), escalated=second.model_dump() if second else None,
                         tried=PROMPT_VERSION)
    spec.field_status = status
    if merged['size']:
        spec.b_in, spec.D_in = merged['size']
    spec.stirrup_dia, spec.stirrup_end_in, spec.stirrup_mid_in = (
        merged['stirrup_dia'], merged['stirrup_end_in'], merged['stirrup_mid_in'])
    lengths = rec.extra_top_lengths(second or first)
    old_len = {(b.nos, b.dia): b.extra_top_in for b in spec.bars if b.role == 'Extra top @ support'}
    spec.bars = [Bar(role=role, nos=n, dia=d, sort=i,
                     extra_top_in=(lengths.get((n, d)) or old_len.get((n, d))) if role == 'Extra top @ support' else None)
                 for i, (role, n, d) in enumerate((r, n, d) for r in rec.ROLES for n, d in (merged[r] or ()))]
    spec.source, spec.confidence = 'ocr+model', conf
    worry = {f: s for f, s in status.items() if s != 'agree'}
    spec.notes = ('Readings differ or are incomplete: ' + ', '.join(f'{f} ({s.replace("_", " ")})' for f, s in worry.items())
                  + '.') if worry else None
