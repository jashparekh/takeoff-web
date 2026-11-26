"""Takeoff web API: serves projects, drawings and explained calculations."""
from __future__ import annotations
import logging
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from .api.projects import router as projects_router
from .api.review import router as review_router
from .api.values import router as values_router
from .api.uploads import MAX_FILES_PER_REQUEST, router as uploads_router
from .auth.routes import router as auth_router
from .db.session import get_engine
from .settings import get_settings

WEB = Path(__file__).parent.parent / 'web'
log = logging.getLogger('takeoff')

app = FastAPI(title='Takeoff', docs_url='/api/docs', openapi_url='/api/openapi.json')
app.add_middleware(GZipMiddleware, minimum_size=1024)
app.include_router(auth_router)
app.include_router(projects_router)
app.include_router(uploads_router)
app.include_router(review_router)
app.include_router(values_router)
UNSAFE = {'POST', 'PUT', 'PATCH', 'DELETE'}


@app.middleware('http')
async def same_origin_writes(request, call_next):
    """Refuse cross-site writes. The session cookie is SameSite=Lax already; this also covers
    same-site subdomains and old browsers. Requests without an Origin header (curl, tests) pass."""
    origin = request.headers.get('origin')
    allowed = {request.headers.get('host'), urlsplit(get_settings().app_url).netloc}
    if request.method in UNSAFE and origin and urlsplit(origin).netloc not in allowed:
        return JSONResponse({'detail': 'Cross-site request refused'}, status_code=403)
    return await call_next(request)


@app.middleware('http')
async def body_size_limit(request, call_next):
    """Refuse oversize requests before the multipart body is read and spooled to disk."""
    limit = (get_settings().max_upload_mb * MAX_FILES_PER_REQUEST + 1) << 20
    n = request.headers.get('content-length')
    if n and n.isdigit() and int(n) > limit:
        return JSONResponse({'detail': f'Upload too large (at most {get_settings().max_upload_mb} MB per file)'},
                            status_code=413)
    return await call_next(request)


@app.middleware('http')
async def no_stale_frontend(request, call_next):
    resp = await call_next(request)
    if request.url.path == '/' or request.url.path.startswith('/static/'):
        resp.headers['Cache-Control'] = 'no-cache'   # always revalidate, so updates show without a hard refresh
    return resp


@app.get('/api/health')
def health():
    """Used by the App Platform health check: fails (503) when the database can't be reached."""
    if not get_settings().database_url:
        return JSONResponse(dict(ok=False, db='not configured'), status_code=503)
    try:
        with get_engine().connect() as conn:
            conn.execute(text('select 1'))
    except Exception as e:                       # noqa: BLE001 — any failure means unhealthy
        log.warning('health check: database unreachable: %s', e.__class__.__name__)
        return JSONResponse(dict(ok=False, db='unreachable'), status_code=503)
    return dict(ok=True, db='ok')


app.mount('/static', StaticFiles(directory=WEB), name='static')


@app.get('/')
def index():
    return FileResponse(WEB / 'index.html')
