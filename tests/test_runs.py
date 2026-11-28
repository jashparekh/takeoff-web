"""Runs (plan M4): refused while values are unconfirmed or Review has work left; a person's tick; frozen results; the
latest run is what the app shows, and it says when it is out of date."""
import pytest
from sqlalchemy import select

from app.db.models import ProjectMember, Run, User
from conftest import sign_in
from tools.import_pilot import import_pilot

URL = '/api/projects/worklounge'


@pytest.fixture
def pilot(session):
    p = import_pilot(session, 'worklounge')
    for email, role in (('new@example.com', 'editor'), ('engineer@firm.in', 'viewer')):
        u = User(email=email)
        session.add(u)
        session.flush()
        session.add(ProjectMember(project_id=p.id, user_id=u.id, role=role))
    session.flush()
    return p


def confirm_all(client):
    vals = client.get(f'{URL}/values-needed').json()['values']
    r = client.put(f'{URL}/assumptions', json=dict(values=[dict(key=v['key'], confirm=True) for v in vals
                                                          if v['needs_confirm'] and not v['confirmed']]))
    assert r.status_code == 200 and r.json()['needed'] == 0


def test_the_imported_run_is_shown_until_a_new_one(client, pilot):
    sign_in(client, 'engineer@firm.in')
    p = client.get(URL).json()
    assert p['latest_run']['note'] == 'Imported from app/data/worklounge' and p['latest_run']['by'] is None
    assert p['latest_run']['current'] is True                           # registry defaults change nothing
    r = client.get(f'{URL}/runs/latest').json()
    assert round(r['results']['summary']['beam_steel_kg']) == 7744
    assert round(r['results']['notebook']['totals']['beams']['diff']) == 1638     # site check from the DB
    assert client.post(f'{URL}/runs', json=dict(checked=True)).status_code == 403  # viewers can't calculate


def test_a_run_waits_for_every_value_and_a_persons_tick(client, session, pilot):
    sign_in(client, 'new@example.com')
    blockers = client.get(URL).json()['blockers']
    assert len(blockers) == 1 and '10 values still to confirm' in blockers[0] and 'Concrete grade' in blockers[0]
    r = client.post(f'{URL}/runs', json=dict(checked=True))
    assert r.status_code == 409 and r.json()['detail']['missing'] == blockers

    confirm_all(client)
    assert client.get(URL).json()['blockers'] == []
    r = client.post(f'{URL}/runs', json=dict(checked=False))
    assert r.status_code == 422 and 'I have checked these' in r.json()['detail']

    r = client.post(f'{URL}/runs', json=dict(checked=True, note='First run'))
    assert r.status_code == 201
    run = r.json()['run']
    assert (run['by'], run['note'], run['current']) == ('new@example.com', 'First run', True)
    assert round(r.json()['results']['summary']['beam_steel_kg']) == 7744
    saved = session.scalar(select(Run).where(Run.note == 'First run'))
    assert saved.engine_version.startswith('engine-') and saved.inputs_json['beams']
    assert {a['key']: a['confirmed'] for a in saved.inputs_json['assumptions']}['concrete_grade'] is True
    assert client.get(f'{URL}/runs/latest').json()['run']['id'] == run['id']
    assert [x['note'] for x in client.get(f'{URL}/runs').json()][:2] == ['First run', 'Imported from app/data/worklounge']

    # A value changed afterwards: the run is out of date, and the change needs a tick before the next run.
    client.put(f'{URL}/assumptions', json=dict(values=[dict(key='ld_factor', value=53)]))
    p = client.get(URL).json()
    assert p['latest_run']['current'] is False and 'Anchorage / lap length factor' in p['blockers'][0]
    assert round(client.get(f'{URL}/runs/latest').json()['results']['summary']['beam_steel_kg']) == 7744  # frozen


def test_what_if_is_never_saved(client, session, pilot):
    sign_in(client, 'new@example.com')
    n = session.query(Run).count()
    client.post(f'{URL}/calculate', json=dict(overrides=dict(ld_factor=60)))
    assert session.query(Run).count() == n
