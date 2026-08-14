"""add account deletion request table

Revision ID: account_deletion_v1
Revises: documents_v1
Create Date: 2026-08-14

Tracks a patient's self-service account deletion request: a 7-day
scheduled date fixed at request time, requiring explicit admin approval
before the deletion can actually execute - never on the schedule alone.
"""
from alembic import op
import sqlalchemy as sa

revision = 'account_deletion_v1'
down_revision = 'documents_v1'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'account_deletion_request',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('patient_id', sa.Integer(), nullable=False),
        sa.Column('requested_at', sa.DateTime(), nullable=True),
        sa.Column('scheduled_for', sa.DateTime(), nullable=False),
        sa.Column('status', sa.String(length=20), nullable=False, server_default='pending'),
        sa.Column('approved_by_id', sa.Integer(), nullable=True),
        sa.Column('approved_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['patient_id'], ['user.id']),
        sa.ForeignKeyConstraint(['approved_by_id'], ['user.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_account_deletion_request_patient_id', 'account_deletion_request', ['patient_id'])


def downgrade():
    op.drop_index('ix_account_deletion_request_patient_id', table_name='account_deletion_request')
    op.drop_table('account_deletion_request')
