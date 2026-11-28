"""Comparing two runs (plan M5): totals, steel by bar size with cost, beams added / removed / changed with why,
slab parts, values changed."""
import copy

import pytest

from app.db.models import ProjectMember, Run, User
from app.engine import Calc
from app.services.inputs_builder import engine_version
from conftest import sign_in
from tools.import_pilot import import_pilot

URL = '/api/projects/worklounge'


@pytest.fixture
def pilot(session):
    p = import_pilot(session, 'worklounge')
    u = User(email='engineer@firm.in')
    session.add(u)
    session.flush()
    session.add(ProjectMember(project_id=p.id, user_id=u.id, role='viewer'))
    session.flush()
    return p


def test_what_changed_between_two_runs(client, session, pilot):
    first = session.query(Run).filter_by(project_id=pilot.id).one()
    inputs = copy.deepcopy(first.inputs_json)
    beams = {b['name']: b for b in inputs['beams']}
    beams['B5']['D_in'] = 24                                             # the revision makes B5 deeper
    beams['B12']['clear_in'] += 6
    removed = inputs['beams'].pop(next(i for i, b in enumerate(inputs['beams']) if b['name'] == 'B6'))
    inputs['beams'].append(dict(copy.deepcopy(removed), name='B6A'))     # renamed: one removed, one added
    for a in inputs['assumptions']:
        if a['key'] == 'ld_factor':
            a['value'] = 53
    second = Run(project_id=pilot.id, engine_version=engine_version(), inputs_json=inputs,
                 results_json=Calc(inputs).run(), note='R1')
    session.add(second)
    session.flush()

    sign_in(client, 'engineer@firm.in')                                  # viewers can compare
    assert [r['note'] for r in client.get(f'{URL}/runs').json()][:2] == ['R1', 'Imported from app/data/worklounge']
    d = client.get(f'{URL}/runs/{first.id}/diff/{second.id}').json()
    m = {x['name']: x for x in d['members']}
    assert m['B6']['change'] == 'removed' and m['B6A']['change'] == 'added'
    assert m['B5']['change'] == 'changed' and m['B5']['reasons'][0] == 'size 9″×18″ → 9″×24″'
    assert any(r.startswith('span ') for r in m['B12']['reasons'])
    # Ld 50 → 53 lengthens every top bar: beams with the same drawing values changed because of a value.
    assert any(x['reasons'] == ['same drawing values; values changed: Anchorage / lap length factor 50 → 53']
               for x in d['members'])
    first_rows = [x['name'] for x in d['members'][:4]]                    # drawing changes listed first
    assert set(first_rows) == {'B5', 'B6', 'B6A', 'B12'}
    assert [v['key'] for v in d['values']] == ['ld_factor'] and (d['values'][0]['a'], d['values'][0]['b']) == (50, 53)
    steel = next(s for s in d['summary'] if s['key'] == 'steel_with_wastage_kg')
    assert steel['delta'] > 0 and steel['b'] == pytest.approx(second.results_json['summary']['steel_with_wastage_kg'])
    # Beam changes + the chajja's (its bars use Ld too) = the change in beam steel.
    assert d['beams_kg_delta'] + d['chajja']['delta'] == pytest.approx(
        second.results_json['summary']['beam_steel_kg'] - first.results_json['summary']['beam_steel_kg'], abs=0.1)
    assert d['by_dia'] and all(abs(r['kg']['delta']) > 0 or abs(r['cost']['delta']) > 0 for r in d['by_dia'])
    assert d['counts'] == dict(added=1, removed=1, changed=d['counts']['changed']) and not d['engine_changed']
    assert d['slab']['kg']['delta'] == pytest.approx(second.results_json['slab']['kg'] - first.results_json['slab']['kg'])

    assert client.get(f'{URL}/runs/{first.id}/diff/not-a-run').status_code == 404
