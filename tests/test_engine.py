import json
from pathlib import Path
from fastapi.testclient import TestClient
from app.engine import Calc
from app.main import app

INPUTS = json.loads((Path(__file__).parents[1] / 'app/data/worklounge/inputs.json').read_text())


def test_totals_match_pilot_workbook():
    s = Calc(INPUTS).run()['summary']
    assert round(s['beam_steel_kg']) == 7744
    assert round(s['slab_steel_kg']) == 3894
    assert round(s['steel_with_wastage_kg']) == 11987
    assert round(s['concrete_m3'], 2) == 83.72


def test_b47_matches_hand_check():
    r = Calc(INPUTS).run()
    b = next(x for x in r['beams'] if x['name'] == 'B47')
    assert round(b['kg'], 1) == 31.5
    assert b['stirrups']['nos'] == 18
    assert [round(x['cut_mm']) for x in b['bars']] == [4477, 3657, 2621]


def test_override_changes_quantities():
    base = Calc(INPUTS).run()['summary']['beam_steel_kg']
    more = Calc(INPUTS, {'ld_factor': 53}).run()['summary']['beam_steel_kg']
    assert more > base


def test_api():
    c = TestClient(app)
    assert c.get('/api/health').json() == {'ok': True}
    r = c.post('/api/projects/worklounge/calculate', json={}).json()
    assert round(r['notebook']['totals']['beams']['diff']) == 1638
    assert c.get('/').status_code == 200
    assert c.get('/drawings/worklounge/layout.svg').status_code == 200
    assert c.get('/drawings/worklounge/../inputs.json').status_code == 404


def test_model_links_to_beams():
    c = TestClient(app)
    p = c.get('/api/projects/worklounge').json()
    names = {b['name'] for b in p['beams_input']}
    m = p['model']
    assert len(m['beams']) >= 100 and all(b['name'] in names for b in m['beams'])
    assert len(m['slab']['outline']) > 10 and len(m['columns']) >= 20
