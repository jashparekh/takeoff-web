"""Takeoff quantity engine.

Every number is returned with the formula that produced it and the same formula with the
numbers filled in, so the web app can show *how* each quantity was calculated.
Units: geometry inputs in inches (as drawn), lengths out in mm, weights in kg, concrete in m³.
"""
from __future__ import annotations
import math
from dataclasses import dataclass

IN = 25.4
DIAS = (8, 10, 12, 16, 20, 25)


def unit_wt(d: float) -> float:
    """kg per metre of a bar of diameter d mm (d²/162)."""
    return d * d / 162


def f(x: float, nd: int = 0) -> str:
    return f"{x:,.{nd}f}"


@dataclass
class Step:
    label: str
    formula: str
    working: str
    value: float
    unit: str

    def json(self):
        return dict(label=self.label, formula=self.formula, working=self.working,
                    value=round(self.value, 4), unit=self.unit)


# Detailing conventions, as project values (plan M4 step 3). These are the defaults when a project has not set them,
# so older inputs (the pinned pilot) calculate exactly as before.
CONVENTIONS = dict(
    extra_top_frac=1 / 3,         # extra top bar into the span when no length is drawn: L/3
    stirrup_hook_d=10,            # 135° stirrup hook: 10d each (two hooks), less 3 × 2d for the bends
    cant_hook_d=12,               # cantilever top and bottom bar end hook: 12d
)


class Calc:
    def __init__(self, inputs: dict, overrides: dict | None = None):
        self.inputs = inputs
        self.a = {**CONVENTIONS, **{x['key']: x['value'] for x in inputs['assumptions']}}
        if overrides:
            for k, v in overrides.items():
                if k in self.a and v is not None:
                    self.a[k] = float(v)

    # ---------------- beams ----------------
    def bar(self, beam: dict, bar: dict) -> dict:
        a = self.a
        L, b = beam['clear_in'] * IN, beam['b_in'] * IN
        d, n, role = bar['dia'], bar['nos'], bar['role']
        cant = beam['kind'] == 'C'
        if role in ('Top through', 'Top main'):
            if cant:
                h = a['cant_hook_d']
                ln = 2 * L + b + h * d
                fm, wk = f"2 × L + b + {h:g}d", f"2 × {f(L)} + {f(b)} + {h:g} × {d}"
                why = f"Cantilever top bar: runs the overhang, the support, and an equal length back into the span, plus a {h:g}d hook."
            else:
                ln = L + 2 * a['ld_factor'] * d
                fm, wk = "L + 2 × Ld,  Ld = k·d", f"{f(L)} + 2 × {a['ld_factor']:g} × {d}"
                why = "Continuous top bar: clear span plus a development length into the next span at each end."
        elif role == 'Bottom through':
            if cant:
                h = a['cant_hook_d']
                ln = L + b + h * d
                fm, wk = f"L + b + {h:g}d", f"{f(L)} + {f(b)} + {h:g} × {d}"
                why = f"Cantilever bottom bar: overhang plus support width plus a {h:g}d hook."
            else:
                ln = L + 2 * a['bot_anch_mm']
                fm, wk = "L + 2 × anchorage", f"{f(L)} + 2 × {a['bot_anch_mm']:g}"
                why = "Bottom bar anchored into each support."
        elif role == 'Bottom extra':
            ln = a['extra_bot_frac'] * L
            fm, wk = "α × L", f"{a['extra_bot_frac']:g} × {f(L)}"
            why = "Extra bottom bars are curtailed: they are needed only over the middle part of the span."
        elif role == 'Extra top @ support':
            if bar.get('extra_top_in'):
                into = bar['extra_top_in'] * IN
                fm, wk = "drawn length + b/2", f"{f(into)} + {f(b)}/2"
                why = "Extra top bar over the support, length as drawn on the elevation, plus half the support."
            else:
                k = a['extra_top_frac']
                into = k * L
                share = 'L/3' if abs(k - 1 / 3) < 1e-9 else f'{k:g}L'
                fm, wk = f"{share} + b/2", (f"{f(L)}/3 + {f(b)}/2" if share == 'L/3' else f"{k:g} × {f(L)} + {f(b)}/2")
                why = f"Extra top bar over the support: {share} into the span (no length drawn), plus half the support."
            ln = into + b / 2
        else:
            raise ValueError(role)
        kg = n * ln / 1000 * unit_wt(d)
        return dict(role=role, nos=n, dia=d, cut_mm=round(ln, 1), total_m=round(n * ln / 1000, 3), kg=round(kg, 3),
                    why=why,
                    steps=[Step('Cut length', fm, wk, ln, 'mm').json(),
                           Step('Weight', 'nos × cut length × d²/162', f"{n} × {f(ln / 1000, 3)} m × {unit_wt(d):.3f} kg/m", kg, 'kg').json()])

    def stirrups(self, beam: dict) -> dict:
        a = self.a
        L, b, D = beam['clear_in'] * IN, beam['b_in'] * IN, beam['D_in'] * IN
        s = beam['stirrup']
        d, se, sm = s['dia'], s['end_in'], s['mid_in']
        c = a['cover_mm']
        if beam['kind'] == 'C':
            nos = math.ceil(beam['clear_in'] / se) + 1
            nf, nw = "⌈L / s⌉ + 1", f"⌈{beam['clear_in']}″ / {se}″⌉ + 1"
        else:
            z = beam['clear_in'] / 3
            n1, n2 = math.ceil(z / se), math.ceil(z / sm)
            nos = 2 * n1 + n2 + 1
            nf = "2 × ⌈(L/3) / s_end⌉ + ⌈(L/3) / s_mid⌉ + 1"
            nw = f"2 × ⌈{z:.1f}″ / {se}″⌉ + ⌈{z:.1f}″ / {sm}″⌉ + 1 = 2 × {n1} + {n2} + 1"
        h = a['stirrup_hook_d']
        cut = 2 * (b - 2 * c) + 2 * (D - 2 * c) + (2 * h - 6) * d
        kg = nos * cut / 1000 * unit_wt(d)
        return dict(role='Stirrups', nos=nos, dia=d, cut_mm=round(cut, 1), total_m=round(nos * cut / 1000, 3), kg=round(kg, 3),
                    spacing=dict(end_in=se, mid_in=sm),
                    why="Two-legged closed stirrups with 135° hooks; closer spacing over the end thirds of the span.",
                    steps=[Step('Number', nf, nw, nos, 'nos').json(),
                           Step('Cut length', f"2(b − 2c) + 2(D − 2c) + 2 × {h:g}d − 3 × 2d",
                                f"2({f(b, 1)} − {2 * c:g}) + 2({f(D, 1)} − {2 * c:g}) + {2 * h * d:g} − {6 * d}", cut, 'mm').json(),
                           Step('Weight', 'nos × cut length × d²/162', f"{nos} × {cut / 1000:.3f} m × {unit_wt(d):.3f} kg/m", kg, 'kg').json()])

    def beam(self, beam: dict) -> dict:
        a = self.a
        bars = [self.bar(beam, x) for x in beam['bars']]
        st = self.stirrups(beam)
        L, b, D, t = beam['clear_in'] * IN, beam['b_in'] * IN, beam['D_in'] * IN, a['slab_t_in'] * IN
        conc = b * (D - t) * L / 1e9
        by = {}
        for x in bars + [st]:
            by[x['dia']] = by.get(x['dia'], 0) + x['kg']
        return dict(name=beam['name'], base=base_name(beam['name']), b_in=beam['b_in'], D_in=beam['D_in'], clear_in=beam['clear_in'],
                    kind=beam['kind'], bars=bars, stirrups=st, kg=round(sum(by.values()), 3),
                    kg_by_dia={k: round(v, 3) for k, v in sorted(by.items())},
                    concrete=Step('Concrete below slab', 'b × (D − slab t) × L',
                                  f"{f(b, 1)} × ({f(D, 1)} − {f(t, 1)}) × {f(L)} mm", conc, 'm³').json(),
                    plan_sqft=beam['b_in'] * beam['clear_in'] / 144)

    def chajja(self) -> dict | None:
        cb = self.inputs.get('chajja')
        if not cb:
            return None
        a = self.a
        n, Lin = cb['count'], cb['length_in']
        rows = []
        for x in cb['bars']:
            ln = Lin * IN + 2 * a['ld_factor'] * x['dia']
            nos = x['nos'] * n
            kg = nos * ln / 1000 * unit_wt(x['dia'])
            rows.append(dict(role=f"{x['role']} bars", nos=nos, dia=x['dia'], cut_mm=round(ln, 1), kg=round(kg, 3),
                             steps=[Step('Cut length', 'L + 2 × Ld', f"{f(Lin * IN)} + 2 × {a['ld_factor']:g} × {x['dia']}", ln, 'mm').json()]))
        lk = cb['link']
        nos = n * (math.ceil(Lin / lk['sp_in']) + 1)
        c = a['cover_mm']
        cut = 2 * (cb['b_in'] * IN - 2 * c) + 2 * (cb['D_in'] * IN - 2 * c) + 14 * lk['dia']
        kg = nos * cut / 1000 * unit_wt(lk['dia'])
        rows.append(dict(role='Links', nos=nos, dia=lk['dia'], cut_mm=round(cut, 1), kg=round(kg, 3),
                         steps=[Step('Number', 'count × (⌈L / s⌉ + 1)', f"{n} × (⌈{Lin} / {lk['sp_in']}⌉ + 1)", nos, 'nos').json()]))
        by = {}
        for r in rows:
            by[r['dia']] = by.get(r['dia'], 0) + r['kg']
        return dict(name='CB1', count=n, length_in=Lin, rows=rows, kg=round(sum(by.values()), 3), kg_by_dia=by)

    # ---------------- slab ----------------
    def slab(self, beams_out: list) -> dict:
        a, s = self.a, self.inputs['slab']
        beam_area = sum(b['plan_sqft'] for b in beams_out)
        net = a['floor_sqft'] - s['staircase_sqft'] - s['lift_sqft'] - s['ducts_sqft'] - beam_area
        m2 = net * 0.092903
        conc = m2 * a['slab_t_in'] * IN / 1000
        per_m = 1000 / (a['slab_bot_sp_in'] * IN)
        bot = m2 * 2 * per_m * unit_wt(8) * (1 + a['slab_anch_allow'])
        span_len = sum(b['clear_in'] for b in beams_out if b['kind'] == 'S') * IN / 1000
        top_per_m = 1000 / (a['slab_top_sp_in'] * IN)
        top_len = (2 * a['slab_top_ext_in'] + 9) * IN + 2 * 12 * 8
        top = span_len * top_per_m * top_len / 1000 * unit_wt(8)
        dist = span_len * 2 * math.ceil(a['slab_top_ext_in'] / a['slab_dist_sp_in']) * unit_wt(8)
        steps = [
            Step('Net slab area', 'footprint − staircase − lift − ducts − beam plan area',
                 f"{f(a['floor_sqft'])} − {s['staircase_sqft']} − {s['lift_sqft']} − {s['ducts_sqft']} − {f(beam_area, 1)}", net, 'sq ft'),
            Step('Slab concrete', 'net area × thickness', f"{m2:.2f} m² × {a['slab_t_in'] * IN / 1000:.4f} m", conc, 'm³'),
            Step('Bottom mesh bars per m', '1000 / spacing', f"1000 / ({a['slab_bot_sp_in']:g}″ × 25.4)", per_m, '/m'),
            Step('Bottom mesh (8φ both ways)', 'area × 2 × bars/m × 0.395 × (1 + allowance)',
                 f"{m2:.2f} × 2 × {per_m:.3f} × 0.395 × {1 + a['slab_anch_allow']:g}", bot, 'kg'),
            Step('Beam length carrying top bars', 'Σ clear spans of span beams', f"{span_len:.2f} m", span_len, 'm'),
            Step('Top bar length', '(2 × extension + 9″) × 25.4 + 2 × 12d', f"(2 × {a['slab_top_ext_in']:g} + 9) × 25.4 + 192", top_len, 'mm'),
            Step('Top bars over beams', 'beam length × bars/m × bar length × 0.395',
                 f"{span_len:.2f} × {top_per_m:.3f} × {top_len / 1000:.3f} × 0.395", top, 'kg'),
            Step('Top distribution bars', 'beam length × 2 sides × ⌈extension / spacing⌉ × 0.395',
                 f"{span_len:.2f} × 2 × ⌈{a['slab_top_ext_in']:g} / {a['slab_dist_sp_in']:g}⌉ × 0.395", dist, 'kg'),
        ]
        return dict(net_sqft=net, area_m2=m2, concrete_m3=conc, bottom_kg=bot, top_kg=top, dist_kg=dist,
                    kg=bot + top + dist, steps=[x.json() for x in steps])

    # ---------------- totals ----------------
    def run(self) -> dict:
        a = self.a
        beams = [self.beam(b) for b in self.inputs['beams']]
        cb = self.chajja()
        slab = self.slab(beams)
        by_beam = {d: 0.0 for d in DIAS}
        for b in beams:
            for d, v in b['kg_by_dia'].items():
                by_beam[d] += v
        if cb:
            for d, v in cb['kg_by_dia'].items():
                by_beam[d] += v
        rows, steel_cost = [], 0.0
        for d in DIAS:
            sl = slab['kg'] if d == 8 else 0.0
            tot = by_beam[d] + sl
            ww = tot * (1 + a['steel_wastage'])
            cost = ww * (a[f'rate_{d}'] + a['fe550_premium'])
            steel_cost += cost
            rows.append(dict(dia=d, beams=by_beam[d], slab=sl, total=tot, with_wastage=ww, cost=cost))
        beam_conc = sum(b['concrete']['value'] for b in beams)
        conc = (beam_conc + slab['concrete_m3']) * (1 + a['conc_wastage'])
        bags = conc * a['cement_kg_m3'] / a['bag_kg']
        steel = sum(r['with_wastage'] for r in rows)
        rmc = conc * a['rate_rmc']
        area = a['floor_sqft']
        merged = {}
        for b in beams:
            m = merged.setdefault(b['base'], dict(name=b['base'], kg=0.0, parts=[], kg_by_dia={}))
            m['kg'] += b['kg']; m['parts'].append(b['name'])
            for d, v in b['kg_by_dia'].items():
                m['kg_by_dia'][d] = m['kg_by_dia'].get(d, 0) + v
        summary = dict(
            beam_steel_kg=sum(r['beams'] for r in rows), slab_steel_kg=slab['kg'],
            steel_kg=sum(r['total'] for r in rows), steel_with_wastage_kg=steel,
            beam_concrete_m3=beam_conc, slab_concrete_m3=slab['concrete_m3'], concrete_m3=conc,
            cement_bags=bags, steel_cost=steel_cost, rmc_cost=rmc, cement_cost=bags * a['rate_cement_bag'],
            floor_sqft=area, steel_per_sqft=steel_cost / area, rmc_per_sqft=rmc / area,
            total_per_sqft=(steel_cost + rmc) / area, kg_per_sqft=steel / area, kg_per_m3=steel / conc,
            by_dia=rows,
            concrete_steps=[
                Step('Total concrete', '(beams below slab + slab) × (1 + wastage)',
                     f"({beam_conc:.2f} + {slab['concrete_m3']:.2f}) × {1 + a['conc_wastage']:g}", conc, 'm³').json(),
                Step('Cement', 'concrete × cement content / bag weight',
                     f"{conc:.2f} × {a['cement_kg_m3']:g} / {a['bag_kg']:g}", bags, 'bags').json(),
            ])
        return dict(assumptions=a, summary=summary, beams=beams, members=list(merged.values()), chajja=cb, slab=slab)


def base_name(n: str) -> str:
    return n.split('-')[0]
