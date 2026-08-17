"""add simple_mode to user

Revision ID: simple_mode_v1
Revises: prescribed_medicine_v1
Create Date: 2026-08-17

Optional accessibility mode for patients - larger sidebar nav and page
headers, toggled from Profile, off by default. Evidence-backed
motivation: WhatsApp-familiar, larger-target interaction patterns
measurably help adoption among older Indian users (IIT Guwahati study).
"""
from alembic import op
import sqlalchemy as sa

revision = 'simple_mode_v1'
down_revision = 'prescribed_medicine_v1'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('user', sa.Column('simple_mode', sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade():
    op.drop_column('user', 'simple_mode')
