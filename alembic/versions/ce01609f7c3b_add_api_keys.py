"""add api_keys

Revision ID: ce01609f7c3b
Revises: e531398b7375
Create Date: 2026-08-16 20:58:12.579989

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'ce01609f7c3b'
down_revision: Union[str, Sequence[str], None] = 'e531398b7375'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create scoped api_keys table (ROADMAP P4-T7)."""
    op.create_table("api_keys",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("key_hash", sa.String(length=64), nullable=False),
        sa.Column("space_id", sa.String(length=32), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("created_by", sa.String(length=32), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ),
        sa.ForeignKeyConstraint(["space_id"], ["spaces.id"], ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("key_hash")
    )
    op.create_index(op.f("ix_api_keys_space_id"), "api_keys", ["space_id"], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f("ix_api_keys_space_id"), table_name="api_keys")
    op.drop_table("api_keys")
