"""Database fixtures. DB tests need TEST_DATABASE_URL (a Postgres database they may wipe); without it they are
skipped, and the engine tests still run. CI sets it. Locally: `docker compose up -d db`, then
TEST_DATABASE_URL=postgresql+psycopg://takeoff:takeoff@localhost:5432/takeoff_test (create that database once)."""
from __future__ import annotations
import os
from pathlib import Path

import re

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.auth import core, mail
from app.db.session import get_db
from app.main import app
from app.settings import get_settings

ROOT = Path(__file__).parents[1]
URL = os.environ.get('TEST_DATABASE_URL')


def alembic_cfg(url: str) -> Config:
    cfg = Config(str(ROOT / 'alembic.ini'))
    cfg.attributes['url'] = url
    return cfg


@pytest.fixture(scope='session')
def db_engine():
    if not URL:
        pytest.skip('TEST_DATABASE_URL not set')
    cfg = alembic_cfg(URL)
    command.downgrade(cfg, 'base')       # clean slate, and proves downgrade works
    command.upgrade(cfg, 'head')
    eng = create_engine(URL)
    yield eng
    eng.dispose()


@pytest.fixture
def session(db_engine):
    """A session whose work is rolled back after each test (commits become savepoints)."""
    with db_engine.connect() as conn:
        tx = conn.begin()
        s = Session(bind=conn, join_transaction_mode='create_savepoint', expire_on_commit=False)
        try:
            yield s
        finally:
            s.close()
            tx.rollback()


@pytest.fixture
def client(session, monkeypatch):
    monkeypatch.setenv('COOKIE_SECURE', 'false')
    monkeypatch.setenv('SIGNUP_ALLOWLIST', 'new@example.com,@firm.in')
    monkeypatch.setenv('MAIL_BACKEND', 'console')
    get_settings.cache_clear()
    mail.outbox.clear()
    app.dependency_overrides[get_db] = lambda: session
    yield TestClient(app)
    app.dependency_overrides.clear()
    get_settings.cache_clear()


def link_token() -> str:
    body = mail.outbox[-1].get_content()
    return re.search(r'token=([\w\-%]+)', body).group(1)


def sign_in(client, email: str) -> None:
    assert client.post('/api/auth/link', json=dict(email=email)).status_code == 202
    r = client.post('/auth/verify', data=dict(token=link_token()), follow_redirects=False)
    assert r.status_code == 303 and r.headers['location'] == '/'
    assert core.COOKIE in client.cookies
