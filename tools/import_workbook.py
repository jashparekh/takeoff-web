"""One-off import of the Work Lounge pilot workbook into project.json (inputs only; all results are recomputed by app.engine)."""
import json, sys, openpyxl
src = sys.argv[1]; out = sys.argv[2]
wb = openpyxl.load_workbook(src)
S, B, R = wb['Summary'], wb['Beams'], wb['Beam bars']

def row(label):
    for r in range(1, 60):
        if S.cell(r, 1).value == label: return r
    raise KeyError(label)

A = [  # key, workbook label, unit, group
 ('slab_t_in','Slab thickness','in','Geometry'),
 ('ld_factor','Anchorage / lap length factor','× bar dia','Detailing'),
 ('bot_anch_mm','Beam bottom-bar anchorage per end','mm','Detailing'),
 ('cover_mm','Beam clear cover','mm','Detailing'),
 ('extra_bot_frac','Extra bottom bar length','× clear span','Detailing'),
 ('slab_bot_sp_in','Slab bottom bar spacing (avg, both ways)','in','Slab'),
 ('slab_top_sp_in','Slab top bar spacing over beams (avg)','in','Slab'),
 ('slab_top_ext_in','Slab top bar extension each side of beam','in','Slab'),
 ('slab_dist_sp_in','Slab top distribution spacing','in','Slab'),
 ('slab_anch_allow','Slab bar anchorage/lap allowance','fraction','Slab'),
 ('steel_wastage','Steel wastage / laps','fraction','Wastage'),
 ('conc_wastage','Concrete wastage','fraction','Wastage'),
 ('cement_kg_m3','Cement content, M25 design mix','kg/m³','Materials'),
 ('bag_kg','Cement bag weight','kg','Materials'),
 ('floor_sqft','Floor footprint (slab outline incl. beams)','sq ft','Geometry'),
 ('rate_8','TMT 8 mm (Rs/kg)','₹/kg','Rates'),('rate_10','TMT 10 mm (Rs/kg)','₹/kg','Rates'),
 ('rate_12','TMT 12 mm (Rs/kg)','₹/kg','Rates'),('rate_16','TMT 16 mm (Rs/kg)','₹/kg','Rates'),
 ('rate_20','TMT 20 mm (Rs/kg)','₹/kg','Rates'),('rate_25','TMT 25 mm (Rs/kg)','₹/kg','Rates'),
 ('fe550_premium','Fe550 premium over Fe500D (Rs/kg)','₹/kg','Rates'),
 ('rate_rmc','Ready-mix M25 (Rs/m3)','₹/m³','Rates'),('rate_cement_bag','OPC 53 cement (Rs/50 kg bag)','₹/bag','Rates'),
]
CONFIRM = {'ld_factor','cover_mm','slab_bot_sp_in','slab_top_ext_in','cement_kg_m3'}
assumptions = []
for key, label, unit, group in A:
    r = row(label)
    assumptions.append(dict(key=key, label=label.split(' (Rs')[0], value=S.cell(r, 2).value, unit=unit,
                            group=group, source=S.cell(r, 4).value or '', confirm=key in CONFIRM))
beams = []
for r in range(2, B.max_row + 1):
    n = B.cell(r, 1).value
    if not n or n == 'TOTAL': continue
    b, D, L, kind, sd, se, sm = [B.cell(r, c).value for c in range(2, 9)]
    beams.append(dict(name=n, b_in=b, D_in=D, clear_in=L, kind=kind, stirrup=dict(dia=sd, end_in=se, mid_in=sm), bars=[]))
by = {x['name']: x for x in beams}
cb = None
for r in range(2, R.max_row + 1):
    n, role, nos, dia, xt = [R.cell(r, c).value for c in range(1, 6)]
    if not n or n == 'TOTAL': continue
    if n.startswith('CB1'):
        cb = cb or dict(name='CB1', count=10, length_in=54, b_in=9, D_in=5, bars=[])
        if 'links' in role: cb['link'] = dict(dia=8, sp_in=8)
        else: cb['bars'].append(dict(role='Top' if 'top' in role else 'Bottom', nos=3, dia=16))
        continue
    by[n]['bars'].append(dict(role=role, nos=nos, dia=dia, **({'extra_top_in': xt} if xt is not None else {})))
slab = dict(staircase_sqft=wb['Slab']['B4'].value, lift_sqft=wb['Slab']['B5'].value, ducts_sqft=wb['Slab']['B6'].value,
            notes={'staircase': wb['Slab']['C4'].value, 'lift': wb['Slab']['C5'].value, 'ducts': wb['Slab']['C6'].value})
json.dump(dict(assumptions=assumptions, beams=beams, chajja=cb, slab=slab), open(out, 'w'), indent=1)
print(len(beams), 'beams;', sum(len(b['bars']) for b in beams), 'bar rows')
