"""Review queue: order, progress, accept / edit / redraw / reject / reopen / add, corrections, access."""
import pytest
from sqlalchemy import select

from app.db.models import Correction, Member, Sheet, Upload
from conftest import sign_in
from test_uploads import proj  # noqa: F401 (fixture)


@pytest.fixture
def sheet(session, proj):  # noqa: F811
    u = Upload(project_id=proj.id, filename='layout.pdf', sha256='x' * 64, size_bytes=1, storage_key='k', status='done')
    session.add(u)
    session.flush()
    s = Sheet(project_id=proj.id, upload_id=u.id, page_no=1, page_hash='h', width_pt=1191, height_pt=842,
              status='ready', sheet_type='beam_layout', drg_no='MC-SD-07', pt_per_in=0.6, scale_status='confirmed')
    session.add(s)
    session.flush()
    rows = [('B1', 0.9, (100, 100, 200, 100)), ('B2', 0.5, (100, 200, 100, 300)), ('B40', 0, (50, 50, 50, 50))]
    ms = {}
    for name, conf, (x1, y1, x2, y2) in rows:
        m = Member(project_id=proj.id, sheet_id=s.id, kind='beam', name=name, vertical=x1 == x2, x1=x1, y1=y1, x2=x2,
                   y2=y2, confidence=conf, label_box=[x1, y1 - 5, x1 + 20, y1])
        session.add(m)
        ms[name] = m
    session.add(Member(project_id=proj.id, sheet_id=s.id, kind='column', x1=10, y1=10, x2=15.4, y2=24.4, confidence=0.8))
    session.flush()
    return s, ms


def test_queue_order_and_progress(client, sheet):
    sign_in(client, 'engineer@firm.in')                       # viewers can see the queue
    q = client.get('/api/projects/site-b/review').json()
    assert (q['total'], q['checked']) == (3, 0)                # the column is not listed: not counted until M6
    assert [i['name'] for i in q['items']] == ['B40', 'B2', 'B1'] and all(i['kind'] == 'beam' for i in q['items'])
    b40 = q['items'][0]
    assert not b40['placed'] and b40['sheet']['drg_no'] == 'MC-SD-07' and b40['sheet']['image_url'].endswith('display.png')
    assert client.patch(f"/api/projects/site-b/members/{b40['id']}", json=dict(action='reject')).status_code == 403


def test_accept_edit_redraw_reject_reopen(client, session, sheet):
    s, ms = sheet
    sign_in(client, 'new@example.com')
    url = lambda m: f'/api/projects/site-b/members/{m.id}'           # noqa: E731
    assert client.patch(url(ms['B1']), json=dict(action='accept')).json()['status'] == 'accepted'
    # A label with no beam can't be accepted as is; redraw it, then it counts as edited.
    r = client.patch(url(ms['B40']), json=dict(action='accept'))
    assert r.status_code == 422 and 'redraw' in r.json()['detail']
    r = client.patch(url(ms['B40']), json=dict(x1=300, y1=402, x2=420, y2=398, action='accept')).json()
    assert (r['status'], r['vertical'], r['y1'], r['y2'], r['x1'], r['x2']) == ('edited', False, 400, 400, 300, 420)
    assert r['length_in'] == pytest.approx(200)                     # 120 pt at 0.6 pt/in
    r = client.patch(url(ms['B2']), json=dict(name='B2A', label_b_in=9, label_d_in=24)).json()
    assert (r['name'], r['label_d_in'], r['status']) == ('B2A', 24, 'edited')
    assert client.patch(url(ms['B2']), json=dict(name='not a beam')).status_code == 422
    assert client.patch(url(ms['B2']), json=dict(action='reject')).json()['status'] == 'rejected'
    assert client.patch(url(ms['B2']), json=dict(action='reopen')).json()['status'] == 'auto'
    q = client.get('/api/projects/site-b/review').json()
    assert (q['total'], q['checked']) == (3, 2) and {i['name'] for i in q['items']} == {'B2A'}
    fields = [c.field for c in session.scalars(select(Correction).order_by(Correction.created_at))]
    assert {'status', 'geometry', 'name', 'label_b_in', 'label_d_in'} <= set(fields)


def test_add_a_missed_beam(client, session, sheet):
    s, _ = sheet
    sign_in(client, 'new@example.com')
    r = client.post(f'/api/projects/site-b/sheets/{s.id}/members', json=dict(name='B48', x1=500, y1=300, x2=505, y2=420))
    assert r.status_code == 201
    m = r.json()
    assert (m['status'], m['vertical'], m['x1'], m['x2'], m['y1'], m['y2']) == ('added', True, 502.5, 502.5, 300, 420)
    assert session.scalar(select(Correction).where(Correction.field == 'added')).new_value == 'B48'
    assert client.post(f'/api/projects/site-b/sheets/{s.id}/members',
                       json=dict(name='B49', x1=1, y1=1, x2=1, y2=1)).status_code == 422


def test_outsiders_and_wrong_ids(client, sheet):
    _, ms = sheet
    sign_in(client, 'someone@firm.in')
    assert client.get('/api/projects/site-b/review').status_code == 404
    client.cookies.clear()
    sign_in(client, 'new@example.com')
    assert client.patch('/api/projects/site-b/members/nope', json=dict(action='accept')).status_code == 404


def test_a_schedule_change_in_review_reads_the_layout_again(client, session, sheet):
    """The schedule settles cantilevers on the layout, so a Review change to which beams have cantilever parts queues
    the floor's layouts to be read again (after the change is committed). Other edits don't."""
    from app.api.review import get_relayout
    from app.main import app
    from app.db.models import Bar, BeamSpec
    from app.pipeline import stages
    from app.pipeline.jobs import stale_work
    s, _ = sheet
    u = session.get(Upload, s.upload_id)
    sch = Sheet(project_id=s.project_id, upload_id=u.id, page_no=2, page_hash='h2', width_pt=1191, height_pt=842,
                status='ready', sheet_type='beam_schedule', drg_no='MC-SD-06-01')
    session.add(sch)
    session.flush()
    b5 = BeamSpec(project_id=s.project_id, sheet_id=sch.id, name='B5', b_in=9, D_in=18, stirrup_dia=8,
                  bars=[Bar(role='Bottom through', nos=2, dia=12, sort=0)])
    session.add(b5)
    session.flush()
    s.layout_version, s.layout_schedule = stages.LAYOUT_VERSION, stages.schedule_key(stages.schedule_cants(session, s))
    session.flush()
    calls = []
    app.dependency_overrides[get_relayout] = lambda: (lambda ids, reason: calls.append(list(ids)))
    sign_in(client, 'new@example.com')

    r = client.patch(f'/api/projects/site-b/specs/{b5.id}', json=dict(stirrup_mid_in=8)).json()
    assert r['layouts_rereading'] == 0 and calls == []                  # cantilevers unchanged

    bars = [dict(role='Top through', nos=2, dia=12), dict(role='Bottom through', nos=2, dia=12)]
    r = client.post(f'/api/projects/site-b/sheets/{sch.id}/specs',
                    json=dict(name='B5-cant', b_in=9, D_in=18, bars=bars))
    assert r.status_code == 201 and r.json()['layouts_rereading'] == 1 and calls == [[s.id]]
    assert stages.stale_schedule(session, s)
    assert s.id in stale_work(session, stages.now())[4]               # the minute sweep would queue it too

    s.layout_schedule = stages.schedule_key(stages.schedule_cants(session, s))   # as if the worker read it again
    session.flush()
    cant = session.scalar(select(BeamSpec).where(BeamSpec.name == 'B5-cant'))
    r = client.patch(f'/api/projects/site-b/specs/{cant.id}', json=dict(action='reject')).json()
    assert r['layouts_rereading'] == 1 and len(calls) == 2              # rejecting it changes them back
