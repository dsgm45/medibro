"""add patient_data_access_log table

Revision ID: access_log_v1
Revises: patient_note_v1
Create Date: 2026-08-16

Patient-visible access transparency: every time a doctor or admin views
a patient's health records, one entry is logged and shown directly to
that patient (not just an internal admin-only audit trail).
"""
from alembic import op
import sqlalchemy as sa

revision = 'access_log_v1'
down_revision = 'patient_note_v1'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'patient_data_access_log',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('patient_id', sa.Integer(), nullable=False),
        sa.Column('viewer_id', sa.Integer(), nullable=False),
        sa.Column('viewer_role', sa.String(length=20), nullable=False),
        sa.Column('viewed_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['patient_id'], ['user.id']),
        sa.ForeignKeyConstraint(['viewer_id'], ['user.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_patient_data_access_log_patient_id', 'patient_data_access_log', ['patient_id'])


def downgrade():
    op.drop_index('ix_patient_data_access_log_patient_id', table_name='patient_data_access_log')
    op.drop_table('patient_data_access_log')
