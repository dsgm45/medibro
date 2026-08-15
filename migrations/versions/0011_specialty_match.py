"""add suggested_specialty to symptom_log

Revision ID: specialty_match_v1
Revises: account_deletion_v1
Create Date: 2026-08-15

Supports the symptom-to-specialty matching feature: after logging
symptoms, patients may see a suggested specialty (AI-enhanced, with a
deterministic fallback) linking straight into a pre-filtered booking
search.
"""
from alembic import op
import sqlalchemy as sa

revision = 'specialty_match_v1'
down_revision = 'account_deletion_v1'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('symptom_log', sa.Column('suggested_specialty', sa.String(length=120), nullable=True))


def downgrade():
    op.drop_column('symptom_log', 'suggested_specialty')
