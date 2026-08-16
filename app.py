import os
import uuid
import re
import csv
import io
import secrets
from datetime import datetime, timedelta
from functools import wraps
from collections import defaultdict, Counter
from flask import Flask, render_template, request, redirect, url_for, flash, session, Response
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import text, inspect
from sqlalchemy.orm import joinedload
from werkzeug.security import generate_password_hash, check_password_hash
from email_validator import validate_email, EmailNotValidError
from flask_wtf import CSRFProtect
from fpdf import FPDF
from flask_migrate import Migrate, upgrade, stamp
from werkzeug.middleware.proxy_fix import ProxyFix

app = Flask(__name__)

# MediBro is built exclusively for the Indian market - a single timezone
# nationwide (IST, UTC+5:30, no DST, no regional variation). Anywhere
# "today" affects patient-facing logic (medicine schedules, reminders,
# dose-taken tracking) must use IST, not server UTC - using UTC would be
# wrong for roughly 5.5 hours every single day (IST midnight to 5:30am,
# when it's still "yesterday" in UTC), causing real bugs like a medicine
# schedule showing the wrong day. Use this helper instead of
# datetime.utcnow().date() anywhere "today" means "today for the patient".
IST_OFFSET = timedelta(hours=5, minutes=30)

def get_ist_today():
    return (datetime.utcnow() + IST_OFFSET).date()

# Render sits in front of this app as a single reverse-proxy hop, so without
# this, request.remote_addr would always show Render's internal proxy
# address instead of the real visitor IP - which would silently break any
# IP-based rate limiting (either treating every user as the same address,
# or being trivially bypassable). x_for=1 means "trust exactly one hop" -
# matching Render's setup. This must NOT be set higher than the actual
# number of trusted proxies in front of the app, or IP spoofing becomes
# possible.
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

app.secret_key = os.environ.get('SECRET_KEY', 'medibro_secret_key_2026')
if app.secret_key == 'medibro_secret_key_2026':
    import logging
    logging.getLogger(__name__).warning(
        'SECURITY WARNING: Using the default SECRET_KEY. Set a SECRET_KEY '
        'environment variable in production, or session cookies can be forged.'
    )

db_url = os.environ.get('DATABASE_URL', 'sqlite:///medibro.db')
if db_url.startswith('postgres://'):
    db_url = db_url.replace('postgres://', 'postgresql://', 1)

app.config['SQLALCHEMY_DATABASE_URI'] = db_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
    'pool_pre_ping': True,
    'pool_recycle': 300
}

# --- AI SYMPTOM GUIDANCE (Gemini) ---
# Entirely optional: if GEMINI_API_KEY isn't set, the app runs exactly as
# before with rule-based guidance only. The AI layer only ever supplements
# the non-emergency guidance messages - the emergency-symptom detection
# below is deterministic and never depends on this being available.
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY')
GEMINI_MODEL = os.environ.get('GEMINI_MODEL', 'gemini-3.6-flash')
gemini_client = None
if GEMINI_API_KEY:
    try:
        from google import genai
        gemini_client = genai.Client(api_key=GEMINI_API_KEY)
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(f"Gemini client setup failed, AI guidance disabled: {e}")

# Document uploads. Files are stored directly in this app's own Postgres
# database (Document.file_data) - no external service, no external
# account, no external billing risk. These constants define what's
# actually allowed, checked in _validate_document_upload().
ALLOWED_DOCUMENT_EXTENSIONS = {'pdf', 'jpg', 'jpeg', 'png'}
ALLOWED_DOCUMENT_CONTENT_TYPES = {'application/pdf', 'image/jpeg', 'image/png'}
MAX_DOCUMENT_SIZE_BYTES = 10 * 1024 * 1024  # 10 MB

# Session cookie hardening. SESSION_COOKIE_SECURE is left off automatically
# for local development (where requests are plain HTTP), but forced on when
# running in production - controlled via the FLASK_ENV/RENDER env var Render
# sets automatically, so no manual toggle is needed on deploy.
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['SESSION_COOKIE_SECURE'] = bool(os.environ.get('RENDER'))

# How long a "Remember Me" session stays valid. Sessions where the person
# didn't check that box remain regular session cookies that expire when
# the browser closes, controlled per-login in the login route below.
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=30)

db = SQLAlchemy(app)
csrf = CSRFProtect(app)
migrate = Migrate(app, db)

# --- LOGIN RATE LIMITING ---
# In-memory store: fine for a single-worker deployment (this app runs with
# WEB_CONCURRENCY=1). Resets on restart, which is an acceptable trade-off
# for this app's scale rather than adding a Redis dependency.
LOGIN_ATTEMPTS = defaultdict(list)
MAX_LOGIN_ATTEMPTS = 5
LOGIN_LOCKOUT_MINUTES = 5

def is_rate_limited(email):
    now = datetime.utcnow()
    window_start = now - timedelta(minutes=LOGIN_LOCKOUT_MINUTES)
    attempts = [t for t in LOGIN_ATTEMPTS[email] if t > window_start]
    LOGIN_ATTEMPTS[email] = attempts
    return len(attempts) >= MAX_LOGIN_ATTEMPTS

def record_failed_login(email):
    LOGIN_ATTEMPTS[email].append(datetime.utcnow())

def clear_login_attempts(email):
    LOGIN_ATTEMPTS.pop(email, None)

# --- REGISTRATION RATE LIMITING ---
# Keyed by IP rather than email, since a script spamming registrations uses
# a different email each time - the IP is the only consistent signal.
REGISTER_ATTEMPTS = defaultdict(list)
MAX_REGISTER_ATTEMPTS = 5
REGISTER_WINDOW_MINUTES = 60

def is_registration_rate_limited(ip):
    now = datetime.utcnow()
    window_start = now - timedelta(minutes=REGISTER_WINDOW_MINUTES)
    attempts = [t for t in REGISTER_ATTEMPTS[ip] if t > window_start]
    REGISTER_ATTEMPTS[ip] = attempts
    return len(attempts) >= MAX_REGISTER_ATTEMPTS

def record_registration_attempt(ip):
    REGISTER_ATTEMPTS[ip].append(datetime.utcnow())

# --- MODELS ---
class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(200), nullable=False)
    role = db.Column(db.String(20), nullable=False, default='patient')
    full_name = db.Column(db.String(120), nullable=False)
    specialty = db.Column(db.String(120), nullable=True)
    phone = db.Column(db.String(20), nullable=True)
    status = db.Column(db.String(20), nullable=False, default='approved')
    bio = db.Column(db.Text, nullable=True)
    hours = db.Column(db.String(200), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

class Appointment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    patient_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    doctor_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    appointment_date = db.Column(db.String(50), nullable=False)
    appointment_time = db.Column(db.String(50), nullable=True)
    reason = db.Column(db.Text, nullable=True)
    phone_number = db.Column(db.String(20), nullable=True)
    status = db.Column(db.String(20), nullable=False, default='pending')
    follow_up_requested = db.Column(db.Boolean, nullable=False, default=False)
    diagnosis = db.Column(db.Text, nullable=True)
    visit_notes = db.Column(db.Text, nullable=True)
    patient_note = db.Column(db.Text, nullable=True)
    completed_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    patient = db.relationship('User', foreign_keys=[patient_id], backref='patient_appointments')
    doctor = db.relationship('User', foreign_keys=[doctor_id], backref='doctor_appointments')

class FollowUpRequest(db.Model):
    """Replaces the old follow_up_requested boolean flag on Appointment
    with a real proposal/accept/reject flow. The old column is left in
    place (unused) rather than dropped, to avoid a risky column-removal
    migration for something that isn't causing any actual harm sitting
    unused - same approach as the other stray legacy tables already
    flagged in the roadmap."""
    id = db.Column(db.Integer, primary_key=True)
    original_appointment_id = db.Column(db.Integer, db.ForeignKey('appointment.id'), nullable=False, index=True)
    doctor_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    patient_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    proposed_date = db.Column(db.String(50), nullable=False)
    proposed_time = db.Column(db.String(50), nullable=False)
    status = db.Column(db.String(20), nullable=False, default='pending')  # pending / accepted / rejected
    resulting_appointment_id = db.Column(db.Integer, db.ForeignKey('appointment.id'), nullable=True, index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    responded_at = db.Column(db.DateTime, nullable=True)

    original_appointment = db.relationship('Appointment', foreign_keys=[original_appointment_id])
    doctor = db.relationship('User', foreign_keys=[doctor_id])
    patient = db.relationship('User', foreign_keys=[patient_id])
    resulting_appointment = db.relationship('Appointment', foreign_keys=[resulting_appointment_id])

class Notification(db.Model):
    """Shared notification system - used by the follow-up flow now, and
    designed to be reused by medicine reminders next. The 'type' field is
    what lets different notification kinds have different clear behavior
    (e.g. follow-up notifications clear on view, medicine reminders will
    need to stay unread until the dose itself is marked taken)."""
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    type = db.Column(db.String(30), nullable=False)
    message = db.Column(db.Text, nullable=False)
    is_read = db.Column(db.Boolean, nullable=False, default=False)
    read_at = db.Column(db.DateTime, nullable=True)
    related_id = db.Column(db.Integer, nullable=True, index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    user = db.relationship('User', foreign_keys=[user_id], backref='notifications')

class Vital(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    patient_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    systolic = db.Column(db.Integer, nullable=True)
    diastolic = db.Column(db.Integer, nullable=True)
    heart_rate = db.Column(db.Integer, nullable=True)
    spo2 = db.Column(db.Integer, nullable=True)
    temperature = db.Column(db.Float, nullable=True)
    notes = db.Column(db.Text, nullable=True)
    recorded_at = db.Column(db.DateTime, default=datetime.utcnow)

    patient = db.relationship('User', foreign_keys=[patient_id], backref='vitals')

class SymptomLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    patient_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    symptoms = db.Column(db.Text, nullable=False)
    severity = db.Column(db.String(20), nullable=False, default='mild')
    description = db.Column(db.Text, nullable=True)
    guidance = db.Column(db.Text, nullable=True)
    ai_generated = db.Column(db.Boolean, nullable=False, default=False)
    suggested_specialty = db.Column(db.String(120), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    patient = db.relationship('User', foreign_keys=[patient_id], backref='symptom_logs')

class EmergencyContact(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    patient_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    contact_name = db.Column(db.String(120), nullable=False)
    contact_phone = db.Column(db.String(20), nullable=False)
    relation = db.Column(db.String(50), nullable=True)

    patient = db.relationship('User', foreign_keys=[patient_id], backref='emergency_contacts')

class SosEvent(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    patient_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    notes = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    patient = db.relationship('User', foreign_keys=[patient_id], backref='sos_events')

class AdminAuditLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    admin_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    action = db.Column(db.String(100), nullable=False)
    target_name = db.Column(db.String(120), nullable=True)
    details = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    admin = db.relationship('User', foreign_keys=[admin_id])

class PatientDataAccessLog(db.Model):
    """Patient-visible access transparency - every time a doctor or admin
    views a patient's health records, one entry is logged here and shown
    directly to that patient. Deliberately one entry per visit to the
    records view, not split per data section, since that's what actually
    happens (a single page load shows vitals/symptoms/medicines/documents
    together) - splitting it into fake separate events would be
    misleading, not more transparent."""
    id = db.Column(db.Integer, primary_key=True)
    patient_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    viewer_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    viewer_role = db.Column(db.String(20), nullable=False)  # 'doctor' or 'admin'
    viewed_at = db.Column(db.DateTime, default=datetime.utcnow)

    patient = db.relationship('User', foreign_keys=[patient_id])
    viewer = db.relationship('User', foreign_keys=[viewer_id])

class Message(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    appointment_id = db.Column(db.Integer, db.ForeignKey('appointment.id'), nullable=False, index=True)
    sender_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    content = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    appointment = db.relationship('Appointment', backref='messages')
    sender = db.relationship('User', foreign_keys=[sender_id])

class Medicine(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    patient_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    name = db.Column(db.String(120), nullable=False)
    dosage = db.Column(db.String(80), nullable=True)
    frequency = db.Column(db.String(80), nullable=True)
    time_of_day = db.Column(db.String(120), nullable=True)  # legacy free-text, kept for old rows; new entries use MedicineDose
    notes = db.Column(db.Text, nullable=True)
    start_date = db.Column(db.Date, nullable=True)
    end_date = db.Column(db.Date, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    patient = db.relationship('User', foreign_keys=[patient_id], backref='medicines')

class RefillRequest(db.Model):
    """A patient requesting a doctor renew/refill an existing medicine.
    Medicines are patient self-tracked (not linked to a specific
    prescribing doctor), so the patient picks which doctor to send the
    request to, from doctors they've actually had appointments with -
    not any arbitrary doctor on the platform. Approving automatically
    extends the medicine's end date; denying does not."""
    id = db.Column(db.Integer, primary_key=True)
    medicine_id = db.Column(db.Integer, db.ForeignKey('medicine.id'), nullable=False, index=True)
    patient_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    doctor_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    status = db.Column(db.String(20), nullable=False, default='pending')  # pending / approved / denied
    note = db.Column(db.Text, nullable=True)
    doctor_response = db.Column(db.Text, nullable=True)
    requested_at = db.Column(db.DateTime, default=datetime.utcnow)
    responded_at = db.Column(db.DateTime, nullable=True)

    medicine = db.relationship('Medicine', foreign_keys=[medicine_id])
    patient = db.relationship('User', foreign_keys=[patient_id])
    doctor = db.relationship('User', foreign_keys=[doctor_id])

class AIChatMessage(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    patient_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    sender = db.Column(db.String(10), nullable=False)  # 'patient' or 'ai'
    content = db.Column(db.Text, nullable=False)
    is_crisis_response = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    patient = db.relationship('User', foreign_keys=[patient_id], backref='ai_chat_messages')

class MedicineDose(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    medicine_id = db.Column(db.Integer, db.ForeignKey('medicine.id'), nullable=False, index=True)
    time = db.Column(db.String(5), nullable=False)  # 24-hour "HH:MM"

    medicine = db.relationship('Medicine', backref=db.backref('doses', cascade='all, delete-orphan', order_by='MedicineDose.time'))

class MedicineDoseLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    dose_id = db.Column(db.Integer, db.ForeignKey('medicine_dose.id'), nullable=False, index=True)
    log_date = db.Column(db.Date, nullable=False)
    taken_at = db.Column(db.DateTime, nullable=True)

    dose = db.relationship('MedicineDose', backref=db.backref('logs', cascade='all, delete-orphan'))

    __table_args__ = (db.UniqueConstraint('dose_id', 'log_date', name='uq_dose_log_date'),)

class Document(db.Model):
    """The actual file bytes are stored directly in this table (file_data)
    rather than an external service - the patient's own Postgres database,
    same one everything else already uses. No new accounts, no external
    billing risk. Tradeoff: downloads are served through this app's own
    server rather than bypassing it, unlike an external object store
    would allow - a reasonable tradeoff at this app's current scale."""
    id = db.Column(db.Integer, primary_key=True)
    patient_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    original_filename = db.Column(db.String(255), nullable=False)
    file_data = db.Column(db.LargeBinary, nullable=False)
    content_type = db.Column(db.String(100), nullable=True)
    file_size = db.Column(db.Integer, nullable=True)  # bytes
    uploaded_at = db.Column(db.DateTime, default=datetime.utcnow)

    patient = db.relationship('User', foreign_keys=[patient_id], backref='documents')

class AccountDeletionRequest(db.Model):
    """scheduled_for is fixed at request time (requested_at + 7 days) and
    is NOT reset when an admin approves later - see request_account_deletion().
    Deletion only actually executes once BOTH status == 'approved' AND
    scheduled_for has passed - never on the schedule alone."""
    id = db.Column(db.Integer, primary_key=True)
    patient_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    requested_at = db.Column(db.DateTime, default=datetime.utcnow)
    scheduled_for = db.Column(db.DateTime, nullable=False)
    status = db.Column(db.String(20), nullable=False, default='pending')  # pending / approved / cancelled
    approved_by_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    approved_at = db.Column(db.DateTime, nullable=True)

    patient = db.relationship('User', foreign_keys=[patient_id])
    approved_by = db.relationship('User', foreign_keys=[approved_by_id])

def run_migrations():
    """
    Applies database schema changes using real, versioned Alembic migrations
    instead of ad-hoc ALTER statements. Safe to call on every startup.

    On the very first run after adopting this system, the database's tables
    already exist (built up over time by earlier ad-hoc db.create_all() and
    ALTER TABLE calls) but there's no Alembic version history yet. In that
    case we ensure anything the baseline expects is present (db.create_all()
    only ever creates missing tables - it never touches or drops existing
    ones, so this is safe) and then stamp the database as already being at
    the baseline revision, rather than trying to re-run CREATE TABLE
    statements against tables that already exist.

    On every run after that, this just calls upgrade(), which applies any
    migration files that haven't been applied yet - the normal Alembic flow.
    """
    with app.app_context():
        try:
            inspector = inspect(db.engine)
            if not inspector.has_table('alembic_version'):
                db.create_all()
                stamp(revision='baseline_v1')
                app.logger.warning('Alembic adopted: database stamped at baseline_v1.')
            else:
                upgrade()
        except Exception as e:
            app.logger.error(f"Migration run failed: {e}")

def init_db():
    with app.app_context():
        try:
            db.create_all()
            admin = User.query.filter_by(email='admin@medibro.com').first()
            if not admin:
                admin_password = os.environ.get('ADMIN_INITIAL_PASSWORD', 'admin123')
                if admin_password == 'admin123':
                    app.logger.warning(
                        'SECURITY WARNING: Admin account created with the default password. '
                        'Log in and change it immediately at /profile, or set the '
                        'ADMIN_INITIAL_PASSWORD environment variable before first run.'
                    )
                admin = User(
                    email='admin@medibro.com',
                    password_hash=generate_password_hash(admin_password, method='pbkdf2:sha256'),
                    role='hospital',
                    full_name='System Admin',
                    status='approved'
                )
                db.session.add(admin)
                db.session.commit()
        except Exception as e:
            db.session.rollback()

init_db()
run_migrations()

# --- AUTH DECORATORS ---
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            flash('Please log in to access this page.', 'error')
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

def role_required(*roles):
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            if session.get('role') not in roles:
                flash('Access denied.', 'error')
                return redirect(url_for('index'))
            return f(*args, **kwargs)
        return decorated_function
    return decorator

def dashboard_endpoint_for_role(role):
    if role in ('hospital', 'admin'):
        return 'admin_dashboard'
    elif role == 'doctor':
        return 'doctor_dashboard'
    else:
        return 'my_health'

# --- SECURITY HEADERS ---
# The CSP below allows 'unsafe-inline' for scripts and styles because this
# app relies heavily on inline style="..." attributes and a few inline
# <script> blocks throughout its templates. A stricter policy (nonces, an
# external stylesheet) would close that gap further but requires a larger
# template refactor - tracked as a known follow-up, not done here. It also
# explicitly allowlists the two external resources index.html actually
# loads (Tailwind's CDN script, Google Fonts) rather than blocking them.
@app.after_request
def set_security_headers(response):
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
    response.headers['Content-Security-Policy'] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' https://cdn.tailwindcss.com; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com; "
        "img-src 'self' data:; "
        "frame-ancestors 'none';"
    )
    return response

# --- BASE / PUBLIC ROUTES ---
@app.context_processor
def inject_current_year():
    return {'current_year': datetime.utcnow().year}

@app.context_processor
def inject_notification_count():
    if session.get('user_id'):
        if session.get('role') == 'patient':
            ensure_medicine_reminder_notifications(session['user_id'])
        count = Notification.query.filter_by(user_id=session['user_id'], is_read=False).count()

        role = session.get('role')
        if role == 'patient':
            portal_home_url = url_for('my_health')
        elif role == 'doctor':
            portal_home_url = url_for('doctor_dashboard')
        elif role in ('hospital', 'admin'):
            portal_home_url = url_for('admin_dashboard')
        else:
            portal_home_url = url_for('index')

        return {'unread_notification_count': count, 'portal_home_url': portal_home_url}
    return {'unread_notification_count': 0, 'portal_home_url': None}

def ensure_medicine_reminder_notifications(patient_id):
    """Creates a notification for any medicine dose that's due today
    (scheduled time has passed) and hasn't been marked taken, if one
    doesn't already exist for that dose today. Runs opportunistically on
    page load rather than via a background job, since no scheduler
    infrastructure exists yet - same pragmatic approach the existing
    on-page reminder banner already uses, just extended so it shows in
    the shared bell across the whole app, not only the Medicines page.

    Uses a fixed handful of batch queries rather than looping per
    medicine/per dose - the original version made a separate query for
    each dose's log and each dose's existing-notification check inside a
    nested loop, which meant a patient with a few medicines could trigger
    15-20+ database round trips on every single page load, not just the
    Medicines page. This version scales at roughly 4 queries total no
    matter how many medicines or doses exist.

    Unlike follow-up notifications (which clear the moment they're
    viewed), these are only marked read when the dose itself is marked
    taken - see toggle_dose_taken()."""
    today = get_ist_today()
    now_time = (datetime.utcnow() + IST_OFFSET).strftime('%H:%M')
    day_start = datetime.combine(today, datetime.min.time()) - IST_OFFSET
    day_end = day_start + timedelta(days=1)

    active_medicines = Medicine.query.filter(
        Medicine.patient_id == patient_id,
        db.or_(Medicine.start_date == None, Medicine.start_date <= today),
        db.or_(Medicine.end_date == None, Medicine.end_date >= today)
    ).with_entities(Medicine.id, Medicine.name).all()

    if not active_medicines:
        return

    medicine_name_by_id = {m.id: m.name for m in active_medicines}
    active_medicine_ids = list(medicine_name_by_id.keys())

    due_doses = MedicineDose.query.filter(
        MedicineDose.medicine_id.in_(active_medicine_ids),
        MedicineDose.time <= now_time
    ).all()

    if not due_doses:
        return

    due_dose_ids = [d.id for d in due_doses]

    taken_dose_ids = {
        row.dose_id for row in MedicineDoseLog.query.filter(
            MedicineDoseLog.dose_id.in_(due_dose_ids),
            MedicineDoseLog.log_date == today,
            MedicineDoseLog.taken_at.isnot(None)
        ).with_entities(MedicineDoseLog.dose_id).all()
    }

    already_notified_dose_ids = {
        row.related_id for row in Notification.query.filter(
            Notification.user_id == patient_id,
            Notification.type == 'medicine_reminder',
            Notification.related_id.in_(due_dose_ids),
            Notification.created_at >= day_start,
            Notification.created_at < day_end
        ).with_entities(Notification.related_id).all()
    }

    changes_made = False

    # Clear out stale reminders from previous days - you can't meaningfully
    # "take" yesterday's dose today, so a missed reminder shouldn't linger
    # forever once a new day has genuinely started for that same dose.
    stale_reminders = Notification.query.filter(
        Notification.user_id == patient_id,
        Notification.type == 'medicine_reminder',
        Notification.related_id.in_(due_dose_ids),
        Notification.is_read == False,
        Notification.created_at < day_start
    ).all()
    for stale in stale_reminders:
        stale.is_read = True
        stale.read_at = datetime.utcnow()
        changes_made = True

    for dose in due_doses:
        if dose.id in taken_dose_ids or dose.id in already_notified_dose_ids:
            continue

        med_name = medicine_name_by_id.get(dose.medicine_id, 'your medicine')
        notif = Notification(
            user_id=patient_id,
            type='medicine_reminder',
            message=f'Time to take {med_name} ({dose.time}).',
            related_id=dose.id
        )
        db.session.add(notif)
        changes_made = True

    if changes_made:
        db.session.commit()

def _permanently_delete_patient_account(patient_id):
    """Full, genuinely irreversible cascade deletion of a patient account
    and everything tied to it. Only ever call this after a deletion
    request has been BOTH approved by an admin AND its scheduled date has
    passed - see execute_due_account_deletions().

    Deletion order matters - children are deleted before the parents they
    reference, to respect foreign key constraints:
      Message (both sides of any thread tied to this patient's appointments)
      -> FollowUpRequest -> Appointment
      -> Vital, SymptomLog, EmergencyContact, SosEvent, AIChatMessage, Document
      -> Medicine (via ORM delete, not bulk, so its dose/log cascade fires)
      -> Notification (this patient's own - a doctor's notification ABOUT
         this patient, e.g. "patient accepted your follow-up", has
         user_id = the doctor, not this patient, so it's preserved)
      -> AccountDeletionRequest itself
      -> the User record

    Returns True if deletion happened, False if patient_id didn't
    resolve to an actual patient (safety guard against misuse)."""
    patient = db.session.get(User, patient_id)
    if not patient or patient.role != 'patient':
        return False

    appointment_ids = [row.id for row in Appointment.query.filter_by(patient_id=patient_id).with_entities(Appointment.id).all()]
    if appointment_ids:
        Message.query.filter(Message.appointment_id.in_(appointment_ids)).delete(synchronize_session=False)

    FollowUpRequest.query.filter_by(patient_id=patient_id).delete(synchronize_session=False)
    Appointment.query.filter_by(patient_id=patient_id).delete(synchronize_session=False)
    Vital.query.filter_by(patient_id=patient_id).delete(synchronize_session=False)
    SymptomLog.query.filter_by(patient_id=patient_id).delete(synchronize_session=False)
    EmergencyContact.query.filter_by(patient_id=patient_id).delete(synchronize_session=False)
    SosEvent.query.filter_by(patient_id=patient_id).delete(synchronize_session=False)
    AIChatMessage.query.filter_by(patient_id=patient_id).delete(synchronize_session=False)
    Document.query.filter_by(patient_id=patient_id).delete(synchronize_session=False)

    # Medicines need an ORM-level delete (not a bulk .delete() query) so
    # the dose/log cascade already configured on this relationship
    # actually fires - a bulk query operates at the raw SQL level and
    # bypasses relationship cascade behavior entirely.
    for med in Medicine.query.filter_by(patient_id=patient_id).all():
        db.session.delete(med)

    Notification.query.filter_by(user_id=patient_id).delete(synchronize_session=False)
    AccountDeletionRequest.query.filter_by(patient_id=patient_id).delete(synchronize_session=False)

    db.session.delete(patient)
    db.session.commit()
    return True

def execute_due_account_deletions():
    """Checks for any deletion requests that are BOTH approved AND past
    their scheduled date, and actually executes them. Runs opportunistically
    when an admin dashboard loads, rather than via a background job, since
    no scheduler infrastructure exists yet - same pragmatic pattern already
    used for medicine reminders. Deliberately does NOT check pending
    (unapproved) requests, no matter how overdue - approval is required,
    the schedule alone is never sufficient."""
    now = datetime.utcnow()
    due_requests = AccountDeletionRequest.query.filter(
        AccountDeletionRequest.status == 'approved',
        AccountDeletionRequest.scheduled_for <= now
    ).all()

    for req in due_requests:
        try:
            patient = db.session.get(User, req.patient_id)
            if not patient:
                # Already gone somehow - just clean up the stale request record.
                db.session.delete(req)
                db.session.commit()
                continue

            app.logger.warning(
                f"Executing approved account deletion: patient_id={req.patient_id}, "
                f"email={patient.email}, requested_at={req.requested_at}, "
                f"approved_by_id={req.approved_by_id}, scheduled_for={req.scheduled_for}"
            )
            audit = AdminAuditLog(
                admin_id=req.approved_by_id,
                action='account_deletion_executed',
                target_name=patient.email,
                details=f'Requested {req.requested_at}, approved by admin id {req.approved_by_id}, scheduled for {req.scheduled_for}'
            )
            db.session.add(audit)
            db.session.commit()
            _permanently_delete_patient_account(req.patient_id)
        except Exception as e:
            db.session.rollback()
            app.logger.error(f"Failed to execute due account deletion for request {req.id}: {e}")

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/legal/terms')
def legal_terms():
    return render_template('legal_terms.html')

@app.route('/legal/privacy')
def legal_privacy():
    return render_template('legal_privacy.html')

@app.route('/legal/disclaimer')
def legal_disclaimer():
    return render_template('legal_disclaimer.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')

        if is_rate_limited(email):
            flash(f'Too many failed login attempts. Please try again in {LOGIN_LOCKOUT_MINUTES} minutes.', 'error')
            return render_template('login.html')

        try:
            user = User.query.filter_by(email=email).first()
            if user and check_password_hash(user.password_hash, password):
                if user.status == 'pending':
                    flash('Your doctor account is pending verification by hospital admin.', 'error')
                    return redirect(url_for('login'))
                if user.status in ['rejected', 'suspended']:
                    flash('Your account is suspended or rejected.', 'error')
                    return redirect(url_for('login'))

                clear_login_attempts(email)
                session.permanent = bool(request.form.get('remember_me'))
                session['user_id'] = user.id
                session['email'] = user.email
                session['role'] = user.role
                session['full_name'] = user.full_name
                flash(f'Welcome back, {user.full_name}!', 'success')

                if user.role in ['hospital', 'admin']:
                    return redirect(url_for('admin_dashboard'))
                elif user.role == 'doctor':
                    return redirect(url_for('doctor_dashboard'))
                else:
                    return redirect(url_for('my_health'))
            else:
                record_failed_login(email)
                flash('Invalid email or password.', 'error')
        except Exception as e:
            app.logger.error(f"Login error: {e}")
            flash('Something went wrong. Please try again.', 'error')
    return render_template('login.html')

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        client_ip = request.remote_addr

        if is_registration_rate_limited(client_ip):
            flash(f'Too many registration attempts from this network. Please try again in an hour.', 'error')
            return redirect(url_for('register'))

        record_registration_attempt(client_ip)

        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')
        full_name = request.form.get('full_name', '').strip()
        role = request.form.get('role', 'patient')
        specialty = request.form.get('specialty', '').strip()
        phone = request.form.get('phone', '').strip()

        try:
            validate_email(email, check_deliverability=False)
        except EmailNotValidError:
            flash('Please enter a valid email address.', 'error')
            return redirect(url_for('register'))

        if len(password) < 6 or not re.search(r'[A-Za-z]', password) or not re.search(r'[0-9]', password):
            flash('Password must be at least 6 characters and include both letters and numbers.', 'error')
            return redirect(url_for('register'))

        if not full_name:
            flash('Please enter your full name.', 'error')
            return redirect(url_for('register'))

        try:
            if User.query.filter_by(email=email).first():
                flash('Email address already registered.', 'error')
                return redirect(url_for('register'))

            status = 'pending' if role == 'doctor' else 'approved'

            new_user = User(
                email=email,
                password_hash=generate_password_hash(password, method='pbkdf2:sha256'),
                role=role,
                full_name=full_name,
                specialty=specialty if role == 'doctor' else None,
                phone=phone,
                status=status
            )
            db.session.add(new_user)
            db.session.commit()

            if status == 'pending':
                flash('Registration successful! Account pending hospital verification.', 'success')
            else:
                flash('Registration successful! You can now log in.', 'success')
            return redirect(url_for('login'))
        except Exception as e:
            db.session.rollback()
            app.logger.error(f"Registration error: {e}")
            flash('Something went wrong during registration. Please try again.', 'error')

    return render_template('register.html')

# --- DASHBOARD ROUTES ---
@app.route('/my-health')
@login_required
@role_required('patient')
def my_health():
    patient_id = session.get('user_id')
    today = get_ist_today()

    latest_vital = Vital.query.filter_by(patient_id=patient_id).order_by(Vital.recorded_at.desc()).first()
    latest_symptom = SymptomLog.query.filter_by(patient_id=patient_id).order_by(SymptomLog.created_at.desc()).first()

    active_medicines = Medicine.query.filter(
        Medicine.patient_id == patient_id,
        db.or_(Medicine.start_date == None, Medicine.start_date <= today),
        db.or_(Medicine.end_date == None, Medicine.end_date >= today)
    ).order_by(Medicine.created_at.desc()).all()

    now = datetime.utcnow()
    upcoming_appointments = Appointment.query.options(joinedload(Appointment.doctor)).filter_by(patient_id=patient_id, status='accepted').all()
    next_appointment = None
    soonest_diff = None
    for appt in upcoming_appointments:
        try:
            appt_dt = datetime.strptime(f"{appt.appointment_date} {appt.appointment_time}", '%Y-%m-%d %H:%M')
            diff = (appt_dt - now).total_seconds()
            if diff >= 0 and (soonest_diff is None or diff < soonest_diff):
                soonest_diff = diff
                next_appointment = appt
        except (ValueError, TypeError):
            continue

    next_appointment_days_left = None
    if soonest_diff is not None:
        next_appointment_days_left = max(0, int(soonest_diff // 86400))

    pending_follow_ups = FollowUpRequest.query.options(joinedload(FollowUpRequest.doctor)).filter_by(
        patient_id=patient_id, status='pending'
    ).order_by(FollowUpRequest.created_at.desc()).all()

    documents = Document.query.filter_by(patient_id=patient_id).order_by(Document.uploaded_at.desc()).all()

    return render_template(
        'my_health.html',
        latest_vital=latest_vital,
        latest_symptom=latest_symptom,
        active_medicines=active_medicines,
        next_appointment=next_appointment,
        next_appointment_days_left=next_appointment_days_left,
        follow_ups=pending_follow_ups,
        documents=documents
    )

@app.route('/my-health/access-log')
@login_required
@role_required('patient')
def my_access_log():
    patient_id = session.get('user_id')
    entries = PatientDataAccessLog.query.options(joinedload(PatientDataAccessLog.viewer)).filter_by(
        patient_id=patient_id
    ).order_by(PatientDataAccessLog.viewed_at.desc()).limit(100).all()
    return render_template('access_log.html', entries=entries)

@app.route('/my-health/explain-trends', methods=['POST'])
@login_required
@role_required('patient')
def explain_trends():
    patient_id = session.get('user_id')
    explanation = get_ai_trend_explanation(patient_id)
    if explanation:
        flash(explanation, 'trend_explanation')
    else:
        flash("Not enough recent vitals or symptom history yet to spot a meaningful trend - log a few more entries and try again.", 'info')
    return redirect(url_for('my_health'))

def _pdf_safe_text(value, max_run=20):
    """PDF core fonts only support Latin-1. Replace anything outside that
    range instead of letting it crash the export - degraded output is far
    better than a broken download.

    Also reduces the likelihood of a known fpdf2 upstream bug (see
    py-pdf/fpdf2#1250) where its line-wrapper can raise FPDFException("Not
    enough horizontal space to render a single character") on certain
    character sequences - most commonly long unbroken runs of text, but
    the upstream issue shows this can also happen with some non-Latin
    scripts even in shorter runs, depending on internal line-fragment
    processing that isn't fully within this app's control. This inserts a
    breakable space every max_run characters into any long unbroken run as
    a preventive measure - the real safety net against this bug is
    _safe_pdf_multi_cell() below, which degrades gracefully per-line if it
    happens anyway."""
    if value is None:
        return ''
    text = str(value).encode('latin-1', errors='replace').decode('latin-1')

    def break_long_run(match):
        run = match.group(0)
        return ' '.join(run[i:i + max_run] for i in range(0, len(run), max_run))

    text = re.sub(r'\S{' + str(max_run + 1) + r',}', break_long_run, text)
    return text

def _safe_pdf_multi_cell(pdf, w, h, text):
    """Wraps pdf.multi_cell() with a fallback for the known fpdf2 upstream
    bug described above. This is the real safety net: even if the
    preventive text preprocessing in _pdf_safe_text() doesn't catch every
    case (the upstream bug's exact trigger conditions aren't something
    this app can fully control), only THIS ONE LINE degrades to a
    placeholder instead of the entire PDF export failing for the patient."""
    try:
        pdf.multi_cell(w, h, text)
    except Exception as e:
        app.logger.warning(f"PDF line rendering failed, using placeholder: {e}")
        try:
            pdf.multi_cell(w, h, '[This entry could not be displayed due to a formatting issue.]')
        except Exception:
            pass  # even the placeholder failed - skip this line entirely rather than crash

@app.route('/my-health/export-pdf')
@login_required
@role_required('patient')
def export_health_pdf():
    patient = db.get_or_404(User, session.get('user_id'))
    patient_id = patient.id

    vitals = Vital.query.filter_by(patient_id=patient_id).order_by(Vital.recorded_at.desc()).limit(10).all()
    symptoms = SymptomLog.query.filter_by(patient_id=patient_id).order_by(SymptomLog.created_at.desc()).limit(10).all()
    medicines = Medicine.query.filter_by(patient_id=patient_id).order_by(Medicine.created_at.desc()).all()
    visits = Appointment.query.filter_by(patient_id=patient_id, status='completed').order_by(Appointment.completed_at.desc()).limit(10).all()

    t = _pdf_safe_text

    try:
        pdf = FPDF()
        pdf.add_page()

        pdf.set_font('Helvetica', 'B', 18)
        pdf.cell(0, 12, t('MediBro Health Summary'))
        pdf.ln(12)

        pdf.set_font('Helvetica', '', 10)
        pdf.set_text_color(100, 100, 100)
        pdf.cell(0, 6, t(f'{patient.full_name} - Generated {datetime.utcnow().strftime("%B %d, %Y")}'))
        pdf.ln(10)
        pdf.set_text_color(0, 0, 0)

        def section_title(title):
            pdf.set_font('Helvetica', 'B', 13)
            pdf.cell(0, 8, t(title))
            pdf.ln(9)
            pdf.set_font('Helvetica', '', 10)

        section_title('Recent Vitals')
        if vitals:
            for v in vitals:
                date_str = v.recorded_at.strftime('%b %d, %Y') if v.recorded_at else ''
                bp = f'{v.systolic}/{v.diastolic}' if v.systolic and v.diastolic else '-'
                line = f'{date_str}  |  BP: {bp}  |  HR: {v.heart_rate or "-"} bpm  |  SpO2: {v.spo2 or "-"}%  |  Temp: {v.temperature or "-"} F'
                pdf.cell(0, 6, t(line))
                pdf.ln(6)
        else:
            pdf.cell(0, 6, t('No vitals logged.'))
            pdf.ln(6)
        pdf.ln(4)

        section_title('Recent Symptoms')
        if symptoms:
            for s in symptoms:
                date_str = s.created_at.strftime('%b %d, %Y') if s.created_at else ''
                line = f'{date_str}  |  {s.symptoms}  |  Severity: {s.severity}'
                _safe_pdf_multi_cell(pdf, 0, 6, t(line))
        else:
            pdf.cell(0, 6, t('No symptoms logged.'))
            pdf.ln(6)
        pdf.ln(4)

        section_title('Medicines')
        if medicines:
            for m in medicines:
                times = ', '.join(d.time for d in m.doses) if m.doses else (m.time_of_day or '-')
                line = f'{m.name}  |  {m.dosage or "-"}  |  {m.frequency or "-"}  |  Times: {times}'
                _safe_pdf_multi_cell(pdf, 0, 6, t(line))
        else:
            pdf.cell(0, 6, t('No medicines on record.'))
            pdf.ln(6)
        pdf.ln(4)

        section_title('Visit History')
        if visits:
            for v in visits:
                doc_name = v.doctor.full_name.replace('Dr. ', '').replace('Dr ', '') if v.doctor else 'Unknown'
                line = f'{v.appointment_date}  |  Dr. {doc_name}  |  Diagnosis: {v.diagnosis or "Not recorded"}'
                _safe_pdf_multi_cell(pdf, 0, 6, t(line))
                if v.visit_notes:
                    pdf.set_font('Helvetica', 'I', 9)
                    _safe_pdf_multi_cell(pdf, 0, 5, t(f'  Notes: {v.visit_notes}'))
                    pdf.set_font('Helvetica', '', 10)
        else:
            pdf.cell(0, 6, t('No completed visits on record.'))
            pdf.ln(6)

        pdf_bytes = bytes(pdf.output())
    except Exception as e:
        app.logger.error(f"PDF export error: {e}")
        flash('There was an error generating your PDF summary. Please try again, or contact support if this keeps happening.', 'error')
        return redirect(url_for('my_health'))

    response = Response(pdf_bytes, mimetype='application/pdf')
    response.headers['Content-Disposition'] = 'attachment; filename=medibro_health_summary.pdf'
    return response

@app.route('/patient')
@login_required
@role_required('patient')
def patient_dashboard():
    patient_id = session.get('user_id')
    specialty_filter = request.args.get('specialty', '').strip()

    doctor_query = User.query.filter_by(role='doctor', status='approved')
    if specialty_filter:
        doctor_query = doctor_query.filter_by(specialty=specialty_filter)
    doctors = doctor_query.all()

    all_specialties = sorted(set(
        d.specialty for d in User.query.filter_by(role='doctor', status='approved').all() if d.specialty
    ))

    my_appointments = Appointment.query.options(joinedload(Appointment.doctor)).filter_by(patient_id=patient_id).order_by(Appointment.created_at.desc()).all()

    upcoming = []
    now = datetime.utcnow()
    for appt in my_appointments:
        if appt.status != 'accepted':
            continue
        try:
            appt_dt = datetime.strptime(f"{appt.appointment_date} {appt.appointment_time}", '%Y-%m-%d %H:%M')
            hours_away = (appt_dt - now).total_seconds() / 3600
            if 0 <= hours_away <= 48:
                upcoming.append(appt)
        except (ValueError, TypeError):
            continue

    follow_ups = FollowUpRequest.query.options(joinedload(FollowUpRequest.doctor)).filter_by(
        patient_id=patient_id, status='pending'
    ).order_by(FollowUpRequest.created_at.desc()).all()
    pre_doctor_id = request.args.get('book_with', type=int)

    return render_template(
        'patient_dashboard.html',
        doctors=doctors,
        appointments=my_appointments,
        all_specialties=all_specialties,
        specialty_filter=specialty_filter,
        upcoming=upcoming,
        follow_ups=follow_ups,
        pre_doctor_id=pre_doctor_id
    )

@app.route('/book-appointment', methods=['POST'])
@login_required
@role_required('patient')
def book_appointment():
    doctor_id = request.form.get('doctor_id')
    appointment_date = request.form.get('appointment_date')
    appointment_time = request.form.get('appointment_time', '').strip() or None
    reason = request.form.get('reason', '').strip()
    phone_number = request.form.get('phone_number', '').strip()

    if not doctor_id or not appointment_date:
        flash('Please fill in all required appointment fields.', 'error')
        return redirect(url_for('patient_dashboard'))

    if not phone_number:
        flash('Please provide a phone number so your doctor can reach you, since video consultation is not yet available.', 'error')
        return redirect(url_for('patient_dashboard'))

    try:
        doctor_id = int(doctor_id)
    except (ValueError, TypeError):
        flash('Invalid doctor selection.', 'error')
        return redirect(url_for('patient_dashboard'))

    # Only a real, specific time can conflict with another booking - two
    # requests for the same date with no time set yet aren't a conflict,
    # since neither has claimed an actual slot. The real check happens
    # when the doctor assigns a time while accepting (see handle_appointment).
    if appointment_time:
        existing_conflict = Appointment.query.filter_by(
            doctor_id=doctor_id,
            appointment_date=appointment_date,
            appointment_time=appointment_time
        ).filter(Appointment.status.in_(['pending', 'accepted'])).first()

        if existing_conflict:
            flash('This doctor already has a request or appointment at that date and time. Please choose a different time.', 'error')
            return redirect(url_for('patient_dashboard'))

    try:
        new_app = Appointment(
            patient_id=session['user_id'],
            doctor_id=doctor_id,
            appointment_date=appointment_date,
            appointment_time=appointment_time,
            reason=reason,
            phone_number=phone_number,
            status='pending'
        )
        db.session.add(new_app)
        db.session.commit()
        flash('Appointment requested successfully!', 'success')
    except Exception as e:
        db.session.rollback()
        app.logger.error(f"Book appointment error: {e}")
        flash('Error booking appointment. Please check your entries and try again.', 'error')

    return redirect(url_for('patient_dashboard'))

@app.route('/my-appointment/<int:app_id>/cancel', methods=['POST'])
@login_required
@role_required('patient')
def cancel_appointment(app_id):
    try:
        appt = db.get_or_404(Appointment, app_id)
        if appt.patient_id != session.get('user_id'):
            flash('Unauthorized action.', 'error')
            return redirect(url_for('patient_dashboard'))
        if appt.status != 'pending':
            flash('Only pending appointments can be cancelled.', 'error')
            return redirect(url_for('patient_dashboard'))

        appt.status = 'cancelled'
        db.session.commit()
        flash('Appointment cancelled.', 'success')
    except Exception as e:
        db.session.rollback()
        app.logger.error(f"Cancel appointment error: {e}")
        flash('Error cancelling appointment.', 'error')

    return redirect(url_for('patient_dashboard'))

@app.route('/doctor')
@login_required
@role_required('doctor')
def doctor_dashboard():
    doctor_id = session.get('user_id')
    doctor = db.get_or_404(User, doctor_id)
    appointments = Appointment.query.options(joinedload(Appointment.patient)).filter_by(doctor_id=doctor_id).order_by(
        Appointment.appointment_date.desc(), Appointment.appointment_time.asc()
    ).all()

    # Which completed visits already have a pending follow-up proposal,
    # so the "Request Follow-up" button correctly hides for those instead
    # of relying on the old boolean flag, which nothing sets anymore.
    pending_followups = FollowUpRequest.query.filter_by(doctor_id=doctor_id, status='pending').all()
    pending_followup_appointment_ids = {f.original_appointment_id for f in pending_followups}

    now = datetime.utcnow()
    days_until_by_appointment_id = {}
    for appt in appointments:
        if appt.status == 'accepted':
            try:
                appt_dt = datetime.strptime(f"{appt.appointment_date} {appt.appointment_time}", '%Y-%m-%d %H:%M')
                diff_seconds = (appt_dt - now).total_seconds()
                if diff_seconds >= 0:
                    days_until_by_appointment_id[appt.id] = max(0, int(diff_seconds // 86400))
            except (ValueError, TypeError):
                pass

    grouped = {}
    for appt in appointments:
        grouped.setdefault(appt.appointment_date, []).append(appt)
    grouped_appointments = sorted(grouped.items(), key=lambda x: x[0], reverse=True)

    pending_refill_requests = RefillRequest.query.options(
        joinedload(RefillRequest.patient), joinedload(RefillRequest.medicine)
    ).filter_by(doctor_id=doctor_id, status='pending').order_by(RefillRequest.requested_at.desc()).all()

    return render_template(
        'doctor_dashboard.html',
        doctor=doctor,
        grouped_appointments=grouped_appointments,
        pending_followup_appointment_ids=pending_followup_appointment_ids,
        days_until_by_appointment_id=days_until_by_appointment_id,
        pending_refill_requests=pending_refill_requests
    )

@app.route('/appointment/<int:app_id>/<action>', methods=['POST'])
@login_required
@role_required('doctor')
def handle_appointment(app_id, action):
    try:
        appt = db.get_or_404(Appointment, app_id)
        if appt.doctor_id != session.get('user_id'):
            flash('Unauthorized action.', 'error')
            return redirect(url_for('doctor_dashboard'))

        if action == 'accept':
            if not appt.appointment_time:
                assigned_time = request.form.get('assigned_time', '').strip()
                if not assigned_time:
                    flash('Please set a time for this appointment before accepting.', 'error')
                    return redirect(url_for('doctor_dashboard'))

                # This is the real double-booking check for time-optional
                # requests - it only matters once an actual specific time
                # is being claimed, which is happening right now.
                conflict = Appointment.query.filter(
                    Appointment.id != appt.id,
                    Appointment.doctor_id == appt.doctor_id,
                    Appointment.appointment_date == appt.appointment_date,
                    Appointment.appointment_time == assigned_time,
                    Appointment.status.in_(['pending', 'accepted'])
                ).first()
                if conflict:
                    flash('You already have a request or appointment at that date and time. Please choose a different time.', 'error')
                    return redirect(url_for('doctor_dashboard'))

                appt.appointment_time = assigned_time

            appt.status = 'accepted'
            flash('Appointment accepted!', 'success')
        elif action == 'decline':
            appt.status = 'declined'
            flash('Appointment declined.', 'error')

        db.session.commit()
    except Exception as e:
        db.session.rollback()
        app.logger.error(f"Appointment handle error: {e}")
        flash('Database error updating appointment.', 'error')

    return redirect(url_for('doctor_dashboard'))

@app.route('/appointment/<int:app_id>/complete', methods=['GET', 'POST'])
@login_required
@role_required('doctor')
def complete_appointment(app_id):
    appt = db.get_or_404(Appointment, app_id)
    if appt.doctor_id != session.get('user_id'):
        flash('Unauthorized action.', 'error')
        return redirect(url_for('doctor_dashboard'))
    if appt.status not in ('accepted', 'completed'):
        flash('Only accepted or completed appointments can have a visit summary.', 'error')
        return redirect(url_for('doctor_dashboard'))

    if request.method == 'POST':
        diagnosis = request.form.get('diagnosis', '').strip()
        visit_notes = request.form.get('visit_notes', '').strip()

        try:
            appt.diagnosis = diagnosis
            appt.visit_notes = visit_notes
            if appt.status == 'accepted':
                appt.status = 'completed'
                appt.completed_at = datetime.utcnow()
            db.session.commit()
            flash('Visit summary saved.', 'success')
        except Exception as e:
            db.session.rollback()
            app.logger.error(f"Complete appointment error: {e}")
            flash('Error saving visit summary.', 'error')

        return redirect(url_for('doctor_dashboard'))

    return render_template('appointment_complete.html', appt=appt)

@app.route('/my-appointment/<int:app_id>/summary')
@login_required
@role_required('patient')
def appointment_summary(app_id):
    appt = db.get_or_404(Appointment, app_id)
    if appt.patient_id != session.get('user_id'):
        flash('Unauthorized action.', 'error')
        return redirect(url_for('patient_dashboard'))
    if appt.status != 'completed':
        flash('This appointment does not have a visit summary yet.', 'error')
        return redirect(url_for('patient_dashboard'))

    return render_template('appointment_summary.html', appt=appt)

@app.route('/my-appointment/<int:app_id>/note', methods=['POST'])
@login_required
@role_required('patient')
def save_appointment_note(app_id):
    appt = db.get_or_404(Appointment, app_id)
    if appt.patient_id != session.get('user_id'):
        flash('Unauthorized action.', 'error')
        return redirect(url_for('patient_dashboard'))
    if appt.status != 'completed':
        flash('This appointment does not have a visit summary yet.', 'error')
        return redirect(url_for('patient_dashboard'))

    try:
        appt.patient_note = request.form.get('patient_note', '').strip() or None
        db.session.commit()
        flash('Your note has been saved.', 'success')
    except Exception as e:
        db.session.rollback()
        app.logger.error(f"Save appointment note error: {e}")
        flash('Error saving your note. Please try again.', 'error')

    return redirect(url_for('appointment_summary', app_id=app_id))

@app.route('/appointment/<int:app_id>/request-follow-up', methods=['POST'])
@login_required
@role_required('doctor')
def request_follow_up(app_id):
    try:
        appt = db.get_or_404(Appointment, app_id)
        if appt.doctor_id != session.get('user_id'):
            flash('Unauthorized action.', 'error')
            return redirect(url_for('doctor_dashboard'))
        if appt.status != 'completed':
            flash('Follow-up can only be requested for completed appointments.', 'error')
            return redirect(url_for('doctor_dashboard'))

        proposed_date = request.form.get('proposed_date', '').strip()
        proposed_time = request.form.get('proposed_time', '').strip()
        if not proposed_date or not proposed_time:
            flash('Please select a date and time for the follow-up.', 'error')
            return redirect(url_for('doctor_dashboard'))

        existing_pending = FollowUpRequest.query.filter_by(
            original_appointment_id=appt.id, status='pending'
        ).first()
        if existing_pending:
            flash('A follow-up proposal is already pending for this visit.', 'error')
            return redirect(url_for('doctor_dashboard'))

        follow_up = FollowUpRequest(
            original_appointment_id=appt.id,
            doctor_id=appt.doctor_id,
            patient_id=appt.patient_id,
            proposed_date=proposed_date,
            proposed_time=proposed_time,
            status='pending'
        )
        db.session.add(follow_up)
        db.session.commit()
        flash('Follow-up proposed. The patient will see it and can accept or decline.', 'success')
    except Exception as e:
        db.session.rollback()
        app.logger.error(f"Request follow-up error: {e}")
        flash('Error requesting follow-up.', 'error')

    return redirect(url_for('doctor_dashboard'))

@app.route('/follow-up/<int:followup_id>/accept', methods=['POST'])
@login_required
@role_required('patient')
def accept_follow_up(followup_id):
    try:
        follow_up = db.get_or_404(FollowUpRequest, followup_id)
        if follow_up.patient_id != session.get('user_id'):
            flash('Unauthorized action.', 'error')
            return redirect(url_for('my_health'))
        if follow_up.status != 'pending':
            flash('This follow-up request has already been responded to.', 'error')
            return redirect(url_for('my_health'))

        # Same double-booking check as regular booking, in case the
        # doctor's proposed slot got taken by someone else in the meantime.
        existing_conflict = Appointment.query.filter_by(
            doctor_id=follow_up.doctor_id,
            appointment_date=follow_up.proposed_date,
            appointment_time=follow_up.proposed_time
        ).filter(Appointment.status.in_(['pending', 'accepted'])).first()

        if existing_conflict:
            flash('Sorry, that time slot is no longer available. Please contact your doctor to reschedule.', 'error')
            return redirect(url_for('my_health'))

        # The doctor proposed this specific time themselves, so the
        # resulting appointment is created already-accepted rather than
        # making them redundantly re-approve their own proposal.
        new_appt = Appointment(
            patient_id=follow_up.patient_id,
            doctor_id=follow_up.doctor_id,
            appointment_date=follow_up.proposed_date,
            appointment_time=follow_up.proposed_time,
            reason='Follow-up visit',
            status='accepted'
        )
        db.session.add(new_appt)
        db.session.flush()

        follow_up.status = 'accepted'
        follow_up.resulting_appointment_id = new_appt.id
        follow_up.responded_at = datetime.utcnow()

        notification = Notification(
            user_id=follow_up.doctor_id,
            type='follow_up_accepted',
            message=f'{follow_up.patient.full_name} accepted your follow-up proposal for {follow_up.proposed_date} at {follow_up.proposed_time}.',
            related_id=new_appt.id
        )
        db.session.add(notification)
        db.session.commit()
        flash('Follow-up appointment confirmed!', 'success')
    except Exception as e:
        db.session.rollback()
        app.logger.error(f"Accept follow-up error: {e}")
        flash('Error accepting follow-up.', 'error')

    return redirect(url_for('my_health'))

@app.route('/follow-up/<int:followup_id>/reject', methods=['POST'])
@login_required
@role_required('patient')
def reject_follow_up(followup_id):
    try:
        follow_up = db.get_or_404(FollowUpRequest, followup_id)
        if follow_up.patient_id != session.get('user_id'):
            flash('Unauthorized action.', 'error')
            return redirect(url_for('my_health'))
        if follow_up.status != 'pending':
            flash('This follow-up request has already been responded to.', 'error')
            return redirect(url_for('my_health'))

        follow_up.status = 'rejected'
        follow_up.responded_at = datetime.utcnow()

        notification = Notification(
            user_id=follow_up.doctor_id,
            type='follow_up_rejected',
            message=f'{follow_up.patient.full_name} declined your follow-up proposal for {follow_up.proposed_date} at {follow_up.proposed_time}.',
            related_id=follow_up.original_appointment_id
        )
        db.session.add(notification)
        db.session.commit()
        flash('Follow-up declined.', 'success')
    except Exception as e:
        db.session.rollback()
        app.logger.error(f"Reject follow-up error: {e}")
        flash('Error declining follow-up.', 'error')

    return redirect(url_for('my_health'))

@app.route('/notifications')
@login_required
def notifications():
    user_id = session.get('user_id')

    # Mark as read on view - excluding medicine_reminder type, which needs
    # to persist until the dose itself is marked taken, not just viewed.
    Notification.query.filter_by(user_id=user_id, is_read=False).filter(
        Notification.type != 'medicine_reminder'
    ).update({'is_read': True, 'read_at': datetime.utcnow()}, synchronize_session=False)
    db.session.commit()

    # Purge anything that's been read for over a day - opportunistic,
    # checked here since there's no background scheduler. Applies to any
    # read notification regardless of how it became read (viewed, dose
    # taken, Clear All, or the stale-reminder rollover).
    purge_cutoff = datetime.utcnow() - timedelta(days=1)
    Notification.query.filter(
        Notification.user_id == user_id,
        Notification.is_read == True,
        Notification.read_at.isnot(None),
        Notification.read_at < purge_cutoff
    ).delete(synchronize_session=False)
    db.session.commit()

    all_notifications = Notification.query.filter_by(user_id=user_id).order_by(Notification.created_at.desc()).limit(30).all()
    return render_template('notifications.html', notifications=all_notifications)

@app.route('/notifications/clear-all', methods=['POST'])
@login_required
def clear_all_notifications():
    """Immediately deletes everything currently marked as read, rather
    than waiting for the automatic day-later purge. Unread medicine
    reminders are untouched either way, since they're never marked read
    by anything other than actually taking the dose (or the next-day
    stale rollover) - this button can't be used to dismiss one without
    that happening."""
    user_id = session.get('user_id')
    Notification.query.filter_by(user_id=user_id, is_read=True).delete(synchronize_session=False)
    db.session.commit()
    flash('Notifications cleared.', 'success')
    return redirect(url_for('notifications'))

@app.route('/doctor/profile', methods=['GET', 'POST'])
@login_required
@role_required('doctor')
def doctor_profile():
    doctor = db.get_or_404(User, session.get('user_id'))

    if request.method == 'POST':
        specialty = request.form.get('specialty', '').strip()
        phone = request.form.get('phone', '').strip()
        bio = request.form.get('bio', '').strip()
        hours = request.form.get('hours', '').strip()

        try:
            doctor.specialty = specialty
            doctor.phone = phone
            doctor.bio = bio
            doctor.hours = hours
            db.session.commit()
            flash('Profile updated successfully.', 'success')
        except Exception as e:
            db.session.rollback()
            app.logger.error(f"Doctor profile update error: {e}")
            flash('Error updating profile. Please try again.', 'error')

        return redirect(url_for('doctor_profile'))

    return render_template('doctor_profile.html', doctor=doctor)

def _validate_document_upload(file_storage, size):
    """Returns an error message string if the upload is invalid, or None
    if it's fine. Size is computed once by the caller (seeking the stream)
    and passed in, rather than re-computed here."""
    if not file_storage or not file_storage.filename:
        return 'Please choose a file to upload.'

    ext = file_storage.filename.rsplit('.', 1)[-1].lower() if '.' in file_storage.filename else ''
    if ext not in ALLOWED_DOCUMENT_EXTENSIONS:
        return 'Only PDF, JPG, and PNG files are allowed.'

    if file_storage.content_type not in ALLOWED_DOCUMENT_CONTENT_TYPES:
        return 'Only PDF, JPG, and PNG files are allowed.'

    if size == 0:
        return 'The selected file is empty.'

    if size > MAX_DOCUMENT_SIZE_BYTES:
        return f'File is too large. Maximum size is {MAX_DOCUMENT_SIZE_BYTES // (1024 * 1024)} MB.'

    return None

@app.route('/documents/upload', methods=['POST'])
@login_required
@role_required('patient')
def upload_document():
    file_storage = request.files.get('document')
    if not file_storage or not file_storage.filename:
        flash('Please choose a file to upload.', 'error')
        return redirect(url_for('my_health'))

    file_storage.stream.seek(0, os.SEEK_END)
    size = file_storage.stream.tell()
    file_storage.stream.seek(0)

    error = _validate_document_upload(file_storage, size)
    if error:
        flash(error, 'error')
        return redirect(url_for('my_health'))

    try:
        doc = Document(
            patient_id=session['user_id'],
            original_filename=file_storage.filename,
            file_data=file_storage.read(),
            content_type=file_storage.content_type,
            file_size=size
        )
        db.session.add(doc)
        db.session.commit()
        flash('Document uploaded successfully.', 'success')
    except Exception as e:
        db.session.rollback()
        app.logger.error(f"Document upload error: {e}")
        flash('Error uploading document. Please try again.', 'error')

    return redirect(url_for('my_health'))

@app.route('/documents/<int:doc_id>/download')
@login_required
@role_required('patient', 'doctor')
def download_document(doc_id):
    doc = db.get_or_404(Document, doc_id)
    user_id = session.get('user_id')
    role = session.get('role')
    fallback_page = 'my_health' if role == 'patient' else 'doctor_dashboard'

    if role == 'patient':
        if doc.patient_id != user_id:
            flash('Unauthorized action.', 'error')
            return redirect(url_for(fallback_page))
    else:
        has_appointment = Appointment.query.filter_by(doctor_id=user_id, patient_id=doc.patient_id).first()
        if not has_appointment:
            flash('You can only view documents for patients who have booked with you.', 'error')
            return redirect(url_for(fallback_page))

    return Response(
        doc.file_data,
        mimetype=doc.content_type or 'application/octet-stream',
        headers={'Content-Disposition': f'attachment; filename="{doc.original_filename}"'}
    )

@app.route('/documents/<int:doc_id>/delete', methods=['POST'])
@login_required
@role_required('patient')
def delete_document(doc_id):
    doc = db.get_or_404(Document, doc_id)
    if doc.patient_id != session.get('user_id'):
        flash('Unauthorized action.', 'error')
        return redirect(url_for('my_health'))

    try:
        db.session.delete(doc)
        db.session.commit()
        flash('Document deleted.', 'success')
    except Exception as e:
        db.session.rollback()
        app.logger.error(f"Document delete error: {e}")
        flash('Error deleting document.', 'error')

    return redirect(url_for('my_health'))

@app.route('/doctor/patient/<int:patient_id>')
@login_required
@role_required('doctor')
def view_patient_history(patient_id):
    doctor_id = session.get('user_id')

    has_appointment = Appointment.query.filter_by(doctor_id=doctor_id, patient_id=patient_id).first()
    if not has_appointment:
        flash('You can only view history for patients who have booked with you.', 'error')
        return redirect(url_for('doctor_dashboard'))

    try:
        access_log = PatientDataAccessLog(patient_id=patient_id, viewer_id=doctor_id, viewer_role='doctor')
        db.session.add(access_log)
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        app.logger.error(f"Access log error: {e}")

    patient = db.get_or_404(User, patient_id)
    vitals_history = Vital.query.filter_by(patient_id=patient_id).order_by(Vital.recorded_at.desc()).limit(20).all()
    symptom_history = SymptomLog.query.filter_by(patient_id=patient_id).order_by(SymptomLog.created_at.desc()).limit(20).all()
    medicine_history = Medicine.query.filter_by(patient_id=patient_id).order_by(Medicine.created_at.desc()).all()
    visit_history = Appointment.query.options(joinedload(Appointment.doctor)).filter_by(patient_id=patient_id, status='completed').order_by(Appointment.completed_at.desc()).limit(20).all()
    documents = Document.query.filter_by(patient_id=patient_id).order_by(Document.uploaded_at.desc()).all()

    return render_template(
        'patient_history_view.html',
        patient=patient,
        vitals_history=vitals_history,
        symptom_history=symptom_history,
        medicine_history=medicine_history,
        visit_history=visit_history,
        documents=documents,
        current_doctor_id=doctor_id
    )

@app.route('/chat')
@login_required
@role_required('patient', 'doctor')
def chat_list():
    user_id = session.get('user_id')
    role = session.get('role')

    if role == 'patient':
        appts = Appointment.query.options(joinedload(Appointment.doctor)).filter(
            Appointment.patient_id == user_id,
            Appointment.status.in_(['accepted', 'completed'])
        ).order_by(Appointment.appointment_date.desc()).all()
    else:
        appts = Appointment.query.options(joinedload(Appointment.patient)).filter(
            Appointment.doctor_id == user_id,
            Appointment.status.in_(['accepted', 'completed'])
        ).order_by(Appointment.appointment_date.desc()).all()

    # Batch-fetch every relevant message in one query (newest first), then
    # take the first occurrence per appointment in Python - that's the
    # latest message for that thread. This replaces what used to be one
    # separate Message query PER conversation (on top of a separate
    # lazy-load per conversation for the partner's name), which meant a
    # patient or doctor with several chat threads triggered roughly
    # 2x-per-conversation extra database round trips on every visit to
    # this page.
    appointment_ids = [a.id for a in appts]
    last_message_by_appointment_id = {}
    if appointment_ids:
        all_messages = Message.query.filter(
            Message.appointment_id.in_(appointment_ids)
        ).order_by(Message.created_at.desc()).all()
        for msg in all_messages:
            if msg.appointment_id not in last_message_by_appointment_id:
                last_message_by_appointment_id[msg.appointment_id] = msg

    conversations = []
    for appt in appts:
        partner = appt.doctor if role == 'patient' else appt.patient
        last_msg = last_message_by_appointment_id.get(appt.id)
        conversations.append({'appointment': appt, 'partner': partner, 'last_message': last_msg})

    conversations.sort(key=lambda c: c['last_message'].created_at if c['last_message'] else datetime.min, reverse=True)

    return render_template('chat_list.html', conversations=conversations)

@app.route('/chat/<int:appointment_id>', methods=['GET', 'POST'])
@login_required
@role_required('patient', 'doctor')
def chat_thread(appointment_id):
    user_id = session.get('user_id')
    role = session.get('role')

    appt = db.get_or_404(Appointment, appointment_id)

    if role == 'patient' and appt.patient_id != user_id:
        flash('Unauthorized action.', 'error')
        return redirect(url_for('chat_list'))
    if role == 'doctor' and appt.doctor_id != user_id:
        flash('Unauthorized action.', 'error')
        return redirect(url_for('chat_list'))

    if appt.status not in ('accepted', 'completed'):
        flash('Chat is only available for accepted or completed appointments.', 'error')
        return redirect(url_for('chat_list'))

    other_user = appt.doctor if role == 'patient' else appt.patient

    if request.method == 'POST':
        content = request.form.get('content', '').strip()
        if content:
            try:
                msg = Message(appointment_id=appt.id, sender_id=user_id, content=content)
                db.session.add(msg)
                db.session.commit()
            except Exception as e:
                db.session.rollback()
                app.logger.error(f"Send message error: {e}")
                flash('Error sending message.', 'error')
        return redirect(url_for('chat_thread', appointment_id=appt.id))

    messages = Message.query.filter_by(appointment_id=appt.id).order_by(Message.created_at.asc()).all()

    return render_template('chat_thread.html', appointment=appt, other_user=other_user, messages=messages, my_user_id=user_id)

@app.route('/admin')
@login_required
@role_required('hospital', 'admin')
def admin_dashboard():
    execute_due_account_deletions()

    pending_doctors = User.query.filter_by(role='doctor', status='pending').all()
    approved_doctors = User.query.filter_by(role='doctor', status='approved').all()
    patients = User.query.filter_by(role='patient').all()

    appointment_rows = db.session.query(
        Appointment.status, Appointment.created_at, Appointment.doctor_id
    ).all()
    status_counts = Counter(row.status for row in appointment_rows)
    total_appointments = len(appointment_rows)

    today = get_ist_today()
    volume_by_day = []
    for i in range(13, -1, -1):
        day = today - timedelta(days=i)
        count = sum(1 for row in appointment_rows if row.created_at and row.created_at.date() == day)
        volume_by_day.append({'date': day.strftime('%m/%d'), 'count': count})

    doctor_counts = Counter(row.doctor_id for row in appointment_rows)
    top_doctors = []
    for doc_id, count in doctor_counts.most_common(5):
        doc = db.session.get(User, doc_id)
        if doc:
            top_doctors.append({'name': doc.full_name, 'count': count})

    stats = {
        'total_patients': len(patients),
        'active_doctors': len(approved_doctors),
        'pending_approvals': len(pending_doctors),
        'total_appointments': total_appointments,
        'completed_visits': status_counts.get('completed', 0),
    }

    pending_deletion_requests = AccountDeletionRequest.query.filter_by(status='pending').options(
        joinedload(AccountDeletionRequest.patient)
    ).order_by(AccountDeletionRequest.requested_at.asc()).all()
    approved_deletion_requests = AccountDeletionRequest.query.filter_by(status='approved').options(
        joinedload(AccountDeletionRequest.patient)
    ).order_by(AccountDeletionRequest.scheduled_for.asc()).all()

    return render_template(
        'admin.html',
        pending_doctors=pending_doctors,
        approved_doctors=approved_doctors,
        patients=patients,
        stats=stats,
        volume_by_day=volume_by_day,
        top_doctors=top_doctors,
        pending_deletion_requests=pending_deletion_requests,
        approved_deletion_requests=approved_deletion_requests
    )

# The full migration chain in order, so the diagnostic page can show it
# alongside what Alembic currently thinks and what actually exists.
MIGRATION_CHAIN = [
    ('baseline_v1', '0001_baseline.py'),
    ('visit_summary_v1', '0002_visit_summary.py'),
    ('multi_contact_v1', '0003_multi_contact.py'),
    ('ai_symptom_v1', '0004_ai_symptom.py'),
    ('ai_chat_v1', '0005_ai_chat.py'),
    ('notifications_v1', '0006_notifications.py'),
    ('optional_time_v1', '0007_optional_time.py'),
    ('fk_indexes_v1', '0008_fk_indexes.py'),
    ('documents_v1', '0009_documents.py'),
    ('account_deletion_v1', '0010_account_deletion.py'),
    ('specialty_match_v1', '0011_specialty_match.py'),
    ('notification_read_at_v1', '0012_notification_read_at.py'),
    ('patient_note_v1', '0013_patient_note.py'),
    ('access_log_v1', '0014_access_log.py'),
    ('refill_request_v1', '0015_refill_request.py'),
]

def _migration_signature_present(revision_id, inspector):
    """Checks whether a given migration's actual schema change is
    genuinely present in the database - used to detect the real current
    state directly from the schema itself, rather than trusting Alembic's
    tracking table, which has drifted out of sync more than once now."""
    tables = inspector.get_table_names()

    if revision_id == 'baseline_v1':
        return 'user' in tables and 'appointment' in tables
    elif revision_id == 'visit_summary_v1':
        if 'appointment' not in tables:
            return False
        columns = {c['name'] for c in inspector.get_columns('appointment')}
        return 'diagnosis' in columns and 'visit_notes' in columns
    elif revision_id == 'multi_contact_v1':
        if 'emergency_contact' not in tables:
            return False
        unique_constraints = inspector.get_unique_constraints('emergency_contact')
        return not any(uc.get('column_names') == ['patient_id'] for uc in unique_constraints)
    elif revision_id == 'ai_symptom_v1':
        if 'symptom_log' not in tables:
            return False
        columns = {c['name'] for c in inspector.get_columns('symptom_log')}
        return 'ai_generated' in columns
    elif revision_id == 'ai_chat_v1':
        return 'ai_chat_message' in tables
    elif revision_id == 'notifications_v1':
        return 'follow_up_request' in tables and 'notification' in tables
    elif revision_id == 'optional_time_v1':
        if 'appointment' not in tables:
            return False
        columns = {c['name']: c for c in inspector.get_columns('appointment')}
        return columns.get('appointment_time', {}).get('nullable', False)
    elif revision_id == 'fk_indexes_v1':
        if 'appointment' not in tables:
            return False
        index_columns = set()
        for idx in inspector.get_indexes('appointment'):
            index_columns.update(idx.get('column_names', []))
        return 'patient_id' in index_columns and 'doctor_id' in index_columns
    elif revision_id == 'documents_v1':
        return 'document' in tables
    elif revision_id == 'account_deletion_v1':
        return 'account_deletion_request' in tables
    elif revision_id == 'specialty_match_v1':
        if 'symptom_log' not in tables:
            return False
        columns = {c['name'] for c in inspector.get_columns('symptom_log')}
        return 'suggested_specialty' in columns
    elif revision_id == 'notification_read_at_v1':
        if 'notification' not in tables:
            return False
        columns = {c['name'] for c in inspector.get_columns('notification')}
        return 'read_at' in columns
    elif revision_id == 'patient_note_v1':
        if 'appointment' not in tables:
            return False
        columns = {c['name'] for c in inspector.get_columns('appointment')}
        return 'patient_note' in columns
    elif revision_id == 'access_log_v1':
        return 'patient_data_access_log' in tables
    elif revision_id == 'refill_request_v1':
        return 'refill_request' in tables
    return False

def _detect_actual_revision(inspector):
    """Walks the migration chain in order and returns the latest revision
    whose signature is genuinely present - the true current schema state,
    independent of what the alembic_version tracking table claims."""
    detected = None
    for revision_id, _ in MIGRATION_CHAIN:
        if _migration_signature_present(revision_id, inspector):
            detected = revision_id
        else:
            break
    return detected

@app.route('/admin/db-diagnostic')
@login_required
@role_required('hospital', 'admin')
def admin_db_diagnostic():
    """Read-only diagnostic: shows what Alembic's tracking table thinks the
    current migration state is, what the schema itself actually shows
    (detected independently), and every table that exists. Never modifies
    anything."""
    try:
        tracked_revision = db.session.execute(text('SELECT version_num FROM alembic_version')).scalar()
    except Exception as e:
        db.session.rollback()  # Postgres aborts the whole transaction on error until rolled back
        tracked_revision = f'(could not read alembic_version table: {e})'

    inspector = inspect(db.engine)
    existing_tables = sorted(inspector.get_table_names())
    detected_revision = _detect_actual_revision(inspector)

    fix_available = (detected_revision is not None and detected_revision != tracked_revision)

    return render_template(
        'admin_db_diagnostic.html',
        tracked_revision=tracked_revision,
        detected_revision=detected_revision,
        existing_tables=existing_tables,
        migration_chain=MIGRATION_CHAIN,
        fix_available=fix_available
    )

@app.route('/admin/db-diagnostic/fix-tracking', methods=['POST'])
@login_required
@role_required('hospital', 'admin')
def admin_db_diagnostic_fix():
    """Corrects Alembic's tracked revision to match what the schema itself
    actually shows (detected independently via _detect_actual_revision),
    then runs upgrade() to apply anything genuinely still pending beyond
    that point - completing the fix in one action rather than requiring a
    separate redeploy afterward. Re-detects at the moment of the fix
    rather than trusting the earlier page load, as a safety guard."""
    inspector = inspect(db.engine)
    detected_revision = _detect_actual_revision(inspector)

    try:
        tracked_revision = db.session.execute(text('SELECT version_num FROM alembic_version')).scalar()
    except Exception as e:
        db.session.rollback()
        tracked_revision = None

    if detected_revision is None:
        flash('Could not detect a valid schema state - no change made.', 'error')
        return redirect(url_for('admin_db_diagnostic'))

    if detected_revision == tracked_revision:
        flash('Tracking already matches the actual schema state - no change needed.', 'success')
        return redirect(url_for('admin_db_diagnostic'))

    try:
        stamp(revision=detected_revision)
        app.logger.warning(f"Admin corrected Alembic tracking: {tracked_revision} -> {detected_revision}")
        upgrade()
        flash(f'Fixed: tracking corrected to {detected_revision} and any remaining pending migrations applied. No data was changed.', 'success')
    except Exception as e:
        flash(f'Fix failed: {e}', 'error')

    return redirect(url_for('admin_db_diagnostic'))

def csv_response(filename, header, rows):
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(header)
    writer.writerows(rows)
    return Response(
        output.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition': f'attachment; filename={filename}'}
    )

@app.route('/admin/export/patients')
@login_required
@role_required('hospital', 'admin')
def export_patients_csv():
    patients = User.query.filter_by(role='patient').order_by(User.created_at.desc()).all()
    rows = [
        [p.full_name, p.email, p.phone or '', p.status, p.created_at.strftime('%Y-%m-%d') if p.created_at else '']
        for p in patients
    ]
    return csv_response('medibro_patients.csv', ['Name', 'Email', 'Phone', 'Status', 'Registered'], rows)

@app.route('/admin/export/doctors')
@login_required
@role_required('hospital', 'admin')
def export_doctors_csv():
    doctors = User.query.filter_by(role='doctor').order_by(User.created_at.desc()).all()
    rows = [
        [d.full_name, d.email, d.specialty or '', d.phone or '', d.status, d.created_at.strftime('%Y-%m-%d') if d.created_at else '']
        for d in doctors
    ]
    return csv_response('medibro_doctors.csv', ['Name', 'Email', 'Specialty', 'Phone', 'Status', 'Registered'], rows)

@app.route('/admin/export/audit-log')
@login_required
@role_required('hospital', 'admin')
def export_audit_log_csv():
    logs = AdminAuditLog.query.order_by(AdminAuditLog.created_at.desc()).all()
    rows = [
        [
            log.created_at.strftime('%Y-%m-%d %H:%M') if log.created_at else '',
            log.admin.full_name if log.admin else 'Unknown',
            log.action,
            log.target_name or '',
            log.details or ''
        ]
        for log in logs
    ]
    return csv_response('medibro_audit_log.csv', ['Date', 'Admin', 'Action', 'Target', 'Details'], rows)

@app.route('/admin/verify/<int:doctor_id>/<action>', methods=['POST'])
@login_required
@role_required('hospital', 'admin')
def verify_doctor(doctor_id, action):
    try:
        doctor = db.get_or_404(User, doctor_id)
        if action == 'approve':
            doctor.status = 'approved'
            flash(f'Doctor {doctor.full_name} approved successfully!', 'success')
        elif action == 'reject':
            doctor.status = 'rejected'
            flash(f'Doctor {doctor.full_name} rejected.', 'error')

        log_entry = AdminAuditLog(
            admin_id=session.get('user_id'),
            action=f'doctor_{action}',
            target_name=doctor.full_name,
            details=f'Doctor account status set to {doctor.status}'
        )
        db.session.add(log_entry)
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        app.logger.error(f"Verify doctor error: {e}")
        flash('Error updating doctor verification.', 'error')
    return redirect(url_for('admin_dashboard'))

@app.route('/admin/toggle-user/<int:user_id>', methods=['POST'])
@login_required
@role_required('hospital', 'admin')
def toggle_user_status(user_id):
    try:
        user = db.get_or_404(User, user_id)
        if user.role not in ['hospital', 'admin']:
            if user.status == 'approved':
                user.status = 'suspended'
                flash(f'Account for {user.full_name} has been suspended.', 'error')
            else:
                user.status = 'approved'
                flash(f'Account for {user.full_name} has been reactivated.', 'success')

            log_entry = AdminAuditLog(
                admin_id=session.get('user_id'),
                action='toggle_user_status',
                target_name=user.full_name,
                details=f'Account status set to {user.status}'
            )
            db.session.add(log_entry)
            db.session.commit()
    except Exception as e:
        db.session.rollback()
        app.logger.error(f"Toggle user status error: {e}")
        flash('Error toggling user status.', 'error')
    return redirect(url_for('admin_dashboard'))

@app.route('/admin/audit-log')
@login_required
@role_required('hospital', 'admin')
def admin_audit_log():
    logs = AdminAuditLog.query.order_by(AdminAuditLog.created_at.desc()).limit(100).all()
    return render_template('audit_log.html', logs=logs)

@app.route('/admin/doctor/<int:doctor_id>/notes')
@login_required
@role_required('hospital', 'admin')
def admin_doctor_notes(doctor_id):
    doctor = db.get_or_404(User, doctor_id)
    appointments = Appointment.query.options(joinedload(Appointment.patient)).filter(
        Appointment.doctor_id == doctor_id,
        Appointment.status == 'completed',
        Appointment.patient_note.isnot(None)
    ).order_by(Appointment.completed_at.desc()).all()
    return render_template('admin_visit_notes.html', person=doctor, person_role='doctor', appointments=appointments)

@app.route('/admin/patient/<int:patient_id>/notes')
@login_required
@role_required('hospital', 'admin')
def admin_patient_notes(patient_id):
    patient = db.get_or_404(User, patient_id)

    try:
        access_log = PatientDataAccessLog(patient_id=patient_id, viewer_id=session.get('user_id'), viewer_role='admin')
        db.session.add(access_log)
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        app.logger.error(f"Access log error: {e}")

    appointments = Appointment.query.options(joinedload(Appointment.doctor)).filter(
        Appointment.patient_id == patient_id,
        Appointment.status == 'completed',
        Appointment.patient_note.isnot(None)
    ).order_by(Appointment.completed_at.desc()).all()
    return render_template('admin_visit_notes.html', person=patient, person_role='patient', appointments=appointments)

# --- VITALS ---
@app.route('/vitals', methods=['GET', 'POST'])
@login_required
@role_required('patient')
def vitals():
    patient_id = session.get('user_id')

    if request.method == 'POST':
        def parse_int(field):
            val = request.form.get(field, '').strip()
            return int(val) if val else None

        def parse_float(field):
            val = request.form.get(field, '').strip()
            return float(val) if val else None

        systolic = parse_int('systolic')
        diastolic = parse_int('diastolic')
        heart_rate = parse_int('heart_rate')
        spo2 = parse_int('spo2')
        temperature = parse_float('temperature')
        notes = request.form.get('notes', '').strip()

        if not any([systolic, diastolic, heart_rate, spo2, temperature]):
            flash('Please enter at least one vital reading.', 'error')
            return redirect(url_for('vitals'))

        try:
            entry = Vital(
                patient_id=patient_id,
                systolic=systolic,
                diastolic=diastolic,
                heart_rate=heart_rate,
                spo2=spo2,
                temperature=temperature,
                notes=notes
            )
            db.session.add(entry)
            db.session.commit()
            flash('Vitals logged successfully!', 'success')
        except Exception as e:
            db.session.rollback()
            app.logger.error(f"Vitals log error: {e}")
            flash('Error saving vitals. Please check your entries and try again.', 'error')

        return redirect(url_for('vitals'))

    history = Vital.query.filter_by(patient_id=patient_id).order_by(Vital.recorded_at.desc()).limit(20).all()
    latest = history[0] if history else None

    bp_chart_data = [
        {'date': v.recorded_at.strftime('%m/%d'), 'systolic': v.systolic, 'diastolic': v.diastolic}
        for v in reversed(history) if v.systolic and v.diastolic
    ]
    hr_chart_data = [
        {'date': v.recorded_at.strftime('%m/%d'), 'heart_rate': v.heart_rate}
        for v in reversed(history) if v.heart_rate
    ]

    return render_template(
        'vitals.html', history=history, latest=latest,
        bp_chart_data=bp_chart_data, hr_chart_data=hr_chart_data
    )

SYMPTOM_OPTIONS = [
    'Fever', 'Cough', 'Chest pain', 'Shortness of breath', 'Headache',
    'Nausea or vomiting', 'Dizziness', 'Rash', 'Abdominal pain', 'Fatigue'
]
EMERGENCY_SYMPTOMS = {'Chest pain', 'Shortness of breath'}

# Deterministic fallback for the specialty-matching feature - always
# available even if the AI enhancement is down or misconfigured. The AI
# path (get_ai_specialty_suggestion) can use the free-text description
# for more nuance, but this mapping is what keeps a reasonable suggestion
# working even without it.
SYMPTOM_TO_SPECIALTY = {
    'Fever': 'General Physician',
    'Cough': 'General Physician',
    'Chest pain': 'Cardiology',
    'Shortness of breath': 'Pulmonology',
    'Headache': 'Neurology',
    'Nausea or vomiting': 'Gastroenterology',
    'Dizziness': 'Neurology',
    'Rash': 'Dermatology',
    'Abdominal pain': 'Gastroenterology',
    'Fatigue': 'General Physician',
}

def build_patient_context_summary(patient_id):
    """Builds a compact summary of a patient's recent health data (latest
    vitals, active medicines, recent symptom logs) to personalize AI
    responses. This is informational context only - it does NOT loosen
    any of the strict no-diagnosis/no-dosing rules already enforced in the
    system prompts; both prompts explicitly instruct the AI to treat this
    as background context, not a basis for clinical judgments. Returns an
    empty string if the patient has no logged data yet, so prompts degrade
    gracefully rather than including an awkward empty context block."""
    today = get_ist_today()

    latest_vital = Vital.query.filter_by(patient_id=patient_id).order_by(Vital.recorded_at.desc()).first()
    active_medicines = Medicine.query.filter(
        Medicine.patient_id == patient_id,
        db.or_(Medicine.start_date == None, Medicine.start_date <= today),
        db.or_(Medicine.end_date == None, Medicine.end_date >= today)
    ).all()
    recent_symptoms = SymptomLog.query.filter_by(patient_id=patient_id).order_by(SymptomLog.created_at.desc()).limit(3).all()

    lines = []

    if latest_vital:
        parts = []
        if latest_vital.systolic and latest_vital.diastolic:
            parts.append(f'BP {latest_vital.systolic}/{latest_vital.diastolic}')
        if latest_vital.heart_rate:
            parts.append(f'HR {latest_vital.heart_rate} bpm')
        if latest_vital.spo2:
            parts.append(f'SpO2 {latest_vital.spo2}%')
        if latest_vital.temperature:
            parts.append(f'Temp {latest_vital.temperature}F')
        if parts:
            lines.append('Most recent vitals: ' + ', '.join(parts))

    if active_medicines:
        med_names = ', '.join(m.name for m in active_medicines[:8])
        lines.append(f'Current medicines: {med_names}')

    if recent_symptoms:
        symptom_summaries = [f'{s.symptoms} (severity: {s.severity})' for s in recent_symptoms]
        lines.append('Recently logged symptoms: ' + '; '.join(symptom_summaries))

    if not lines:
        return ''

    return 'Patient context for reference only, NOT a basis for diagnosis: ' + ' | '.join(lines)

SYMPTOM_AI_SYSTEM_PROMPT = (
    "You are a cautious health-guidance assistant inside a patient portal called MediBro. "
    "A patient has logged symptoms below. Give brief, general guidance in 2-4 short sentences "
    "on sensible next steps.\n\n"
    "The message may begin with a 'Patient context' line summarizing the patient's recent "
    "vitals, medicines, or symptom history. Use it only to make your guidance more relevant "
    "(e.g. noting a relevant pattern) - never to name a diagnosis, never to comment on specific "
    "medication dosing or interactions, and never to treat it as confirmed clinical information.\n\n"
    "Strict rules:\n"
    "- Never name or suggest a specific medical diagnosis or condition.\n"
    "- Never recommend a specific medication, dosage, or drug, even if the patient context "
    "lists medicines they're already taking.\n"
    "- If anything described sounds potentially urgent or serious, clearly tell the patient to "
    "seek medical care promptly or contact emergency services - do not downplay it.\n"
    "- Always end by suggesting they see a doctor if symptoms worsen or persist.\n"
    "- Keep the tone calm and clear. No medical jargon. Keep the whole response under 80 words.\n"
    "- Write in plain prose only: no markdown, no asterisks, no bullet points, no headers. "
    "Just complete, ordinary sentences."
)

def get_ai_symptom_guidance(selected_symptoms, severity, description, patient_id=None):
    """Returns AI-generated guidance text, or None if the AI is unavailable
    or the call fails for any reason - callers must fall back to the
    rule-based guidance in that case. Never called for emergency-level
    cases; those are handled deterministically before this is reached."""
    if not gemini_client:
        return None
    try:
        from google.genai import types
        symptoms_text = ', '.join(selected_symptoms) if selected_symptoms else 'none selected'
        context_summary = build_patient_context_summary(patient_id) if patient_id else ''
        user_prompt = (
            (context_summary + '\n\n' if context_summary else '')
            + f"Symptoms: {symptoms_text}. Severity: {severity}. "
            f"Additional details from patient: {description or 'none provided'}."
        )
        response = gemini_client.models.generate_content(
            model=GEMINI_MODEL,
            contents=user_prompt,
            config=types.GenerateContentConfig(
                system_instruction=SYMPTOM_AI_SYSTEM_PROMPT,
                # On Gemini's newer "thinking" models, max_output_tokens is a
                # COMBINED budget covering invisible reasoning tokens AND the
                # visible answer together - not just the visible text. Without
                # disabling thinking, the visible answer can get cut off mid-
                # sentence because reasoning silently ate most of the budget.
                # This task needs no multi-step reasoning, so thinking is
                # disabled entirely and the full budget goes to the answer.
                thinking_config=types.ThinkingConfig(thinking_budget=0),
                max_output_tokens=400,
                temperature=0.4,
            )
        )

        # Check finish_reason before trusting the text - a non-clean stop
        # (e.g. still hit the token limit despite the above) means the
        # response may be incomplete, and incomplete health guidance should
        # never reach a patient - fall back to the safe rule-based message.
        candidates = getattr(response, 'candidates', None)
        if candidates:
            finish_reason = getattr(candidates[0], 'finish_reason', None)
            finish_reason_str = getattr(finish_reason, 'name', None) or (str(finish_reason) if finish_reason else '')
            if finish_reason_str and finish_reason_str != 'STOP':
                app.logger.warning(f"Gemini response did not finish cleanly (finish_reason={finish_reason_str}), falling back to rule-based guidance")
                return None

        text = (response.text or '').strip()
        if not text:
            return None

        # Belt-and-suspenders sanity check: a complete guidance message should
        # end with normal punctuation and contain no stray markdown/formatting
        # artifacts. Catches truncation or formatting issues that slip through
        # the checks above.
        if text[-1] not in '.!?':
            app.logger.warning("Gemini response appears truncated (no ending punctuation), falling back to rule-based guidance")
            return None
        if '**' in text or '##' in text or text.lstrip().startswith('*'):
            app.logger.warning("Gemini response contains formatting artifacts, falling back to rule-based guidance")
            return None

        return text
    except Exception as e:
        app.logger.warning(f"Gemini symptom guidance failed, falling back to rule-based guidance: {e}")
        return None

def get_ai_specialty_suggestion(selected_symptoms, description, available_specialties):
    """Suggests a specialty using AI, informed by the free-text description
    for nuance beyond the fixed symptom checkboxes. Returns None on any
    failure, unclear response, or if the AI's answer doesn't exactly match
    one of the real available specialties - callers fall back to the
    deterministic SYMPTOM_TO_SPECIALTY mapping in that case."""
    if not gemini_client or not available_specialties:
        return None
    try:
        from google.genai import types
        symptoms_text = ', '.join(selected_symptoms) if selected_symptoms else 'none selected'
        specialties_text = ', '.join(available_specialties)
        user_prompt = (
            f"Symptoms: {symptoms_text}. Additional details from patient: {description or 'none provided'}.\n\n"
            f"Available specialties on this platform: {specialties_text}."
        )
        response = gemini_client.models.generate_content(
            model=GEMINI_MODEL,
            contents=user_prompt,
            config=types.GenerateContentConfig(
                system_instruction=(
                    "You match a patient's described symptoms to the single most relevant "
                    "medical specialty from a provided list. Respond with ONLY the exact "
                    "specialty name copied from that list, character for character - no "
                    "punctuation, no explanation, nothing else. If nothing in the list is "
                    "clearly relevant, respond with exactly: NONE"
                ),
                thinking_config=types.ThinkingConfig(thinking_budget=0),
                max_output_tokens=30,
                temperature=0.2,
            )
        )

        candidates = getattr(response, 'candidates', None)
        if candidates:
            finish_reason = getattr(candidates[0], 'finish_reason', None)
            finish_reason_str = getattr(finish_reason, 'name', None) or (str(finish_reason) if finish_reason else '')
            if finish_reason_str and finish_reason_str != 'STOP':
                return None

        text = (response.text or '').strip()
        if not text or text == 'NONE':
            return None

        # Must exactly match a real, currently-available specialty - guards
        # against the AI inventing or slightly rewording something that
        # doesn't actually exist on the platform.
        if text in available_specialties:
            return text
        return None
    except Exception as e:
        app.logger.warning(f"AI specialty suggestion failed, falling back to deterministic mapping: {e}")
        return None

def suggest_specialty_for_symptoms(selected_symptoms, description):
    """Returns a suggested specialty string, or None. Never suggests
    anything alongside emergency symptoms - the priority there is
    seeking care immediately, not browsing for a doctor. Only ever
    returns a specialty that has at least one real, currently-approved
    doctor, so the suggestion always leads somewhere bookable rather
    than a dead end."""
    if not selected_symptoms:
        return None

    if any(s in EMERGENCY_SYMPTOMS for s in selected_symptoms):
        return None

    available_specialties = [
        row[0] for row in db.session.query(User.specialty).filter(
            User.role == 'doctor', User.status == 'approved',
            User.specialty.isnot(None), User.specialty != ''
        ).distinct().all()
    ]
    if not available_specialties:
        return None

    ai_suggestion = get_ai_specialty_suggestion(selected_symptoms, description, available_specialties)
    if ai_suggestion:
        return ai_suggestion

    for symptom in selected_symptoms:
        mapped = SYMPTOM_TO_SPECIALTY.get(symptom)
        if mapped and mapped in available_specialties:
            return mapped

    return None

def get_ai_trend_explanation(patient_id):
    """Returns a plain-language explanation of the patient's recent vitals
    and symptom trends, or None if there's not enough data or the AI call
    fails - callers must show a graceful fallback in that case. This is
    an on-demand, read-only feature (patient clicks a button to request
    it) - not a real-time safety decision, so it doesn't need the
    deterministic-before-AI pattern the emergency detectors use, but it
    keeps the same fallback/sanity-check discipline as the other AI
    features."""
    recent_vitals = Vital.query.filter_by(patient_id=patient_id).order_by(Vital.recorded_at.desc()).limit(10).all()
    recent_symptoms = SymptomLog.query.filter_by(patient_id=patient_id).order_by(SymptomLog.created_at.desc()).limit(10).all()

    if not recent_vitals and not recent_symptoms:
        return None

    if not gemini_client:
        return None

    try:
        from google.genai import types

        vitals_text = 'None recorded yet.'
        if recent_vitals:
            vitals_lines = []
            for v in reversed(recent_vitals):  # oldest first, so trend reads chronologically
                parts = []
                if v.systolic and v.diastolic:
                    parts.append(f'BP {v.systolic}/{v.diastolic}')
                if v.heart_rate:
                    parts.append(f'HR {v.heart_rate}')
                if v.spo2:
                    parts.append(f'SpO2 {v.spo2}%')
                if v.temperature:
                    parts.append(f'Temp {v.temperature}F')
                vitals_lines.append(f"{v.recorded_at.strftime('%b %d')}: {', '.join(parts)}")
            vitals_text = '\n'.join(vitals_lines)

        symptoms_text = 'None recorded yet.'
        if recent_symptoms:
            symptoms_lines = [
                f"{s.created_at.strftime('%b %d')}: {s.symptoms} (severity: {s.severity})"
                for s in reversed(recent_symptoms)
            ]
            symptoms_text = '\n'.join(symptoms_lines)

        user_prompt = f"Recent vitals (oldest to newest):\n{vitals_text}\n\nRecent symptom logs (oldest to newest):\n{symptoms_text}"

        response = gemini_client.models.generate_content(
            model=GEMINI_MODEL,
            contents=user_prompt,
            config=types.GenerateContentConfig(
                system_instruction=(
                    "You explain a patient's own recent vitals and symptom history back to "
                    "them in plain, reassuring language - point out any genuine patterns "
                    "(trending up, trending down, staying stable), but never diagnose, never "
                    "tell them what to do medically, and always end by suggesting they "
                    "mention anything notable to their doctor. Keep it to 3-4 short "
                    "sentences. If there isn't enough data to say anything meaningful, say "
                    "so plainly rather than inventing a pattern."
                ),
                thinking_config=types.ThinkingConfig(thinking_budget=0),
                max_output_tokens=250,
                temperature=0.4,
            )
        )

        candidates = getattr(response, 'candidates', None)
        if candidates:
            finish_reason = getattr(candidates[0], 'finish_reason', None)
            finish_reason_str = getattr(finish_reason, 'name', None) or (str(finish_reason) if finish_reason else '')
            if finish_reason_str and finish_reason_str != 'STOP':
                return None

        text = (response.text or '').strip()
        if not text or len(text) < 10:
            return None

        return text
    except Exception as e:
        app.logger.warning(f"AI trend explanation failed: {e}")
        return None

@app.route('/symptoms', methods=['GET', 'POST'])
@login_required
@role_required('patient')
def symptoms():
    patient_id = session.get('user_id')

    if request.method == 'POST':
        selected = request.form.getlist('symptoms')
        severity = request.form.get('severity', 'mild')
        description = request.form.get('description', '').strip()

        if not selected and not description:
            flash('Please select at least one symptom or describe what you are experiencing.', 'error')
            return redirect(url_for('symptoms'))

        has_emergency_symptom = any(s in EMERGENCY_SYMPTOMS for s in selected)
        ai_generated = False

        if has_emergency_symptom or severity == 'severe':
            guidance = ('This could be serious. Please seek emergency care immediately '
                        'or call your local emergency number. Do not wait.')
            flash_category = 'error'
        else:
            ai_guidance = get_ai_symptom_guidance(selected, severity, description, patient_id=patient_id)
            if ai_guidance:
                guidance = ai_guidance
                ai_generated = True
                flash_category = 'error' if (severity == 'moderate' or len(selected) >= 3) else 'success'
            elif severity == 'moderate' or len(selected) >= 3:
                guidance = ('Your symptoms may need medical attention. Please book an '
                            'appointment with a doctor soon.')
                flash_category = 'error'
            else:
                guidance = ('Monitor your symptoms, rest, and stay hydrated. Book an '
                            'appointment if things worsen or persist beyond a few days.')
                flash_category = 'success'

        try:
            suggested_specialty = suggest_specialty_for_symptoms(selected, description)
            entry = SymptomLog(
                patient_id=patient_id,
                symptoms=', '.join(selected) if selected else 'Not specified',
                severity=severity,
                description=description,
                guidance=guidance,
                ai_generated=ai_generated,
                suggested_specialty=suggested_specialty
            )
            db.session.add(entry)
            db.session.commit()
            flash(guidance, flash_category)
        except Exception as e:
            db.session.rollback()
            app.logger.error(f"Symptom log error: {e}")
            flash('Error saving your symptom log. Please try again.', 'error')

        return redirect(url_for('symptoms'))

    history = SymptomLog.query.filter_by(patient_id=patient_id).order_by(SymptomLog.created_at.desc()).limit(20).all()
    return render_template('symptoms.html', symptom_options=SYMPTOM_OPTIONS, history=history)

# --- AI HEALTH CHAT ---
# An open-ended conversation is a larger safety surface than the structured
# symptom checker, since a patient can type anything. The crisis check below
# runs on every message BEFORE the AI is ever consulted, exactly like the
# emergency-symptom check in the symptom checker - deterministic, never
# dependent on the AI getting it right.
CRISIS_KEYWORDS = [
    # possible physical emergencies
    'chest pain', "can't breathe", 'cant breathe', 'cannot breathe', 'difficulty breathing',
    'severe bleeding', 'heavy bleeding', 'unconscious', 'unresponsive',
    'overdose', 'heart attack', 'stroke', 'seizure', 'choking', 'anaphylaxis',
    # possible mental health crisis
    'kill myself', 'want to die', 'end my life', 'ending my life', 'suicidal', 'suicide',
    'ending it all', "don't want to be alive", 'dont want to be alive',
    'hurt myself', 'harm myself', 'self harm', 'self-harm',
]

CRISIS_RESPONSE_MESSAGE = (
    "This sounds serious, and I want to make sure you get real help right now, not just "
    "a chat response. If this is a medical emergency, please call your local emergency "
    "number immediately. If you're thinking about suicide or self-harm, please reach out "
    "to a crisis line right away - in the US you can call or text 988 (Suicide & Crisis "
    "Lifeline). You can also use the SOS page in this app to alert your emergency contact. "
    "Please don't wait to reach out."
)

AI_CHAT_SYSTEM_PROMPT = (
    "You are a general health guidance assistant inside a patient portal called MediBro. "
    "You can discuss general health questions and help patients think through non-urgent "
    "concerns they describe.\n\n"
    "The message you receive may begin with a 'Patient context' line summarizing the "
    "patient's recent vitals, medicines, or symptom history. Use it only to make your "
    "guidance more relevant to their situation - never to name a diagnosis, never to comment "
    "on specific medication dosing or interactions, and never to treat it as confirmed "
    "clinical information. It may then include 'Here is the conversation so far:' followed by "
    "prior turns labeled Patient/Assistant, then end with 'New message from patient:' - only "
    "respond to that new message, using the prior turns and any patient context as background. "
    "Do not repeat the conversation history or patient context back, and do not comment on "
    "this formatting.\n\n"
    "Strict rules:\n"
    "- Never name or suggest a specific medical diagnosis or condition.\n"
    "- Never recommend a specific medication, dosage, or drug, even if the patient context "
    "lists medicines they're already taking.\n"
    "- If a message describes anything that could be a medical emergency or a mental "
    "health crisis, tell the patient clearly to seek emergency care or a crisis line "
    "immediately - do not attempt to handle it yourself.\n"
    "- If asked something unrelated to health, politely redirect to health topics - this "
    "chat is for health guidance only.\n"
    "- Encourage booking an appointment with a doctor for anything that needs real "
    "follow-up or hasn't improved.\n"
    "- Keep the tone calm, warm, and clear. No medical jargon.\n"
    "- Write in plain prose only: no markdown, no asterisks, no bullet points, no headers. "
    "Just complete, ordinary sentences.\n"
    "- Keep responses concise - usually 2-5 sentences, appropriate for a chat "
    "conversation, not a long essay."
)

AI_CHAT_HISTORY_LIMIT = 10
AI_CHAT_FALLBACK_MESSAGE = (
    "I'm having trouble responding right now. Please try again in a moment, or reach out "
    "to your doctor if this is something you'd like to discuss soon."
)

def detect_crisis(text):
    lowered = text.lower()
    return any(kw in lowered for kw in CRISIS_KEYWORDS)

def get_ai_chat_response(prior_messages, new_message_text, patient_id=None):
    """prior_messages: list of AIChatMessage, oldest to newest, NOT including
    the new message. Returns AI response text, or None if unavailable/failed
    - caller shows AI_CHAT_FALLBACK_MESSAGE in that case. Never called when
    detect_crisis() has already matched; that's handled deterministically
    before this is reached.

    Conversation history is folded into a single text prompt rather than
    built as structured multi-turn Content/Part objects. The latter caused
    a real 400 INVALID_ARGUMENT error from the live API that couldn't be
    fully diagnosed without the real SDK available to test against - this
    uses the exact same simple contents=<string> request shape already
    proven working in production by the symptom checker."""
    if not gemini_client:
        return None
    try:
        from google.genai import types

        context_summary = build_patient_context_summary(patient_id) if patient_id else ''

        history_lines = []
        for msg in prior_messages:
            speaker = 'Patient' if msg.sender == 'patient' else 'Assistant'
            history_lines.append(f'{speaker}: {msg.content}')

        if history_lines:
            prompt = (
                (context_summary + '\n\n' if context_summary else '')
                + 'Here is the conversation so far:\n'
                + '\n'.join(history_lines)
                + f'\n\nNew message from patient: {new_message_text}'
            )
        else:
            prompt = (context_summary + '\n\n' if context_summary else '') + new_message_text

        response = gemini_client.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                system_instruction=AI_CHAT_SYSTEM_PROMPT,
                thinking_config=types.ThinkingConfig(thinking_budget=0),
                # Matched exactly to the symptom checker's proven-working
                # values (was max_output_tokens=500, temperature=0.5) to
                # isolate whether one of those specific values was the
                # actual trigger for a 400 INVALID_ARGUMENT error that
                # persisted even after switching to the same request shape.
                max_output_tokens=400,
                temperature=0.4,
            )
        )

        candidates = getattr(response, 'candidates', None)
        if candidates:
            finish_reason = getattr(candidates[0], 'finish_reason', None)
            finish_reason_str = getattr(finish_reason, 'name', None) or (str(finish_reason) if finish_reason else '')
            if finish_reason_str and finish_reason_str != 'STOP':
                app.logger.warning(f"Gemini chat response did not finish cleanly (finish_reason={finish_reason_str}), using fallback")
                return None

        text = (response.text or '').strip()
        if not text:
            return None
        if text[-1] not in '.!?':
            app.logger.warning("Gemini chat response appears truncated, using fallback")
            return None
        if '**' in text or '##' in text or text.lstrip().startswith('*'):
            app.logger.warning("Gemini chat response contains formatting artifacts, using fallback")
            return None

        return text
    except Exception as e:
        app.logger.warning(f"Gemini chat response failed, using fallback: {e}")
        return None

@app.route('/ai-chat', methods=['GET', 'POST'])
@login_required
@role_required('patient')
def ai_chat():
    patient_id = session.get('user_id')

    if request.method == 'POST':
        user_message = request.form.get('message', '').strip()
        if not user_message:
            flash('Please enter a message.', 'error')
            return redirect(url_for('ai_chat'))

        # Fetch history BEFORE saving the new message, so it isn't duplicated
        # when passed to get_ai_chat_response() alongside the new message.
        prior_messages = AIChatMessage.query.filter_by(patient_id=patient_id).order_by(
            AIChatMessage.created_at.desc()
        ).limit(AI_CHAT_HISTORY_LIMIT).all()
        prior_messages = list(reversed(prior_messages))

        try:
            patient_msg = AIChatMessage(patient_id=patient_id, sender='patient', content=user_message)
            db.session.add(patient_msg)
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            app.logger.error(f"AI chat save patient message error: {e}")
            flash('Error sending message. Please try again.', 'error')
            return redirect(url_for('ai_chat'))

        if detect_crisis(user_message):
            reply_text = CRISIS_RESPONSE_MESSAGE
            is_crisis = True
        else:
            ai_reply = get_ai_chat_response(prior_messages, user_message, patient_id=patient_id)
            reply_text = ai_reply if ai_reply else AI_CHAT_FALLBACK_MESSAGE
            is_crisis = False

        try:
            ai_msg = AIChatMessage(patient_id=patient_id, sender='ai', content=reply_text, is_crisis_response=is_crisis)
            db.session.add(ai_msg)
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            app.logger.error(f"AI chat save reply error: {e}")

        return redirect(url_for('ai_chat'))

    messages = AIChatMessage.query.filter_by(patient_id=patient_id).order_by(AIChatMessage.created_at.asc()).all()
    return render_template('ai_chat.html', messages=messages)

@app.route('/sos', methods=['GET', 'POST'])
@login_required
@role_required('patient')
def sos():
    patient_id = session.get('user_id')
    contacts = EmergencyContact.query.filter_by(patient_id=patient_id).order_by(EmergencyContact.id.asc()).all()

    if request.method == 'POST':
        action = request.form.get('action')

        if action == 'save_contact':
            name = request.form.get('contact_name', '').strip()
            phone = request.form.get('contact_phone', '').strip()
            relation = request.form.get('relation', '').strip()

            if not name or not phone:
                flash('Please provide a contact name and phone number.', 'error')
                return redirect(url_for('sos'))

            if len(contacts) >= 5:
                flash('You can save up to 5 emergency contacts. Remove one before adding another.', 'error')
                return redirect(url_for('sos'))

            try:
                contact = EmergencyContact(
                    patient_id=patient_id, contact_name=name,
                    contact_phone=phone, relation=relation
                )
                db.session.add(contact)
                db.session.commit()
                flash('Emergency contact saved.', 'success')
            except Exception as e:
                db.session.rollback()
                app.logger.error(f"Emergency contact save error: {e}")
                flash('Error saving emergency contact.', 'error')

        elif action == 'trigger_sos':
            try:
                event = SosEvent(patient_id=patient_id, notes='SOS triggered from patient portal')
                db.session.add(event)
                db.session.commit()
                flash('SOS alert logged. Please call your emergency contact or local emergency number now.', 'error')
            except Exception as e:
                db.session.rollback()
                app.logger.error(f"SOS event error: {e}")
                flash('Error logging SOS alert, but please still call for help if needed.', 'error')

        return redirect(url_for('sos'))

    recent_events = SosEvent.query.filter_by(patient_id=patient_id).order_by(SosEvent.created_at.desc()).limit(10).all()

    return render_template('sos.html', contacts=contacts, recent_events=recent_events)

@app.route('/sos/contact/<int:contact_id>/delete', methods=['POST'])
@login_required
@role_required('patient')
def delete_emergency_contact(contact_id):
    try:
        contact = db.get_or_404(EmergencyContact, contact_id)
        if contact.patient_id != session.get('user_id'):
            flash('Unauthorized action.', 'error')
            return redirect(url_for('sos'))
        db.session.delete(contact)
        db.session.commit()
        flash('Emergency contact removed.', 'success')
    except Exception as e:
        db.session.rollback()
        app.logger.error(f"Delete emergency contact error: {e}")
        flash('Error removing emergency contact.', 'error')
    return redirect(url_for('sos'))

def classify_time_period(time_str):
    try:
        hour = int(time_str.split(':')[0])
    except (ValueError, IndexError, AttributeError):
        return ('Other', '⏰')
    if hour < 12:
        return ('Morning', '☀️')
    elif hour < 17:
        return ('Afternoon', '🌤️')
    else:
        return ('Evening', '🌙')

def get_todays_schedule(patient_id):
    today = get_ist_today()
    active_meds = Medicine.query.options(joinedload(Medicine.doses)).filter(
        Medicine.patient_id == patient_id,
        db.or_(Medicine.start_date == None, Medicine.start_date <= today),
        db.or_(Medicine.end_date == None, Medicine.end_date >= today)
    ).all()

    all_dose_ids = [dose.id for med in active_meds for dose in med.doses]
    taken_dose_ids = set()
    if all_dose_ids:
        taken_dose_ids = {
            row.dose_id for row in MedicineDoseLog.query.filter(
                MedicineDoseLog.dose_id.in_(all_dose_ids),
                MedicineDoseLog.log_date == today,
                MedicineDoseLog.taken_at.isnot(None)
            ).with_entities(MedicineDoseLog.dose_id).all()
        }

    schedule = []
    for med in active_meds:
        for dose in med.doses:
            period, icon = classify_time_period(dose.time)
            schedule.append({
                'medicine': med,
                'dose': dose,
                'taken': dose.id in taken_dose_ids,
                'period': period,
                'icon': icon
            })
    schedule.sort(key=lambda s: s['dose'].time)
    return schedule

@app.route('/medicines', methods=['GET', 'POST'])
@login_required
@role_required('patient')
def medicines():
    patient_id = session.get('user_id')

    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        dosage = request.form.get('dosage', '').strip()
        frequency = request.form.get('frequency', '').strip()
        notes = request.form.get('notes', '').strip()
        start_date_str = request.form.get('start_date', '').strip()
        end_date_str = request.form.get('end_date', '').strip()
        dose_times = list(dict.fromkeys(t.strip() for t in request.form.getlist('dose_time') if t.strip()))

        if not name:
            flash('Please enter the medicine name.', 'error')
            return redirect(url_for('medicines'))

        try:
            start_date = datetime.strptime(start_date_str, '%Y-%m-%d').date() if start_date_str else None
            end_date = datetime.strptime(end_date_str, '%Y-%m-%d').date() if end_date_str else None
        except ValueError:
            flash('Invalid date format.', 'error')
            return redirect(url_for('medicines'))

        if start_date and end_date and end_date < start_date:
            flash('End date cannot be before start date.', 'error')
            return redirect(url_for('medicines'))

        try:
            med = Medicine(
                patient_id=patient_id, name=name, dosage=dosage,
                frequency=frequency, notes=notes,
                start_date=start_date, end_date=end_date
            )
            db.session.add(med)
            db.session.flush()

            for t in dose_times:
                db.session.add(MedicineDose(medicine_id=med.id, time=t))

            db.session.commit()
            flash('Medicine reminder added.', 'success')
        except Exception as e:
            db.session.rollback()
            app.logger.error(f"Add medicine error: {e}")
            flash('Error adding medicine reminder.', 'error')

        return redirect(url_for('medicines'))

    my_medicines = Medicine.query.options(joinedload(Medicine.doses)).filter_by(patient_id=patient_id).order_by(Medicine.created_at.desc()).all()
    todays_schedule = get_todays_schedule(patient_id)
    return render_template('medicines.html', medicines=my_medicines, todays_schedule=todays_schedule)

@app.route('/medicines/<int:med_id>/edit', methods=['GET', 'POST'])
@login_required
@role_required('patient')
def edit_medicine(med_id):
    med = db.get_or_404(Medicine, med_id)
    if med.patient_id != session.get('user_id'):
        flash('Unauthorized action.', 'error')
        return redirect(url_for('medicines'))

    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        dosage = request.form.get('dosage', '').strip()
        frequency = request.form.get('frequency', '').strip()
        notes = request.form.get('notes', '').strip()
        start_date_str = request.form.get('start_date', '').strip()
        end_date_str = request.form.get('end_date', '').strip()
        new_dose_times = [t.strip() for t in request.form.getlist('new_dose_time') if t.strip()]

        if not name:
            flash('Please enter the medicine name.', 'error')
            return redirect(url_for('edit_medicine', med_id=med_id))

        try:
            start_date = datetime.strptime(start_date_str, '%Y-%m-%d').date() if start_date_str else None
            end_date = datetime.strptime(end_date_str, '%Y-%m-%d').date() if end_date_str else None
        except ValueError:
            flash('Invalid date format.', 'error')
            return redirect(url_for('edit_medicine', med_id=med_id))

        if start_date and end_date and end_date < start_date:
            flash('End date cannot be before start date.', 'error')
            return redirect(url_for('edit_medicine', med_id=med_id))

        try:
            med.name = name
            med.dosage = dosage
            med.frequency = frequency
            med.notes = notes
            med.start_date = start_date
            med.end_date = end_date

            existing_times = {d.time for d in med.doses}
            for t in new_dose_times:
                if t not in existing_times:
                    db.session.add(MedicineDose(medicine_id=med.id, time=t))
                    existing_times.add(t)

            db.session.commit()
            flash('Medicine reminder updated.', 'success')
        except Exception as e:
            db.session.rollback()
            app.logger.error(f"Edit medicine error: {e}")
            flash('Error updating medicine reminder.', 'error')

        return redirect(url_for('edit_medicine', med_id=med_id))

    return render_template('medicine_edit.html', med=med)

@app.route('/medicines/<int:med_id>/delete', methods=['POST'])
@login_required
@role_required('patient')
def delete_medicine(med_id):
    try:
        med = db.get_or_404(Medicine, med_id)
        if med.patient_id != session.get('user_id'):
            flash('Unauthorized action.', 'error')
            return redirect(url_for('medicines'))
        db.session.delete(med)
        db.session.commit()
        flash('Medicine reminder removed.', 'success')
    except Exception as e:
        db.session.rollback()
        app.logger.error(f"Delete medicine error: {e}")
        flash('Error removing medicine reminder.', 'error')
    return redirect(url_for('medicines'))

@app.route('/medicines/<int:med_id>/request-refill', methods=['GET', 'POST'])
@login_required
@role_required('patient')
def request_refill(med_id):
    med = db.get_or_404(Medicine, med_id)
    if med.patient_id != session.get('user_id'):
        flash('Unauthorized action.', 'error')
        return redirect(url_for('medicines'))

    patient_id = session.get('user_id')
    treating_doctor_ids = {row[0] for row in db.session.query(Appointment.doctor_id).filter_by(patient_id=patient_id).distinct().all()}
    treating_doctors = User.query.filter(User.id.in_(treating_doctor_ids), User.role == 'doctor').all() if treating_doctor_ids else []

    if request.method == 'POST':
        doctor_id = request.form.get('doctor_id', type=int)
        note = request.form.get('note', '').strip()

        if not doctor_id or doctor_id not in treating_doctor_ids:
            flash('Please select a doctor you have an appointment history with.', 'error')
            return redirect(url_for('request_refill', med_id=med_id))

        try:
            refill_req = RefillRequest(medicine_id=med.id, patient_id=patient_id, doctor_id=doctor_id, note=note or None)
            db.session.add(refill_req)
            db.session.flush()

            notif = Notification(
                user_id=doctor_id,
                type='refill_request',
                message=f'{med.patient.full_name} requested a refill for {med.name}.',
                related_id=refill_req.id
            )
            db.session.add(notif)
            db.session.commit()
            flash('Refill request sent.', 'success')
        except Exception as e:
            db.session.rollback()
            app.logger.error(f"Request refill error: {e}")
            flash('Error sending refill request. Please try again.', 'error')

        return redirect(url_for('medicines'))

    return render_template('request_refill.html', med=med, treating_doctors=treating_doctors)

@app.route('/refill-request/<int:request_id>/respond', methods=['POST'])
@login_required
@role_required('doctor')
def respond_to_refill_request(request_id):
    refill_req = db.get_or_404(RefillRequest, request_id)
    if refill_req.doctor_id != session.get('user_id'):
        flash('Unauthorized action.', 'error')
        return redirect(url_for('doctor_dashboard'))
    if refill_req.status != 'pending':
        flash('This request has already been responded to.', 'error')
        return redirect(url_for('doctor_dashboard'))

    action = request.form.get('action')
    if action not in ('approve', 'deny'):
        flash('Invalid action.', 'error')
        return redirect(url_for('doctor_dashboard'))

    try:
        refill_req.status = 'approved' if action == 'approve' else 'denied'
        refill_req.doctor_response = request.form.get('doctor_response', '').strip() or None
        refill_req.responded_at = datetime.utcnow()

        if action == 'approve':
            med = refill_req.medicine
            extend_from = med.end_date if (med.end_date and med.end_date > get_ist_today()) else get_ist_today()
            med.end_date = extend_from + timedelta(days=30)

        patient_notif = Notification(
            user_id=refill_req.patient_id,
            type='refill_response',
            message=f"Your refill request for {refill_req.medicine.name} was {'approved' if action == 'approve' else 'denied'}.",
            related_id=refill_req.id
        )
        db.session.add(patient_notif)
        db.session.commit()
        flash(f'Refill request {"approved" if action == "approve" else "denied"}.', 'success')
    except Exception as e:
        db.session.rollback()
        app.logger.error(f"Respond to refill request error: {e}")
        flash('Error responding to refill request.', 'error')

    return redirect(url_for('doctor_dashboard'))

@app.route('/medicines/dose/<int:dose_id>/delete', methods=['POST'])
@login_required
@role_required('patient')
def delete_dose(dose_id):
    try:
        dose = db.get_or_404(MedicineDose, dose_id)
        if dose.medicine.patient_id != session.get('user_id'):
            flash('Unauthorized action.', 'error')
            return redirect(url_for('medicines'))
        med_id = dose.medicine_id
        db.session.delete(dose)
        db.session.commit()
        flash('Reminder time removed.', 'success')
        return redirect(url_for('edit_medicine', med_id=med_id))
    except Exception as e:
        db.session.rollback()
        app.logger.error(f"Delete dose error: {e}")
        flash('Error removing reminder time.', 'error')
        return redirect(url_for('medicines'))

@app.route('/medicines/dose/<int:dose_id>/toggle-taken', methods=['POST'])
@login_required
@role_required('patient')
def toggle_dose_taken(dose_id):
    try:
        dose = db.get_or_404(MedicineDose, dose_id)
        if dose.medicine.patient_id != session.get('user_id'):
            flash('Unauthorized action.', 'error')
            return redirect(url_for('medicines'))

        today = get_ist_today()
        day_start = datetime.combine(today, datetime.min.time()) - IST_OFFSET
        day_end = day_start + timedelta(days=1)
        log = MedicineDoseLog.query.filter_by(dose_id=dose.id, log_date=today).first()

        todays_reminder = Notification.query.filter(
            Notification.user_id == dose.medicine.patient_id,
            Notification.type == 'medicine_reminder',
            Notification.related_id == dose.id,
            Notification.created_at >= day_start,
            Notification.created_at < day_end
        ).first()

        if log and log.taken_at:
            log.taken_at = None
            if todays_reminder:
                todays_reminder.is_read = False
                todays_reminder.read_at = None
        elif log:
            log.taken_at = datetime.utcnow()
            if todays_reminder:
                todays_reminder.is_read = True
                todays_reminder.read_at = datetime.utcnow()
        else:
            log = MedicineDoseLog(dose_id=dose.id, log_date=today, taken_at=datetime.utcnow())
            db.session.add(log)
            if todays_reminder:
                todays_reminder.is_read = True
                todays_reminder.read_at = datetime.utcnow()

        db.session.commit()
    except Exception as e:
        db.session.rollback()
        app.logger.error(f"Toggle dose taken error: {e}")
        flash('Error updating dose status.', 'error')

    return redirect(url_for('medicines'))

@app.route('/profile', methods=['GET', 'POST'])
@login_required
def profile():
    if request.method == 'POST':
        current_password = request.form.get('current_password', '')
        new_password = request.form.get('new_password', '')
        confirm_password = request.form.get('confirm_password', '')

        user = db.session.get(User, session.get('user_id'))

        if not user or not check_password_hash(user.password_hash, current_password):
            flash('Current password is incorrect.', 'error')
            return redirect(url_for('profile'))

        if len(new_password) < 6:
            flash('New password must be at least 6 characters.', 'error')
            return redirect(url_for('profile'))

        if new_password != confirm_password:
            flash('New passwords do not match.', 'error')
            return redirect(url_for('profile'))

        try:
            user.password_hash = generate_password_hash(new_password, method='pbkdf2:sha256')
            db.session.commit()
            flash('Password updated successfully.', 'success')
        except Exception as e:
            db.session.rollback()
            app.logger.error(f"Password change error: {e}")
            flash('Error updating password.', 'error')

        return redirect(url_for('profile'))

    back_endpoint = dashboard_endpoint_for_role(session.get('role'))
    return render_template('profile.html', back_endpoint=back_endpoint)

@app.route('/profile/request-deletion', methods=['POST'])
@login_required
@role_required('patient')
def request_account_deletion():
    patient_id = session.get('user_id')

    existing = AccountDeletionRequest.query.filter(
        AccountDeletionRequest.patient_id == patient_id,
        AccountDeletionRequest.status.in_(['pending', 'approved'])
    ).first()
    if existing:
        return redirect(url_for('account_locked'))

    try:
        now = datetime.utcnow()
        req = AccountDeletionRequest(
            patient_id=patient_id,
            requested_at=now,
            scheduled_for=now + timedelta(days=7),
            status='pending'
        )
        db.session.add(req)
        db.session.flush()

        patient = db.session.get(User, patient_id)
        admins = User.query.filter(User.role.in_(['hospital', 'admin'])).all()
        for admin in admins:
            notif = Notification(
                user_id=admin.id,
                type='account_deletion_requested',
                message=f'{patient.full_name} ({patient.email}) requested account deletion, scheduled for {req.scheduled_for.strftime("%b %d, %Y")}. Review and approve in the admin dashboard.',
                related_id=req.id
            )
            db.session.add(notif)

        db.session.commit()
        flash('Account deletion requested.', 'success')
    except Exception as e:
        db.session.rollback()
        app.logger.error(f"Request account deletion error: {e}")
        flash('Error requesting account deletion.', 'error')

    return redirect(url_for('account_locked'))

@app.route('/account-deletion/<int:req_id>/approve', methods=['POST'])
@login_required
@role_required('hospital', 'admin')
def approve_account_deletion(req_id):
    req = db.get_or_404(AccountDeletionRequest, req_id)
    if req.status != 'pending':
        flash('This request has already been handled.', 'error')
        return redirect(url_for('admin_dashboard'))

    try:
        req.status = 'approved'
        req.approved_by_id = session.get('user_id')
        req.approved_at = datetime.utcnow()
        db.session.commit()
        flash('Deletion request approved.', 'success')
    except Exception as e:
        db.session.rollback()
        app.logger.error(f"Approve account deletion error: {e}")
        flash('Error approving deletion request.', 'error')

    return redirect(url_for('admin_dashboard'))

@app.route('/account-deletion/<int:req_id>/cancel', methods=['POST'])
@login_required
@role_required('patient', 'hospital', 'admin')
def cancel_account_deletion(req_id):
    req = db.get_or_404(AccountDeletionRequest, req_id)
    role = session.get('role')
    user_id = session.get('user_id')

    if role == 'patient' and req.patient_id != user_id:
        flash('Unauthorized action.', 'error')
        return redirect(url_for('account_locked'))

    if req.status not in ('pending', 'approved'):
        flash('This request has already been handled.', 'error')
        return redirect(url_for('account_locked') if role == 'patient' else url_for('admin_dashboard'))

    try:
        req.status = 'cancelled'
        db.session.commit()
        flash('Account deletion cancelled.', 'success')
    except Exception as e:
        db.session.rollback()
        app.logger.error(f"Cancel account deletion error: {e}")
        flash('Error cancelling deletion request.', 'error')

    return redirect(url_for('my_health') if role == 'patient' else url_for('admin_dashboard'))

@app.route('/account-locked')
@login_required
@role_required('patient')
def account_locked():
    patient_id = session.get('user_id')
    req = AccountDeletionRequest.query.filter(
        AccountDeletionRequest.patient_id == patient_id,
        AccountDeletionRequest.status.in_(['pending', 'approved'])
    ).order_by(AccountDeletionRequest.requested_at.desc()).first()

    if not req:
        return redirect(url_for('my_health'))

    return render_template('account_locked.html', deletion_request=req)

@app.before_request
def check_account_deletion_lock():
    """If a logged-in patient has a pending or approved deletion request,
    every page redirects to the locked status screen except the screen
    itself, the cancel action, and logout. Doctors and admins are never
    affected by this, regardless of what's happening with any patient's
    deletion request."""
    if session.get('role') == 'patient' and session.get('user_id'):
        allowed_endpoints = {'account_locked', 'cancel_account_deletion', 'logout', 'static'}
        if request.endpoint in allowed_endpoints:
            return None

        has_active_request = AccountDeletionRequest.query.filter(
            AccountDeletionRequest.patient_id == session['user_id'],
            AccountDeletionRequest.status.in_(['pending', 'approved'])
        ).first()
        if has_active_request:
            return redirect(url_for('account_locked'))
    return None

@app.route('/logout', methods=['POST'])
def logout():
    session.clear()
    flash('Logged out successfully.', 'success')
    return redirect(url_for('login'))

@app.errorhandler(404)
def not_found_error(e):
    return render_template('error.html', code=404, message="We couldn't find that page."), 404

@app.errorhandler(500)
def internal_error(e):
    db.session.rollback()
    app.logger.error(f"Unhandled server error: {e}")
    return render_template('error.html', code=500, message="Something went wrong on our end."), 500

if __name__ == '__main__':
    app.run(debug=os.environ.get('FLASK_DEBUG', 'false').lower() == 'true')
