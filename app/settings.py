"""Runtime settings, read from the environment (or a local `.env`).

Without `database_url` the app starts, but every page that needs data answers 503 and `/api/health` fails.
"""
from __future__ import annotations
from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file='.env', extra='ignore')

    database_url: str | None = None          # postgresql+psycopg://user:pass@host:5432/db
    redis_url: str | None = None             # redis://localhost:6379/0 (worker, M1)
    s3_endpoint_url: str | None = None       # MinIO locally, https://blr1.digitaloceanspaces.com on DO
    s3_bucket: str | None = None
    s3_access_key: str | None = None
    s3_secret_key: str | None = None
    secret_key: str | None = None            # reserved (CSRF tokens later); sessions are opaque DB tokens

    # Auth (magic link)
    app_url: str = 'http://localhost:8000'   # used to build sign-in links
    cookie_secure: bool = True               # False only for local http
    session_days: int = 30
    login_link_minutes: int = 15
    login_links_per_15min: int = 5          # per email
    # Beta is invite-only: existing users can always sign in; new emails must match this list
    # (comma-separated emails or @domains), e.g. "a@x.com,@mycompany.in".
    signup_allowlist: str = ''

    # Email: "console" logs the link (development); "smtp" sends it (Postmark, Resend, SES all speak SMTP)
    mail_backend: str = 'console'
    mail_from: str = 'Takeoff <no-reply@localhost>'
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_user: str | None = None
    smtp_password: str | None = None

    @field_validator('database_url')
    @classmethod
    def psycopg_driver(cls, v: str | None) -> str | None:
        """DigitalOcean (and most hosts) give `postgresql://…` or `postgres://…`; SQLAlchemy would pick psycopg2 for
        those, which isn't installed. Use psycopg 3. Query options such as `?sslmode=require` are kept."""
        if not v:
            return None
        for scheme in ('postgres://', 'postgresql://'):
            if v.startswith(scheme):
                return 'postgresql+psycopg://' + v[len(scheme):]
        return v


@lru_cache
def get_settings() -> Settings:
    return Settings()
