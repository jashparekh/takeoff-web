"""Tokens and sessions. Raw tokens only ever live in the email link and the cookie; the DB has their SHA-256."""
from __future__ import annotations
import hashlib
import secrets
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db.models import AuthSession, LoginToken, User
from ..settings import get_settings

COOKIE = 'takeoff_session'


def now() -> datetime:
    return datetime.now(UTC)


def digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def norm_email(email: str) -> str:
    return email.strip().lower()


def find_user(db: Session, email: str) -> User | None:
    return db.scalar(select(User).where(func.lower(User.email) == norm_email(email)))


def may_sign_in(db: Session, email: str) -> bool:
    """Existing users, or new emails matching SIGNUP_ALLOWLIST (beta is invite-only)."""
    email = norm_email(email)
    if find_user(db, email):
        return True
    allow = [x.strip().lower() for x in get_settings().signup_allowlist.split(',') if x.strip()]
    return any(email == a or (a.startswith('@') and email.endswith(a)) for a in allow)


def recent_links(db: Session, email: str) -> int:
    since = now() - timedelta(minutes=15)
    return db.scalar(select(func.count()).select_from(LoginToken)
                     .where(LoginToken.email == norm_email(email), LoginToken.created_at >= since)) or 0


def new_login_token(db: Session, email: str) -> str:
    token = secrets.token_urlsafe(32)
    db.add(LoginToken(email=norm_email(email), token_hash=digest(token),
                      expires_at=now() + timedelta(minutes=get_settings().login_link_minutes)))
    db.flush()
    return token


def use_login_token(db: Session, token: str) -> User | None:
    """Consume a sign-in token (once). Creates the user on first sign-in."""
    t = db.scalar(select(LoginToken).where(LoginToken.token_hash == digest(token)).with_for_update())
    if not t or t.used_at or t.expires_at <= now():
        return None
    t.used_at = now()
    user = find_user(db, t.email)
    if not user:
        if not may_sign_in(db, t.email):      # allowlist changed since the link was sent
            return None
        user = User(email=t.email)
        db.add(user)
        db.flush()
    return user


def new_session(db: Session, user: User) -> tuple[str, datetime]:
    token = secrets.token_urlsafe(32)
    expires = now() + timedelta(days=get_settings().session_days)
    db.add(AuthSession(user_id=user.id, token_hash=digest(token), expires_at=expires))
    db.flush()
    return token, expires


def session_user(db: Session, token: str | None) -> User | None:
    if not token:
        return None
    s = db.scalar(select(AuthSession).where(AuthSession.token_hash == digest(token)))
    if not s or s.revoked_at or s.expires_at <= now():
        return None
    return s.user


def revoke(db: Session, token: str | None) -> None:
    if token:
        s = db.scalar(select(AuthSession).where(AuthSession.token_hash == digest(token)))
        if s and not s.revoked_at:
            s.revoked_at = now()
