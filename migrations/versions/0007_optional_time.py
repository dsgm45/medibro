"""make appointment_time optional

Revision ID: optional_time_v1
Revises: notifications_v1
Create Date: 2026-08-13

Allows a patient to book an appointment with just a date, leaving the
exact time for the doctor to set when they accept the request.
"""
from alembic import op
import sqlalchemy as sa

revision = 'optional_time_v1'
down_revision = 'notifications_v1'
branch_labels = None
depends_on = None


def upgrade():
    op.alter_column('appointment', 'appointment_time',
                     existing_type=sa.String(length=50),
                     nullable=True)


def downgrade():
    op.alter_column('appointment', 'appointment_time',
                     existing_type=sa.String(length=50),
                     nullable=False)
