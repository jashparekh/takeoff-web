"""Takeoff web API: serves projects, drawings and explained calculations."""
from __future__ import annotations
import json
from functools import lru_cache
from pathlib import Path

from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .auth.routes import router as auth_router
from .engine import Calc, unit_wt

ROOT = Path(__file__).parent
DATA = ROOT / 'data'
WEB = ROOT.parent / 'web'
FT = 0.3048

app = FastAPI(title='Takeoff', docs_url='/api/docs', openapi_url='/api/openapi.json')
app.add_middleware(GZipMiddleware, minimum_size=1024)
app.include_router(auth_router)
UNSAFE = {'POST', 'PUT', 'PATCH', 'DELETE'}


@app.middleware('http')
async def same_origin_writes(request, call_next):
    """Refuse cross-site writes. The session cookie is SameSite=Lax already; this also covers
    same-site subdomains and old browsers. Requests without an Origin header (curl, tests) pass."""
    origin = request.headers.get('origin')
    if request.method in UNSAFE and origin and urlsplit(origin).netloc != request.headers.get('host'):
        return JSONResponse({'detail': 'Cross-site request refused'}, status_code=403)
    return await call_next(request)


@app.middleware('http')
async def no_stale_frontend(request, call_next):
    resp = await call_next(request)
    if request.url.path == '/' or request.url.path.startswith('/static/'):
        resp.headers['Cache-Control'] = 'no-cache'   # always revalidate, so updates show without a hard refresh
    return resp


@lru_cache
def load(pid: str) -> dict:
    d = DATA / pid
    if not (d / 'project.json').exists():
        raise HTTPException(404, 'Project not found')
    rd = lambda n: json.loads((d / n).read_text()) if (d / n).exists() else None
    return dict(meta=rd('project.json'), inputs=rd('inputs.json'), issues=rd('issues.json') or [], notebook=rd('notebook.json'), model=rd('model.json'))


def projects() -> list[str]:
    return sorted(p.name for p in DATA.iterdir() if (p / 'project.json').exists())


class CalcRequest(BaseModel):
    overrides: dict[str, float] = {}


def notebook_compare(nb: dict, result: dict) -> dict | None:
    if not nb:
        return None
    kg = {b['name']: b for b in result['beams']}
    rows = []
    for g in nb['groups']:
        ours = sum(kg[n]['kg'] for n in g['beams'] if n in kg)
        ours_dia = {}
        for n in g['beams']:
            if n in kg:
                for d, v in kg[n]['kg_by_dia'].items():
                    ours_dia[d] = ours_dia.get(d, 0) + v
        if 'feet' in g:
            theirs_dia = {int(d): sum(v) * FT * unit_wt(int(d)) for d, v in g['feet'].items()}
            theirs = sum(theirs_dia.values())
        else:
            theirs_dia, theirs = None, g['kg']
        rows.append(dict(g, theirs_kg=theirs, ours_kg=ours, diff_kg=theirs - ours,
                         diff_pct=(theirs - ours) / ours * 100 if ours else None,
                         theirs_by_dia=theirs_dia, ours_by_dia=ours_dia))
    used = {n for g in nb['groups'] for n in g['beams']}
    p1_ours = sum(b['kg'] for b in result['beams'] if b['base'] in {f'B{i}' for i in range(1, 36)} | {'HB29'})
    p2_theirs = sum(r['theirs_kg'] for r in rows if r['page'] == 2)
    p2_ours = sum(r['ours_kg'] for r in rows if r['page'] == 2)
    not_in_nb = [b['name'] for b in result['beams'] if b['name'] not in used and b['base'] not in {f'B{i}' for i in range(1, 36)} | {'HB29'}]
    cb = result['chajja']['kg'] if result.get('chajja') else 0
    theirs_total = nb['page1_total_kg'] + p2_theirs
    ours_total = result['summary']['beam_steel_kg']
    return dict(source=nb['source'], page1_note=nb['page1_note'], rows=rows,
                totals=dict(page1=dict(theirs=nb['page1_total_kg'], ours=p1_ours),
                            page2=dict(theirs=p2_theirs, ours=p2_ours),
                            chajja=dict(theirs=0, ours=cb),
                            not_in_notebook=not_in_nb,
                            beams=dict(theirs=theirs_total, ours=ours_total, diff=theirs_total - ours_total)))


def auto_issues(result: dict, cmp: dict | None) -> list:
    out = []
    l3 = sorted({b['base'] for b in result['beams'] for x in b['bars']
                 if x['role'] == 'Extra top @ support' and 'L/3' in x['steps'][0]['formula']})
    if l3:
        out.append(dict(id='A1', severity='low', kind='Assumed length', auto=True,
                        title=f'Extra top bar length not drawn on {len(l3)} beams (L/3 used)',
                        detail='Where the elevation shows extra top bars without a length, they are taken L/3 into the span from the support face.',
                        affects='Extra top bars', assumption='L/3 + b/2', ask='Confirm the curtailment length for extra top bars.', beams=l3))
    if cmp:
        big = [r for r in cmp['rows'] if r['diff_pct'] is not None and abs(r['diff_pct']) >= 25 and abs(r['diff_kg']) >= 20]
        if big:
            out.append(dict(id='A2', severity='medium', kind='Site count differs', auto=True,
                            title=f'Site QS notebook differs by 25% or more on {len(big)} beam groups',
                            detail='Largest gaps: ' + ', '.join(f"{r['label']} ({r['diff_kg']:+.0f} kg)" for r in sorted(big, key=lambda r: -abs(r['diff_kg']))[:6]) + '.',
                            affects='Steel quantity', assumption='Our count follows the drawn bar lengths',
                            ask='Check these beams on site against the drawing.',
                            beams=sorted({n.split('-')[0] for r in big for n in r['beams']})))
    return out


@app.get('/api/health')
def health():
    return dict(ok=True)


@app.get('/api/projects')
def list_projects():
    out = []
    for pid in projects():
        p = load(pid)
        s = Calc(p['inputs']).run()['summary']
        out.append(dict(id=pid, name=p['meta']['name'], scope=p['meta']['scope'], status=p['meta']['status'],
                        steel_t=s['steel_with_wastage_kg'] / 1000, concrete_m3=s['concrete_m3'],
                        per_sqft=s['total_per_sqft'], issues=len(p['issues'])))
    return out


@app.get('/api/projects/{pid}')
def get_project(pid: str):
    p = load(pid)
    return dict(meta=p['meta'], assumptions=p['inputs']['assumptions'], issues=p['issues'], model=p['model'],
                beams_input=p['inputs']['beams'])


@app.post('/api/projects/{pid}/calculate')
def calculate(pid: str, req: CalcRequest):
    p = load(pid)
    r = Calc(p['inputs'], req.overrides).run()
    cmp = notebook_compare(p['notebook'], r)
    r['notebook'] = cmp
    r['auto_issues'] = auto_issues(r, cmp)
    return r


@app.get('/drawings/{pid}/{name}')
def drawing(pid: str, name: str):
    f = DATA / pid / 'drawings' / name
    if not name.endswith('.svg') or not f.exists() or '..' in name:
        raise HTTPException(404)
    return FileResponse(f, media_type='image/svg+xml', headers={'Cache-Control': 'public, max-age=86400'})


app.mount('/static', StaticFiles(directory=WEB), name='static')


@app.get('/')
def index():
    return FileResponse(WEB / 'index.html')
