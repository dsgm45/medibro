"""add indexes to foreign key columns

Revision ID: fk_indexes_v1
Revises: optional_time_v1
Create Date: 2026-08-14

Postgres does not automatically index foreign key columns (only the
primary key gets one automatically) - every one of these columns is
filtered on in nearly every route in the app, so this adds the indexes
that should have been there from the start. Pure performance change, no
behavior change.
"""
from alembic import op

revision = 'fk_indexes_v1'
down_revision = 'optional_time_v1'
branch_labels = None
depends_on = None

INDEXES = [
    ('ix_appointment_patient_id', 'appointment', 'patient_id'),
    ('ix_appointment_doctor_id', 'appointment', 'doctor_id'),
    ('ix_follow_up_request_original_appointment_id', 'follow_up_request', 'original_appointment_id'),
    ('ix_follow_up_request_doctor_id', 'follow_up_request', 'doctor_id'),
    ('ix_follow_up_request_patient_id', 'follow_up_request', 'patient_id'),
    ('ix_follow_up_request_resulting_appointment_id', 'follow_up_request', 'resulting_appointment_id'),
    ('ix_notification_user_id', 'notification', 'user_id'),
    ('ix_notification_related_id', 'notification', 'related_id'),
    ('ix_vital_patient_id', 'vital', 'patient_id'),
    ('ix_symptom_log_patient_id', 'symptom_log', 'patient_id'),
    ('ix_emergency_contact_patient_id', 'emergency_contact', 'patient_id'),
    ('ix_sos_event_patient_id', 'sos_event', 'patient_id'),
    ('ix_admin_audit_log_admin_id', 'admin_audit_log', 'admin_id'),
    ('ix_message_appointment_id', 'message', 'appointment_id'),
    ('ix_message_sender_id', 'message', 'sender_id'),
    ('ix_medicine_patient_id', 'medicine', 'patient_id'),
    ('ix_ai_chat_message_patient_id', 'ai_chat_message', 'patient_id'),
    ('ix_medicine_dose_medicine_id', 'medicine_dose', 'medicine_id'),
    ('ix_medicine_dose_log_dose_id', 'medicine_dose_log', 'dose_id'),
]


def upgrade():
    for index_name, table_name, column_name in INDEXES:
        op.create_index(index_name, table_name, [column_name])


def downgrade():
    for index_name, table_name, column_name in INDEXES:
        op.drop_index(index_name, table_name=table_name)
