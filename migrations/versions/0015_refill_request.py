"""add refill_request table

Revision ID: refill_request_v1
Revises: access_log_v1
Create Date: 2026-08-17

Lets patients request a medicine refill from a doctor they've actually
had appointments with (medicines are patient self-tracked, not linked
to a prescribing doctor, so the patient picks who to send it to).
Approving automatically extends the medicine's end date by 30 days.
"""
from alembic import op
import sqlalchemy as sa

revision = 'refill_request_v1'
down_revision = 'access_log_v1'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'refill_request',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('medicine_id', sa.Integer(), nullable=False),
        sa.Column('patient_id', sa.Integer(), nullable=False),
        sa.Column('doctor_id', sa.Integer(), nullable=False),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('note', sa.Text(), nullable=True),
        sa.Column('doctor_response', sa.Text(), nullable=True),
        sa.Column('requested_at', sa.DateTime(), nullable=True),
        sa.Column('responded_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['medicine_id'], ['medicine.id']),
        sa.ForeignKeyConstraint(['patient_id'], ['user.id']),
        sa.ForeignKeyConstraint(['doctor_id'], ['user.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_refill_request_medicine_id', 'refill_request', ['medicine_id'])
    op.create_index('ix_refill_request_patient_id', 'refill_request', ['patient_id'])
    op.create_index('ix_refill_request_doctor_id', 'refill_request', ['doctor_id'])


def downgrade():
    op.drop_index('ix_refill_request_doctor_id', table_name='refill_request')
    op.drop_index('ix_refill_request_patient_id', table_name='refill_request')
    op.drop_index('ix_refill_request_medicine_id', table_name='refill_request')
    op.drop_table('refill_request')
