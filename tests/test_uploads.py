"""Uploads: validation, access, de-duplication, and the ingest/render stages (run directly, without Redis)."""
import io
from pathlib import Path

import pikepdf
import pytest
from PIL import Image
from sqlalchemy import select

from app.api.uploads import get_enqueue
from app.db.models import Job, Project, ProjectMember, Sheet, Upload, User
from app.main import app
from app.pipeline import pdf, stages
from app.settings import get_settings
from app.storage import MemoryStorage, get_storage
from conftest import sign_in

SRC = Path(__file__).parents[1] / 'app/data/worklounge/source'
LAYOUT = (SRC / '260919_SF_BEAM LAYOUT.pdf').read_bytes()
SCHED1 = (SRC / '260919_SF_BEAM SCHEDULE -01.pdf').read_bytes()


def combined(*parts: bytes, encrypt: bool = False) -> bytes:
    """One PDF with the pages of `parts`, re-saved (so its bytes differ from any part)."""
    out = pikepdf.new()
    srcs = [pikepdf.open(io.BytesIO(b)) for b in parts]
    for s in srcs:
        out.pages.extend(s.pages)
    out.docinfo['/Title'] = 'Combined set'
    buf = io.BytesIO()
    out.save(buf, encryption=pikepdf.Encryption(user='secret', owner='secret') if encrypt else False)
    return buf.getvalue()


@pytest.fixture
def storage():
    s = MemoryStorage()
    app.dependency_overrides[get_storage] = lambda: s
    return s


@pytest.fixture
def queued():
    q = []
    app.dependency_overrides[get_enqueue] = lambda: q.append
    return q


@pytest.fixture
def proj(session):
    p = Project(slug='site-b', name='Site B', meta_json={})
    session.add(p)
    session.flush()
    for email, role in (('new@example.com', 'editor'), ('engineer@firm.in', 'viewer')):
        u = User(email=email)
        session.add(u)
        session.flush()
        session.add(ProjectMember(project_id=p.id, user_id=u.id, role=role))
    session.flush()
    return p


def post(client, *files):
    return client.post('/api/projects/site-b/uploads', files=[('files', (n, b, 'application/pdf')) for n, b in files])


def test_upload_needs_editor(client, proj, storage, queued):
    assert post(client, ('a.pdf', LAYOUT)).status_code == 401
    sign_in(client, 'engineer@firm.in')
    assert post(client, ('a.pdf', LAYOUT)).status_code == 403
    assert client.get('/api/projects/site-b/uploads').json() == []
    assert storage.files == {} and queued == []


def test_upload_validation(client, proj, storage, queued, monkeypatch):
    sign_in(client, 'new@example.com')
    r = post(client, ('notes.pdf', b'hello, not a pdf'))
    assert r.status_code == 415 and 'not a PDF' in r.json()['detail']
    monkeypatch.setenv('MAX_UPLOAD_MB', '1')
    get_settings.cache_clear()
    r = post(client, ('big.pdf', b'%PDF-1.7\n' + b'0' * (1 << 20)))
    assert r.status_code == 413 and '1 MB' in r.json()['detail']
    assert storage.files == {} and queued == []


def test_same_file_twice_does_no_new_work(client, proj, storage, queued):
    sign_in(client, 'new@example.com')
    a = post(client, ('layout.pdf', LAYOUT)).json()['uploads'][0]
    assert a['status'] == 'queued' and not a['duplicate']
    b = post(client, ('copy of layout.pdf', LAYOUT)).json()['uploads'][0]
    assert b['id'] == a['id'] and b['duplicate']
    assert len(queued) == 1 and len(storage.files) == 1


def test_ingest_and_render(client, session, proj, storage, queued):
    sign_in(client, 'new@example.com')
    uid = post(client, ('layout.pdf', LAYOUT)).json()['uploads'][0]['id']
    new = stages.ingest(session, storage, queued[0])
    assert len(new) == 1
    stages.render(session, storage, new[0])
    s = session.get(Sheet, new[0])
    assert s.status == 'ready' and (s.width_pt, s.height_pt) == (1191.0, 842.0) and s.display_dpi == 300
    img = Image.open(io.BytesIO(storage.get(s.display_key)))
    assert abs(img.size[0] - 1191 * 300 / 72) <= 1 and abs(img.size[1] - 842 * 300 / 72) <= 1   # pdfium rounds up
    assert Image.open(io.BytesIO(storage.get(s.ocr_key))).mode == 'L'

    ups = client.get('/api/projects/site-b/uploads').json()
    assert ups[0]['id'] == uid and ups[0]['status'] == 'done' and ups[0]['pages'] == 1
    assert ups[0]['sheets'][0]['status'] == 'ready'
    r = client.get(ups[0]['sheets'][0]['thumb_url'], follow_redirects=False)
    assert r.status_code == 302 and r.headers['location'] == f'memory://{s.thumb_key}'
    assert client.get(f'/api/projects/site-b/sheets/{s.id}/ocr300.png').status_code == 404
    assert client.get('/api/projects/site-b/sheets/not-a-uuid/thumb.png').status_code == 404

    # Running the stages again (a retry or a duplicate message) does nothing new.
    assert stages.ingest(session, storage, queued[0]) == []
    stages.render(session, storage, new[0])
    assert session.scalar(select(Job).where(Job.stage == 'render')).attempts == 1


def test_pages_already_in_the_project_are_not_redone(client, session, proj, storage, queued):
    sign_in(client, 'new@example.com')
    post(client, ('layout.pdf', LAYOUT))
    stages.ingest(session, storage, queued[0])
    post(client, ('full set.pdf', combined(LAYOUT, SCHED1)))
    new = stages.ingest(session, storage, queued[1])
    up = session.get(Upload, queued[1])
    assert up.pages == 2 and up.new_sheets == 1 and len(new) == 1
    assert session.get(Sheet, new[0]).page_no == 2


def test_password_protected_pdf_is_refused_with_a_reason(client, session, proj, storage, queued):
    sign_in(client, 'new@example.com')
    post(client, ('locked.pdf', combined(LAYOUT, encrypt=True)))
    assert stages.ingest(session, storage, queued[0]) == []
    u = client.get('/api/projects/site-b/uploads').json()[0]
    assert u['status'] == 'failed' and 'password-protected' in u['error']


def test_failed_render_marks_only_that_sheet(client, session, proj, storage, queued, monkeypatch):
    sign_in(client, 'new@example.com')
    post(client, ('set.pdf', combined(LAYOUT, SCHED1)))
    a, b = stages.ingest(session, storage, queued[0])
    real = pdf.render_page

    def flaky(path, page_no):
        if page_no == 1:
            raise MemoryError('too big')
        return real(path, page_no)
    monkeypatch.setattr(pdf, 'render_page', flaky)
    with pytest.raises(MemoryError):
        stages.render(session, storage, a, final=False)
    assert session.scalar(select(Job).where(Job.sheet_id == a)).status == 'queued'      # will be retried
    with pytest.raises(MemoryError):
        stages.render(session, storage, a, final=True)
    stages.render(session, storage, b)
    assert session.get(Sheet, a).status == 'needs_attention'
    assert session.get(Sheet, b).status == 'ready'
    j = session.scalar(select(Job).where(Job.sheet_id == a))
    assert j.status == 'failed' and j.attempts == 2 and 'too big' in j.error


def test_outsider_cannot_see_sheets(client, session, proj, storage, queued):
    sign_in(client, 'new@example.com')
    post(client, ('layout.pdf', LAYOUT))
    sid = stages.ingest(session, storage, queued[0])[0]
    stages.render(session, storage, sid)
    client.cookies.clear()
    sign_in(client, 'someone@firm.in')
    assert client.get('/api/projects/site-b/sheets').status_code == 404
    assert client.get(f'/api/projects/site-b/sheets/{sid}/display.png', follow_redirects=False).status_code == 404


def test_sweep_finds_work_a_dead_worker_left(client, session, proj, storage, queued):
    from datetime import timedelta
    from app.pipeline.jobs import JOB_TIMEOUT, stale_work
    sign_in(client, 'new@example.com')
    post(client, ('layout.pdf', LAYOUT))
    post(client, ('sched.pdf', SCHED1))
    a, b = queued
    t = stages.now()
    assert stale_work(session, t) == ([], [], [], [])                           # just queued: the queue has them
    later = t + timedelta(minutes=3)
    assert set(stale_work(session, later)[0]) == {a, b}                     # never picked up
    with pytest.raises(RuntimeError):
        with stages.job(session, 'ingest', a, 'x', proj.id, final=False, upload_id=a):
            session.get(Upload, a).status = 'processing'
            session.commit()
            raise RuntimeError('worker killed')                             # leaves the job queued again
    j = session.scalar(select(Job).where(Job.upload_id == a))
    j.status, j.started_at = 'running', t                                   # as if the process died mid-job
    session.get(Upload, a).status = 'processing'
    session.get(Upload, b).status = 'done'
    session.commit()
    assert stale_work(session, t + timedelta(seconds=60)) == ([], [], [], [])   # still within the time a job may take
    assert stale_work(session, t + timedelta(seconds=JOB_TIMEOUT + 61)) == ([a], [], [], [])


SCHED2 = (SRC / '260919_SF_BEAM SCHEDULE -02.pdf').read_bytes()
SLAB_B = (SRC / '260919_SF_BOTTOM SLAB REINFORCEMENT LAYOUT.pdf').read_bytes()
SLAB_T = (SRC / '260919_SF_TOP SLAB REINFORCEMENT LAYOUT.pdf').read_bytes()


@pytest.fixture
def floors(session, proj):
    from app.db.models import Building, Floor
    b = Building(project_id=proj.id, name='Main building', copies=1, sort=0)
    b.floors = [Floor(name=n, sort=i) for i, n in enumerate(['First floor', 'Second floor', 'Third floor'])]
    session.add(b)
    session.flush()
    return {f.name: f for f in b.floors}


def test_classify_worklounge(client, session, proj, floors, storage, queued):
    """Golden test (plan M1 acceptance): the five Work Lounge PDFs get the right type, floor and title block."""
    sign_in(client, 'new@example.com')
    for name, data in [('260919_SF_BEAM LAYOUT.pdf', LAYOUT), ('sched1.pdf', SCHED1), ('sched2.pdf', SCHED2),
                       ('bottom.pdf', SLAB_B), ('top.pdf', SLAB_T)]:
        post(client, (name, data))
    for uid in queued:
        for sid in stages.ingest(session, storage, uid):
            stages.classify(session, storage, sid)
    got = {s['drg_no']: s for s in client.get('/api/projects/site-b/sheets').json()}
    assert {k: v['sheet_type'] for k, v in got.items()} == {
        'MC-SD-07': 'beam_layout', 'MC-SD-06-01': 'beam_schedule', 'MC-SD-06-02': 'beam_schedule',
        'MC-SD-06-04': 'slab_bottom', 'MC-SD-06-05': 'slab_top'}
    second = str(floors['Second floor'].id)
    assert all(s['floor_id'] == second and s['classify_status'] == 'auto' for s in got.values())
    lay = got['MC-SD-07']
    assert (lay['title'], lay['revision'], lay['scale_text'], lay['drawn_on']) == \
        ('SECOND FLOOR SHUTTERING LAYOUT', 'R0', '1:100', '16.09.2026')


def test_unclear_sheet_needs_review_and_a_person_can_fix_it(client, session, proj, floors, storage, queued):
    sign_in(client, 'new@example.com')
    # A page with no text layer (an image saved as PDF), like a scan.
    buf = io.BytesIO()
    Image.new('RGB', (842, 595), 'white').save(buf, 'PDF')
    post(client, ('scan.pdf', buf.getvalue()))
    sid = stages.ingest(session, storage, queued[0])[0]
    stages.classify(session, storage, sid)
    s = client.get('/api/projects/site-b/sheets').json()[0]
    assert s['classify_status'] == 'needs_review'
    assert s['classify_note'] == 'No text could be read on this sheet, so its title block could not be read.'
    assert s['sheet_type'] is None and s['floor_id'] is None

    third = str(floors['Third floor'].id)
    r = client.patch(f'/api/projects/site-b/sheets/{sid}', json=dict(sheet_type='footing', floor_id=third))
    assert r.status_code == 200
    assert (r.json()['sheet_type'], r.json()['floor_id'], r.json()['classify_status']) == ('footing', third, 'confirmed')
    from app.db.models import Correction
    assert {c.field for c in session.scalars(select(Correction))} == {'sheet_type', 'floor_id'}

    # A confirmed sheet is never changed by the reader again, even when the floors change.
    from app.db.models import Floor
    session.add(Floor(building_id=floors['First floor'].building_id, name='Fourth floor', sort=9))
    session.flush()
    stages.classify(session, storage, sid)
    assert session.get(Sheet, sid).sheet_type == 'footing'

    assert client.patch(f'/api/projects/site-b/sheets/{sid}', json=dict(sheet_type='bogus')).status_code == 422
    other = Project(slug='other', name='Other', meta_json={})
    session.add(other)
    session.flush()
    from app.db.models import Building
    ob = Building(project_id=other.id, name='X', sort=0)
    ob.floors = [Floor(name='Ground', sort=0)]
    session.add(ob)
    session.flush()
    r = client.patch(f'/api/projects/site-b/sheets/{sid}', json=dict(floor_id=str(ob.floors[0].id)))
    assert r.status_code == 422


def test_viewer_cannot_correct(client, session, proj, floors, storage, queued):
    sign_in(client, 'new@example.com')
    post(client, ('layout.pdf', LAYOUT))
    sid = stages.ingest(session, storage, queued[0])[0]
    client.cookies.clear()
    sign_in(client, 'engineer@firm.in')
    assert client.patch(f'/api/projects/site-b/sheets/{sid}', json=dict(sheet_type='footing')).status_code == 403
    assert [t['key'] for t in client.get('/api/sheet-types').json()][:4] == \
        ['beam_layout', 'beam_schedule', 'slab_bottom', 'slab_top']


def test_floor_from_file_name_is_flagged(client, session, proj, floors, storage, queued):
    """The title says the floor; when only the file name does, the sheet is placed but marked for review."""
    from app.pipeline.classify import classify
    from app.pipeline.pdf import TextLine
    lines = [TextLine('MC-SD-09', .85, .87, .9, .88), TextLine('BEAM LAYOUT', .82, .82, .9, .83)]
    fl = [(f.id, f.name) for f in floors.values()]
    r = classify(lines, fl, '260919_SF_BEAM LAYOUT.pdf')
    assert r.sheet_type == 'beam_layout' and r.floor_id == floors['Second floor'].id
    assert r.status == 'needs_review' and 'file name' in r.note
    r = classify(lines, fl, 'layout.pdf')
    assert r.floor_id is None and 'does not name a floor' in r.note
