"""Manage users and project access from the command line (beta has no invite UI yet).

    python -m tools.users add jash@example.com --name "Jash" --project worklounge --role owner
    python -m tools.users grant someone@example.com worklounge viewer
    python -m tools.users revoke someone@example.com worklounge      # remove access, keep the user
    python -m tools.users list
"""
from __future__ import annotations
import argparse
import sys

from sqlalchemy import select

from app.auth.core import find_user, norm_email
from app.db.models import ROLES, Project, ProjectMember, User
from app.db.session import session_factory


def project(db, slug: str) -> Project:
    p = db.scalar(select(Project).where(Project.slug == slug))
    if not p:
        raise SystemExit(f'No project {slug!r}')
    return p


def grant(db, user: User, p: Project, role: str) -> None:
    if role == 'owner' and p.owner_id is None:
        p.owner_id = user.id
    m = db.get(ProjectMember, (p.id, user.id))
    if m:
        m.role = role
    else:
        db.add(ProjectMember(project_id=p.id, user_id=user.id, role=role))


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest='cmd', required=True)
    a = sub.add_parser('add'); a.add_argument('email'); a.add_argument('--name')
    a.add_argument('--project'); a.add_argument('--role', choices=ROLES, default='viewer')
    g = sub.add_parser('grant'); g.add_argument('email'); g.add_argument('project'); g.add_argument('role', choices=ROLES)
    r = sub.add_parser('revoke'); r.add_argument('email'); r.add_argument('project')
    sub.add_parser('list')
    args = ap.parse_args(argv)

    with session_factory()() as db, db.begin():
        if args.cmd == 'list':
            for u in db.scalars(select(User).order_by(User.email)):
                roles = db.execute(select(Project.slug, ProjectMember.role).join(ProjectMember)
                                   .where(ProjectMember.user_id == u.id)).all()
                print(u.email, u.name or '', ', '.join(f'{s}:{r}' for s, r in roles))
            return
        user = find_user(db, args.email)
        if args.cmd == 'add':
            if not user:
                user = User(email=norm_email(args.email), name=args.name)
                db.add(user); db.flush()
            if args.project:
                grant(db, user, project(db, args.project), args.role)
        elif not user:
            raise SystemExit(f'No user {args.email!r}')
        elif args.cmd == 'grant':
            grant(db, user, project(db, args.project), args.role)
        elif args.cmd == 'revoke':
            p = project(db, args.project)
            m = db.get(ProjectMember, (p.id, user.id))
            if m: db.delete(m)
            if p.owner_id == user.id: p.owner_id = None
    print('done')


if __name__ == '__main__':
    sys.exit(main())
