"""add read_at to notification

Revision ID: notification_read_at_v1
Revises: specialty_match_v1
Create Date: 2026-08-16

Supports the notification retention policy: read notifications are
purged one day after being marked read (checked opportunistically on
notifications page load, since there's no background scheduler), and
the new "Clear All" button can delete everything currently read
immediately instead of waiting.
"""
from alembic import op
import sqlalchemy as sa

revision = 'notification_read_at_v1'
down_revision = 'specialty_match_v1'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('notification', sa.Column('read_at', sa.DateTime(), nullable=True))


def downgrade():
    op.drop_column('notification', 'read_at')
