"""Settings and checks the deploy depends on."""
import pytest
from fastapi.testclient import TestClient

from app.db.session import get_engine
from app.main import app
from app.settings import Settings, get_settings


@pytest.mark.parametrize('given, used', [
    ('postgresql://u:p@db.example:25060/takeoff?sslmode=require',
     'postgresql+psycopg://u:p@db.example:25060/takeoff?sslmode=require'),
    ('postgres://u:p@h/d', 'postgresql+psycopg://u:p@h/d'),
    ('postgresql+psycopg://u:p@h/d', 'postgresql+psycopg://u:p@h/d'),
    ('', None),
])
def test_database_url_uses_psycopg3(given, used):
    assert Settings(database_url=given, _env_file=None).database_url == used


@pytest.fixture
def env(monkeypatch):
    def set_env(**kw):
        for k, v in kw.items():
            monkeypatch.setenv(k, v)
        get_settings.cache_clear()
        get_engine.cache_clear()
    yield set_env
    get_settings.cache_clear()
    get_engine.cache_clear()


def test_health_fails_without_database(env):
    env(DATABASE_URL='')
    r = TestClient(app).get('/api/health')
    assert r.status_code == 503 and r.json() == dict(ok=False, db='not configured')


def test_health_fails_when_database_unreachable(env):
    env(DATABASE_URL='postgresql://u:p@127.0.0.1:9/none')
    r = TestClient(app).get('/api/health')
    assert r.status_code == 503 and r.json()['db'] == 'unreachable'


def test_health_ok_with_database(env, db_engine):
    env(DATABASE_URL=db_engine.url.render_as_string(hide_password=False))
    assert TestClient(app).get('/api/health').json() == dict(ok=True, db='ok')


def test_origin_of_app_url_is_trusted(env):
    # Behind a proxy the Host header may not be the public name; APP_URL's origin is always accepted.
    env(APP_URL='https://takeoff.example.app', DATABASE_URL='')
    c = TestClient(app)
    assert c.post('/api/auth/logout', headers={'Origin': 'https://takeoff.example.app'}).status_code == 503
    assert c.post('/api/auth/logout', headers={'Origin': 'https://evil.example'}).status_code == 403
