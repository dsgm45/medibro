"""baseline - represents the schema as it exists in production today

Revision ID: baseline_v1
Revises:
Create Date: 2026-08-09

This migration is not expected to run against the existing production
database - that database is stamped directly at this revision on first
deploy (see run_migrations() in app.py), since its tables already exist
from prior ad-hoc db.create_all()/ALTER TABLE calls. This migration exists
so that (a) a brand new/empty database can be built from scratch with
`flask db upgrade`, and (b) all future schema changes have a real,
versioned starting point to diff against.
"""
from alembic import op
import sqlalchemy as sa

revision = 'baseline_v1'
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'user',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('email', sa.String(length=120), nullable=False),
        sa.Column('password_hash', sa.String(length=200), nullable=False),
        sa.Column('role', sa.String(length=20), nullable=False),
        sa.Column('full_name', sa.String(length=120), nullable=False),
        sa.Column('specialty', sa.String(length=120), nullable=True),
        sa.Column('phone', sa.String(length=20), nullable=True),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('bio', sa.Text(), nullable=True),
        sa.Column('hours', sa.String(length=200), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('email'),
    )

    op.create_table(
        'appointment',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('patient_id', sa.Integer(), nullable=False),
        sa.Column('doctor_id', sa.Integer(), nullable=False),
        sa.Column('appointment_date', sa.String(length=50), nullable=False),
        sa.Column('appointment_time', sa.String(length=50), nullable=False),
        sa.Column('reason', sa.Text(), nullable=True),
        sa.Column('phone_number', sa.String(length=20), nullable=True),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('follow_up_requested', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['doctor_id'], ['user.id']),
        sa.ForeignKeyConstraint(['patient_id'], ['user.id']),
        sa.PrimaryKeyConstraint('id'),
    )

    op.create_table(
        'vital',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('patient_id', sa.Integer(), nullable=False),
        sa.Column('systolic', sa.Integer(), nullable=True),
        sa.Column('diastolic', sa.Integer(), nullable=True),
        sa.Column('heart_rate', sa.Integer(), nullable=True),
        sa.Column('spo2', sa.Integer(), nullable=True),
        sa.Column('temperature', sa.Float(), nullable=True),
        sa.Column('notes', sa.Text(), nullable=True),
        sa.Column('recorded_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['patient_id'], ['user.id']),
        sa.PrimaryKeyConstraint('id'),
    )

    op.create_table(
        'symptom_log',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('patient_id', sa.Integer(), nullable=False),
        sa.Column('symptoms', sa.Text(), nullable=False),
        sa.Column('severity', sa.String(length=20), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('guidance', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['patient_id'], ['user.id']),
        sa.PrimaryKeyConstraint('id'),
    )

    op.create_table(
        'emergency_contact',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('patient_id', sa.Integer(), nullable=False),
        sa.Column('contact_name', sa.String(length=120), nullable=False),
        sa.Column('contact_phone', sa.String(length=20), nullable=False),
        sa.Column('relation', sa.String(length=50), nullable=True),
        sa.ForeignKeyConstraint(['patient_id'], ['user.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('patient_id'),
    )

    op.create_table(
        'sos_event',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('patient_id', sa.Integer(), nullable=False),
        sa.Column('notes', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['patient_id'], ['user.id']),
        sa.PrimaryKeyConstraint('id'),
    )

    op.create_table(
        'admin_audit_log',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('admin_id', sa.Integer(), nullable=False),
        sa.Column('action', sa.String(length=100), nullable=False),
        sa.Column('target_name', sa.String(length=120), nullable=True),
        sa.Column('details', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['admin_id'], ['user.id']),
        sa.PrimaryKeyConstraint('id'),
    )

    op.create_table(
        'medicine',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('patient_id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(length=120), nullable=False),
        sa.Column('dosage', sa.String(length=80), nullable=True),
        sa.Column('frequency', sa.String(length=80), nullable=True),
        sa.Column('time_of_day', sa.String(length=120), nullable=True),
        sa.Column('notes', sa.Text(), nullable=True),
        sa.Column('start_date', sa.Date(), nullable=True),
        sa.Column('end_date', sa.Date(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['patient_id'], ['user.id']),
        sa.PrimaryKeyConstraint('id'),
    )

    op.create_table(
        'message',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('appointment_id', sa.Integer(), nullable=False),
        sa.Column('sender_id', sa.Integer(), nullable=False),
        sa.Column('content', sa.Text(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['appointment_id'], ['appointment.id']),
        sa.ForeignKeyConstraint(['sender_id'], ['user.id']),
        sa.PrimaryKeyConstraint('id'),
    )

    op.create_table(
        'medicine_dose',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('medicine_id', sa.Integer(), nullable=False),
        sa.Column('time', sa.String(length=5), nullable=False),
        sa.ForeignKeyConstraint(['medicine_id'], ['medicine.id']),
        sa.PrimaryKeyConstraint('id'),
    )

    op.create_table(
        'medicine_dose_log',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('dose_id', sa.Integer(), nullable=False),
        sa.Column('log_date', sa.Date(), nullable=False),
        sa.Column('taken_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['dose_id'], ['medicine_dose.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('dose_id', 'log_date', name='uq_dose_log_date'),
    )


def downgrade():
    op.drop_table('medicine_dose_log')
    op.drop_table('medicine_dose')
    op.drop_table('message')
    op.drop_table('admin_audit_log')
    op.drop_table('sos_event')
    op.drop_table('emergency_contact')
    op.drop_table('symptom_log')
    op.drop_table('medicine')
    op.drop_table('vital')
    op.drop_table('appointment')
    op.drop_table('user')
