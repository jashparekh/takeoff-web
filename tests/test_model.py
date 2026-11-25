"""3D model from members, cantilevers and the slab outline (plan M2 PR 3)."""
import pypdfium2.raw as R
import pytest

from app.db.models import Building, Floor, Member, Sheet, Upload
from app.pipeline import geometry as g
from app.pipeline.layout import Label, Layout, add_cantilevers, match
from app.services.model_builder import build_model
from conftest import sign_in
from test_uploads import proj  # noqa: F401 (fixture)

PPI = 0.6


def strip(vertical, c, a, b, w=5.4):
    return g.Strip(vertical, c, a, b, w)


def col(cx, cy, w=5.4, d=14.4):
    return g.Rect(cx - w / 2, cy - d / 2, cx + w / 2, cy + d / 2, 2.04, False)


def test_cantilever_past_the_last_column():
    line = strip(False, 100, 0, 200)               # beam B1 between columns at 40 and 140, overhangs to 200
    cols = [col(40, 100, 14.4, 5.4), col(140, 100, 14.4, 5.4)]
    lab = Label('B1', False, 90, 95, (80, 92, 100, 98), 9, 18)
    lay = match([lab], [line], cols, PPI)
    add_cantilevers(lay, [line], cols, [lab], PPI)
    names = {b.name: (b.p1[0], b.p2[0]) for b in lay.beams}
    assert names['B1'] == (40, 140)
    # Past each end column the line runs on with nothing under it: both ends overhang.
    assert (names['B1-cantL'], names['B1-cantR']) == ((0, 40), (140, 200))


def test_no_cantilever_when_a_column_holds_the_line_further_out():
    line = strip(False, 100, 0, 200)
    cols = [col(40, 100, 14.4, 5.4), col(140, 100, 14.4, 5.4), col(195, 100, 14.4, 5.4), col(3, 100, 14.4, 5.4)]
    lab = Label('B1', False, 90, 95, (80, 92, 100, 98))
    lay = match([lab], [line], cols, PPI)
    add_cantilevers(lay, [line], cols, [lab], PPI)
    assert [b.name for b in lay.beams] == ['B1']


def test_both_ends_overhang_named_left_and_right():
    line = strip(True, 100, 0, 300)
    cols = [col(100, 80), col(100, 220)]
    lab = Label('B74', True, 95, 150, (92, 140, 98, 160))
    lay = match([lab], [line], cols, PPI)
    add_cantilevers(lay, [line], cols, [lab], PPI)
    assert sorted(b.name for b in lay.beams) == ['B74', 'B74-cantL', 'B74-cantR']


def test_slab_outline_of_a_dashed_rectangle():
    # 60 ft × 40 ft plate at 0.6 pt/in: beam edges drawn dashed (8 pt dash, 3 pt gap).
    W, H = 60 * 12 * PPI, 40 * 12 * PPI
    def dashed(x0, y0, x1, y1):
        n = int(max(abs(x1 - x0), abs(y1 - y0)) // 11) + 1
        out = []
        for i in range(n):
            t0, t1 = i / n, min(1, i / n + 8 / max(abs(x1 - x0), abs(y1 - y0)))
            out.append(g.Path(0.84, [(R.FPDF_SEGMENT_MOVETO, x0 + (x1 - x0) * t0, y0 + (y1 - y0) * t0),
                                     (R.FPDF_SEGMENT_LINETO, x0 + (x1 - x0) * t1, y0 + (y1 - y0) * t1)], False, False))
        return out
    paths = []
    for off in (0, 5.4):
        paths += dashed(100 - off, 100 - off, 100 + W + off, 100 - off) + dashed(100 - off, 100 + H + off, 100 + W + off, 100 + H + off)
        paths += dashed(100 - off, 100 - off, 100 - off, 100 + H + off) + dashed(100 + W + off, 100 - off, 100 + W + off, 100 + H + off)
    vec = g.Vectors(1191, 842, paths)
    strips = [strip(False, 97.3, 94.6, 105.4 + W), strip(True, 97.3, 94.6, 105.4 + H)]
    o = g.slab_outline(vec, 0.84, strips, PPI)
    outer = (60 + 1.5) * (40 + 1.5)                    # beams included: 9" edge beams on all four sides
    assert o is not None and abs(o.area_sqft - outer) / outer < 0.02


@pytest.fixture
def read_layout(session, proj):  # noqa: F811
    b = Building(project_id=proj.id, name='Main', sort=0)
    b.floors = [Floor(name=n, sort=i, level_m=lv) for i, (n, lv) in enumerate([('First floor', 3.15), ('Second floor', 6.3), ('Third floor', 9.45)])]
    session.add(b)
    session.flush()
    u = Upload(project_id=proj.id, filename='l.pdf', sha256='y' * 64, size_bytes=1, storage_key='k', status='done')
    session.add(u)
    session.flush()
    s = Sheet(project_id=proj.id, upload_id=u.id, page_no=1, page_hash='h2', width_pt=1191, height_pt=842, status='ready',
              sheet_type='beam_layout', drg_no='MC-SD-07', pt_per_in=0.6, floor_id=b.floors[1].id)
    session.add(s)
    session.flush()
    add = lambda **kw: session.add(Member(project_id=proj.id, sheet_id=s.id, floor_id=s.floor_id, confidence=0.8, **kw))  # noqa: E731
    add(kind='beam', name='B1', vertical=False, x1=100, y1=100, x2=172, y2=100, label_b_in=9, label_d_in=18)
    add(kind='beam', name='B1-cant', vertical=False, x1=172, y1=100, x2=190, y2=100, label_b_in=9)
    add(kind='beam', name='B9', vertical=True, x1=100, y1=100, x2=100, y2=160, status='rejected')
    add(kind='beam', name='B40', x1=50, y1=50, x2=50, y2=50)                  # label, no beam
    add(kind='column', x1=97.3, y1=92.8, x2=102.7, y2=107.2)
    add(kind='slab_panel', name='Slab', x1=90, y1=90, x2=200, y2=170, points=[[90, 90], [200, 90], [200, 170], [90, 170]], area_sqft=2400)
    session.flush()
    return s


def test_build_model_from_members(session, proj, read_layout):  # noqa: F811
    m = build_model(session, proj)
    assert m['from_members'] and m['origin_pt'] == [90, 90] and m['m_per_pt'] == pytest.approx(0.0254 / 0.6)
    names = {b['name']: b for b in m['beams']}
    assert set(names) == {'B1', 'B1-cant'}                                    # rejected and unplaced left out
    assert names['B1-cant']['cant'] and not names['B1-cant']['depth_known'] and names['B1']['D_in'] == 18
    assert names['B1']['p2'][0] - names['B1']['p1'][0] == pytest.approx(72 * 0.0254 / 0.6, abs=1e-3)
    assert m['unplaced'] == ['B40'] and len(m['columns']) == 1 and m['slab']['footprint_sqft'] == 2400
    assert (m['level'], m['floor_h']) == (1, pytest.approx(3.15)) and m['floors'][1]['measured']


def test_project_api_serves_the_model(client, session, proj, read_layout):  # noqa: F811
    sign_in(client, 'new@example.com')
    p = client.get('/api/projects/site-b').json()
    assert p['model']['from_members'] and p['model']['source'].startswith('MC-SD-07')
