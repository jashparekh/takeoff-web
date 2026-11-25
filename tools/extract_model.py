"""Extract a 3D framing model (beam centrelines, columns, slab footprint) from the beam layout PDF vectors.

Beams are drawn as pairs of 0.84 pt edge lines 9" apart; columns as 2.04 pt quads. Each strip is linked to a beam
name using the OCR'd label positions and the clear span from the schedule. Output units: metres, plan x→east, y→south.
"""
import json, sys, math
import numpy as np
import pymupdf
from PIL import Image, ImageDraw
from scipy import ndimage

pdf, proj_json, inputs_json, out = sys.argv[1:5]
PT_IN = 0.6                      # 9" beam = 5.4 pt on this sheet
M_PER_PT = 0.0254 / PT_IN
page = pymupdf.open(pdf)[0]
dr = page.get_drawings()
meta = json.load(open(proj_json)); inputs = json.load(open(inputs_json))
hot = next(s for s in meta['sheets'] if s['id'] == 'layout')['hotspots']
beams_in = {b['name']: b for b in inputs['beams']}

H, V, arcs, cols = [], [], [], []
for d in dr:
    w = round(d.get('width') or 0, 2)
    for it in d['items']:
        if it[0] == 'l' and w == 0.84:
            a, b = it[1], it[2]
            if abs(a.y - b.y) < 0.3 and abs(a.x - b.x) > 0.3: H.append((round((a.y + b.y) / 2, 1), min(a.x, b.x), max(a.x, b.x)))
            elif abs(a.x - b.x) < 0.3 and abs(a.y - b.y) > 0.3: V.append((round((a.x + b.x) / 2, 1), min(a.y, b.y), max(a.y, b.y)))
        if it[0] == 'c' and w == 0.84: arcs.append(it)
        if it[0] == 'qu' and w == 2.04:
            r = it[1].rect
            if 4 < min(r.width, r.height) < 7 and 10 < max(r.width, r.height) < 18: cols.append(r)

def lines(segs, gap=9.5):
    by = {}
    for c, a, b in segs:
        k = next((kk for kk in by if abs(kk - c) <= 0.35), c)
        by.setdefault(k, []).append((a, b))
    out = {}
    for c, iv in by.items():
        iv.sort(); m = [list(iv[0])]
        for a, b in iv[1:]:
            if a <= m[-1][1] + gap: m[-1][1] = max(m[-1][1], b)
            else: m.append([a, b])
        out[c] = m
    return out

def strips(segs):
    L = lines(segs); keys = sorted(L); res = []
    for i, c1 in enumerate(keys):
        for c2 in keys[i + 1:]:
            if c2 - c1 > 6.4: break
            if c2 - c1 < 4.6: continue
            for a1, b1 in L[c1]:
                for a2, b2 in L[c2]:
                    a, b = max(a1, a2), min(b1, b2)
                    if b - a > 6: res.append(dict(c=(c1 + c2) / 2, a=a, b=b))
    # drop duplicates (same strip found from nested line pairs)
    res.sort(key=lambda s: -(s['b'] - s['a'])); keep = []
    for s in res:
        if not any(abs(s['c'] - k['c']) < 2 and s['a'] >= k['a'] - 2 and s['b'] <= k['b'] + 2 for k in keep): keep.append(s)
    return keep

HS, VS = strips(H), strips(V)
print('strips', len(HS), len(VS), 'columns', len(cols))

def cuts_for(s, perp):
    cs = {s['a'], s['b']}
    for p in perp:
        if p['a'] - 3 <= s['c'] <= p['b'] + 3 and s['a'] - 3 <= p['c'] <= s['b'] + 3: cs.add(p['c'])
    for r in cols:
        cx, cy = (r.x0 + r.x1) / 2, (r.y0 + r.y1) / 2
        cs.add(cx if perp is VS else cy) if abs((cy if perp is VS else cx) - s['c']) < 6 and s['a'] - 3 <= (cx if perp is VS else cy) <= s['b'] + 3 else None
    return sorted(cs)

placed, misses = [], []
for name, b in beams_in.items():
    if '-' in name or name not in hot: continue
    h = hot[name]; vert = h['vertical']
    pool, perp = (VS, HS) if vert else (HS, VS)
    along, across = (h['y'], h['x']) if vert else (h['x'], h['y'])
    L = b['clear_in'] * PT_IN
    best = None
    for s in pool:
        d = abs(s['c'] - across)
        if d > 16 or not (s['a'] - 10 <= along <= s['b'] + 10): continue
        cs = cuts_for(s, perp)
        for i, a in enumerate(cs):
            for bb in cs[i + 1:]:
                if not (a - 8 <= along <= bb + 8): continue
                err = abs((bb - a) - (L + 5.4)) / (L + 5.4)
                score = err + d / 60
                if best is None or score < best[0]: best = (score, s, a, bb, err)
    if not best or best[4] > 0.25:
        misses.append(name); continue
    _, s, a, bb, err = best
    # trim to the clear span centred in the cut interval (keeps the drawn span exact)
    mid = (a + bb) / 2; half = L / 2 + 2.7
    a2, b2 = mid - half, mid + half
    seg = ((s['c'], a2), (s['c'], b2)) if vert else ((a2, s['c']), (b2, s['c']))
    placed.append(dict(name=name, vertical=vert, p1=seg[0], p2=seg[1], err=round(err, 3), strip=s))

# cantilever parts: free strip length beyond the main beam end, same line
for name, b in beams_in.items():
    if '-' not in name: continue
    base = name.split('-')[0]; m = next((p for p in placed if p['name'] == base), None)
    if not m: misses.append(name); continue
    s, L = m['strip'], b['clear_in'] * PT_IN + 2.7
    ends = [(m['p1'][1 if m['vertical'] else 0], -1), (m['p2'][1 if m['vertical'] else 0], 1)]
    pref = {'cantL': [-1], 'cantR': [1]}.get(name.split('-')[1], [-1, 1])
    cand = []
    for e, sg in ends:
        if sg not in pref: continue
        room = (e - s['a']) if sg < 0 else (s['b'] - e)
        cand.append((abs(room - L), e, sg))
    e, sg = min(cand)[1:] if cand else (ends[1][0], 1)
    c = s['c']; q1, q2 = sorted([e, e + sg * L])
    seg = ((c, q1), (c, q2)) if m['vertical'] else ((q1, c), (q2, c))
    placed.append(dict(name=name, vertical=m['vertical'], p1=seg[0], p2=seg[1], cant=True))

# slab footprint: rasterise beam edges, close dash gaps, fill enclosed area
k = 2.0
W, Hh = int(page.rect.width * k), int(page.rect.height * k)
im = Image.new('L', (W, Hh), 0); g = ImageDraw.Draw(im)
for c, a, b in H: g.line([(a * k, c * k), (b * k, c * k)], fill=255, width=2)
for c, a, b in V: g.line([(c * k, a * k), (c * k, b * k)], fill=255, width=2)
for it in arcs:
    pts = [it[i] for i in range(1, 5)]
    bez = [((1 - t) ** 3 * pts[0].x + 3 * (1 - t) ** 2 * t * pts[1].x + 3 * (1 - t) * t * t * pts[2].x + t ** 3 * pts[3].x,
            (1 - t) ** 3 * pts[0].y + 3 * (1 - t) ** 2 * t * pts[1].y + 3 * (1 - t) * t * t * pts[2].y + t ** 3 * pts[3].y) for t in np.linspace(0, 1, 20)]
    g.line([(x * k, y * k) for x, y in bez], fill=255, width=2)
a = np.array(im) > 0
# limit to the drawing frame (exclude title block): bounding box of strips
xs = [s['a'] for s in HS] + [s['b'] for s in HS] + [s['c'] for s in VS]
ys = [s['c'] for s in HS] + [s['a'] for s in VS] + [s['b'] for s in VS]
x0, x1, y0, y1 = min(xs) - 4, max(xs) + 4, min(ys) - 4, max(ys) + 4
a = a[int(y0 * k):int(y1 * k), int(x0 * k):int(x1 * k)]
a = ndimage.binary_dilation(a, iterations=int(4.5 * k))      # bridge dash gaps
filled = ndimage.binary_fill_holes(a)
inside = ndimage.binary_erosion(filled, iterations=int(4.5 * k) - 1)
# keep the main floor plate (largest enclosed region) and trace its outline
import cv2
lab, n = ndimage.label(inside)
sizes = ndimage.sum(inside, lab, range(1, n + 1))
plate = lab == (int(np.argmax(sizes)) + 1)
cs, hier = cv2.findContours(plate.astype(np.uint8), cv2.RETR_CCOMP, cv2.CHAIN_APPROX_SIMPLE)
outer = max(cs, key=cv2.contourArea)
poly = cv2.approxPolyDP(outer, 1.0 * k, True)[:, 0, :]
to_pt = lambda px, py: (x0 + px / k, y0 + py / k)
outline = [to_pt(*q) for q in poly]
area_sqft = plate.sum() / (k * k) * (M_PER_PT ** 2) / 0.092903
rects = []
print('placed', len(placed), 'misses', misses, 'outline pts', len(outline), 'footprint sq ft', round(area_sqft))

ox, oy = x0, y0
M = lambda x, y: [round((x - ox) * M_PER_PT, 3), round((y - oy) * M_PER_PT, 3)]
model = dict(units='m', source=f'{meta["sheets"][0]["drg"]} beam layout (vectors)', m_per_pt=M_PER_PT, origin_pt=[ox, oy],
             size=M(x1, y1),
             beams=[dict(name=p['name'], vertical=p['vertical'], p1=M(*p['p1']), p2=M(*p['p2']), cant=p.get('cant', False)) for p in placed],
             columns=[dict(c=M((r.x0 + r.x1) / 2, (r.y0 + r.y1) / 2), w=round(r.width * M_PER_PT, 3), d=round(r.height * M_PER_PT, 3)) for r in cols],
             slab=dict(outline=[M(*q) for q in outline], footprint_sqft=round(area_sqft)),
             unplaced=misses)
json.dump(model, open(out, 'w'))
