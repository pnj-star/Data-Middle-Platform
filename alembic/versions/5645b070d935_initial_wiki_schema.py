"""initial wiki schema

Revision ID: 5645b070d935
Revises:
Create Date: 2026-08-14 10:09:56.644247

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '5645b070d935'
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema.

    Table creation is hand-reordered topologically: Alembic's autogenerate emits
    tables alphabetically, which fails on Postgres because referenced tables must
    already exist (users/roles are referenced by nearly everything). The one
    genuinely circular FK — pages.current_revision_id → revisions.id — is added
    last via ALTER (ROADMAP P0-T3).

    CAUTION for future migrations: do NOT rely on autogenerate to order tables
    that participate in the pages↔revisions cycle. If a new table references
    either side of the cycle, either create it after both exist, or split its FK
    into a trailing ALTER as done here.
    """
    # ── Leaf tables (referenced by all others) ──
    op.create_table('roles',
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('name', sa.String(length=64), nullable=False),
    sa.Column('scope', sa.String(length=16), nullable=False),
    sa.Column('description', sa.String(length=255), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('name')
    )
    op.create_table('users',
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('username', sa.String(length=64), nullable=False),
    sa.Column('email', sa.String(length=255), nullable=True),
    sa.Column('password_hash', sa.String(length=255), nullable=True),
    sa.Column('provider', sa.String(length=32), nullable=False),
    sa.Column('external_id', sa.String(length=128), nullable=True),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('provider', 'external_id'),
    sa.UniqueConstraint('username')
    )
    op.create_table('spaces',
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('slug', sa.String(length=64), nullable=False),
    sa.Column('name', sa.String(length=128), nullable=False),
    sa.Column('description', sa.String(length=512), nullable=True),
    sa.Column('owner_user_id', sa.String(length=32), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['owner_user_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('slug')
    )

    # ── Pages (current_revision_id FK deferred to the end, circular with revisions) ──
    op.create_table('pages',
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('space_id', sa.String(length=32), nullable=False),
    sa.Column('parent_page_id', sa.String(length=32), nullable=True),
    sa.Column('title', sa.String(length=512), nullable=False),
    sa.Column('slug', sa.String(length=512), nullable=True),
    sa.Column('content_type', sa.String(length=16), nullable=False),
    sa.Column('current_revision_id', sa.String(length=32), nullable=True),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('source_file_id', sa.String(length=32), nullable=True),
    sa.Column('source_file_name', sa.String(length=255), nullable=True),
    sa.Column('source_file_extension', sa.String(length=32), nullable=True),
    sa.Column('created_by', sa.String(length=32), nullable=True),
    sa.Column('updated_by', sa.String(length=32), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['created_by'], ['users.id'], ),
    sa.ForeignKeyConstraint(['parent_page_id'], ['pages.id'], ),
    sa.ForeignKeyConstraint(['space_id'], ['spaces.id'], ),
    sa.ForeignKeyConstraint(['updated_by'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('space_id', 'slug')
    )
    op.create_index('ix_pages_source_file_id', 'pages', ['source_file_id'], unique=False)
    op.create_index(op.f('ix_pages_space_id'), 'pages', ['space_id'], unique=False)
    op.create_index('ix_pages_status', 'pages', ['status'], unique=False)
    op.create_table('revisions',
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('page_id', sa.String(length=32), nullable=False),
    sa.Column('revision_id', sa.Integer(), nullable=False),
    sa.Column('content_md', sa.Text(), nullable=False),
    sa.Column('editor_user_id', sa.String(length=32), nullable=True),
    sa.Column('note', sa.String(length=512), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['editor_user_id'], ['users.id'], ),
    sa.ForeignKeyConstraint(['page_id'], ['pages.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('page_id', 'revision_id')
    )
    op.create_index(op.f('ix_revisions_page_id'), 'revisions', ['page_id'], unique=False)

    # ── Tables referencing pages / users ──
    op.create_table('attachments',
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('page_id', sa.String(length=32), nullable=False),
    sa.Column('original_name', sa.String(length=512), nullable=False),
    sa.Column('stored_path', sa.String(length=1024), nullable=False),
    sa.Column('size', sa.Integer(), nullable=False),
    sa.Column('mime_type', sa.String(length=128), nullable=True),
    sa.Column('created_by', sa.String(length=32), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['created_by'], ['users.id'], ),
    sa.ForeignKeyConstraint(['page_id'], ['pages.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_attachments_page_id'), 'attachments', ['page_id'], unique=False)
    op.create_table('audit_logs',
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('user_id', sa.String(length=32), nullable=True),
    sa.Column('action', sa.String(length=64), nullable=False),
    sa.Column('target_type', sa.String(length=64), nullable=False),
    sa.Column('target_id', sa.String(length=64), nullable=True),
    sa.Column('detail', sa.JSON(), nullable=True),
    sa.Column('ip', sa.String(length=64), nullable=True),
    sa.Column('result', sa.String(length=16), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_audit_created', 'audit_logs', ['created_at'], unique=False)
    op.create_index('ix_audit_target', 'audit_logs', ['target_type', 'target_id'], unique=False)
    op.create_index('ix_audit_user', 'audit_logs', ['user_id'], unique=False)
    op.create_table('comments',
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('page_id', sa.String(length=32), nullable=False),
    sa.Column('revision_id', sa.Integer(), nullable=True),
    sa.Column('user_id', sa.String(length=32), nullable=True),
    sa.Column('parent_comment_id', sa.String(length=32), nullable=True),
    sa.Column('content', sa.Text(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['page_id'], ['pages.id'], ),
    sa.ForeignKeyConstraint(['parent_comment_id'], ['comments.id'], ),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_comments_page_id'), 'comments', ['page_id'], unique=False)
    op.create_table('links',
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('source_page_id', sa.String(length=32), nullable=False),
    sa.Column('target_page_id', sa.String(length=32), nullable=True),
    sa.Column('target_slug', sa.String(length=512), nullable=True),
    sa.Column('label', sa.String(length=255), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['source_page_id'], ['pages.id'], ),
    sa.ForeignKeyConstraint(['target_page_id'], ['pages.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_links_source_page_id'), 'links', ['source_page_id'], unique=False)
    op.create_index('ix_links_target', 'links', ['target_page_id', 'target_slug'], unique=False)
    op.create_table('space_members',
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('space_id', sa.String(length=32), nullable=False),
    sa.Column('user_id', sa.String(length=32), nullable=False),
    sa.Column('role_id', sa.String(length=32), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['role_id'], ['roles.id'], ),
    sa.ForeignKeyConstraint(['space_id'], ['spaces.id'], ),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('space_id', 'user_id')
    )
    op.create_index(op.f('ix_space_members_space_id'), 'space_members', ['space_id'], unique=False)
    op.create_index(op.f('ix_space_members_user_id'), 'space_members', ['user_id'], unique=False)

    # ── Circular FK added last ──
    op.create_foreign_key(
        'fk_pages_current_revision_id', 'pages', 'revisions',
        ['current_revision_id'], ['id'],
    )
    # ### end Alembic commands ###


def downgrade() -> None:
    """Downgrade schema."""
    # Drop the deferred circular FK first, then tables in reverse dependency order.
    op.drop_constraint('fk_pages_current_revision_id', 'pages', type_='foreignkey')
    op.drop_index(op.f('ix_space_members_user_id'), table_name='space_members')
    op.drop_index(op.f('ix_space_members_space_id'), table_name='space_members')
    op.drop_table('space_members')
    op.drop_index('ix_links_target', table_name='links')
    op.drop_index(op.f('ix_links_source_page_id'), table_name='links')
    op.drop_table('links')
    op.drop_index(op.f('ix_comments_page_id'), table_name='comments')
    op.drop_table('comments')
    op.drop_index('ix_audit_user', table_name='audit_logs')
    op.drop_index('ix_audit_target', table_name='audit_logs')
    op.drop_index('ix_audit_created', table_name='audit_logs')
    op.drop_table('audit_logs')
    op.drop_index(op.f('ix_attachments_page_id'), table_name='attachments')
    op.drop_table('attachments')
    op.drop_index(op.f('ix_revisions_page_id'), table_name='revisions')
    op.drop_table('revisions')
    op.drop_index('ix_pages_status', table_name='pages')
    op.drop_index(op.f('ix_pages_space_id'), table_name='pages')
    op.drop_index('ix_pages_source_file_id', table_name='pages')
    op.drop_table('pages')
    op.drop_table('spaces')
    op.drop_table('users')
    op.drop_table('roles')
    # ### end Alembic commands ###
