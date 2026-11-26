"""Reading beams and columns off a layout (plan M2). Golden numbers are regression guards for the pilot sheet."""
import shutil

import pytest
from sqlalchemy import select

from app.db.models import BeamSpec, Member, Sheet
from app.pipeline import geometry as g
from app.pipeline import stages
from app.pipeline.layout import Beam, Label, match
from conftest import sign_in
from test_scale_ocr import floors, run_all  # noqa: F401 (fixture)
from test_uploads import LAYOUT, post, proj, queued, storage  # noqa: F401 (fixtures)

needs_tesseract = pytest.mark.skipif(not shutil.which('tesseract'), reason='tesseract not installed')
PPI = 0.6


def strip(vertical, c, a, b, w=5.4):
    return g.Strip(vertical, c, a, b, w)


def test_strips_from_edge_pairs():
    # Two horizontal edges 5.4 pt apart (9" at 0.6 pt/in), one broken by a 6 pt gap; a third line far away.
    H = [(100.0, 0, 50), (100.0, 56, 120), (105.4, 0, 120), (140.0, 0, 120)]
    ss = g.strips(H, False, 4.5 * PPI, 15 * PPI, gap=16 * PPI)
    assert [(round(s.c, 1), s.a, s.b, round(s.width_pt, 1)) for s in ss] == [(102.7, 0, 120, 5.4)]


def test_a_beam_runs_through_beams_that_frame_in_or_cross():
    main = strip(False, 100, 0, 300)
    t_stem = strip(True, 100, 102.7, 200)            # frames in from below: rests on `main`
    cross = strip(True, 200, 50, 150)                # passes right across: still not a support of `main`
    col = g.Rect(296, 96, 304, 104, 2.0, True)       # the far end sits on a column
    lab = Label('B1', False, 150, 95, (140, 92, 160, 98), 9, 18)
    out = match([lab], [main, t_stem, cross], [col], PPI)
    assert [(b.p1, b.p2) for b in out.beams] == [((0, 100), (300, 100))]


def test_labels_sharing_a_line_split_where_each_sits_mid_span():
    # Crossings at 90 and 150; labels at 45 and 220. Centring both: cut at 90 (pieces 0–90, 90–300 put the labels
    # 0 and 25 off centre) beats 150 (75 and 5 off).
    main = strip(False, 100, 0, 300)
    crosses = [strip(True, 90, 50, 150), strip(True, 150, 50, 150)]
    col = g.Rect(296, 96, 304, 104, 2.0, True)
    a = Label('B1', False, 45, 95, (35, 92, 55, 98))
    b = Label('B2', False, 220, 95, (210, 92, 230, 98))
    out = match([a, b], [main] + crosses, [col, g.Rect(-4, 96, 4, 104, 2.0, True)], PPI)
    assert {m.name: (m.p1[0], m.p2[0]) for m in out.beams} == {'B1': (0, 90), 'B2': (90, 300)}


def test_the_line_past_the_last_crossing_beam_is_an_overhang():
    # A column at 0, a crossing beam at 240, the line runs on to 300 with no column and no label: the span stops at
    # 240 and the 100" beyond is left for the cantilever reader.
    main = strip(False, 100, 0, 300)
    cross = strip(True, 240, 50, 150)
    col = g.Rect(-4, 96, 4, 104, 2.0, True)
    lab = Label('B1', False, 120, 95, (110, 92, 130, 98))
    b = match([lab], [main, cross], [col], PPI).beams[0]
    assert (b.p1[0], b.p2[0]) == (0, 240)
    # Ending on a column instead, the whole line is the span.
    b = match([lab], [main, cross], [col, g.Rect(296, 96, 304, 104, 2.0, True)], PPI).beams[0]
    assert (b.p1[0], b.p2[0]) == (0, 300)


def test_two_labels_in_one_span_split_at_the_beam_between():
    main = strip(False, 100, 0, 300)
    t_stem = strip(True, 140, 102.7, 200)
    a = Label('B1', False, 60, 95, (50, 92, 70, 98))
    b = Label('B2', False, 220, 95, (210, 92, 230, 98))
    out = match([a, b], [main, t_stem], [], PPI)
    got = {m.name: (m.p1[0], m.p2[0]) for m in out.beams}
    assert got == {'B1': (0, 140), 'B2': (140, 300)}


def test_label_size_mismatch_lowers_confidence():
    s = strip(False, 100, 0, 300, w=7.2)             # drawn 12" wide
    lab = Label('B1', False, 150, 95, (140, 92, 160, 98), 9, 18)
    b = match([lab], [s], [], PPI).beams[0]
    assert b.confidence <= 0.4 and 'label says 9"' in b.note


def test_rotation_maps_to_the_displayed_page():
    assert g.to_display(10, 20, 100, 200, 0) == (10, 180)
    assert g.to_display(10, 20, 100, 200, 90) == (20, 10)


@needs_tesseract
def test_golden_worklounge_layout():
    from tools.golden_report import report
    r = report(PPI)
    assert r['edge_width'] == 0.84                     # found, not hard-coded
    assert r['labels_found'] >= 85                     # 89 of 91 when written
    assert r['beams_within_4ft'] >= 80                 # 83 of 88 when written (79 before spans ran through crossings)
    assert r['columns_matching_pilot'] == 25
    assert r['median_mid_error_in'] <= 3
    assert r['cantilever_beams_matching_schedule'] >= 18          # 19 of 21 when written (B21, B88 not drawn as such)
    assert r['cantilevers_not_in_schedule'] == []                 # the schedule settles them
    assert r['joined_with_schedule'] == ['B5']
    assert r['floor_plate_has_curved_corner']                     # the pilot's own outline missed it
    assert 5200 <= r['floor_plate_sqft'] <= 5500                  # pilot 5,131 without the curved corner
    assert r['span_dims_read'] >= 75                              # 85 when written
    assert r['spans_from_dimension'] >= 30                        # 34 when written (most longer spans cross a beam)
    assert r['spans_matching_workbook_or_drawn'] >= 72            # 74 of 87 when written (73, 71 of 93, 65 of 99 before)
    assert len(r['spans_short']) <= 8                             # 7, all face-to-face spans the workbook took as c/c − 9"
    assert r['spans_dimension_agrees_with_geometry'] >= 0.9 * r['spans_from_dimension']


@needs_tesseract
def test_layout_stage_writes_members_and_keeps_peoples_decisions(client, session, proj, floors, storage, queued):  # noqa: F811
    sign_in(client, 'new@example.com')
    post(client, ('layout.pdf', LAYOUT))
    sheet = list(run_all(session, storage, queued).values())[0]
    stages.layout(session, storage, sheet.id)
    s = session.get(Sheet, sheet.id)
    assert s.layout_version == stages.LAYOUT_VERSION and 'beams placed' in s.layout_note
    ms = client.get(f'/api/projects/site-b/sheets/{s.id}/members').json()
    beams = [m for m in ms if m['kind'] == 'beam' and m['placed']]
    assert len({m['name'] for m in beams}) >= 80 and sum(m['kind'] == 'column' for m in ms) >= 25
    b13 = next(m for m in beams if m['name'] == 'B13')
    assert b13['label_b_in'] == 9 and b13['label_d_in'] == 18 and b13['length_in'] > 0

    # A person's decision survives a re-read.
    m = session.get(Member, __import__('uuid').UUID(b13['id']))
    m.status, m.x2 = 'edited', m.x2 + 10
    session.commit()
    s.layout_version = None
    stages.LAYOUT_VERSION += 1
    try:
        stages.layout(session, storage, s.id)
    finally:
        stages.LAYOUT_VERSION -= 1
    b13s = session.scalars(select(Member).where(Member.sheet_id == s.id, Member.name == 'B13')).all()
    assert len(b13s) == 1 and b13s[0].status == 'edited'

    # Once a schedule is read it settles cantilevers: B5 has none in it, so its overhang joins its span. The layout
    # is read again because what the schedule says changed (the job key holds it); B7's cantilever stays.
    def names():
        return {m.name for m in session.scalars(select(Member).where(Member.sheet_id == s.id, Member.kind == 'beam'))}
    assert {'B5-cant', 'B7-cant'} <= names()
    b5_before = max(abs(m.x2 - m.x1) for m in session.scalars(select(Member).where(Member.sheet_id == s.id,
                                                                                    Member.name == 'B5')))
    for n in ('B5', 'B7', 'B7-cant'):
        session.add(BeamSpec(project_id=s.project_id, sheet_id=s.id, floor_id=s.floor_id, name=n))
    session.commit()
    assert stages.schedule_cants(session, s)['B5'] is False
    stages.layout(session, storage, s.id)
    assert 'B5-cant' not in names() and 'B7-cant' in names()
    b5 = session.scalars(select(Member).where(Member.sheet_id == s.id, Member.name == 'B5')).all()
    assert max(abs(m.x2 - m.x1) for m in b5) > b5_before and 'overhang joined' in session.get(Sheet, s.id).layout_note


def test_one_name_per_spot_and_doubtful_reads():
    from app.pipeline.layout import settle_names
    b10 = Label('B10', False, 648, 174, (640, 171, 656, 177), reads=2)
    b1 = Label('B1', False, 646, 174, (640, 171, 652, 177), reads=1)          # same spot, a digit dropped
    b85, b5 = Label('B85', True, 717, 309, (714, 300, 720, 318)), Label('B5', True, 717, 309, (714, 302, 720, 316))
    clear_b5 = Label('B5', False, 179, 110, (170, 107, 188, 113), reads=3)
    out = settle_names([b10, b1, b85, b5, clear_b5])
    names = sorted(lb.name for lb in out)
    assert names == ['B10', 'B5', 'B85']                    # B1 dropped; B85 (longer, B5 read clearly elsewhere) kept
    assert next(lb for lb in out if lb.name == 'B85').doubt == 'Also read as B5.'
    assert next(lb for lb in out if lb.name == 'B5').doubt is None


def test_a_doubtful_read_does_not_split_a_clear_beam():
    main = strip(False, 100, 0, 300)
    cross = strip(True, 100, 50, 150)
    cols = [g.Rect(-4, 96, 4, 104, 2.0, True), g.Rect(296, 96, 304, 104, 2.0, True)]
    good = Label('B24', False, 200, 95, (190, 92, 210, 98), reads=3)
    bad = Label('CB1', False, 60, 95, (55, 92, 65, 98), doubt='Read once; CB1 is read clearly elsewhere.')
    out = match([good, bad], [main, cross], cols, PPI)
    assert [(m.name, m.p1[0], m.p2[0]) for m in out.beams] == [('B24', 0, 300)]
    assert [u.name for u in out.unplaced] == ['CB1']


def test_two_labels_split_at_a_crossing_beam_before_one_framing_in():
    # T at 160 would centre the labels better (pieces 0–160, 160–300), but the beam crossing at 190 is the support.
    main = strip(False, 100, 0, 300)
    t_stem = strip(True, 160, 102.7, 140)
    cross = strip(True, 190, 50, 150)
    cols = [g.Rect(-4, 96, 4, 104, 2.0, True), g.Rect(296, 96, 304, 104, 2.0, True)]
    a = Label('B73', False, 80, 95, (70, 92, 90, 98))
    b = Label('B72', False, 230, 95, (220, 92, 240, 98))
    out = match([a, b], [main, t_stem, cross], cols, PPI)
    assert {m.name: (m.p1[0], m.p2[0]) for m in out.beams} == {'B73': (0, 190), 'B72': (190, 300)}


def test_a_label_just_past_the_line_end_takes_the_whole_line():
    main = strip(False, 100, 0, 50)
    lab = Label('HB70', False, 50.3, 95, (45, 92, 56, 98))
    b = match([lab], [main], [], PPI).beams[0]
    assert (b.p1[0], b.p2[0]) == (0, 50)


def cant_beam(name, p1, p2):
    return Beam(name, False, p1, p2, 9, Label(name, False, 0, 0, (0, 0, 0, 0)), 0.5, cantilever=True)


def test_the_schedule_joins_an_overhang_it_does_not_have():
    from app.pipeline.layout import Beam, Layout, join_overhangs
    lab = Label('B5', False, 60, 95, (50, 92, 70, 98))
    cols = [g.Rect(-4, 96, 4, 104, 2.0, True)]
    crossing_tip = strip(True, 160, 50, 150)              # a beam passes right across the tip: a support
    lay = Layout(beams=[Beam('B5', False, (0, 100), (120, 100), 9, lab, 0.8), cant_beam('B5-cant', (120, 100), (160, 100))])
    assert join_overhangs(lay, {'B5': False}, [crossing_tip], cols, PPI) == ['B5']
    assert [(b.name, b.p1[0], b.p2[0]) for b in lay.beams] == [('B5', 0, 160)]

    # Tip on a beam that ends there (the edge beam a cantilever carries): dropped, the span stays.
    edge = strip(True, 160, 102.7, 150)
    lay = Layout(beams=[Beam('B5', False, (0, 100), (120, 100), 9, lab, 0.8), cant_beam('B5-cant', (120, 100), (160, 100))])
    assert join_overhangs(lay, {'B5': False}, [edge], cols, PPI) == []
    assert [(b.name, b.p2[0]) for b in lay.beams] == [('B5', 120)]

    # The schedule has the cantilever: untouched.
    lay = Layout(beams=[Beam('B5', False, (0, 100), (120, 100), 9, lab, 0.8), cant_beam('B5-cant', (120, 100), (160, 100))])
    assert join_overhangs(lay, {'B5': True}, [crossing_tip], cols, PPI) == [] and len(lay.beams) == 2


def test_overhang_past_a_column_and_beside_another_lines_label():
    from app.pipeline.layout import add_cantilevers
    # B87: span 0–120 ends on a column (120–136); the line's edges stop there and go on 138–170 with nothing under it.
    span, out = strip(False, 100, 0, 120), strip(False, 100, 138, 170)
    col_root = g.Rect(120, 96, 136, 104, 2.0, True)
    col_far = g.Rect(-4, 96, 4, 104, 2.0, True)
    lab = Label('B87', False, 60, 95, (50, 92, 70, 98))
    other = Label('B56', False, 150, 107, (140, 104, 160, 110))   # 7 pt off this line, placed on its own beam
    neighbour = strip(False, 112, 140, 200)
    lay = match([lab, other], [span, out, neighbour], [col_root, col_far], PPI)
    add_cantilevers(lay, [span, out, neighbour], [col_root, col_far], [lab, other], PPI)
    assert [(b.name, b.p1[0], b.p2[0]) for b in lay.beams if b.cantilever] == [('B87-cant', 120, 170)]
