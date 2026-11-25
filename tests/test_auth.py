from datetime import timedelta

import pytest
from fastapi import APIRouter, Depends
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.auth import core, mail
from app.auth.deps import project_role
from app.db.models import LoginToken, Project, ProjectMember, User
from app.main import app
from app.settings import get_settings
from conftest import link_token, sign_in

# Test-only routes to exercise the access dependency.
probe = APIRouter()


@probe.get('/api/test/projects/{pid}/view')
def view(p: Project = Depends(project_role('viewer'))):
    return dict(slug=p.slug)


@probe.post('/api/test/projects/{pid}/edit')
def edit(p: Project = Depends(project_role('editor'))):
    return dict(slug=p.slug)


app.include_router(probe)


def test_unknown_email_gets_same_answer_but_no_mail(client):
    r = client.post('/api/auth/link', json=dict(email='stranger@example.com'))
    assert r.status_code == 202 and mail.outbox == []


def test_magic_link_signs_in_and_creates_user(client, session):
    sign_in(client, 'New@Example.com ')
    me = client.get('/api/me').json()
    assert me['email'] == 'new@example.com' and me['projects'] == []
    assert session.scalar(select(User).where(User.email == 'new@example.com'))


def test_domain_allowlist(client):
    sign_in(client, 'engineer@firm.in')
    assert client.get('/api/me').status_code == 200


def test_callback_page_does_not_use_up_the_token(client):
    client.post('/api/auth/link', json=dict(email='new@example.com'))
    tok = link_token()
    page = client.get(f'/auth/callback?token={tok}')
    assert page.status_code == 200 and 'action="/auth/verify"' in page.text and page.headers['cache-control'] == 'no-store'
    # 'no-referrer' would make browsers send `Origin: null` on the form post, which the cross-site check refuses.
    assert page.headers['referrer-policy'] == 'same-origin'
    r = client.post('/auth/verify', data=dict(token=tok), headers={'Origin': 'http://testserver'}, follow_redirects=False)
    assert r.headers['location'] == '/'


def test_token_works_once(client):
    client.post('/api/auth/link', json=dict(email='new@example.com'))
    tok = link_token()
    client.post('/auth/verify', data=dict(token=tok), follow_redirects=False)
    client.cookies.clear()
    r = client.post('/auth/verify', data=dict(token=tok), follow_redirects=False)
    assert r.headers['location'] == '/#signin/expired' and core.COOKIE not in client.cookies


def test_expired_token_refused(client, session):
    client.post('/api/auth/link', json=dict(email='new@example.com'))
    t = session.scalar(select(LoginToken))
    t.expires_at = core.now() - timedelta(seconds=1)
    session.flush()
    r = client.post('/auth/verify', data=dict(token=link_token()), follow_redirects=False)
    assert r.headers['location'] == '/#signin/expired'


def test_rate_limit_per_email(client):
    for _ in range(5):
        assert client.post('/api/auth/link', json=dict(email='new@example.com')).status_code == 202
    assert client.post('/api/auth/link', json=dict(email='NEW@example.com')).status_code == 429


def test_me_needs_sign_in_and_logout_revokes(client):
    assert client.get('/api/me').status_code == 401
    sign_in(client, 'new@example.com')
    raw = client.cookies[core.COOKIE]
    assert client.post('/api/auth/logout').status_code == 204
    client.cookies.set(core.COOKIE, raw)            # a copied cookie stops working too
    assert client.get('/api/me').status_code == 401


def test_bad_email_rejected(client):
    assert client.post('/api/auth/link', json=dict(email='not-an-email')).status_code == 422


@pytest.fixture
def project(session):
    p = Project(slug='site-a', name='Site A', meta_json={})
    session.add(p)
    session.flush()
    return p


def member(session, project, email, role):
    u = User(email=email)
    session.add(u)
    session.flush()
    session.add(ProjectMember(project_id=project.id, user_id=u.id, role=role))
    session.flush()


def test_project_access_by_role(client, session, project):
    member(session, project, 'viewer@example.com', 'viewer')
    sign_in(client, 'viewer@example.com')
    assert client.get('/api/test/projects/site-a/view').json() == dict(slug='site-a')
    assert client.post('/api/test/projects/site-a/edit').status_code == 403
    assert client.get('/api/me').json()['projects'] == [dict(id='site-a', name='Site A', role='viewer')]


def test_editor_and_owner_can_edit(client, session, project):
    member(session, project, 'editor@example.com', 'editor')
    sign_in(client, 'editor@example.com')
    assert client.post('/api/test/projects/site-a/edit').status_code == 200
    owner = User(email='owner@example.com')
    session.add(owner)
    session.flush()
    project.owner_id = owner.id
    session.flush()
    client.cookies.clear()
    sign_in(client, 'owner@example.com')
    assert client.post('/api/test/projects/site-a/edit').status_code == 200


def test_outsiders_see_404_not_403(client, session, project):
    sign_in(client, 'new@example.com')
    assert client.get('/api/test/projects/site-a/view').status_code == 404
    assert client.get('/api/test/projects/nope/view').status_code == 404


def test_signed_out_gets_401_on_project(client, project):
    assert client.get('/api/test/projects/site-a/view').status_code == 401


def test_cross_site_write_refused(client):
    r = client.post('/api/auth/link', json=dict(email='new@example.com'), headers={'Origin': 'https://evil.example'})
    assert r.status_code == 403 and mail.outbox == []
    r = client.post('/api/auth/link', json=dict(email='new@example.com'), headers={'Origin': 'http://testserver'})
    assert r.status_code == 202


def test_no_database_gives_503(monkeypatch):
    monkeypatch.delenv('DATABASE_URL', raising=False)
    monkeypatch.setenv('DATABASE_URL', '')
    get_settings.cache_clear()
    try:
        r = TestClient(app).post('/api/auth/link', json=dict(email='new@example.com'))
        assert r.status_code == 503
    finally:
        get_settings.cache_clear()
