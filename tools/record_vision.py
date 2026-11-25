"""Record vision-model readings of the pilot's schedule elevations, for the golden report and CI replay (plan §4).

    ANTHROPIC_API_KEY=… python -m tools.record_vision --send-crops [--limit 10] [--model claude-sonnet-5-5]

This makes live, billed calls and sends crops of the Work Lounge schedule drawings to the Anthropic API, so it does
nothing without --send-crops. Each answer is saved under tests/fixtures/vision/ (keyed by crop hash, model and
prompt version); commit them so the golden report and CI can replay without calls. Already-recorded crops are
skipped. Prints calls made, tokens and cost.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
import sys

from app.pipeline import pdf, vision
from app.pipeline.schedule import confidence, part_name, read_schedule
from app.settings import get_settings
from tools.golden_report import SCHEDULES


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--send-crops', action='store_true', help='confirm: send crops of the drawings to the API (billed)')
    ap.add_argument('--model', default=None, help='default: VISION_MODEL setting')
    ap.add_argument('--limit', type=int, default=0, help='stop after this many new calls (0 = no limit)')
    ap.add_argument('--all', action='store_true', help='also elevations OCR read completely')
    a = ap.parse_args(argv)
    if not a.send_crops:
        print('Nothing sent. This makes billed calls and sends drawing crops to the Anthropic API; '
              'pass --send-crops to go ahead.')
        return 2
    if not (os.environ.get('ANTHROPIC_API_KEY') or os.environ.get('ANTHROPIC_AUTH_TOKEN')):
        print('No Anthropic key in ANTHROPIC_API_KEY (or ANTHROPIC_AUTH_TOKEN).')
        return 2
    model = a.model or get_settings().vision_model
    client = vision.default_client()
    vision.FIXTURES.mkdir(parents=True, exist_ok=True)
    made = tin = tout = 0
    usd = 0.0
    for f in SCHEDULES:
        s = read_schedule(str(f), 1)
        img, dpi = pdf.render_gray(str(f), 1, vision.CROP_DPI)
        for e in s.elevations:
            if not a.all and confidence(e) >= get_settings().vision_below_confidence:
                continue
            png = vision.crop_from(img, dpi, list(e.box))
            sha = hashlib.sha256(png).hexdigest()
            out = vision.fixture_path(sha, model)
            if out.exists():
                continue
            reading, call = vision.read(None, client, png, model)
            out.write_text(json.dumps(dict(
                crop_sha256=sha, model=model, prompt_version=vision.PROMPT_VERSION, sheet=f.name,
                elevation=part_name(e, s.elevations), stop_reason='refusal' if call.status == 'refused' else 'end_turn',
                reading=reading.model_dump() if reading else None, error=call.error, request_id=call.request_id,
                input_tokens=call.input_tokens or 0, output_tokens=call.output_tokens or 0, cost_usd=call.cost_usd),
                indent=1))
            made += 1
            tin, tout, usd = tin + (call.input_tokens or 0), tout + (call.output_tokens or 0), usd + (call.cost_usd or 0)
            print(f'{part_name(e, s.elevations) or "?":12} {call.status:8} {call.latency_ms} ms  ${call.cost_usd or 0:.4f}')
            if a.limit and made >= a.limit:
                break
        if a.limit and made >= a.limit:
            break
    print(f'{made} calls, {tin} input + {tout} output tokens, ${usd:.2f}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
