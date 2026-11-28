"""Values we need (plan M4): registry seeding, confirming, editing, validation, access; conventions as values."""
import json
from pathlib import Path

import pytest
from sqlalchemy import select

from app.db.models import Correction
from app.engine import CONVENTIONS, Calc
from app.services.values import REGISTRY
from conftest import sign_in
from test_uploads import proj  # noqa: F401 (fixture)

URL = '/api/projects/site-b'
INPUTS = json.loads((Path(__file__).parents[1] / 'app' / 'data' / 'worklounge' / 'inputs.json').read_text())


def test_every_value_is_proposed_and_the_ones_drawings_dont_give_need_a_check(client, proj):  # noqa: F811
    sign_in(client, 'engineer@firm.in')                                    # viewers can read them
    d = client.get(f'{URL}/values-needed').json()
    keys = {v['key'] for v in d['values']}
    assert keys == {v.key for v in REGISTRY}
    assert d['needed'] == sum(v.confirm for v in REGISTRY) and d['needed'] > 20
    grade = next(v for v in d['values'] if v['key'] == 'concrete_grade')
    assert (grade['value'], grade['confirmed'], grade['needs_confirm']) == (25, False, True)
    assert 'Proposed default' in grade['source']
    assert client.get(URL).json()['values_needed'] == d['needed']
    assert client.put(f'{URL}/assumptions', json=dict(values=[dict(key='cover_mm', confirm=True)])).status_code == 403


def test_confirm_edit_and_validate(client, session, proj):  # noqa: F811
    sign_in(client, 'new@example.com')
    n0 = client.get(f'{URL}/values-needed').json()['needed']
    r = client.put(f'{URL}/assumptions', json=dict(values=[dict(key='cover_mm', confirm=True),
                                                         dict(key='concrete_grade', value=30, confirm=True)]))
    assert r.status_code == 200
    d = r.json()
    by = {v['key']: v for v in d['values']}
    assert d['needed'] == n0 - 2
    assert by['cover_mm']['confirmed'] and by['cover_mm']['confirmed_by'] == 'new@example.com'
    assert by['concrete_grade']['value'] == 30 and by['concrete_grade']['source'] == 'Entered by new@example.com.'

    # Changing a confirmed value clears its confirmation unless the same request ticks it again.
    by = {v['key']: v for v in client.put(f'{URL}/assumptions', json=dict(values=[dict(key='cover_mm', value=30)])).json()['values']}
    assert by['cover_mm']['value'] == 30 and not by['cover_mm']['confirmed']

    # All or nothing: an unknown key or an out-of-range value rejects the whole request.
    r = client.put(f'{URL}/assumptions', json=dict(values=[dict(key='ld_factor', confirm=True), dict(key='nope', value=1)]))
    assert r.status_code == 422 and 'Unknown value' in r.json()['detail']
    r = client.put(f'{URL}/assumptions', json=dict(values=[dict(key='ld_factor', confirm=True), dict(key='cover_mm', value=500)]))
    assert r.status_code == 422 and 'outside' in r.json()['detail']
    ld = next(v for v in client.get(f'{URL}/values-needed').json()['values'] if v['key'] == 'ld_factor')
    assert not ld['confirmed']

    fields = {c.field for c in session.scalars(select(Correction))}
    assert {'value:cover_mm:confirmed', 'value:concrete_grade', 'value:cover_mm'} <= fields


def test_conventions_are_values_with_todays_defaults():
    base = Calc(INPUTS).run()['summary']['beam_steel_kg']
    assert CONVENTIONS == dict(extra_top_frac=1 / 3, stirrup_hook_d=10, cant_hook_d=12)
    same = Calc({**INPUTS, 'assumptions': INPUTS['assumptions'] + [dict(key=k, value=v) for k, v in CONVENTIONS.items()]}).run()
    assert same['summary']['beam_steel_kg'] == base
    what_if = Calc(INPUTS, overrides=dict(stirrup_hook_d=12)).run()         # a what-if works on conventions too
    longer = Calc({**INPUTS, 'assumptions': INPUTS['assumptions'] + [dict(key='stirrup_hook_d', value=12)]}).run()
    assert longer['summary']['beam_steel_kg'] == what_if['summary']['beam_steel_kg'] > base
    st = longer['beams'][0]['stirrups']['steps'][1]
    assert '2 × 12d' in st['formula']


@pytest.mark.parametrize('key', ['extra_top_frac', 'cant_hook_d'])
def test_each_convention_changes_the_result(key):
    base = Calc(INPUTS).run()['summary']['beam_steel_kg']
    changed = Calc({**INPUTS, 'assumptions': INPUTS['assumptions'] + [dict(key=key, value=CONVENTIONS[key] * 1.2)]}).run()
    assert changed['summary']['beam_steel_kg'] > base
