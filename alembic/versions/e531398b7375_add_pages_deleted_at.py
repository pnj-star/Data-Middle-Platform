"""add pages deleted_at

Revision ID: e531398b7375
Revises: 5645b070d935
Create Date: 2026-08-14 15:11:14.012032

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e531398b7375'
down_revision: Union[str, Sequence[str], None] = '5645b070d935'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add soft-delete marker to pages (ROADMAP P3-T2)."""
    op.add_column("pages", sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    """Remove the soft-delete marker."""
    op.drop_column("pages", "deleted_at")
