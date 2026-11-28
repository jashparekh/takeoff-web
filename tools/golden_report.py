"""Compare the drawing readers with the hand-checked Work Lounge pilot (plan M2 step 6, M3 step 9).

    python -m tools.golden_report [--pt-per-in 0.6] [--schedules] [--vision-replay] [--json out.json]

Pilot truth: `app/data/worklounge/project.json` label positions (hotspots; ~20 placed by hand) and `model.json`
beam centrelines and columns (from tools/extract_model.py with the schedule's clear spans). Prints labels found,
beams placed, centreline error, and columns found. Metrics only; not yet a CI gate.
"""
from __future__ import annotations
import argparse
import json
import math
import sys
import time
from pathlib import Path

from app.pipeline.layout import read_layout

DATA = Path(__file__).parents[1] / 'app' / 'data' / 'worklounge'
SHEET = DATA / 'source' / '260919_SF_BEAM LAYOUT.pdf'


def pilot():
    meta = json.loads((DATA / 'project.json').read_text())
    hot = next(s for s in meta['sheets'] if s['id'] == 'layout')['hotspots']
    model = json.loads((DATA / 'model.json').read_text())
    ox, oy = model['origin_pt']
    k = model['m_per_pt']
    to_pt = lambda p: (p[0] / k + ox, p[1] / k + oy)                        # noqa: E731
    beams = {b['name']: (to_pt(b['p1']), to_pt(b['p2'])) for b in model['beams'] if not b.get('cant')}
    cants = {b['name']: (to_pt(b['p1']), to_pt(b['p2'])) for b in model['beams'] if b.get('cant')}
    cols = [to_pt(c['c']) for c in model['columns']]
    return hot, beams, cols, cants


def schedule_cants() -> dict[str, bool]:
    """What the pilot's schedule says about cantilever parts (from inputs.json, which follows the schedule)."""
    names = [b['name'] for b in json.loads((DATA / 'inputs.json').read_text())['beams']]
    return {n: any(m.startswith(n + '-cant') for m in names) for n in names if '-cant' not in n}


def report(pt_per_in: float, with_schedule: bool = True) -> dict:
    hot, truth, tcols, tcants = pilot()
    t = time.time()
    lay = read_layout(str(SHEET), 1, pt_per_in, schedule_cants() if with_schedule else None)
    secs = time.time() - t
    names = {b.name for b in lay.beams} | {u.name for u in lay.unplaced}
    placed = {}
    for b in lay.beams:                         # one per name: the most confident
        if b.name not in placed or b.confidence > placed[b.name].confidence:
            placed[b.name] = b
    errs, wrong = [], []
    for n, (p1, p2) in truth.items():
        if n not in placed:
            continue
        b = placed[n]
        mid_t = ((p1[0] + p2[0]) / 2, (p1[1] + p2[1]) / 2)
        mid_o = ((b.p1[0] + b.p2[0]) / 2, (b.p1[1] + b.p2[1]) / 2)
        e = math.dist(mid_t, mid_o) / pt_per_in                                  # inches
        (errs if e < 48 else wrong).append((n, round(e, 1)))
    # Cantilevers: same base beam, overlapping the pilot's cantilever part (the pilot sometimes put it on the wrong
    # end, CLAUDE.md "Some cantilever stubs sit on the wrong end", so the match is by base name and overlap).
    ours_c = [b for b in lay.beams if b.cantilever]
    def overlaps(b, t):
        (a1, a2), (b1, b2) = sorted([b.p1, b.p2]), sorted(t)
        return math.dist(((a1[0] + a2[0]) / 2, (a1[1] + a2[1]) / 2), ((b1[0] + b2[0]) / 2, (b1[1] + b2[1]) / 2)) < 30
    cant_match = sorted({n.split('-')[0] for n, t in tcants.items()
                         if any(b.name.split('-')[0] == n.split('-')[0] and overlaps(b, t) for b in ours_c)})
    # Which beams have cantilever parts is known from the schedule (inputs.json names like B7-cant).
    inputs = json.loads((DATA / 'inputs.json').read_text())
    sched_c = {b['name'] for b in inputs['beams'] if '-cant' in b['name']}
    ours_c_names = {b.name for b in ours_c}
    base = lambda names: {n.split('-')[0] for n in names}                   # noqa: E731
    col_hits = sum(1 for c in tcols if any(math.dist(c, (r.cx, r.cy)) < 3 for r in lay.columns))
    out = dict(
        seconds=round(secs, 1), edge_width=lay.edge_width, strips=len(lay.strips),
        labels_found=len(names & set(hot)), labels_pilot=len(hot), labels_extra=sorted(names - set(hot)),
        labels_missing=sorted(set(hot) - names, key=lambda s: (len(s), s)),
        beams_placed=sum(1 for n in truth if n in placed), beams_pilot=len(truth),
        beams_within_4ft=len(errs), beams_far=sorted(wrong),
        median_mid_error_in=sorted(e for _, e in errs)[len(errs) // 2] if errs else None,
        cantilevers_found=len(ours_c), cantilevers_schedule=len(sched_c),
        cantilever_beams_matching_schedule=len(base(ours_c_names) & base(sched_c)),
        cantilevers_not_in_schedule=sorted(base(ours_c_names) - base(sched_c)),
        cantilevers_missed=sorted(base(sched_c) - base(ours_c_names)),
        cantilevers_same_place_as_pilot=len(cant_match),
        columns_found=len(lay.columns), columns_pilot=len(tcols), columns_matching_pilot=col_hits,
        unplaced_labels=sorted({u.name for u in lay.unplaced}),
        joined_with_schedule=sorted(lay.joined),
        floor_plate_sqft=lay.outline.area_sqft if lay.outline else None,
        floor_plate_pilot_sqft=json.loads((DATA / 'model.json').read_text())['slab']['footprint_sqft'],
        **span_report(lay, inputs),
        floor_plate_has_curved_corner=bool(lay.outline and any(p[0] < 100 and p[1] < 130 for p in lay.outline.points)))
    return out


def span_report(lay, inputs) -> dict:
    """Clear spans: drawn dimensions read, beams given one, agreement with the geometry and with the pilot workbook.
    The workbook is 4–5" under the drawn dimension on many beams (no rule found); the drawn value is what counts."""
    wb = {b['name']: b['clear_in'] for b in inputs['beams']}
    dim = [b for b in lay.beams if b.clear_source == 'dimension']
    one: dict = {}                                   # one per name: the most confident (misread copies are doubtful)
    for b in lay.beams:
        if not b.cantilever and b.name in wb and (b.name not in one or b.confidence > one[b.name].confidence):
            one[b.name] = b
    plain = list(one.values())
    return dict(
        span_dims_read=len(lay.dims),
        spans_from_dimension=len(dim),
        spans_dimension_agrees_with_geometry=sum(1 for b in dim if b.geo_clear_in and abs(b.clear_in - b.geo_clear_in) <= 1),
        spans_within_1in_of_workbook=sum(1 for b in plain if b.clear_in and abs(b.clear_in - wb[b.name]) <= 1),
        spans_drawn_4_5in_over_workbook=sorted(b.name for b in dim if b.name in wb and 3.5 <= b.clear_in - wb[b.name] <= 5.5),
        spans_matching_workbook_or_drawn=sum(1 for b in plain if b.clear_in and (abs(b.clear_in - wb[b.name]) <= 1 or
                                                                                3.5 <= b.clear_in - wb[b.name] <= 5.5)),
        spans_short=sorted(b.name for b in plain if b.clear_in and b.clear_in - wb[b.name] < -1),
        spans_long=sorted(b.name for b in plain if b.clear_in and b.clear_in - wb[b.name] > 5.5),
        spans_compared=len(plain))


SLAB_BOTTOM = DATA / 'source' / '260919_SF_BOTTOM SLAB REINFORCEMENT LAYOUT.pdf'


def slab_report(pt_per_in: float) -> dict:
    """Bottom slab panel by panel against the averaged method (plan M5 golden: within ±3%, difference explained)."""
    from app.engine import Calc
    from app.pipeline.slab import read_slab
    t = time.time()
    r = read_slab(str(SLAB_BOTTOM), 1, pt_per_in)
    inputs = json.loads((DATA / 'inputs.json').read_text())
    avg = Calc(inputs).run()['slab']
    panels = [dict(name=f'P{i}', area_sqft=p.size_in(pt_per_in)[0] * p.size_in(pt_per_in)[1] / 144, thk_in=p.thk_in,
                   bars=[b.json() for b in p.bars]) for i, p in enumerate(r.panels, 1)]
    per = Calc({**inputs, 'slab': {**inputs['slab'], 'panels': panels}}).run()['slab']
    return dict(seconds=round(time.time() - t, 1), callouts=r.callouts, bar_lines=r.hairlines, panels=len(r.panels),
                both_directions=sum(1 for p in r.panels if len({c.vertical for c in p.callouts}) == 2),
                distribution=r.distribution, thick_panels=[(f'P{i}', p.thk_in) for i, p in enumerate(r.panels, 1) if p.thk_in],
                panel_sqft=round(sum(p['area_sqft'] for p in panels)), net_sqft=round(avg['net_sqft']),
                bottom_kg_panels=round(per['bottom_kg']), bottom_kg_average=round(avg['bottom_kg']),
                bottom_diff_pct=round((per['bottom_kg'] / avg['bottom_kg'] - 1) * 100, 1),
                slab_kg_panels=round(per['kg']), slab_kg_average=round(avg['kg']),
                slab_diff_pct=round((per['kg'] / avg['kg'] - 1) * 100, 1))


SCHEDULES = sorted((DATA / 'source').glob('*BEAM SCHEDULE*.pdf'))


def schedule_report() -> dict:
    """Schedule reader against the pilot's hand-entered beams (`inputs.json`): per beam, is the size right, are the
    bars (role, number, diameter) right, are the stirrups right, and is all of it right."""
    import collections
    from app.pipeline.schedule import part_name, read_schedule
    truth = {b['name']: b for b in json.loads((DATA / 'inputs.json').read_text())['beams']}
    t = time.time()
    got, repeats = {}, []
    for f in SCHEDULES:
        s = read_schedule(str(f), 1)
        for e in s.elevations:
            n = part_name(e, s.elevations)
            if not n:
                continue
            if n in got:
                repeats.append(n)
            else:
                got[n] = e
    key = lambda bars: collections.Counter((b['role'], b['nos'], b['dia']) for b in bars)    # noqa: E731
    c = collections.Counter()
    for n, tb in truth.items():
        e = got.get(n)
        if e is None:
            continue
        size = (e.b_in, e.D_in) == (tb['b_in'], tb['D_in'])
        bars = key([b.__dict__ for b in e.bars]) == key(tb['bars'])
        stir = e.stirrup == tb['stirrup']
        c['size'] += size
        c['bars'] += bars
        c['stirrups'] += stir
        c['all'] += size and bars and stir
    return dict(seconds=round(time.time() - t, 1), beams_schedule=len(truth),
                elevations_matched=sum(1 for n in truth if n in got),
                size_right=c['size'], bars_right=c['bars'], stirrups_right=c['stirrups'],
                fully_right=c['all'], fully_right_pct=round(100 * c['all'] / len(truth)),
                repeated_elevations=sorted(set(repeats)),
                missing=sorted(n for n in truth if n not in got),
                not_in_schedule=sorted(n for n in got if n not in truth))


def vision_report() -> dict:
    """Schedules again, with the vision model's recorded answers reconciled in (tests/fixtures/vision, made by
    tools/record_vision.py). Elevations without a recording keep their OCR reading. No live calls."""
    import collections
    import hashlib
    from types import SimpleNamespace
    from app.pipeline import pdf, vision
    from app.pipeline.reconcile import ROLES, from_model, from_spec, reconcile
    from app.pipeline.schedule import confidence, part_name, read_schedule
    from app.settings import get_settings
    truth = {b['name']: b for b in json.loads((DATA / 'inputs.json').read_text())['beams']}
    model, esc = get_settings().vision_model, get_settings().vision_escalate_model
    client = vision.ReplayClient()
    final, replayed, usd = {}, 0, 0.0
    for f in SCHEDULES:
        s = read_schedule(str(f), 1)
        img, dpi = pdf.render_gray(str(f), 1, vision.CROP_DPI)
        for e in s.elevations:
            n = part_name(e, s.elevations)
            if not n or n in final:
                continue
            st = e.stirrup or {}
            spec = SimpleNamespace(b_in=e.b_in, D_in=e.D_in, stirrup_dia=st.get('dia'), stirrup_end_in=st.get('end_in'),
                                   stirrup_mid_in=st.get('mid_in'), bars=e.bars)
            ocr = from_spec(spec)
            merged = ocr
            if confidence(e) < get_settings().vision_below_confidence:
                png = vision.crop_from(img, dpi, list(e.box))
                first, call = vision.read(None, client, png, model)
                if first is not None:
                    replayed += 1
                    usd += call.cost_usd or 0
                    merged, status, _ = reconcile(ocr, from_model(first))
                    if 'disagree' in status.values():
                        second, c2 = vision.read(None, client, png, esc)
                        if second is not None:
                            usd += c2.cost_usd or 0
                            merged, _, _ = reconcile(ocr, from_model(first), from_model(second))
            final[n] = merged
    c = collections.Counter()
    for n, tb in truth.items():
        m = final.get(n)
        if m is None:
            continue
        want_bars = {r: tuple(sorted((b['nos'], b['dia']) for b in tb['bars'] if b['role'] == r)) or None for r in ROLES}
        size = m['size'] == (tb['b_in'], tb['D_in'])
        bars = all((m[r] or None) == want_bars[r] for r in ROLES)
        stir = (m['stirrup_dia'], m['stirrup_end_in'], m['stirrup_mid_in']) == \
            (tb['stirrup']['dia'], tb['stirrup']['end_in'], tb['stirrup']['mid_in'])
        c['all'] += size and bars and stir
    recorded = len(list(vision.FIXTURES.glob('*.json')))
    return dict(recordings=recorded, elevations_replayed=replayed, fully_right=c['all'],
                fully_right_pct=round(100 * c['all'] / len(truth)), recorded_cost_usd=round(usd, 2),
                note=None if recorded else 'No recordings yet: run tools.record_vision --send-crops with a key.')


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--pt-per-in', type=float, default=0.6)
    ap.add_argument('--json')
    ap.add_argument('--schedules', action='store_true', help='also read the beam schedules')
    ap.add_argument('--vision-replay', action='store_true', help='schedules with recorded vision readings')
    ap.add_argument('--slab', action='store_true', help='also read the bottom slab panel by panel')
    a = ap.parse_args(argv)
    r = report(a.pt_per_in)
    if a.schedules:
        r.update({f'schedule_{k}': v for k, v in schedule_report().items()})
    if a.slab:
        r.update({f'slab_{k}': v for k, v in slab_report(a.pt_per_in).items()})
    if a.vision_replay:
        r.update({f'vision_{k}': v for k, v in vision_report().items()})
    for k, v in r.items():
        print(f'{k:24} {v}')
    if a.json:
        Path(a.json).write_text(json.dumps(r, indent=1))


if __name__ == '__main__':
    sys.exit(main())
