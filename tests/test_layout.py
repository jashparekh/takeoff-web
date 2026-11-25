"""Reading beams and columns off a layout (plan M2). Golden numbers are regression guards for the pilot sheet."""
import shutil

import pytest
from sqlalchemy import select

from app.db.models import Member, Sheet
from app.pipeline import geometry as g
from app.pipeline import stages
from app.pipeline.layout import Label, match
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


def test_t_junction_does_not_split_but_crossing_does():
    main = strip(False, 100, 0, 300)
    t_stem = strip(True, 100, 102.7, 200)            # frames in from below: rests on `main`
    cross = strip(True, 200, 50, 150)                # passes right across
    lab = Label('B1', False, 150, 95, (140, 92, 160, 98), 9, 18)
    out = match([lab], [main, t_stem, cross], [], PPI)
    b = out.beams[0]
    assert (b.p1, b.p2) == ((0, 100), (200, 100))


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
    assert r['beams_within_4ft'] >= 75                 # 79 of 88 when written; the rest need the schedule (M3)
    assert r['columns_matching_pilot'] == 25
    assert r['median_mid_error_in'] <= 3


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
