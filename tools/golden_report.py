"""Compare the layout reader with the hand-checked Work Lounge pilot (plan M2 step 6).

    python -m tools.golden_report [--pt-per-in 0.6] [--json out.json]

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
    cols = [to_pt(c['c']) for c in model['columns']]
    return hot, beams, cols


def report(pt_per_in: float) -> dict:
    hot, truth, tcols = pilot()
    t = time.time()
    lay = read_layout(str(SHEET), 1, pt_per_in)
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
    col_hits = sum(1 for c in tcols if any(math.dist(c, (r.cx, r.cy)) < 3 for r in lay.columns))
    out = dict(
        seconds=round(secs, 1), edge_width=lay.edge_width, strips=len(lay.strips),
        labels_found=len(names & set(hot)), labels_pilot=len(hot), labels_extra=sorted(names - set(hot)),
        labels_missing=sorted(set(hot) - names, key=lambda s: (len(s), s)),
        beams_placed=sum(1 for n in truth if n in placed), beams_pilot=len(truth),
        beams_within_4ft=len(errs), beams_far=sorted(wrong),
        median_mid_error_in=sorted(e for _, e in errs)[len(errs) // 2] if errs else None,
        columns_found=len(lay.columns), columns_pilot=len(tcols), columns_matching_pilot=col_hits,
        unplaced_labels=sorted({u.name for u in lay.unplaced}))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--pt-per-in', type=float, default=0.6)
    ap.add_argument('--json')
    a = ap.parse_args(argv)
    r = report(a.pt_per_in)
    for k, v in r.items():
        print(f'{k:24} {v}')
    if a.json:
        Path(a.json).write_text(json.dumps(r, indent=1))


if __name__ == '__main__':
    sys.exit(main())
