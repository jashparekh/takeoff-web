"""m3 layout schedule: which schedule reading a layout was read with

Revision ID: 0012
Revises: 0011
Create Date: 2026-11-26 10:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '0012'
down_revision: Union[str, Sequence[str], None] = '0011'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('sheets', sa.Column('layout_schedule', sa.String(length=16), nullable=True))


def downgrade() -> None:
    op.drop_column('sheets', 'layout_schedule')
