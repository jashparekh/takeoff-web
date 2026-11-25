"""Sign-in routes.

1. POST /api/auth/link {email}  → always 202 (never says whether an account exists); emails a one-time link.
2. GET  /auth/callback?token=…  → a small page with a "Sign in" button (link scanners that prefetch GETs
   don't use up the token); the button POSTs to
3. POST /auth/verify (form)     → consumes the token, sets the session cookie, redirects to /.
"""
from __future__ import annotations
import html
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Cookie, Form, HTTPException, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, EmailStr
from sqlalchemy import select

from ..db.models import Project, ProjectMember
from ..settings import get_settings
from . import core, mail
from .deps import CurrentUser, Db, role_on

router = APIRouter()


class LinkRequest(BaseModel):
    email: EmailStr


@router.post('/api/auth/link', status_code=202)
def request_link(req: LinkRequest, db: Db):
    s = get_settings()
    if core.recent_links(db, req.email) >= s.login_links_per_15min:
        raise HTTPException(429, 'Too many sign-in links requested; try again in 15 minutes')
    if core.may_sign_in(db, req.email):
        token = core.new_login_token(db, req.email)
        link = f"{s.app_url.rstrip('/')}/auth/callback?token={quote(token)}"
        mail.send(mail.sign_in_message(core.norm_email(req.email), link, s.login_link_minutes))
    return dict(ok=True, message='If this email can sign in, a link is on its way.')


PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Sign in · Takeoff</title>
<style>body{{font:16px system-ui,sans-serif;display:grid;place-items:center;min-height:90vh;margin:0}}
form{{text-align:center}}button{{font:inherit;padding:.6em 1.6em;cursor:pointer}}</style></head>
<body><form method="post" action="/auth/verify"><p>Sign in to Takeoff</p>
<input type="hidden" name="token" value="{token}"><button type="submit">Sign in</button></form></body></html>"""


@router.get('/auth/callback', response_class=HTMLResponse)
def callback(token: str):
    return HTMLResponse(PAGE.format(token=html.escape(token, quote=True)),
                        headers={'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer'})


@router.post('/auth/verify')
def verify(token: Annotated[str, Form()], db: Db):
    user = core.use_login_token(db, token)
    if not user:
        return RedirectResponse('/#signin/expired', status_code=303)
    raw, expires = core.new_session(db, user)
    r = RedirectResponse('/', status_code=303)
    r.set_cookie(core.COOKIE, raw, expires=expires, httponly=True, secure=get_settings().cookie_secure,
                 samesite='lax', path='/')
    return r


@router.post('/api/auth/logout', status_code=204)
def logout(db: Db, response: Response, token: Annotated[str | None, Cookie(alias=core.COOKIE)] = None):
    core.revoke(db, token)
    response.delete_cookie(core.COOKIE, path='/')


@router.get('/api/me')
def me(user: CurrentUser, db: Db):
    """The signed-in user and the projects they can open (with their role)."""
    member = select(ProjectMember.project_id).where(ProjectMember.user_id == user.id)
    ps = db.scalars(select(Project).where((Project.owner_id == user.id) | Project.id.in_(member))
                    .order_by(Project.name)).all()
    return dict(id=str(user.id), email=user.email, name=user.name,
                projects=[dict(id=p.slug, name=p.name, role=role_on(db, p, user)) for p in ps])
