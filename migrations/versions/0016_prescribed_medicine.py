"""add appointment_id to medicine

Revision ID: prescribed_medicine_v1
Revises: refill_request_v1
Create Date: 2026-08-17

Links doctor-prescribed medicines to the specific visit they were
prescribed at, enabling a real structured prescription document
(diagnosis + actual medicine list) instead of just reformatted free
text. Nullable, since patient self-tracked medicines aren't tied to
any appointment.
"""
from alembic import op
import sqlalchemy as sa

revision = 'prescribed_medicine_v1'
down_revision = 'refill_request_v1'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('medicine', sa.Column('appointment_id', sa.Integer(), nullable=True))
    op.create_index('ix_medicine_appointment_id', 'medicine', ['appointment_id'])


def downgrade():
    op.drop_index('ix_medicine_appointment_id', table_name='medicine')
    op.drop_column('medicine', 'appointment_id')
