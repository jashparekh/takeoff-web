"""m5 slab panels: panel members with their bars

Revision ID: 0013
Revises: 0012
Create Date: 2026-11-28 10:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = '0013'
down_revision: Union[str, Sequence[str], None] = '0012'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('members', sa.Column('data', postgresql.JSONB(), nullable=True))
    op.add_column('sheets', sa.Column('slab_version', sa.Integer(), nullable=True))
    op.drop_constraint('kind_valid', 'members', type_='check')
    op.create_check_constraint('kind_valid', 'members', "kind IN ('beam', 'column', 'slab_panel', 'panel')")


def downgrade() -> None:
    op.execute("DELETE FROM members WHERE kind = 'panel'")
    op.drop_constraint('kind_valid', 'members', type_='check')
    op.create_check_constraint('kind_valid', 'members', "kind IN ('beam', 'column', 'slab_panel')")
    op.drop_column('members', 'data')
    op.drop_column('sheets', 'slab_version')
