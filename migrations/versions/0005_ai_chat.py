"""add ai_chat_message table

Revision ID: ai_chat_v1
Revises: ai_symptom_v1
Create Date: 2026-08-10

Adds the table backing the AI Health Chat feature - an ongoing,
persistent conversation thread between a patient and the AI assistant,
separate from the existing doctor-patient chat.
"""
from alembic import op
import sqlalchemy as sa

revision = 'ai_chat_v1'
down_revision = 'ai_symptom_v1'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'ai_chat_message',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('patient_id', sa.Integer(), nullable=False),
        sa.Column('sender', sa.String(length=10), nullable=False),
        sa.Column('content', sa.Text(), nullable=False),
        sa.Column('is_crisis_response', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['patient_id'], ['user.id']),
        sa.PrimaryKeyConstraint('id'),
    )


def downgrade():
    op.drop_table('ai_chat_message')
