"""Beam schedule reader (plan M3, OCR pass). Golden numbers are a baseline to beat, not a target."""
import shutil

import pytest
from sqlalchemy import select

from app.db.models import BeamSpec, Issue
from app.pipeline import stages
from app.pipeline.schedule import callouts, clean, dim_inches, stirrup
from conftest import sign_in
from test_scale_ocr import floors, run_all  # noqa: F401 (fixture)
from test_uploads import SCHED1, SCHED2, post, proj, queued, storage  # noqa: F401 (fixtures)

needs_tesseract = pytest.mark.skipif(not shutil.which('tesseract'), reason='tesseract not installed')


@pytest.mark.parametrize('text, want', [
    ('3K12', [(3, 12)]), ('2K20+1K25', [(2, 20), (1, 25)]), ('2K2041K25', [(2, 20), (1, 25)]),
    ('2KI2+H1K16', [(2, 12), (1, 16)]), ('3KI6', [(3, 16)]), ('K8@6"C/C', []), ('B13(9"X18")', [])])
def test_callouts(text, want):
    assert callouts(text) == want


@pytest.mark.parametrize('text, want', [
    ('K8@6"C/C', (8, 6)), ('K806"C/C', (8, 6)), ('K1005"C/c', (10, 5)), ('ka@6"C/c', (8, 6)), ('KB06"C/C', (8, 6)),
    ('@8"c/c', (None, 8)), ('06°C/C', (None, 6)), ('69"C/c', (None, 9)), ('o10°C/c', (None, 10)), ('3K12', None), ('C/C', None)])
def test_stirrup_notes(text, want):
    assert stirrup(text) == want


def test_dimensions_and_cleaning():
    assert dim_inches("3'-3\"") == 39 and dim_inches('EQ') is None and dim_inches("1'-14\"") is None
    assert clean('2KI6+1Kl2') == '2K16+1K12'


@needs_tesseract
def test_golden_worklounge_schedules():
    from tools.golden_report import schedule_report
    r = schedule_report()
    assert r['elevations_matched'] >= 100          # 105 of 115 when written (misses: cantilever parts)
    assert r['size_right'] >= 98                   # 103
    assert r['stirrups_right'] >= 60               # 69
    assert r['fully_right'] >= 12                  # 16: OCR alone; the vision pass (M3 PR 2) must raise this
    assert r['repeated_elevations'] == ['B68', 'B69', 'B71', 'B72', 'B73', 'HB70']


@needs_tesseract
def test_schedule_stage_counts_repeats_once(client, session, proj, floors, storage, queued):  # noqa: F811
    sign_in(client, 'new@example.com')
    post(client, ('s1.pdf', SCHED1))
    post(client, ('s2.pdf', SCHED2))
    sheets = run_all(session, storage, queued)
    for s in sheets.values():
        stages.schedule(session, storage, s.id)
    specs = session.scalars(select(BeamSpec)).all()
    b13 = next(b for b in specs if b.name == 'B13')
    assert (b13.b_in, b13.D_in, b13.kind, b13.source) == (9, 18, 'S', 'ocr') and b13.box and b13.floor_id
    assert any(b.role == 'Bottom through' for b in b13.bars)
    dups = sorted(b.name for b in specs if b.duplicate_of)
    assert dups == ['B68', 'B69', 'B71', 'B72', 'B73', 'HB70']
    issue = session.scalar(select(Issue).where(Issue.rule == 'check_repeat'))
    assert issue and 'B68' in issue.detail and issue.beams == ['B68', 'B69', 'B71', 'B72', 'B73', 'HB70']
    # Read again: nothing doubles up.
    s2 = sheets['MC-SD-06-02']
    s2.schedule_version = None
    stages.SCHEDULE_VERSION += 1
    try:
        stages.schedule(session, storage, s2.id)
    finally:
        stages.SCHEDULE_VERSION -= 1
    assert len(session.scalars(select(BeamSpec)).all()) == len(specs)
    assert len(session.scalars(select(Issue).where(Issue.rule == 'check_repeat')).all()) == 1
