"""M3 acceptance (plan §3 M3): Work Lounge from upload → review gives beam inputs equal to inputs.json and the
pinned 7,744 kg; B68–B73 counted once with an Issue; the B5 size conflict raised.

Review is done through the API exactly as a person would in the Review screen, using the pilot's hand-entered
schedule (inputs.json) as that person's answers. The test also reports how much reviewing it took."""
import json
import shutil
from pathlib import Path

import pytest
from sqlalchemy import select

from app.db.models import BeamSpec, Issue, Member, ProjectMember, Sheet, User
from app.pipeline import stages
from app.services.inputs_builder import build_inputs
from conftest import sign_in
from test_uploads import LAYOUT, SCHED1, SCHED2, storage, queued  # noqa: F401 (fixtures)
from tools.import_pilot import import_pilot

needs_tesseract = pytest.mark.skipif(not shutil.which('tesseract'), reason='tesseract not installed')
TRUTH = {b['name']: b for b in json.loads((Path(__file__).parents[1] / 'app/data/worklounge/inputs.json').read_text())['beams']}


def norm_beam(b):
    return (b['name'], float(b['b_in']), float(b['D_in']), float(b['clear_in']), b['kind'],
            (b['stirrup']['dia'], float(b['stirrup']['end_in']), float(b['stirrup']['mid_in'])),
            tuple(sorted((x['role'], x['nos'], x['dia'], float(x.get('extra_top_in') or 0))
                         for x in b['bars'])))


@needs_tesseract
def test_upload_review_calculate_worklounge(client, session, storage, queued):  # noqa: F811
    p = import_pilot(session, 'worklounge')
    u = User(email='new@example.com')
    session.add(u)
    session.flush()
    session.add(ProjectMember(project_id=p.id, user_id=u.id, role='owner'))
    session.flush()
    sign_in(client, 'new@example.com')
    for name, data in [('layout.pdf', LAYOUT), ('s1.pdf', SCHED1), ('s2.pdf', SCHED2)]:
        assert client.post('/api/projects/worklounge/uploads', files=[('files', (name, data, 'application/pdf'))]).status_code == 201
    ids = [sid for uid in queued for sid in stages.ingest(session, storage, uid)]
    for sid in ids:
        stages.classify(session, storage, sid)
        stages.calibrate(session, storage, sid)
    layout = session.scalar(select(Sheet).where(Sheet.sheet_type == 'beam_layout'))
    # The person confirms the layout's scale first (the pilot measured 1:120 by hand).
    assert client.patch(f'/api/projects/worklounge/sheets/{layout.id}', json=dict(scale_ratio=120)).status_code == 200
    for sid in ids:
        stages.layout(session, storage, sid)
        stages.schedule(session, storage, sid)

    # Before review: nothing unreviewed reaches a number.
    r = client.post('/api/projects/worklounge/calculate', json={})
    assert r.status_code == 409 and 'still to check in Review' in r.json()['detail']
    issues = {i.code: i for i in session.scalars(select(Issue).where(Issue.project_id == p.id))}
    assert 'X-SIZE-B5' in issues and '9"×24"' in issues['X-SIZE-B5'].title           # the B5 conflict on record
    assert any(i.rule == 'schedule_repeat' and 'B68' in i.detail for i in issues.values())

    # Review, as a person would.
    edits = dict(accepted=0, corrected=0, added=0, rejected=0)
    url = lambda kind, x: f'/api/projects/worklounge/{kind}/{x.id}'          # noqa: E731
    members = {}
    for m in session.scalars(select(Member).where(Member.project_id == p.id, Member.kind == 'beam')):
        if m.name in TRUTH and m.name not in members and (m.x1, m.y1) != (m.x2, m.y2):
            members[m.name] = m
    for m in session.scalars(select(Member).where(Member.project_id == p.id)):
        if m.kind != 'beam' or members.get(m.name) is not m:
            if m.status == 'auto':
                client.patch(url('members', m), json=dict(action='reject'))       # extras, duplicates, columns, plate
                edits['rejected'] += m.kind == 'beam'
            continue
        t = TRUTH[m.name]
        if abs((m.clear_in or 0) - t['clear_in']) > 0.05:
            assert client.patch(url('members', m), json=dict(clear_in=t['clear_in'], action='accept')).status_code == 200
            edits['corrected'] += 1
        else:
            assert client.patch(url('members', m), json=dict(action='accept')).json()['status'] == 'accepted'
            edits['accepted'] += 1
    for n in sorted(set(TRUTH) - set(members)):                                 # missed on the layout: draw them
        r = client.post(f'/api/projects/worklounge/sheets/{layout.id}/members',
                        json=dict(name=n, x1=10, y1=10, x2=60, y2=10)).json()
        client.patch(f"/api/projects/worklounge/members/{r['id']}", json=dict(clear_in=TRUTH[n]['clear_in']))
        edits['added'] += 1

    sched = session.scalar(select(Sheet).where(Sheet.drg_no == 'MC-SD-06-01'))
    specs = {b.name: b for b in session.scalars(select(BeamSpec).where(BeamSpec.project_id == p.id,
                                                                       BeamSpec.duplicate_of.is_(None)))}
    for n, b in specs.items():
        if n not in TRUTH:
            client.patch(url('specs', b), json=dict(action='reject'))
            edits['rejected'] += 1
            continue
        t = TRUTH[n]
        want = dict(kind=t['kind'], b_in=t['b_in'], D_in=t['D_in'], stirrup_dia=t['stirrup']['dia'],
                    stirrup_end_in=t['stirrup']['end_in'], stirrup_mid_in=t['stirrup']['mid_in'],
                    bars=[dict(role=x['role'], nos=x['nos'], dia=x['dia'], extra_top_in=x.get('extra_top_in')) for x in t['bars']])
        r = client.patch(url('specs', b), json=dict(want, action='accept'))
        assert r.status_code == 200, (n, r.json())
        edits['accepted' if r.json()['status'] == 'accepted' else 'corrected'] += 1
    for n in sorted(set(TRUTH) - set(specs)):                                   # missed in the schedule: enter them
        t = TRUTH[n]
        r = client.post(f'/api/projects/worklounge/sheets/{sched.id}/specs', json=dict(
            name=n, kind=t['kind'], b_in=t['b_in'], D_in=t['D_in'], stirrup_dia=t['stirrup']['dia'], stirrup_end_in=t['stirrup']['end_in'],
            stirrup_mid_in=t['stirrup']['mid_in'],
            bars=[dict(role=x['role'], nos=x['nos'], dia=x['dia'], extra_top_in=x.get('extra_top_in')) for x in t['bars']]))
        assert r.status_code == 201, (n, r.json())
        edits['added'] += 1

    # After review: the engine's beam inputs are the hand-entered ones, and the pinned total comes out.
    session.expire_all()
    beams = build_inputs(session.get(type(p), p.id))['beams']
    assert sorted(norm_beam(b) for b in beams) == sorted(norm_beam(b) for b in TRUTH.values())
    r = client.post('/api/projects/worklounge/calculate', json={}).json()
    assert round(r['summary']['beam_steel_kg']) == 7744
    assert client.get('/api/projects/worklounge/review').json()['items'] == []
    print('\nReview effort on Work Lounge:', edits)
