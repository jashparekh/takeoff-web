import json
from pathlib import Path
from app.engine import Calc

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
