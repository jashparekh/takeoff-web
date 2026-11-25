"""Vision pass on schedule elevations (plan M3 step 4, decision D5): a crop of one beam elevation goes to Claude with
the project's notation; the answer comes back as a validated structured reading.

Cost and safety controls:
  - off unless `VISION_ENABLED=true` and an Anthropic key is set (it sends crops of the drawings to the API);
  - only elevations whose OCR reading looks incomplete are sent, and never more than a cap per sheet;
  - crops only, never whole sheets (plan §5);
  - every call is cached by (crop SHA-256, model, prompt version) in `model_calls`, with tokens, cost and latency.

The client is injectable: tests and `tools/golden_report --vision-replay` use recorded answers, so CI makes no calls.
"""
from __future__ import annotations
import hashlib
import io
import os
import time
from typing import Literal, Protocol

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db.models import ModelCall
from ..settings import get_settings

PROMPT_VERSION = 'elev-v1'
CROP_DPI = 300
# $ per million tokens (input, output), from the Claude API price list (Nov 2026).
PRICES = {'claude-sonnet-5-5': (2.0, 10.0), 'claude-opus-5-5': (4.0, 20.0), 'claude-haiku-5-5': (0.10, 0.50)}


class BarGroup(BaseModel):
    nos: int = Field(ge=1, le=20, description='number of bars, e.g. 3 in 3K16')
    dia: int = Field(description='bar diameter in mm, e.g. 16 in 3K16')


class ExtraTop(BarGroup):
    support: Literal['left', 'right', 'both', 'unknown'] = Field(description='which support the extra top bar is over')
    length_in: int | None = Field(None, description='length written on its dimension bracket, in inches; null for EQ or none')


class Stirrups(BaseModel):
    dia: int | None = Field(None, description='stirrup bar diameter in mm, from the K-number, e.g. 8 in K8@6"C/C')
    end_in: float | None = Field(None, description='spacing in inches in the end zones')
    mid_in: float | None = Field(None, description='spacing in inches in the middle zone')


class ElevationReading(BaseModel):
    """What one beam elevation on a schedule says. Leave anything you cannot read clearly out, and list it in
    `unreadable`; never fill a value in from what is usual."""
    name: str | None = Field(None, description='beam mark as written, e.g. B13; null if the elevation has only a size')
    b_in: float | None = Field(None, description='width in inches from the size label, e.g. 9 in (9"X18")')
    D_in: float | None = Field(None, description='depth in inches from the size label, e.g. 18 in (9"X18"); for two depths such as (9"X21"/24") the first')
    top_through: list[BarGroup] = Field(description='top bars running the full length (not under a support bracket)')
    top_main: list[BarGroup] = Field(description='top bars of a cantilever part; empty for spans')
    extra_top: list[ExtraTop] = Field(description='extra top bars over supports (under EQ or length brackets)')
    bottom_through: list[BarGroup] = Field(description='bottom bars running the full length')
    bottom_extra: list[BarGroup] = Field(description='additional shorter bottom bars in the span')
    stirrups: Stirrups
    unreadable: list[str] = Field(description='anything on the elevation you could not read clearly, in plain words')


SYSTEM = """You read beam elevations from Indian RCC structural drawings (beam reinforcement schedules).

Notation on this project: {notation}
- A callout like 3K16 means 3 bars of 16 mm; 2K20+1K25 means 2 of 20 mm plus 1 of 25 mm (two groups).
- Callouts above the beam are top bars. A top callout under a dimension bracket (EQ or a length such as 3'-3")
  over a support is an extra top bar over that support; the bracket's length belongs to it.
- Callouts at the bottom of the beam are bottom bars: the one whose line runs the full length is the through bar,
  shorter ones are extra bottom bars.
- Stirrup notes such as K8@6"C/C (bar 8 mm at 6 inch spacing) sit below the beam; ends and middle may differ.
- The label below the elevation gives the mark and size, e.g. B13(9"X18"). An elevation labelled only by a size
  next to another beam is that beam's cantilever part; its top bars are top_main.

Read only what is drawn in this crop. If a value is unclear, leave it out and say so in `unreadable`. Do not guess
from what is typical: a wrong number becomes a wrong steel quantity."""


def notation_text(notation: dict | None) -> str:
    n = notation or {}
    return f"'{n.get('bar_prefix', 'K')}' marks high-strength deformed (TMT/TOR) bars; dimensions in feet-inches."


class Client(Protocol):          # the parts of anthropic.Anthropic used here (tests pass a fake)
    messages: object


def default_client():
    import anthropic
    return anthropic.Anthropic()


def enabled() -> bool:
    s = get_settings()
    return bool(s.vision_enabled and (os.environ.get('ANTHROPIC_API_KEY') or os.environ.get('ANTHROPIC_AUTH_TOKEN')))


def crop_png(pdf_path: str, page_no: int, box: list[float], dpi: int = CROP_DPI) -> bytes:
    """The elevation, cut from a full render of the sheet (all strokes, so the model sees bar lines and brackets)."""
    from . import pdf
    img, d = pdf.render_gray(pdf_path, page_no, dpi)
    return crop_from(img, d, box)


def crop_from(img, d: int, box: list[float]) -> bytes:
    k = d / 72
    x0, y0, x1, y1 = box
    pad = 6
    crop = img.crop((max(0, int((x0 - pad) * k)), max(0, int((y0 - pad) * k)),
                     min(img.width, int((x1 + pad) * k)), min(img.height, int((y1 + pad) * k))))
    buf = io.BytesIO()
    crop.save(buf, 'PNG', optimize=True)
    return buf.getvalue()


def cost(model: str, input_tokens: int, output_tokens: int) -> float | None:
    p = PRICES.get(model)
    return round((input_tokens * p[0] + output_tokens * p[1]) / 1e6, 6) if p else None


def read(db: Session, client, png: bytes, model: str, *, notation: dict | None = None, project_id=None,
         spec_id=None) -> tuple[ElevationReading | None, ModelCall]:
    """One elevation reading, from the cache when this crop was already read by this model and prompt version.
    A refusal or an error is recorded and returns None (the caller escalates or leaves it for Review)."""
    import base64
    sha = hashlib.sha256(png).hexdigest()
    hit = None if db is None else db.scalar(
        select(ModelCall).where(ModelCall.crop_sha256 == sha, ModelCall.model == model,
                                ModelCall.prompt_version == PROMPT_VERSION, ModelCall.status == 'ok')
        .order_by(ModelCall.created_at.desc()).limit(1))
    if hit is not None:
        hit.cached = True                             # not a mapped column: tells the caller no call was made
        return ElevationReading.model_validate(hit.response_json), hit
    system = SYSTEM.format(notation=notation_text(notation))
    call = ModelCall(project_id=project_id, spec_id=spec_id, crop_sha256=sha, model=model,
                     prompt_version=PROMPT_VERSION, status='error',
                     request_json=dict(system=system, crop_bytes=len(png), notation=notation))
    call.cached = False
    t = time.monotonic()
    try:
        resp = client.messages.parse(
            model=model,
            max_tokens=16000,
            system=system,
            messages=[{'role': 'user', 'content': [
                {'type': 'image', 'source': {'type': 'base64', 'media_type': 'image/png',
                                             'data': base64.standard_b64encode(png).decode()}},
                {'type': 'text', 'text': 'Read this beam elevation.'},
            ]}],
            output_format=ElevationReading,
        )
    except Exception as e:                            # noqa: BLE001 — recorded; the SDK already retried 429/5xx
        call.error, call.latency_ms = f'{type(e).__name__}: {e}'[:2000], int((time.monotonic() - t) * 1000)
        if db is not None:
            db.add(call)
            db.flush()
        return None, call
    call.latency_ms = int((time.monotonic() - t) * 1000)
    call.request_id = getattr(resp, '_request_id', None)
    u = resp.usage
    call.input_tokens, call.output_tokens = u.input_tokens, u.output_tokens
    call.cost_usd = cost(model, u.input_tokens, u.output_tokens)
    reading = None
    if resp.stop_reason == 'refusal':
        call.status = 'refused'
        details = getattr(resp, 'stop_details', None)
        call.error = f'refused: {getattr(details, "category", None)}'
    elif resp.stop_reason == 'max_tokens' or resp.parsed_output is None:
        call.error = f'no reading (stop_reason={resp.stop_reason})'
    else:
        reading = resp.parsed_output
        call.status, call.response_json = 'ok', reading.model_dump()
    if db is not None:
        db.add(call)
        db.flush()
    return reading, call


# ---------- recorded answers (CI and the golden report make no live calls) ----------

FIXTURES = __import__('pathlib').Path(__file__).parents[2] / 'tests' / 'fixtures' / 'vision'


def fixture_path(sha: str, model: str):
    return FIXTURES / f'{sha[:20]}-{model}-{PROMPT_VERSION}.json'


class ReplayMiss(Exception):
    pass


class ReplayClient:
    """Serves answers recorded by `tools/record_vision.py`, looked up by the crop's hash. A crop with no recording
    raises ReplayMiss (recorded as an error, like a failed call)."""
    def __init__(self, folder=None):
        self.folder = folder or FIXTURES
        self.messages = self

    def parse(self, *, model, messages, **_):
        import base64
        import json
        from types import SimpleNamespace
        img = next(b for b in messages[0]['content'] if b['type'] == 'image')
        sha = hashlib.sha256(base64.standard_b64decode(img['source']['data'])).hexdigest()
        f = self.folder / fixture_path(sha, model).name
        if not f.exists():
            raise ReplayMiss(f'no recording for crop {sha[:12]} on {model}')
        rec = json.loads(f.read_text())
        return SimpleNamespace(
            parsed_output=ElevationReading.model_validate(rec['reading']) if rec.get('reading') else None,
            stop_reason=rec.get('stop_reason', 'end_turn'), stop_details=None, _request_id=rec.get('request_id'),
            usage=SimpleNamespace(input_tokens=rec['input_tokens'], output_tokens=rec['output_tokens']))
