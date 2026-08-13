"""add follow_up_request and notification tables

Revision ID: notifications_v1
Revises: ai_chat_v1
Create Date: 2026-08-13

Adds the tables backing the redesigned follow-up flow (doctor proposes a
specific date/time, patient accepts/rejects) and the shared in-app
notification system (bell icon) that both the follow-up flow and, next,
medicine reminders will use.
"""
from alembic import op
import sqlalchemy as sa

revision = 'notifications_v1'
down_revision = 'ai_chat_v1'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'follow_up_request',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('original_appointment_id', sa.Integer(), nullable=False),
        sa.Column('doctor_id', sa.Integer(), nullable=False),
        sa.Column('patient_id', sa.Integer(), nullable=False),
        sa.Column('proposed_date', sa.String(length=50), nullable=False),
        sa.Column('proposed_time', sa.String(length=50), nullable=False),
        sa.Column('status', sa.String(length=20), nullable=False, server_default='pending'),
        sa.Column('resulting_appointment_id', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('responded_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['original_appointment_id'], ['appointment.id']),
        sa.ForeignKeyConstraint(['doctor_id'], ['user.id']),
        sa.ForeignKeyConstraint(['patient_id'], ['user.id']),
        sa.ForeignKeyConstraint(['resulting_appointment_id'], ['appointment.id']),
        sa.PrimaryKeyConstraint('id'),
    )

    op.create_table(
        'notification',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('type', sa.String(length=30), nullable=False),
        sa.Column('message', sa.Text(), nullable=False),
        sa.Column('is_read', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('related_id', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['user_id'], ['user.id']),
        sa.PrimaryKeyConstraint('id'),
    )


def downgrade():
    op.drop_table('notification')
    op.drop_table('follow_up_request')
