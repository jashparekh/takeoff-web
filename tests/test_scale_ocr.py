"""OCR fallback for title blocks, and the scale check (plan M1 steps 6–7). Needs the `tesseract` program."""
import io
import shutil

import pypdfium2 as pdfium
import pytest
from sqlalchemy import select

from app.db.models import Building, Correction, Floor, Sheet
from app.pipeline import stages
from app.pipeline.calibrate import parse_dim
from test_uploads import LAYOUT, SCHED1, SLAB_B, SLAB_T, post, proj, queued, storage  # noqa: F401 (fixtures)
from conftest import sign_in

needs_tesseract = pytest.mark.skipif(not shutil.which('tesseract'), reason='tesseract not installed')
PILOT_PT_PER_IN = 0.60          # tools/extract_model.py: "9-inch beam = 5.4 pt on this sheet", found by hand


@pytest.mark.parametrize('text, inches', [
    ('16\'-2"', 194), ('15-11', 191), ('{9-11', 119), ('14-10!', 178), ('4\'-6"', 54), ('3\'-3 1/2"', 39.5),
    ('9"', None), ('B13(9"X18")', None), ('16-14', None), ('0-4', None), ('K8@8"C/C', None)])
def test_parse_dim(text, inches):
    assert parse_dim(text) == inches


@pytest.fixture
def floors(session, proj):  # noqa: F811
    b = Building(project_id=proj.id, name='Main building', copies=1, sort=0)
    b.floors = [Floor(name=n, sort=i) for i, n in enumerate(['First floor', 'Second floor', 'Third floor'])]
    session.add(b)
    session.flush()
    return {f.name: f for f in b.floors}


def run_all(session, storage, queued):  # noqa: F811
    ids = [sid for uid in queued for sid in stages.ingest(session, storage, uid)]
    for sid in ids:
        stages.classify(session, storage, sid)
    for sid in ids:
        stages.calibrate(session, storage, sid)
    return {session.get(Sheet, i).drg_no: session.get(Sheet, i) for i in ids}


@needs_tesseract
def test_scale_check_on_worklounge(client, session, proj, floors, storage, queued):  # noqa: F811
    sign_in(client, 'new@example.com')
    for name, data in [('layout.pdf', LAYOUT), ('s1.pdf', SCHED1), ('bottom.pdf', SLAB_B), ('top.pdf', SLAB_T)]:
        post(client, (name, data))
    got = run_all(session, storage, queued)
    lay = got['MC-SD-07']
    # Printed 1:100, but the PDF was printed to fit A3: the dimensions measure about 1:120, as found by hand.
    assert lay.scale_status == 'differs' and lay.scale_checks >= 2
    assert abs(lay.pt_per_in / PILOT_PT_PER_IN - 1) < 0.02
    assert '1:100' in lay.scale_note and 'printed to fit' in lay.scale_note
    assert got['MC-SD-06-01'].scale_status == 'not_needed' and got['MC-SD-06-01'].pt_per_in is None
    bottom = got['MC-SD-06-04']                     # bar callouts only, no dimensions: borrows from the layout
    assert bottom.scale_status == 'borrowed' and bottom.pt_per_in == lay.pt_per_in and 'MC-SD-07' in bottom.scale_note
    assert got['MC-SD-06-05'].scale_status == 'differs'

    # A person confirms the layout at 1:120; the borrowing sheet follows, and both are recorded.
    r = client.patch(f'/api/projects/site-b/sheets/{lay.id}', json=dict(scale_ratio=120)).json()
    assert (r['scale_status'], r['scale_ratio'], r['classify_status']) == ('confirmed', 120, 'auto')
    session.refresh(bottom)
    assert bottom.scale_status == 'borrowed' and round(72 / bottom.pt_per_in) == 120
    assert session.scalar(select(Correction).where(Correction.field == 'scale')).new_value == '1:120'
    # Accepting the scale shown on the top slab sheet.
    top = got['MC-SD-06-05']
    r = client.patch(f'/api/projects/site-b/sheets/{top.id}', json=dict(accept_scale=True)).json()
    assert r['scale_status'] == 'confirmed' and r['pt_per_in'] == pytest.approx(top.pt_per_in)
    # A confirmed scale is not changed by the worker.
    stages.calibrate(session, storage, lay.id)
    assert session.get(Sheet, lay.id).scale_status == 'confirmed'


@needs_tesseract
def test_title_block_by_ocr_when_the_pdf_has_no_text(client, session, proj, floors, storage, queued):  # noqa: F811
    """The layout drawn to an image and saved as PDF (like a scan): read by OCR instead."""
    img = pdfium.PdfDocument(LAYOUT)[0].render(scale=200 / 72).to_pil().convert('RGB')
    buf = io.BytesIO()
    img.save(buf, 'PDF', resolution=200)
    sign_in(client, 'new@example.com')
    post(client, ('scanned layout.pdf', buf.getvalue()))
    s = list(run_all(session, storage, queued).values())[0]
    assert s.text_source == 'ocr'
    assert (s.drg_no, s.sheet_type, s.floor_id, s.revision, s.scale_text) == \
        ('MC-SD-07', 'beam_layout', floors['Second floor'].id, 'R0', '1:100')
    assert s.classify_status == 'auto'


def test_changing_the_type_rechecks_the_scale(client, session, proj, floors, storage, queued):  # noqa: F811
    sign_in(client, 'new@example.com')
    post(client, ('s1.pdf', SCHED1))
    sid = stages.ingest(session, storage, queued[0])[0]
    stages.classify(session, storage, sid)
    stages.calibrate(session, storage, sid)
    assert session.get(Sheet, sid).scale_status == 'not_needed'
    client.patch(f'/api/projects/site-b/sheets/{sid}', json=dict(sheet_type='beam_layout'))
    assert session.get(Sheet, sid).scale_status is None          # the worker's sweep checks it again
    r = client.patch(f'/api/projects/site-b/sheets/{sid}', json=dict(accept_scale=True))
    assert r.status_code == 422 and 'enter it as 1:N' in r.json()['detail']
