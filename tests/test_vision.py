"""Vision pass and reconcile (plan M3 steps 4–5). No live API calls: a fake client stands in for Anthropic."""
import base64
import hashlib
import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.db.models import BeamSpec, ModelCall
from app.pipeline import pdf, stages, vision
from app.pipeline.reconcile import from_model, reconcile
from app.pipeline.vision import BarGroup, ElevationReading, ExtraTop, Stirrups
from app.settings import get_settings
from conftest import sign_in
from test_scale_ocr import floors, run_all  # noqa: F401 (fixture)
from test_uploads import SCHED1, post, proj, queued, storage  # noqa: F401 (fixtures)

needs_tesseract = pytest.mark.skipif(not shutil.which('tesseract'), reason='tesseract not installed')
TRUTH = {b['name']: b for b in json.loads((Path(__file__).parents[1] / 'app/data/worklounge/inputs.json').read_text())['beams']}


def reading_from_truth(name: str) -> ElevationReading:
    """What a perfect vision model would say about a pilot beam."""
    t = TRUTH[name]
    g = lambda role: [BarGroup(nos=b['nos'], dia=b['dia']) for b in t['bars'] if b['role'] == role]  # noqa: E731
    return ElevationReading(
        name=None if '-' in name else name, b_in=t['b_in'], D_in=t['D_in'], top_through=g('Top through'),
        top_main=g('Top main'), bottom_through=g('Bottom through'), bottom_extra=g('Bottom extra'),
        extra_top=[ExtraTop(nos=b['nos'], dia=b['dia'], support='unknown') for b in t['bars'] if b['role'] == 'Extra top @ support'],
        stirrups=Stirrups(**t['stirrup']), unreadable=[])


class FakeClient:
    """Answers by crop hash; counts calls. `answers[sha]` may be an ElevationReading, 'refuse' or an Exception."""
    def __init__(self, answers=None, default=None):
        self.answers, self.default, self.calls = answers or {}, default, []
        self.messages = self

    def parse(self, *, model, messages, output_format, **_):
        img = next(b for b in messages[0]['content'] if b['type'] == 'image')
        sha = hashlib.sha256(base64.standard_b64decode(img['source']['data'])).hexdigest()
        self.calls.append((sha, model))
        a = self.answers.get((sha, model), self.answers.get(sha, self.default))
        if isinstance(a, Exception):
            raise a
        usage = SimpleNamespace(input_tokens=1500, output_tokens=300)
        if a == 'refuse':
            return SimpleNamespace(parsed_output=None, stop_reason='refusal', usage=usage, _request_id='req_x',
                                   stop_details=SimpleNamespace(category='other'))
        return SimpleNamespace(parsed_output=a, stop_reason='end_turn', usage=usage, _request_id='req_1')


OCR_B13 = {'size': (9, 18), 'stirrup_dia': 8, 'stirrup_end_in': 7, 'stirrup_mid_in': 7,
           'Top through': None, 'Top main': None, 'Extra top @ support': ((3, 16),),
           'Bottom through': ((2, 16),), 'Bottom extra': ((3, 16),)}


def test_reconcile_states():
    m = from_model(reading_from_truth('B13'))
    merged, status, conf = reconcile(OCR_B13, m)
    assert status['size'] == 'agree' and status['Bottom through'] == 'agree'
    assert status['Top through'] == 'model_only' and merged['Top through'] == ((3, 12),)
    assert status['stirrup_mid_in'] == 'disagree' and status['Extra top @ support'] == 'disagree'
    assert status['Top main'] == 'agree' and merged['Top main'] == ()             # the model says none drawn
    assert conf == 0.35
    # The escalation model sides with one reading: resolved, not silently picked.
    merged, status, conf = reconcile(OCR_B13, m, m)
    assert status['stirrup_mid_in'] == 'resolved' and merged['stirrup_mid_in'] == 9 and conf == 0.75
    # 2K20+1K20 and 3K20 are the same bars.
    a = dict(OCR_B13, **{'Bottom through': ((1, 20), (2, 20))})
    b = dict(OCR_B13, **{'Bottom through': ((3, 20),)})
    assert reconcile(a, b)[1]['Bottom through'] == 'agree'
    # Nobody read the size: missing, and it lowers confidence.
    assert reconcile(dict(OCR_B13, size=None), dict(OCR_B13, size=None))[2] == 0.5


def test_read_caches_costs_and_records_refusals(session):
    png = b'\x89PNG fake crop'
    sha = hashlib.sha256(png).hexdigest()
    fake = FakeClient({sha: reading_from_truth('B7')})
    r1, c1 = vision.read(session, fake, png, 'claude-sonnet-5-5')
    r2, c2 = vision.read(session, fake, png, 'claude-sonnet-5-5')
    assert r1 == r2 and len(fake.calls) == 1 and c2.cached           # second read: from the cache, no call
    assert c1.status == 'ok' and c1.cost_usd == pytest.approx((1500 * 2 + 300 * 10) / 1e6)
    assert c1.request_json['crop_bytes'] == len(png) and 'data' not in json.dumps(c1.request_json)
    r, c = vision.read(session, FakeClient(default='refuse'), b'other', 'claude-sonnet-5-5')
    assert r is None and c.status == 'refused' and 'refused' in c.error
    r, c = vision.read(session, FakeClient(default=RuntimeError('boom')), b'third', 'claude-sonnet-5-5')
    assert r is None and c.status == 'error' and 'boom' in c.error
    assert session.scalar(select(ModelCall).where(ModelCall.status == 'ok')).crop_sha256 == sha


def test_off_without_a_key(session, monkeypatch):
    monkeypatch.setenv('VISION_ENABLED', 'true')
    monkeypatch.delenv('ANTHROPIC_API_KEY', raising=False)
    monkeypatch.delenv('ANTHROPIC_AUTH_TOKEN', raising=False)
    get_settings.cache_clear()
    try:
        assert not vision.enabled()
        monkeypatch.setenv('ANTHROPIC_API_KEY', 'sk-test')
        assert vision.enabled()
        monkeypatch.setenv('VISION_ENABLED', 'false')
        get_settings.cache_clear()
        assert not vision.enabled()
    finally:
        get_settings.cache_clear()


def test_replay_client_and_recorder_guard(tmp_path):
    png = b'crop bytes'
    sha = hashlib.sha256(png).hexdigest()
    (tmp_path / vision.fixture_path(sha, 'claude-sonnet-5-5').name).write_text(json.dumps(
        dict(reading=reading_from_truth('B12').model_dump(), input_tokens=1000, output_tokens=200)))
    r, c = vision.read(None, vision.ReplayClient(tmp_path), png, 'claude-sonnet-5-5')
    assert r.bottom_through[0].dia == 16 and c.cost_usd == pytest.approx(0.004)
    r, c = vision.read(None, vision.ReplayClient(tmp_path), b'unknown', 'claude-sonnet-5-5')
    assert r is None and 'no recording' in c.error
    from tools.record_vision import main
    assert main([]) == 2                               # nothing is sent without --send-crops


@needs_tesseract
def test_vision_stage_with_a_perfect_model(client, session, proj, floors, storage, queued):  # noqa: F811
    sign_in(client, 'new@example.com')
    post(client, ('s1.pdf', SCHED1))
    sheet = list(run_all(session, storage, queued).values())[0]
    stages.schedule(session, storage, sheet.id)
    assert stages.vision(session, storage, sheet.id) == 'off'          # no key: nothing sent
    # Answer each crop from the pilot's hand-entered schedule, as a perfect model would.
    specs = {b.name: b for b in session.scalars(select(BeamSpec).where(BeamSpec.sheet_id == sheet.id))}
    with open('/dev/null'):
        pass
    path = Path(__file__).parents[1] / 'app/data/worklounge/source/260919_SF_BEAM SCHEDULE -01.pdf'
    img, dpi = pdf.render_gray(str(path), 1, vision.CROP_DPI)
    answers = {hashlib.sha256(vision.crop_from(img, dpi, b.box)).hexdigest(): reading_from_truth(n)
               for n, b in specs.items() if n in TRUTH and b.box}
    fake = FakeClient(answers)
    before = sum(1 for b in specs.values() if b.confidence < get_settings().vision_below_confidence and not b.duplicate_of)
    result = stages.vision(session, storage, sheet.id, client=fake)
    assert result.startswith(f'{before} elevations read')
    b13 = session.scalar(select(BeamSpec).where(BeamSpec.sheet_id == sheet.id, BeamSpec.name == 'B13'))
    assert b13.source == 'ocr+model' and b13.readings['model'] and b13.field_status['Top through'] == 'model_only'
    got = sorted((x.role, x.nos, x.dia) for x in b13.bars)
    assert got == sorted((x['role'], x['nos'], x['dia']) for x in TRUTH['B13']['bars'])
    assert (b13.stirrup_dia, b13.stirrup_end_in, b13.stirrup_mid_in) == (8, 7, 9)
    # OCR read only 3K16 over the supports; the model read 2K16 and 3K16; the escalation model sided with the model.
    assert b13.field_status['Extra top @ support'] == 'resolved' and 'Extra top' in b13.notes   # shown, not hidden
    assert b13.readings['ocr']['bars'] and b13.readings['escalated']
    n_calls = len(fake.calls)
    assert session.scalar(select(ModelCall).where(ModelCall.spec_id == b13.id, ModelCall.model == 'claude-opus-5-5'))
    # Running again sends nothing new.
    assert stages.vision(session, storage, sheet.id, client=fake) in ('already read', 'nothing to read')
    assert len(fake.calls) == n_calls
