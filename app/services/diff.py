"""Compare two runs (plan M5 step 3): what changed between two saved calculations, and by how much.

Both runs are frozen (inputs, values, engine version, results), so the comparison is exact: totals, steel by bar
size with cost, every beam added / removed / changed with the reason read from its inputs (size, span, kind,
stirrups, bars), the slab by part, and the values that changed. A beam whose inputs are the same but whose steel
moved changed because of a value (e.g. Ld) or the engine; that is said too.
"""
from __future__ import annotations

SUMMARY = (('steel_with_wastage_kg', 'Steel incl. wastage', 'kg'), ('beam_steel_kg', 'Beam steel', 'kg'),
           ('slab_steel_kg', 'Slab steel', 'kg'), ('concrete_m3', 'Concrete incl. wastage', 'm³'),
           ('cement_bags', 'Cement', 'bags'), ('steel_cost', 'Steel cost', '₹'), ('rmc_cost', 'Ready-mix cost', '₹'),
           ('total_per_sqft', 'Per sq ft of floor', '₹'))
EPS = 0.01


def _d(a, b):
    a, b = a or 0, b or 0
    return dict(a=a, b=b, delta=b - a)


def _bars(beam: dict) -> list:
    return sorted((x['role'], x['nos'], x['dia'], x.get('extra_top_in') or 0) for x in beam.get('bars', []))


def _bar_text(bars: list) -> str:
    return ', '.join(f"{n}-{d}φ {r.lower()}" + (f' ({x:g}″)' if x else '') for r, n, d, x in bars) or 'none'


def beam_reasons(a: dict | None, b: dict | None) -> list[str]:
    """Why a beam's input changed between runs, in plain words."""
    if not a or not b:
        return []
    out = []
    if (a['b_in'], a['D_in']) != (b['b_in'], b['D_in']):
        out.append(f"size {a['b_in']:g}″×{a['D_in']:g}″ → {b['b_in']:g}″×{b['D_in']:g}″")
    if abs(a['clear_in'] - b['clear_in']) > EPS:
        out.append(f"span {a['clear_in']:g}″ → {b['clear_in']:g}″")
    if a.get('kind') != b.get('kind'):
        out.append({'C': 'now a cantilever', 'S': 'now a span'}.get(b.get('kind'), 'kind changed'))
    if a.get('stirrup') != b.get('stirrup'):
        sa, sb = a.get('stirrup') or {}, b.get('stirrup') or {}
        out.append(f"stirrups {sa.get('dia')}φ @ {sa.get('end_in')}/{sa.get('mid_in')}″ → "
                   f"{sb.get('dia')}φ @ {sb.get('end_in')}/{sb.get('mid_in')}″")
    if _bars(a) != _bars(b):
        out.append(f"bars {_bar_text(_bars(a))} → {_bar_text(_bars(b))}")
    return out


def compare(run_a, run_b) -> dict:
    ra, rb = run_a.results_json, run_b.results_json
    ia, ib = run_a.inputs_json, run_b.inputs_json
    sa, sb = ra['summary'], rb['summary']
    summary = [dict(key=k, label=lbl, unit=u, **_d(sa.get(k), sb.get(k))) for k, lbl, u in SUMMARY]

    da = {r['dia']: r for r in sa.get('by_dia', [])}
    db_ = {r['dia']: r for r in sb.get('by_dia', [])}
    by_dia = []
    for d in sorted(set(da) | set(db_)):
        x, y = da.get(d, {}), db_.get(d, {})
        row = dict(dia=d, kg=_d(x.get('with_wastage'), y.get('with_wastage')), cost=_d(x.get('cost'), y.get('cost')))
        if abs(row['kg']['delta']) > EPS or abs(row['cost']['delta']) > EPS:
            by_dia.append(row)

    va = {x['key']: x for x in ia.get('assumptions', [])}
    vb = {x['key']: x for x in ib.get('assumptions', [])}
    values = [dict(key=k, label=(vb.get(k) or va.get(k))['label'], unit=(vb.get(k) or va.get(k)).get('unit'),
                   a=va[k]['value'] if k in va else None, b=vb[k]['value'] if k in vb else None)
              for k in sorted(set(va) | set(vb))
              if (va.get(k) or {}).get('value') != (vb.get(k) or {}).get('value')]
    engine_changed = run_a.engine_version != run_b.engine_version
    because = ('values changed: ' + '; '.join(f"{v['label']} {_num(v['a'])} → {_num(v['b'])}" for v in values[:3])
               + (f' and {len(values) - 3} more' if len(values) > 3 else '')) if values else \
        ('the engine was updated' if engine_changed else 'the calculation changed')
    ka = {b['name']: b for b in ra['beams']}
    kb = {b['name']: b for b in rb['beams']}
    inp_a = {b['name']: b for b in ia.get('beams', [])}
    inp_b = {b['name']: b for b in ib.get('beams', [])}
    members = []
    for n in sorted(set(ka) | set(kb), key=_natural):
        x, y = ka.get(n), kb.get(n)
        if x and not y:
            members.append(dict(name=n, change='removed', kg=_d(x['kg'], 0), reasons=[], drawing=True))
        elif y and not x:
            members.append(dict(name=n, change='added', kg=_d(0, y['kg']), reasons=[], drawing=True))
        else:
            reasons = beam_reasons(inp_a.get(n), inp_b.get(n))
            if abs(y['kg'] - x['kg']) > EPS or reasons:
                members.append(dict(name=n, change='changed', kg=_d(x['kg'], y['kg']), drawing=bool(reasons),
                                    reasons=reasons or [f'same drawing values; {because}']))
    # Drawing changes first (added, removed, inputs changed), then beams that moved only because of a value.
    members.sort(key=lambda m: (m['change'] == 'changed' and not m.get('drawing'), _natural(m['name'])))

    sl_a, sl_b = ra['slab'], rb['slab']
    slab = dict(kg=_d(sl_a['kg'], sl_b['kg']), bottom=_d(sl_a.get('bottom_kg'), sl_b.get('bottom_kg')),
                top=_d(sl_a.get('top_kg'), sl_b.get('top_kg')), dist=_d(sl_a.get('dist_kg'), sl_b.get('dist_kg')),
                concrete=_d(sl_a.get('concrete_m3'), sl_b.get('concrete_m3')),
                method=dict(a=_slab_method(sl_a), b=_slab_method(sl_b)))
    ca, cb = (ra.get('chajja') or {}).get('kg'), (rb.get('chajja') or {}).get('kg')

    counts = {c: sum(1 for m in members if m['change'] == c) for c in ('added', 'removed', 'changed')}
    return dict(summary=summary, by_dia=by_dia, members=members, counts=counts, slab=slab,
                chajja=_d(ca, cb) if ca is not None or cb is not None else None, values=values,
                engine_changed=engine_changed,
                beams_kg_delta=sum(m['kg']['delta'] for m in members))


def _num(v) -> str:
    return '—' if v is None else f'{v:g}'


def _slab_method(sl: dict) -> str:
    bottom = 'panel by panel' if sl.get('method') == 'panels' else 'average spacing'
    top = 'as drawn' if sl.get('top_method') == 'drawn' else 'average spacing'
    return f'bottom {bottom}, top {top}'


def _natural(name: str):
    import re
    return [int(t) if t.isdigit() else t for t in re.split(r'(\d+)', name)]
