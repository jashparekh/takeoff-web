"""Runtime settings, read from the environment (or a local `.env`).

Everything is optional so the pilot still runs with no database: `database_url` unset means the
file-backed pilot only (as before).
"""
from __future__ import annotations
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file='.env', extra='ignore')

    database_url: str | None = None          # postgresql+psycopg://user:pass@host:5432/db
    redis_url: str | None = None             # redis://localhost:6379/0 (worker, M1)
    s3_endpoint_url: str | None = None       # MinIO locally, https://blr1.digitaloceanspaces.com on DO
    s3_bucket: str | None = None
    s3_access_key: str | None = None
    s3_secret_key: str | None = None
    secret_key: str | None = None            # session cookie signing (auth, M0 PR 2)


@lru_cache
def get_settings() -> Settings:
    return Settings()
