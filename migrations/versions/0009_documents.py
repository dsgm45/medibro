"""add document table

Revision ID: documents_v1
Revises: fk_indexes_v1
Create Date: 2026-08-14

Stores uploaded documents directly in this database (file_data) rather
than an external service - no external account, no external billing
risk. Tradeoff: downloads are served through this app's own server
rather than bypassing it.
"""
from alembic import op
import sqlalchemy as sa

revision = 'documents_v1'
down_revision = 'fk_indexes_v1'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'document',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('patient_id', sa.Integer(), nullable=False),
        sa.Column('original_filename', sa.String(length=255), nullable=False),
        sa.Column('file_data', sa.LargeBinary(), nullable=False),
        sa.Column('content_type', sa.String(length=100), nullable=True),
        sa.Column('file_size', sa.Integer(), nullable=True),
        sa.Column('uploaded_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['patient_id'], ['user.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_document_patient_id', 'document', ['patient_id'])


def downgrade():
    op.drop_index('ix_document_patient_id', table_name='document')
    op.drop_table('document')
