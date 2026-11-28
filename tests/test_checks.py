"""Checks → Issues (plan M4): stable upserts, people's statuses kept, only evaluated rules remove Issues; the
numbering and size rules; issue status changes; issues from a saved run."""
from types import SimpleNamespace as NS

from sqlalchemy import select

from app.db.models import Assumption, Correction, Issue, ProjectMember, Sheet, Upload, User
from app.services import checks
from conftest import sign_in
from test_uploads import proj  # noqa: F401 (fixture)
from tools.import_pilot import import_pilot


def sheet_for(session, p):
    u = Upload(project_id=p.id, filename='a.pdf', sha256='y' * 64, size_bytes=1, storage_key='k', status='done')
    session.add(u)
    session.flush()
    session.add(Sheet(project_id=p.id, upload_id=u.id, page_no=1, page_hash='h', width_pt=10, height_pt=10, status='ready'))
    session.flush()


def codes(session, p):
    return {i.code: i for i in session.scalars(select(Issue).where(Issue.project_id == p.id))}


def test_values_not_stated_upsert_keep_status_and_clear(session, proj):  # noqa: F811
    from app.services.values import ensure_values
    ensure_values(proj)
    checks.run(session, proj.id)
    assert codes(session, proj) == {}                                     # no drawings uploaded: not evaluated
    sheet_for(session, proj)
    checks.run(session, proj.id)
    checks.run(session, proj.id)                                          # again: no duplicates
    got = codes(session, proj)
    assert set(got) == {'X-STATED-GRADE', 'X-STATED-COVER'} and got['X-STATED-GRADE'].assumption == 'Concrete grade M25'

    # The value used follows Values we need; a person's status survives re-runs.
    a = {x.key: x for x in proj.assumptions}
    a['concrete_grade'].value = 30
    got['X-STATED-COVER'].status = 'answered'
    checks.run(session, proj.id)
    got = codes(session, proj)
    assert got['X-STATED-GRADE'].assumption == 'Concrete grade M30' and got['X-STATED-COVER'].status == 'answered'

    # Read from the drawings: the open issue goes, the answered one stays.
    for k in ('concrete_grade', 'cover_mm', 'ld_factor'):
        a[k].source = 'Read from MC-SD-07 general notes.'
    checks.run(session, proj.id)
    assert set(codes(session, proj)) == {'X-STATED-COVER'}


def m(name, b=None, d=None, bw=9.0, status='auto'):
    return NS(name=name, label_b_in=b, label_d_in=d, b_in=bw, status=status, confidence=0.9, note=None)


def s(name, b=9, d=18, d2=None):
    return NS(name=name, b_in=b, D_in=d, D2_in=d2)


def test_numbering_gaps_include_hidden_beams_and_need_few_gaps():
    names = [f'B{k}' for k in range(1, 21) if k not in (7, 13)]
    ctx = checks.Ctx(None, None, [m(n) for n in names] + [m('HB15')], [s('B20A')], [])
    assert checks.check_numbering(ctx)['X-NUMBERING']['title'] == 'Beam numbers B7, B13 do not appear'
    ctx = checks.Ctx(None, None, [m(f'B{k}') for k in range(1, 21, 3)], [], [])   # most missing: not read yet
    assert checks.check_numbering(ctx) == {}


def test_size_conflicts_skip_misreads_cantilever_parts_and_two_depths():
    ctx = checks.Ctx(None, None,
                     [m('B5', 9, 24), m('B32', 8, 18), m('HB70', 9, 14), m('B90-cant', 9, 24), m('B49', 9, 21), m('B6', 9, 18)],
                     [s('B5'), s('B32'), s('HB70'), s('B90-cant'), s('B49', 9, 21, 24), s('B6')], [])
    assert set(checks.check_size(ctx)) == {'X-SIZE-B5'}


def test_issue_status_changes(client, session, proj):  # noqa: F811
    session.add(Issue(project_id=proj.id, code='I1', severity='high', title='Grade not stated', ask='Confirm grade.'))
    session.flush()
    sign_in(client, 'engineer@firm.in')
    assert client.patch('/api/projects/site-b/issues/I1', json=dict(status='resolved')).status_code == 403
    sign_in(client, 'new@example.com')
    r = client.patch('/api/projects/site-b/issues/I1', json=dict(status='answered'))
    assert r.status_code == 200 and r.json()['status'] == 'answered'
    assert client.patch('/api/projects/site-b/issues/I1', json=dict(status='closed')).status_code == 422
    assert client.patch('/api/projects/site-b/issues/NOPE', json=dict(status='open')).status_code == 404
    assert session.scalar(select(Correction).where(Correction.field == 'issue:I1:status')).new_value == 'answered'
    issues = client.get('/api/projects/site-b').json()['issues']
    assert issues[0]['status'] == 'answered' and issues[0]['auto'] is False


def test_issues_from_the_pilots_run(session):
    p = import_pilot(session, 'worklounge')
    got = codes(session, p)
    assert {'X-ASSUMED-TOP', 'X-SITE'} <= set(got)                      # from the imported run's results
    assert got['X-ASSUMED-TOP'].assumption == 'L/3 + b/2' and 'L/3 used' in got['X-ASSUMED-TOP'].title
    assert not any(c.startswith('X-STATED') for c in got)                 # the pilot's own I1/I2 cover these
