"""add ai_generated flag to symptom_log

Revision ID: ai_symptom_v1
Revises: multi_contact_v1
Create Date: 2026-08-10

Adds a flag tracking whether a symptom log's guidance came from the AI
integration or the rule-based fallback, for transparency in the UI.
"""
from alembic import op
import sqlalchemy as sa

revision = 'ai_symptom_v1'
down_revision = 'multi_contact_v1'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('symptom_log', sa.Column('ai_generated', sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade():
    op.drop_column('symptom_log', 'ai_generated')
