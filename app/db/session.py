"""Engine and session factory. Sync SQLAlchemy is enough for v1; the API runs it in FastAPI's threadpool."""
from __future__ import annotations
from functools import lru_cache

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from ..settings import get_settings


@lru_cache
def get_engine(url: str | None = None) -> Engine:
    url = url or get_settings().database_url
    if not url:
        raise RuntimeError('DATABASE_URL is not set (see .env.example)')
    return create_engine(url, pool_pre_ping=True)


def session_factory(url: str | None = None) -> sessionmaker[Session]:
    return sessionmaker(get_engine(url), expire_on_commit=False)


def get_db():
    """FastAPI dependency: one session per request, committed on success, rolled back on error."""
    from fastapi import HTTPException
    if not get_settings().database_url:
        raise HTTPException(503, 'Database not configured')
    with session_factory()() as s:
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
