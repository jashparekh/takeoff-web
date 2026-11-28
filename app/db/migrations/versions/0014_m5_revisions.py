"""m5 revisions: a sheet superseded by a later revision of the same drawing

Revision ID: 0014
Revises: 0013
Create Date: 2026-11-28 12:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '0014'
down_revision: Union[str, Sequence[str], None] = '0013'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('sheets', sa.Column('superseded_by', sa.Uuid(), nullable=True))
    op.create_foreign_key('fk_sheets_superseded_by', 'sheets', 'sheets', ['superseded_by'], ['id'], ondelete='SET NULL')


def downgrade() -> None:
    op.drop_constraint('fk_sheets_superseded_by', 'sheets', type_='foreignkey')
    op.drop_column('sheets', 'superseded_by')
