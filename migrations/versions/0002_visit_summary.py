"""add visit summary fields to appointment

Revision ID: visit_summary_v1
Revises: baseline_v1
Create Date: 2026-08-09

Adds diagnosis, visit_notes, and completed_at to the appointment table so
doctors can capture what happened during a visit when marking it complete.
"""
from alembic import op
import sqlalchemy as sa

revision = 'visit_summary_v1'
down_revision = 'baseline_v1'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('appointment', sa.Column('diagnosis', sa.Text(), nullable=True))
    op.add_column('appointment', sa.Column('visit_notes', sa.Text(), nullable=True))
    op.add_column('appointment', sa.Column('completed_at', sa.DateTime(), nullable=True))


def downgrade():
    op.drop_column('appointment', 'completed_at')
    op.drop_column('appointment', 'visit_notes')
    op.drop_column('appointment', 'diagnosis')
