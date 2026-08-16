"""add patient_note to appointment

Revision ID: patient_note_v1
Revises: notification_read_at_v1
Create Date: 2026-08-16

Lets patients leave a private note on a completed visit, visible to
them, the treating doctor, and admin (for oversight) - separate from
visit_notes, which is the doctor's own record of the visit.
"""
from alembic import op
import sqlalchemy as sa

revision = 'patient_note_v1'
down_revision = 'notification_read_at_v1'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('appointment', sa.Column('patient_note', sa.Text(), nullable=True))


def downgrade():
    op.drop_column('appointment', 'patient_note')
