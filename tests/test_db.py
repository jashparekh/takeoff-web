import json
from pathlib import Path

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext

from app.db.models import Base, Building, Floor, Project, Run, SiteTakeoff
from app.services.inputs_builder import NotReady, build_inputs, calculate, engine_version
from tools.import_pilot import import_pilot

DATA = Path(__file__).parents[1] / 'app/data/worklounge'
INPUTS = json.loads((DATA / 'inputs.json').read_text())


def test_migrations_match_models(db_engine):
    with db_engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn, opts={'compare_type': True}), Base.metadata)
    assert diff == [], 'models changed without a migration: run `alembic revision --autogenerate`'


@pytest.fixture
def pilot(session):
    return import_pilot(session, 'worklounge')


def test_import_gives_pinned_totals(pilot):
    s = calculate(pilot)['summary']
    assert round(s['beam_steel_kg']) == 7744
    assert round(s['slab_steel_kg']) == 3894
    assert round(s['steel_with_wastage_kg']) == 11987
    assert round(s['concrete_m3'], 2) == 83.72


def test_built_inputs_equal_inputs_json(pilot):
    built = build_inputs(pilot)
    assert built['beams'] == INPUTS['beams']
    assert built['chajja'] == INPUTS['chajja']
    assert built['slab'] == INPUTS['slab']
    n = len(INPUTS['assumptions'])
    assert [(a['key'], a['value'], a['confirm']) for a in built['assumptions'][:n]] == \
           [(a['key'], a['value'], a['confirm']) for a in INPUTS['assumptions']]
    # Values the workbook does not hold are added from the registry, proposed and unconfirmed; the conventions keep
    # today's values, so the pilot's totals are unchanged.
    extra = {a['key']: (a['value'], a['confirm'], a['confirmed']) for a in built['assumptions'][n:]}
    assert extra == {'concrete_grade': (25, True, False), 'steel_grade': (550, True, False),
                     'extra_top_frac': (1 / 3, True, False), 'stirrup_hook_d': (10, True, False),
                     'cant_hook_d': (12, True, False)}


def test_import_records(session, pilot):
    floors = [f for b in pilot.buildings for f in b.floors]
    assert [f.name for f in floors if f.measured] == ['Second floor']
    assert len(floors) == 4 and floors[1].level_m == 6.0
    assert sorted(i.code for i in pilot.issues) == sorted(x['id'] for x in json.loads((DATA / 'issues.json').read_text()))
    assert session.query(SiteTakeoff).filter_by(project_id=pilot.id).count() == 1
    run = session.query(Run).filter_by(project_id=pilot.id).one()
    assert run.engine_version == engine_version()
    assert round(run.results_json['summary']['beam_steel_kg']) == 7744


def test_reimport_needs_replace(session, pilot):
    with pytest.raises(SystemExit):
        import_pilot(session, 'worklounge')
    import_pilot(session, 'worklounge', replace=True)
    session.flush()
    p = session.query(Project).filter_by(slug='worklounge').one()
    assert session.query(Run).filter_by(project_id=p.id).count() == 1


def test_project_without_drawings_is_not_ready(session):
    p = Project(slug='empty', name='Empty', meta_json={})
    p.buildings.append(Building(name='A', floors=[Floor(name='Ground floor')]))
    session.add(p)
    session.flush()
    with pytest.raises(NotReady):
        build_inputs(p)
