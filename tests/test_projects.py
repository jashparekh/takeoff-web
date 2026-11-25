"""Projects API: the pilot served from the database, access checks, and creating a project."""
import pytest
from sqlalchemy import select

from app.db.models import Project, ProjectMember, User
from conftest import sign_in
from tools.import_pilot import import_pilot


@pytest.fixture
def pilot(session):
    p = import_pilot(session, 'worklounge')
    u = User(email='new@example.com')
    session.add(u)
    session.flush()
    session.add(ProjectMember(project_id=p.id, user_id=u.id, role='viewer'))
    session.flush()
    return p


def test_pilot_routes_need_sign_in(client, pilot):
    assert client.get('/api/projects').status_code == 401
    assert client.get('/api/projects/worklounge').status_code == 401
    assert client.post('/api/projects/worklounge/calculate', json={}).status_code == 401
    assert client.get('/drawings/worklounge/layout.svg').status_code == 401


def test_pilot_from_database_matches_pinned_results(client, pilot):
    sign_in(client, 'new@example.com')
    r = client.post('/api/projects/worklounge/calculate', json={}).json()
    assert round(r['summary']['beam_steel_kg']) == 7744
    assert round(r['notebook']['totals']['beams']['diff']) == 1638
    assert round(r['notebook']['totals']['page1']['ours']) > 0
    more = client.post('/api/projects/worklounge/calculate', json=dict(overrides=dict(ld_factor=53))).json()
    assert more['summary']['beam_steel_kg'] > r['summary']['beam_steel_kg']


def test_pilot_project_view(client, pilot):
    sign_in(client, 'new@example.com')
    p = client.get('/api/projects/worklounge').json()
    assert p['role'] == 'viewer' and p['ready'] and p['meta']['id'] == 'worklounge' and p['meta']['city'] == 'Hubballi'
    assert {s['id'] for s in p['meta']['sheets']} >= {'layout', 'slabbot', 'slabtop'}
    assert [i['id'] for i in p['issues']][:1] and all(i['title'] for i in p['issues'])
    names = {b['name'] for b in p['beams_input']}
    m = p['model']
    assert len(m['beams']) >= 100 and all(b['name'] in names for b in m['beams'])
    assert len(m['slab']['outline']) > 10 and len(m['columns']) >= 20
    assert client.get('/drawings/worklounge/layout.svg').status_code == 200
    assert client.get('/drawings/worklounge/..%2Finputs.json').status_code == 404
    lst = client.get('/api/projects').json()
    assert lst[0]['id'] == 'worklounge' and round(lst[0]['steel_t'], 2) == 11.99 and lst[0]['measured'] == 1


def test_outsider_cannot_see_pilot(client, pilot):
    sign_in(client, 'engineer@firm.in')
    assert client.get('/api/projects').json() == []
    assert client.get('/api/projects/worklounge').status_code == 404
    assert client.get('/drawings/worklounge/layout.svg').status_code == 404


NEW = dict(name='  Shanti Nagar Villas ', client='Rao Builders', city='Dharwad',
           buildings=[dict(name='Villa type A', copies=6,
                           floors=[dict(name='Ground', level_m=0), dict(name='First', level_m=3.15)])])


def test_create_project(client, session):
    sign_in(client, 'new@example.com')
    r = client.post('/api/projects', json=NEW)
    assert r.status_code == 201 and r.json() == dict(id='shanti-nagar-villas')
    assert client.post('/api/projects', json=NEW).json() == dict(id='shanti-nagar-villas-2')
    p = client.get('/api/projects/shanti-nagar-villas').json()
    assert p['role'] == 'owner' and not p['ready'] and p['meta']['name'] == 'Shanti Nagar Villas'
    strip = lambda d: {k: v for k, v in d.items() if k != 'id'}                     # noqa: E731
    assert [dict(strip(b), floors=[strip(f) for f in b['floors']]) for b in p['buildings']] == [
        dict(name='Villa type A', copies=6, floors=[
            dict(name='Ground', level_m=0, typical_count=1, measured=False),
            dict(name='First', level_m=3.15, typical_count=1, measured=False)])]
    assert p['model'] is None and p['beams_input'] == []
    c = client.post('/api/projects/shanti-nagar-villas/calculate', json={})
    assert c.status_code == 409 and 'No measured floor' in c.json()['detail']
    assert {x['id'] for x in client.get('/api/projects').json()} == {'shanti-nagar-villas', 'shanti-nagar-villas-2'}
    proj = session.scalar(select(Project).where(Project.slug == 'shanti-nagar-villas'))
    assert proj.owner_id is not None


@pytest.mark.parametrize('bad', [
    dict(NEW, name='   '),
    dict(NEW, buildings=[]),
    dict(NEW, buildings=[dict(name='A', floors=[])]),
    dict(NEW, buildings=[dict(name='A', copies=0, floors=[dict(name='G')])]),
])
def test_create_project_validation(client, bad):
    sign_in(client, 'new@example.com')
    assert client.post('/api/projects', json=bad).status_code == 422


def test_create_project_needs_sign_in(client):
    assert client.post('/api/projects', json=NEW).status_code == 401
