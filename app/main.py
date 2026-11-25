"""Takeoff web API: serves projects, drawings and explained calculations."""
from __future__ import annotations
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .api.projects import router as projects_router
from .auth.routes import router as auth_router

WEB = Path(__file__).parent.parent / 'web'

app = FastAPI(title='Takeoff', docs_url='/api/docs', openapi_url='/api/openapi.json')
app.add_middleware(GZipMiddleware, minimum_size=1024)
app.include_router(auth_router)
app.include_router(projects_router)
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


@app.get('/api/health')
def health():
    return dict(ok=True)


app.mount('/static', StaticFiles(directory=WEB), name='static')


@app.get('/')
def index():
    return FileResponse(WEB / 'index.html')
