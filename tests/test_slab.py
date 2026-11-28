"""Bottom slab panel by panel (plan M5): callout parsing, the engine's per-panel path, the pilot golden numbers, and
the slab stage (panels as members, checks, used once checked in Review)."""
import json
import shutil
from pathlib import Path

import pytest
from sqlalchemy import select

from app.db.models import Floor, Issue, Member, Sheet
from app.engine import Calc, unit_wt
from app.pipeline import stages
from app.pipeline.slab import PanelBar, parse_callout
from app.services.inputs_builder import slab_panels
from conftest import sign_in
from test_scale_ocr import floors, run_all  # noqa: F401 (fixture)
from test_uploads import LAYOUT, SLAB_B, SLAB_T, post, proj, queued, storage  # noqa: F401 (fixtures)

needs_tesseract = pytest.mark.skipif(not shutil.which('tesseract'), reason='tesseract not installed')
INPUTS = json.loads((Path(__file__).parents[1] / 'app' / 'data' / 'worklounge' / 'inputs.json').read_text())


@pytest.mark.parametrize('text, want', [
    ('*K8@9"C/C', (8, 9)), ('K10@6"C/C', (10, 6)), ('9K808"C/C', (8, 8)), ('*K8010"C/C', (8, 10)),
    ('kse10°c/%', (8, 10)), ('Kgeo"c/@', None), ('*«8@10"C/C', (8, 10)), ('K8@15"C/C', None),
    ('B13(9"X18")', None),
])
def test_callouts_as_ocr_reads_them(text, want):
    assert parse_callout(text) == want


def test_bars_across_a_panel():
    b = PanelBar(False, 8, 9, length_in=120, width_in=100, source='callout')
    assert b.nos == 12 and b.json()['nos'] == 12                      # ⌊100 / 9⌋ + 1


def panel(name, w, h, bars, thk=None):
    return dict(name=name, area_sqft=w * h / 144, thk_in=thk,
                bars=[dict(vertical=v, dia=d, sp=s, length_in=h if v else w, width_in=w if v else h,
                           nos=int((w if v else h) // s) + 1, source=src) for v, d, s, src in bars])


def test_engine_slab_panel_by_panel():
    ps = [panel('P1', 120, 100, [(False, 8, 9, 'callout'), (True, 8, 9, 'distribution note')]),
          panel('P2', 96, 96, [(False, 10, 6, 'callout'), (True, 10, 6, 'callout')], thk=6)]
    base = Calc(INPUTS).run()
    r = Calc({**INPUTS, 'slab': {**INPUTS['slab'], 'panels': ps}}).run()
    s = r['slab']
    allow = 1.08
    want = sum(x['nos'] * x['length_in'] * 0.0254 * allow * unit_wt(x['dia']) for p in ps for x in p['bars'])
    assert s['method'] == 'panels' and s['bottom_kg'] == pytest.approx(want)
    assert [p['name'] for p in s['panels']] == ['P1', 'P2'] and s['bottom_by_dia'][10] > 0
    assert s['top_kg'] == pytest.approx(base['slab']['top_kg'])           # top bars: average method still
    extra = ps[1]['area_sqft'] * 0.092903 * (6 - 5) * 0.0254
    assert s['concrete_m3'] == pytest.approx(base['slab']['concrete_m3'] + extra)
    ten = next(row for row in r['summary']['by_dia'] if row['dia'] == 10)
    assert ten['slab'] == pytest.approx(s['bottom_by_dia'][10])           # K10 slab bars counted as 10 mm
    step = next(x for x in s['steps'] if x['label'] == 'Bottom bars, panel by panel')
    assert '2 panels' in step['working'] and '1 from the distribution-steel note' in step['working']


def test_engine_top_bars_as_drawn():
    groups = [dict(name='T1', vertical=True, dia=8, sp=8, nos=23, line_in=176, shapes=[dict(length_in=33, nos=23)]),
              dict(name='T2', vertical=False, dia=8, sp=7, nos=26, line_in=176,
                   shapes=[dict(length_in=33, nos=13), dict(length_in=57, nos=13)])]    # alternate shapes
    base = Calc(INPUTS).run()['slab']
    s = Calc({**INPUTS, 'slab': {**INPUTS['slab'], 'top': groups, 'top_distribution': [8, 9]}}).run()['slab']
    want = sum(sh['nos'] * (sh['length_in'] * 0.0254 + 2 * 12 * 8 / 1000) * unit_wt(8) for g in groups for sh in g['shapes'])
    assert s['top_method'] == 'drawn' and s['top_kg'] == pytest.approx(want)
    dist = (3 * 176 + 6 * 176) * 0.0254 * unit_wt(8)                     # ⌈24/9⌉ = 3 and ⌈48/9⌉ = 6 bars
    assert s['dist_kg'] == pytest.approx(dist)
    assert s['bottom_kg'] == pytest.approx(base['bottom_kg']) and s['method'] == 'average'   # bottom unchanged
    assert any(x['label'] == 'Top bars over beams, as drawn' and '3 drawn bar shapes' in x['working'] for x in s['steps'])


@needs_tesseract
def test_golden_worklounge_top_slab():
    from tools.golden_report import slab_report
    r = slab_report(0.6)
    assert r['top_callouts'] >= 110 and r['top_marks'] == 123              # 116 callouts when written
    assert r['top_groups'] >= 98 and 115 <= r['top_shapes'] <= 123         # 104 groups, 119 of 123 marks (each once)
    assert r['top_distribution'] == (8, 9)
    # As drawn the top bars are shorter and fewer than the workbook's estimate (2'-6" both sides of every span beam):
    assert 1000 <= r['top_kg_drawn'] <= 1200 and 580 <= r['dist_kg_drawn'] <= 750        # 1,087 and 656 when written
    assert -16 <= r['slab_diff_pct'] <= -11                                               # 3,362 vs 3,894 kg


@needs_tesseract
def test_golden_worklounge_bottom_slab():
    from tools.golden_report import slab_report
    r = slab_report(0.6)
    assert r['callouts'] >= 80 and r['bar_lines'] >= 80                  # 86 / 86 when written
    assert r['panels'] >= 50 and r['distribution'] == (8, 9)
    assert [t for _, t in r['thick_panels']] == [6.0]                     # the pilot's I6
    assert abs(r['bottom_diff_pct']) <= 5                                 # -3.5%: 3,981 of 4,569 sq ft read as panels


@needs_tesseract
def test_slab_stage_panels_checks_and_review(client, session, proj, floors, storage, queued):  # noqa: F811
    sign_in(client, 'new@example.com')
    post(client, ('layout.pdf', LAYOUT))
    post(client, ('bottom.pdf', SLAB_B))
    got = run_all(session, storage, queued)
    bottom = got['MC-SD-06-04']
    stages.slab(session, storage, bottom.id)
    s = session.get(Sheet, bottom.id)
    assert s.slab_version == stages.SLAB_VERSION and 'distribution steel K8@9"' in s.layout_note
    ps = session.scalars(select(Member).where(Member.sheet_id == s.id, Member.kind == 'panel')).all()
    assert len(ps) >= 50 and all(p.data['bars'] for p in ps) and all(p.status == 'auto' for p in ps)
    issues = {i.code: i for i in session.scalars(select(Issue).where(Issue.project_id == proj.id))}
    thick = [i for c, i in issues.items() if c.startswith('X-SLAB-THK-')]
    assert len(thick) == 1 and '6" thick on MC-SD-06-04' in thick[0].title and thick[0].where_json['sheet'] == str(s.id)
    assert 'X-SLAB-AVG' in issues
    assert slab_panels(session, session.get(Floor, s.floor_id)) == {}                          # not checked yet: average method

    # Panels appear in Review; once all are checked the engine gets them, and the waiting issue goes.
    q = client.get('/api/projects/site-b/review').json()
    assert sum(i['kind'] == 'panel' for i in q['items']) == len(ps)
    for p in ps:
        assert client.patch(f'/api/projects/site-b/members/{p.id}', json=dict(action='accept')).status_code == 200
    session.expire_all()
    got_panels = slab_panels(session, session.get(Floor, s.floor_id))["panels"]
    assert len(got_panels) == len(ps) and got_panels[0]['bars'][0]['nos'] >= 1
    assert not session.scalar(select(Issue).where(Issue.project_id == proj.id, Issue.code == 'X-SLAB-AVG'))


@needs_tesseract
def test_top_slab_stage_groups_in_review(client, session, proj, floors, storage, queued):  # noqa: F811
    sign_in(client, 'new@example.com')
    post(client, ('layout.pdf', LAYOUT))
    post(client, ('top.pdf', SLAB_T))
    got = run_all(session, storage, queued)
    top = got['MC-SD-06-05']
    stages.slab(session, storage, top.id)
    s = session.get(Sheet, top.id)
    assert s.slab_version == stages.SLAB_VERSION and 'top bar groups' in s.layout_note and '123 bar marks' in s.layout_note
    ts = session.scalars(select(Member).where(Member.sheet_id == s.id, Member.kind == 'panel')).all()
    assert len(ts) >= 90 and all(t.name.startswith('T') and t.data['top']['shapes'] for t in ts)
    floor = session.get(Floor, s.floor_id)
    assert 'top' not in slab_panels(session, floor)
    for t in ts:
        assert client.patch(f'/api/projects/site-b/members/{t.id}', json=dict(action='accept')).status_code == 200
    session.expire_all()
    got_top = slab_panels(session, floor)
    assert len(got_top['top']) == len(ts) and got_top['top_distribution'] == [8, 9] and 'panels' not in got_top
