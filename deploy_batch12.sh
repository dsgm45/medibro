#!/bin/bash
set -e

echo "=== MediBro: Unified My Health dashboard (new patient landing page) ==="

if [ ! -f "app.py" ]; then
  echo "ERROR: app.py not found. cd into your medimind project folder first, then re-run this script."
  exit 1
fi

mkdir -p templates

cat > app.py << 'FILEEOF_1'
import os
import re
import secrets
from datetime import datetime, timedelta
from functools import wraps
from collections import defaultdict
from flask import Flask, render_template, request, redirect, url_for, flash, session
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import text, inspect
from werkzeug.security import generate_password_hash, check_password_hash
from email_validator import validate_email, EmailNotValidError
from flask_wtf import CSRFProtect
from flask_migrate import Migrate, upgrade, stamp

app = Flask(__name__)
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

# Session cookie hardening. SESSION_COOKIE_SECURE is left off automatically
# for local development (where requests are plain HTTP), but forced on when
# running in production - controlled via the FLASK_ENV/RENDER env var Render
# sets automatically, so no manual toggle is needed on deploy.
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['SESSION_COOKIE_SECURE'] = bool(os.environ.get('RENDER'))

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
    patient_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    doctor_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    appointment_date = db.Column(db.String(50), nullable=False)
    appointment_time = db.Column(db.String(50), nullable=False)
    reason = db.Column(db.Text, nullable=True)
    phone_number = db.Column(db.String(20), nullable=True)
    status = db.Column(db.String(20), nullable=False, default='pending')
    follow_up_requested = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    patient = db.relationship('User', foreign_keys=[patient_id], backref='patient_appointments')
    doctor = db.relationship('User', foreign_keys=[doctor_id], backref='doctor_appointments')

class Vital(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    patient_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
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
    patient_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    symptoms = db.Column(db.Text, nullable=False)
    severity = db.Column(db.String(20), nullable=False, default='mild')
    description = db.Column(db.Text, nullable=True)
    guidance = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    patient = db.relationship('User', foreign_keys=[patient_id], backref='symptom_logs')

class EmergencyContact(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    patient_id = db.Column(db.Integer, db.ForeignKey('user.id'), unique=True, nullable=False)
    contact_name = db.Column(db.String(120), nullable=False)
    contact_phone = db.Column(db.String(20), nullable=False)
    relation = db.Column(db.String(50), nullable=True)

    patient = db.relationship('User', foreign_keys=[patient_id], backref='emergency_contact')

class SosEvent(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    patient_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    notes = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    patient = db.relationship('User', foreign_keys=[patient_id], backref='sos_events')

class AdminAuditLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    admin_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    action = db.Column(db.String(100), nullable=False)
    target_name = db.Column(db.String(120), nullable=True)
    details = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    admin = db.relationship('User', foreign_keys=[admin_id])

class Message(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    appointment_id = db.Column(db.Integer, db.ForeignKey('appointment.id'), nullable=False)
    sender_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    content = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    appointment = db.relationship('Appointment', backref='messages')
    sender = db.relationship('User', foreign_keys=[sender_id])

class Medicine(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    patient_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    name = db.Column(db.String(120), nullable=False)
    dosage = db.Column(db.String(80), nullable=True)
    frequency = db.Column(db.String(80), nullable=True)
    time_of_day = db.Column(db.String(120), nullable=True)  # legacy free-text, kept for old rows; new entries use MedicineDose
    notes = db.Column(db.Text, nullable=True)
    start_date = db.Column(db.Date, nullable=True)
    end_date = db.Column(db.Date, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    patient = db.relationship('User', foreign_keys=[patient_id], backref='medicines')

class MedicineDose(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    medicine_id = db.Column(db.Integer, db.ForeignKey('medicine.id'), nullable=False)
    time = db.Column(db.String(5), nullable=False)  # 24-hour "HH:MM"

    medicine = db.relationship('Medicine', backref=db.backref('doses', cascade='all, delete-orphan', order_by='MedicineDose.time'))

class MedicineDoseLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    dose_id = db.Column(db.Integer, db.ForeignKey('medicine_dose.id'), nullable=False)
    log_date = db.Column(db.Date, nullable=False)
    taken_at = db.Column(db.DateTime, nullable=True)

    dose = db.relationship('MedicineDose', backref=db.backref('logs', cascade='all, delete-orphan'))

    __table_args__ = (db.UniqueConstraint('dose_id', 'log_date', name='uq_dose_log_date'),)

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
                    password_hash=generate_password_hash(admin_password),
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
@app.route('/')
def index():
    return render_template('index.html')

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
                password_hash=generate_password_hash(password),
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
    today = datetime.utcnow().date()

    latest_vital = Vital.query.filter_by(patient_id=patient_id).order_by(Vital.recorded_at.desc()).first()
    latest_symptom = SymptomLog.query.filter_by(patient_id=patient_id).order_by(SymptomLog.created_at.desc()).first()

    active_medicines = Medicine.query.filter(
        Medicine.patient_id == patient_id,
        db.or_(Medicine.start_date == None, Medicine.start_date <= today),
        db.or_(Medicine.end_date == None, Medicine.end_date >= today)
    ).order_by(Medicine.created_at.desc()).all()

    now = datetime.utcnow()
    upcoming_appointments = Appointment.query.filter_by(patient_id=patient_id, status='accepted').all()
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

    follow_ups = Appointment.query.filter_by(
        patient_id=patient_id, status='completed', follow_up_requested=True
    ).all()

    return render_template(
        'my_health.html',
        latest_vital=latest_vital,
        latest_symptom=latest_symptom,
        active_medicines=active_medicines,
        next_appointment=next_appointment,
        follow_ups=follow_ups
    )

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

    my_appointments = Appointment.query.filter_by(patient_id=patient_id).order_by(Appointment.created_at.desc()).all()

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

    follow_ups = [a for a in my_appointments if a.follow_up_requested and a.status == 'completed']
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
    appointment_time = request.form.get('appointment_time')
    reason = request.form.get('reason', '').strip()
    phone_number = request.form.get('phone_number', '').strip()

    if not doctor_id or not appointment_date or not appointment_time:
        flash('Please fill in all required appointment fields.', 'error')
        return redirect(url_for('patient_dashboard'))

    if not phone_number:
        flash('Please provide a phone number so your doctor can reach you, since video consultation is not yet available.', 'error')
        return redirect(url_for('patient_dashboard'))

    try:
        new_app = Appointment(
            patient_id=session['user_id'],
            doctor_id=int(doctor_id),
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
        appt = Appointment.query.get_or_404(app_id)
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
    doctor = User.query.get_or_404(doctor_id)
    appointments = Appointment.query.filter_by(doctor_id=doctor_id).order_by(
        Appointment.appointment_date.desc(), Appointment.appointment_time.asc()
    ).all()

    grouped = {}
    for appt in appointments:
        grouped.setdefault(appt.appointment_date, []).append(appt)
    grouped_appointments = sorted(grouped.items(), key=lambda x: x[0], reverse=True)

    return render_template('doctor_dashboard.html', doctor=doctor, grouped_appointments=grouped_appointments)

@app.route('/appointment/<int:app_id>/<action>', methods=['POST'])
@login_required
@role_required('doctor')
def handle_appointment(app_id, action):
    try:
        appt = Appointment.query.get_or_404(app_id)
        if appt.doctor_id != session.get('user_id'):
            flash('Unauthorized action.', 'error')
            return redirect(url_for('doctor_dashboard'))

        if action == 'accept':
            appt.status = 'accepted'
            flash('Appointment accepted!', 'success')
        elif action == 'decline':
            appt.status = 'declined'
            flash('Appointment declined.', 'error')
        elif action == 'complete':
            if appt.status != 'accepted':
                flash('Only accepted appointments can be marked completed.', 'error')
                return redirect(url_for('doctor_dashboard'))
            appt.status = 'completed'
            flash('Appointment marked as completed.', 'success')

        db.session.commit()
    except Exception as e:
        db.session.rollback()
        app.logger.error(f"Appointment handle error: {e}")
        flash('Database error updating appointment.', 'error')

    return redirect(url_for('doctor_dashboard'))

@app.route('/appointment/<int:app_id>/request-follow-up', methods=['POST'])
@login_required
@role_required('doctor')
def request_follow_up(app_id):
    try:
        appt = Appointment.query.get_or_404(app_id)
        if appt.doctor_id != session.get('user_id'):
            flash('Unauthorized action.', 'error')
            return redirect(url_for('doctor_dashboard'))
        if appt.status != 'completed':
            flash('Follow-up can only be requested for completed appointments.', 'error')
            return redirect(url_for('doctor_dashboard'))

        appt.follow_up_requested = True
        db.session.commit()
        flash('Follow-up requested. The patient will see a prompt to book one.', 'success')
    except Exception as e:
        db.session.rollback()
        app.logger.error(f"Request follow-up error: {e}")
        flash('Error requesting follow-up.', 'error')

    return redirect(url_for('doctor_dashboard'))

@app.route('/doctor/profile', methods=['GET', 'POST'])
@login_required
@role_required('doctor')
def doctor_profile():
    doctor = User.query.get_or_404(session.get('user_id'))

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

@app.route('/doctor/patient/<int:patient_id>')
@login_required
@role_required('doctor')
def view_patient_history(patient_id):
    doctor_id = session.get('user_id')

    has_appointment = Appointment.query.filter_by(doctor_id=doctor_id, patient_id=patient_id).first()
    if not has_appointment:
        flash('You can only view history for patients who have booked with you.', 'error')
        return redirect(url_for('doctor_dashboard'))

    patient = User.query.get_or_404(patient_id)
    vitals_history = Vital.query.filter_by(patient_id=patient_id).order_by(Vital.recorded_at.desc()).limit(20).all()
    symptom_history = SymptomLog.query.filter_by(patient_id=patient_id).order_by(SymptomLog.created_at.desc()).limit(20).all()
    medicine_history = Medicine.query.filter_by(patient_id=patient_id).order_by(Medicine.created_at.desc()).all()

    return render_template(
        'patient_history_view.html',
        patient=patient,
        vitals_history=vitals_history,
        symptom_history=symptom_history,
        medicine_history=medicine_history
    )

@app.route('/chat')
@login_required
@role_required('patient', 'doctor')
def chat_list():
    user_id = session.get('user_id')
    role = session.get('role')

    if role == 'patient':
        appts = Appointment.query.filter(
            Appointment.patient_id == user_id,
            Appointment.status.in_(['accepted', 'completed'])
        ).order_by(Appointment.appointment_date.desc()).all()
    else:
        appts = Appointment.query.filter(
            Appointment.doctor_id == user_id,
            Appointment.status.in_(['accepted', 'completed'])
        ).order_by(Appointment.appointment_date.desc()).all()

    conversations = []
    for appt in appts:
        partner = appt.doctor if role == 'patient' else appt.patient
        last_msg = Message.query.filter_by(appointment_id=appt.id).order_by(Message.created_at.desc()).first()
        conversations.append({'appointment': appt, 'partner': partner, 'last_message': last_msg})

    conversations.sort(key=lambda c: c['last_message'].created_at if c['last_message'] else datetime.min, reverse=True)

    return render_template('chat_list.html', conversations=conversations)

@app.route('/chat/<int:appointment_id>', methods=['GET', 'POST'])
@login_required
@role_required('patient', 'doctor')
def chat_thread(appointment_id):
    user_id = session.get('user_id')
    role = session.get('role')

    appt = Appointment.query.get_or_404(appointment_id)

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
    pending_doctors = User.query.filter_by(role='doctor', status='pending').all()
    approved_doctors = User.query.filter_by(role='doctor', status='approved').all()
    patients = User.query.filter_by(role='patient').all()

    stats = {
        'total_patients': len(patients),
        'active_doctors': len(approved_doctors),
        'pending_approvals': len(pending_doctors)
    }

    return render_template(
        'admin.html',
        pending_doctors=pending_doctors,
        approved_doctors=approved_doctors,
        patients=patients,
        stats=stats
    )

@app.route('/admin/verify/<int:doctor_id>/<action>', methods=['POST'])
@login_required
@role_required('hospital', 'admin')
def verify_doctor(doctor_id, action):
    try:
        doctor = User.query.get_or_404(doctor_id)
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
        user = User.query.get_or_404(user_id)
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
    return render_template('vitals.html', history=history, latest=latest)

SYMPTOM_OPTIONS = [
    'Fever', 'Cough', 'Chest pain', 'Shortness of breath', 'Headache',
    'Nausea or vomiting', 'Dizziness', 'Rash', 'Abdominal pain', 'Fatigue'
]
EMERGENCY_SYMPTOMS = {'Chest pain', 'Shortness of breath'}

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
        if has_emergency_symptom or severity == 'severe':
            guidance = ('This could be serious. Please seek emergency care immediately '
                        'or call your local emergency number. Do not wait.')
            flash_category = 'error'
        elif severity == 'moderate' or len(selected) >= 3:
            guidance = ('Your symptoms may need medical attention. Please book an '
                        'appointment with a doctor soon.')
            flash_category = 'error'
        else:
            guidance = ('Monitor your symptoms, rest, and stay hydrated. Book an '
                        'appointment if things worsen or persist beyond a few days.')
            flash_category = 'success'

        try:
            entry = SymptomLog(
                patient_id=patient_id,
                symptoms=', '.join(selected) if selected else 'Not specified',
                severity=severity,
                description=description,
                guidance=guidance
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

@app.route('/sos', methods=['GET', 'POST'])
@login_required
@role_required('patient')
def sos():
    patient_id = session.get('user_id')
    contact = EmergencyContact.query.filter_by(patient_id=patient_id).first()

    if request.method == 'POST':
        action = request.form.get('action')

        if action == 'save_contact':
            name = request.form.get('contact_name', '').strip()
            phone = request.form.get('contact_phone', '').strip()
            relation = request.form.get('relation', '').strip()

            if not name or not phone:
                flash('Please provide a contact name and phone number.', 'error')
                return redirect(url_for('sos'))

            try:
                if contact:
                    contact.contact_name = name
                    contact.contact_phone = phone
                    contact.relation = relation
                else:
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

    return render_template('sos.html', contact=contact)

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
    today = datetime.utcnow().date()
    active_meds = Medicine.query.filter(
        Medicine.patient_id == patient_id,
        db.or_(Medicine.start_date == None, Medicine.start_date <= today),
        db.or_(Medicine.end_date == None, Medicine.end_date >= today)
    ).all()

    schedule = []
    for med in active_meds:
        for dose in med.doses:
            log = MedicineDoseLog.query.filter_by(dose_id=dose.id, log_date=today).first()
            period, icon = classify_time_period(dose.time)
            schedule.append({
                'medicine': med,
                'dose': dose,
                'taken': bool(log and log.taken_at),
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

    my_medicines = Medicine.query.filter_by(patient_id=patient_id).order_by(Medicine.created_at.desc()).all()
    todays_schedule = get_todays_schedule(patient_id)
    return render_template('medicines.html', medicines=my_medicines, todays_schedule=todays_schedule)

@app.route('/medicines/<int:med_id>/edit', methods=['GET', 'POST'])
@login_required
@role_required('patient')
def edit_medicine(med_id):
    med = Medicine.query.get_or_404(med_id)
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
        med = Medicine.query.get_or_404(med_id)
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

@app.route('/medicines/dose/<int:dose_id>/delete', methods=['POST'])
@login_required
@role_required('patient')
def delete_dose(dose_id):
    try:
        dose = MedicineDose.query.get_or_404(dose_id)
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
        dose = MedicineDose.query.get_or_404(dose_id)
        if dose.medicine.patient_id != session.get('user_id'):
            flash('Unauthorized action.', 'error')
            return redirect(url_for('medicines'))

        today = datetime.utcnow().date()
        log = MedicineDoseLog.query.filter_by(dose_id=dose.id, log_date=today).first()

        if log and log.taken_at:
            log.taken_at = None
        elif log:
            log.taken_at = datetime.utcnow()
        else:
            log = MedicineDoseLog(dose_id=dose.id, log_date=today, taken_at=datetime.utcnow())
            db.session.add(log)

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

        user = User.query.get(session.get('user_id'))

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
            user.password_hash = generate_password_hash(new_password)
            db.session.commit()
            flash('Password updated successfully.', 'success')
        except Exception as e:
            db.session.rollback()
            app.logger.error(f"Password change error: {e}")
            flash('Error updating password.', 'error')

        return redirect(url_for('profile'))

    back_endpoint = dashboard_endpoint_for_role(session.get('role'))
    return render_template('profile.html', back_endpoint=back_endpoint)

@app.route('/logout')
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
FILEEOF_1
cat > templates/base.html << 'FILEEOF_2'
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{% block title %}MediBro Healthcare{% endblock %}</title>
    <style>
        :root {
            --surface: #ffffff;
            --border: #cbd5e1;
            --radius: 8px;
            --primary: #2563eb;
            --text-dark: #0f172a;
            --text-sub: #64748b;
            --success: #15803d;
            --success-light: #dcfce7;
            --danger: #b91c1c;
            --danger-light: #fee2e2;
            --warning: #b45309;
        }
        * { box-sizing: border-box; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
            background-color: #f8fafc;
            color: #0f172a;
            margin: 0;
            padding: 0;
        }
        nav {
            background-color: #ffffff;
            border-bottom: 1px solid #cbd5e1;
            padding: 14px 24px;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }
        .nav-brand {
            font-size: 1.3rem;
            font-weight: 700;
            color: #2563eb;
            text-decoration: none;
        }
        .flash-messages {
            max-width: 1100px;
            margin: 16px auto 0;
            padding: 0 20px;
        }
        .flash {
            padding: 12px 16px 12px 40px;
            border-radius: 6px;
            margin-bottom: 12px;
            font-weight: 600;
            font-size: 0.95rem;
            position: relative;
        }
        .flash::before {
            position: absolute;
            left: 14px;
            top: 50%;
            transform: translateY(-50%);
        }
        .flash.success { background-color: #dcfce7; color: #15803d; border: 1px solid #bbf7d0; }
        .flash.success::before { content: "✅"; }
        .flash.error { background-color: #fee2e2; color: #b91c1c; border: 1px solid #fecaca; }
        .flash.error::before { content: "⚠️"; }
        .flash.warning { background-color: #fef3c7; color: #92400e; border: 1px solid #fde68a; }
        .flash.warning::before { content: "⚠️"; }
        .flash.info { background-color: #dbeafe; color: #1e40af; border: 1px solid #bfdbfe; }
        .flash.info::before { content: "ℹ️"; }

        .patient-layout {
            display: flex;
            gap: 24px;
            max-width: 1200px;
            margin: 30px auto;
            padding: 0 20px;
            align-items: flex-start;
        }
        .patient-sidebar {
            display: flex;
            flex-direction: column;
            gap: 4px;
            width: 220px;
            flex-shrink: 0;
            background-color: #ffffff;
            border: 1px solid #cbd5e1;
            border-radius: 8px;
            padding: 12px;
            position: sticky;
            top: 16px;
        }
        .patient-sidebar a {
            display: flex;
            align-items: center;
            gap: 10px;
            padding: 10px 12px;
            border-radius: 6px;
            text-decoration: none;
            color: #334155;
            font-weight: 600;
            font-size: 0.9rem;
        }
        .patient-sidebar a:hover {
            background-color: #f1f5f9;
        }
        .patient-sidebar a.active {
            background-color: #2563eb;
            color: #ffffff;
        }
        .patient-sidebar a.sos-link {
            color: #dc2626;
        }
        .patient-sidebar a.sos-link:hover {
            background-color: #fee2e2;
        }
        .patient-sidebar a.sos-link.active {
            color: #ffffff;
        }
        .patient-main {
            flex: 1;
            min-width: 0;
        }

        @media (max-width: 640px) {
            nav {
                padding: 12px 16px;
                flex-wrap: wrap;
                gap: 10px;
            }
            .nav-brand {
                font-size: 1.15rem;
            }
            .flash-messages {
                padding: 0 12px;
            }
            main > div {
                padding-left: 12px !important;
                padding-right: 12px !important;
            }
            .patient-layout {
                flex-direction: column;
                padding: 0 12px;
                margin: 16px auto;
            }
            .patient-sidebar {
                flex-direction: row;
                width: 100%;
                overflow-x: auto;
                position: static;
                gap: 6px;
            }
            .patient-sidebar a {
                white-space: nowrap;
                flex-shrink: 0;
            }
        }
    </style>
</head>
<body>
    <nav>
        <a href="{{ url_for('index') }}" class="nav-brand">🩺 MediBro</a>
        <div style="display: flex; align-items: center; gap: 16px;">
            {% if session.get('user_id') %}
                <a href="{{ url_for('profile') }}" style="color: #64748b; text-decoration: none; font-weight: 500;">{{ session.get('full_name') }}</a>
                <a href="{{ url_for('logout') }}" style="color: #dc2626; text-decoration: none; font-weight: 600; background-color: #fee2e2; padding: 6px 12px; border-radius: 6px;">Log Out</a>
            {% else %}
                <a href="{{ url_for('login') }}" style="color: #2563eb; text-decoration: none; font-weight: 600;">Log In</a>
                <a href="{{ url_for('register') }}" style="background-color: #2563eb; color: #ffffff; padding: 6px 14px; border-radius: 6px; text-decoration: none; font-weight: 600;">Register</a>
            {% endif %}
        </div>
    </nav>

    <div class="flash-messages">
        {% with messages = get_flashed_messages(with_categories=true) %}
            {% if messages %}
                {% for category, message in messages %}
                    <div class="flash {{ category }}">{{ message }}</div>
                {% endfor %}
            {% endif %}
        {% endwith %}
    </div>

    <main>
        {% block content %}{% endblock %}
    </main>
</body>
</html>
FILEEOF_2
cat > templates/my_health.html << 'FILEEOF_3'
{% extends "base.html" %}
{% block title %}My Health - MediBro{% endblock %}
{% block content %}
<div class="patient-layout">
    <aside class="patient-sidebar">
        <a href="{{ url_for('my_health') }}" class="active">🏠 My Health</a>
        <a href="{{ url_for('patient_dashboard') }}">📅 Appointments</a>
        <a href="{{ url_for('vitals') }}">💓 Vitals</a>
        <a href="{{ url_for('symptoms') }}">🩺 Symptom Checker</a>
        <a href="{{ url_for('medicines') }}">💊 Medicines</a>
        <a href="{{ url_for('chat_list') }}">💬 Chat</a>
        <a href="{{ url_for('sos') }}" class="sos-link">🚨 SOS</a>
        <a href="{{ url_for('profile') }}">⚙️ Profile</a>
        <a href="{{ url_for('logout') }}">🚪 Log Out</a>
    </aside>

    <main class="patient-main">
        <div style="margin-bottom: 24px;">
            <h1 style="margin: 0; font-size: 1.8rem; color: #0f172a;">My Health</h1>
            <p style="margin: 4px 0 0 0; color: #64748b;">Welcome back, {{ session.full_name }}</p>
        </div>

        <!-- Follow-up banner -->
        {% if follow_ups %}
        <div style="background-color: #ede9fe; border: 1px solid #ddd6fe; border-radius: 8px; padding: 16px 20px; margin-bottom: 24px;">
            <p style="margin: 0 0 8px 0; font-weight: 700; color: #6d28d9; font-size: 0.9rem;">🔁 Follow-up Requested</p>
            {% for appt in follow_ups %}
            <p style="margin: 4px 0; color: #6d28d9; font-size: 0.85rem;">
                Dr. {{ appt.doctor.full_name.replace('Dr. ', '').replace('Dr ', '') }} recommended a follow-up visit.
                <a href="{{ url_for('patient_dashboard', book_with=appt.doctor.id) }}" style="color: #6d28d9; font-weight: 700; text-decoration: underline;">Book it now</a>
            </p>
            {% endfor %}
        </div>
        {% endif %}

        <!-- Next appointment -->
        {% if next_appointment %}
        <div style="background: linear-gradient(135deg, #2563eb 0%, #1e40af 100%); border-radius: 12px; padding: 20px 24px; margin-bottom: 24px; color: #ffffff;">
            <p style="margin: 0 0 4px 0; font-size: 0.8rem; font-weight: 600; opacity: 0.85; text-transform: uppercase; letter-spacing: 0.05em;">Next Appointment</p>
            <p style="margin: 0; font-size: 1.15rem; font-weight: 700;">Dr. {{ next_appointment.doctor.full_name.replace('Dr. ', '').replace('Dr ', '') }}</p>
            <p style="margin: 4px 0 0 0; font-size: 0.9rem; opacity: 0.9;">{{ next_appointment.appointment_date }} at {{ next_appointment.appointment_time }}{% if next_appointment.reason %} &middot; {{ next_appointment.reason }}{% endif %}</p>
        </div>
        {% endif %}

        <!-- Overview cards -->
        <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 16px; margin-bottom: 24px;">

            <!-- Vitals card -->
            <div style="background-color: #ffffff; border: 1px solid #cbd5e1; border-radius: 12px; padding: 20px; box-shadow: 0 1px 3px rgba(0,0,0,0.05);">
                <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 14px;">
                    <p style="margin: 0; font-weight: 700; color: #0f172a; font-size: 0.95rem;">💓 Latest Vitals</p>
                    <a href="{{ url_for('vitals') }}" style="color: #2563eb; text-decoration: none; font-size: 0.8rem; font-weight: 600;">View all →</a>
                </div>
                {% if latest_vital %}
                <div style="display: grid; grid-template-columns: repeat(2, 1fr); gap: 10px;">
                    <div>
                        <p style="margin: 0; font-size: 0.7rem; color: #94a3b8; font-weight: 600;">BLOOD PRESSURE</p>
                        <p style="margin: 2px 0 0 0; font-weight: 700; color: #0f172a;">{% if latest_vital.systolic and latest_vital.diastolic %}{{ latest_vital.systolic }}/{{ latest_vital.diastolic }}{% else %}&mdash;{% endif %}</p>
                    </div>
                    <div>
                        <p style="margin: 0; font-size: 0.7rem; color: #94a3b8; font-weight: 600;">HEART RATE</p>
                        <p style="margin: 2px 0 0 0; font-weight: 700; color: #0f172a;">{{ latest_vital.heart_rate or '—' }} <span style="font-size: 0.7rem; font-weight: 500; color: #94a3b8;">bpm</span></p>
                    </div>
                    <div>
                        <p style="margin: 0; font-size: 0.7rem; color: #94a3b8; font-weight: 600;">SPO2</p>
                        <p style="margin: 2px 0 0 0; font-weight: 700; color: #0f172a;">{{ latest_vital.spo2 or '—' }} <span style="font-size: 0.7rem; font-weight: 500; color: #94a3b8;">%</span></p>
                    </div>
                    <div>
                        <p style="margin: 0; font-size: 0.7rem; color: #94a3b8; font-weight: 600;">TEMP</p>
                        <p style="margin: 2px 0 0 0; font-weight: 700; color: #0f172a;">{{ latest_vital.temperature or '—' }} <span style="font-size: 0.7rem; font-weight: 500; color: #94a3b8;">&deg;F</span></p>
                    </div>
                </div>
                <p style="margin: 12px 0 0 0; color: #94a3b8; font-size: 0.75rem;">Recorded {{ latest_vital.recorded_at.strftime('%b %d, %Y') }}</p>
                {% else %}
                <p style="color: #94a3b8; margin: 0 0 10px 0; font-size: 0.85rem;">No vitals logged yet.</p>
                <a href="{{ url_for('vitals') }}" style="color: #2563eb; text-decoration: none; font-size: 0.85rem; font-weight: 600;">+ Log your first reading</a>
                {% endif %}
            </div>

            <!-- Symptoms card -->
            <div style="background-color: #ffffff; border: 1px solid #cbd5e1; border-radius: 12px; padding: 20px; box-shadow: 0 1px 3px rgba(0,0,0,0.05);">
                <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 14px;">
                    <p style="margin: 0; font-weight: 700; color: #0f172a; font-size: 0.95rem;">🩺 Recent Symptoms</p>
                    <a href="{{ url_for('symptoms') }}" style="color: #2563eb; text-decoration: none; font-size: 0.8rem; font-weight: 600;">View all →</a>
                </div>
                {% if latest_symptom %}
                <p style="margin: 0; font-weight: 700; color: #0f172a; font-size: 0.95rem;">{{ latest_symptom.symptoms }}</p>
                <p style="margin: 6px 0 0 0; color: #334155; font-size: 0.8rem; text-transform: capitalize;">Severity: {{ latest_symptom.severity }}</p>
                <p style="margin: 10px 0 0 0; color: #94a3b8; font-size: 0.75rem;">Logged {{ latest_symptom.created_at.strftime('%b %d, %Y') }}</p>
                {% else %}
                <p style="color: #94a3b8; margin: 0 0 10px 0; font-size: 0.85rem;">No symptoms logged yet.</p>
                <a href="{{ url_for('symptoms') }}" style="color: #2563eb; text-decoration: none; font-size: 0.85rem; font-weight: 600;">+ Check a symptom</a>
                {% endif %}
            </div>

            <!-- Medicines card -->
            <div style="background-color: #ffffff; border: 1px solid #cbd5e1; border-radius: 12px; padding: 20px; box-shadow: 0 1px 3px rgba(0,0,0,0.05);">
                <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 14px;">
                    <p style="margin: 0; font-weight: 700; color: #0f172a; font-size: 0.95rem;">💊 Active Medicines</p>
                    <a href="{{ url_for('medicines') }}" style="color: #2563eb; text-decoration: none; font-size: 0.8rem; font-weight: 600;">View all →</a>
                </div>
                {% if active_medicines %}
                <div style="display: grid; gap: 8px;">
                    {% for med in active_medicines[:4] %}
                    <div style="display: flex; justify-content: space-between; align-items: center;">
                        <p style="margin: 0; font-weight: 600; color: #0f172a; font-size: 0.85rem;">{{ med.name }}{% if med.dosage %} <span style="color: #94a3b8; font-weight: 500;">&middot; {{ med.dosage }}</span>{% endif %}</p>
                        {% if med.doses %}<span style="background-color: #eff6ff; color: #1e40af; padding: 2px 8px; border-radius: 10px; font-size: 0.72rem; font-weight: 600;">{{ med.doses[0].time }}{% if med.doses|length > 1 %}+{{ med.doses|length - 1 }}{% endif %}</span>{% endif %}
                    </div>
                    {% endfor %}
                    {% if active_medicines|length > 4 %}
                    <p style="margin: 0; color: #94a3b8; font-size: 0.75rem;">+{{ active_medicines|length - 4 }} more</p>
                    {% endif %}
                </div>
                {% else %}
                <p style="color: #94a3b8; margin: 0 0 10px 0; font-size: 0.85rem;">No active medicines.</p>
                <a href="{{ url_for('medicines') }}" style="color: #2563eb; text-decoration: none; font-size: 0.85rem; font-weight: 600;">+ Add a medicine</a>
                {% endif %}
            </div>

            <!-- Appointments card -->
            <div style="background-color: #ffffff; border: 1px solid #cbd5e1; border-radius: 12px; padding: 20px; box-shadow: 0 1px 3px rgba(0,0,0,0.05);">
                <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 14px;">
                    <p style="margin: 0; font-weight: 700; color: #0f172a; font-size: 0.95rem;">📅 Appointments</p>
                    <a href="{{ url_for('patient_dashboard') }}" style="color: #2563eb; text-decoration: none; font-size: 0.8rem; font-weight: 600;">Manage →</a>
                </div>
                {% if next_appointment %}
                <p style="margin: 0; font-weight: 700; color: #0f172a; font-size: 0.95rem;">Dr. {{ next_appointment.doctor.full_name.replace('Dr. ', '').replace('Dr ', '') }}</p>
                <p style="margin: 6px 0 0 0; color: #334155; font-size: 0.8rem;">{{ next_appointment.appointment_date }} at {{ next_appointment.appointment_time }}</p>
                {% else %}
                <p style="color: #94a3b8; margin: 0 0 10px 0; font-size: 0.85rem;">No upcoming appointments.</p>
                <a href="{{ url_for('patient_dashboard') }}" style="color: #2563eb; text-decoration: none; font-size: 0.85rem; font-weight: 600;">+ Book an appointment</a>
                {% endif %}
            </div>
        </div>
    </main>
</div>
{% endblock %}
FILEEOF_3
cat > templates/patient_dashboard.html << 'FILEEOF_4'
{% extends "base.html" %}
{% block title %}Patient Portal - MediBro{% endblock %}
{% block content %}
<div class="patient-layout">
    <aside class="patient-sidebar">
        <a href="{{ url_for('my_health') }}">🏠 My Health</a>
        <a href="{{ url_for('patient_dashboard') }}" class="active">📅 Appointments</a>
        <a href="{{ url_for('vitals') }}">💓 Vitals</a>
        <a href="{{ url_for('symptoms') }}">🩺 Symptom Checker</a>
        <a href="{{ url_for('medicines') }}">💊 Medicines</a>
        <a href="{{ url_for('chat_list') }}">💬 Chat</a>
        <a href="{{ url_for('sos') }}" class="sos-link">🚨 SOS</a>
        <a href="{{ url_for('profile') }}">⚙️ Profile</a>
        <a href="{{ url_for('logout') }}">🚪 Log Out</a>
    </aside>

    <main class="patient-main">
        <div style="margin-bottom: 24px;">
            <h1 style="margin: 0; font-size: 1.8rem; color: #0f172a;">Patient Portal</h1>
            <p style="margin: 4px 0 0 0; color: #64748b;">Welcome back, {{ session.full_name }}</p>
        </div>

        <!-- Upcoming Appointment Reminders -->
        {% if upcoming %}
        <div style="background-color: var(--warning-light, #fef3c7); border: 1px solid #fde68a; border-radius: var(--radius, 8px); padding: 16px 20px; margin-bottom: 24px;">
            <p style="margin: 0 0 8px 0; font-weight: 700; color: #92400e; font-size: 0.9rem;">⏰ Upcoming Appointments</p>
            {% for appt in upcoming %}
            <p style="margin: 4px 0; color: #92400e; font-size: 0.85rem;">
                Dr. {{ appt.doctor.full_name.replace('Dr. ', '').replace('Dr ', '') }} &mdash; {{ appt.appointment_date }} at {{ appt.appointment_time }}
            </p>
            {% endfor %}
        </div>
        {% endif %}

        <!-- Follow-up Requests -->
        {% if follow_ups %}
        <div style="background-color: #ede9fe; border: 1px solid #ddd6fe; border-radius: 8px; padding: 16px 20px; margin-bottom: 24px;">
            <p style="margin: 0 0 8px 0; font-weight: 700; color: #6d28d9; font-size: 0.9rem;">🔁 Follow-up Requested</p>
            {% for appt in follow_ups %}
            <p style="margin: 4px 0; color: #6d28d9; font-size: 0.85rem;">
                Dr. {{ appt.doctor.full_name.replace('Dr. ', '').replace('Dr ', '') }} recommended a follow-up visit.
                <a href="{{ url_for('patient_dashboard', book_with=appt.doctor.id) }}" style="color: #6d28d9; font-weight: 700; text-decoration: underline;">Book it now</a>
            </p>
            {% endfor %}
        </div>
        {% endif %}

        <!-- Section 1: Book Appointment Card -->
        <div style="background-color: #ffffff; border: 1px solid #cbd5e1; border-radius: 8px; padding: 24px; margin-bottom: 32px; box-shadow: 0 1px 3px rgba(0,0,0,0.05);">
            <h2 style="font-size: 1.25rem; margin-top: 0; margin-bottom: 20px; color: #0f172a;">📅 Book an Appointment</h2>

            {% if all_specialties %}
            <div style="margin-bottom: 16px; display: flex; align-items: center; gap: 10px; flex-wrap: wrap;">
                <span style="font-size: 0.85rem; font-weight: 600; color: #334155;">Filter by specialty:</span>
                <a href="{{ url_for('patient_dashboard') }}" style="padding: 5px 12px; border-radius: 14px; text-decoration: none; font-size: 0.8rem; font-weight: 600; {% if not specialty_filter %}background-color: #2563eb; color: #ffffff;{% else %}background-color: #f1f5f9; color: #334155;{% endif %}">All</a>
                {% for spec in all_specialties %}
                <a href="{{ url_for('patient_dashboard', specialty=spec) }}" style="padding: 5px 12px; border-radius: 14px; text-decoration: none; font-size: 0.8rem; font-weight: 600; {% if specialty_filter == spec %}background-color: #2563eb; color: #ffffff;{% else %}background-color: #f1f5f9; color: #334155;{% endif %}">{{ spec }}</a>
                {% endfor %}
            </div>
            {% endif %}

            {% if doctors %}
            <div style="background-color: #eff6ff; border: 1px solid #bfdbfe; border-radius: 6px; padding: 10px 14px; margin-bottom: 16px; font-size: 0.8rem; color: #1e40af;">
                📞 Video consultation isn't available yet — please share a phone number so your doctor can reach you directly.
            </div>
            <form action="{{ url_for('book_appointment') }}" method="POST">
            <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
                <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 16px; margin-bottom: 16px;">
                    <div>
                        <label style="display: block; font-size: 0.85rem; font-weight: 600; color: #334155; margin-bottom: 6px;">Select Doctor</label>
                        <select name="doctor_id" required style="width: 100%; padding: 10px; border: 1px solid #cbd5e1; border-radius: 6px; background-color: #ffffff; font-size: 0.95rem; color: #0f172a;">
                            <option value="">-- Choose Specialist --</option>
                            {% for doc in doctors %}
                            <option value="{{ doc.id }}" {% if pre_doctor_id and doc.id == pre_doctor_id %}selected{% endif %}>Dr. {{ doc.full_name.replace('Dr. ', '').replace('Dr ', '') }} ({{ doc.specialty or 'General Practice' }})</option>
                            {% endfor %}
                        </select>
                    </div>
                    <div>
                        <label style="display: block; font-size: 0.85rem; font-weight: 600; color: #334155; margin-bottom: 6px;">Date</label>
                        <input type="date" name="appointment_date" required style="width: 100%; padding: 10px; border: 1px solid #cbd5e1; border-radius: 6px; font-size: 0.95rem; color: #0f172a; background-color: #ffffff;">
                    </div>
                    <div>
                        <label style="display: block; font-size: 0.85rem; font-weight: 600; color: #334155; margin-bottom: 6px;">Time</label>
                        <input type="time" name="appointment_time" required style="width: 100%; padding: 10px; border: 1px solid #cbd5e1; border-radius: 6px; font-size: 0.95rem; color: #0f172a; background-color: #ffffff;">
                    </div>
                    <div>
                        <label style="display: block; font-size: 0.85rem; font-weight: 600; color: #334155; margin-bottom: 6px;">Phone Number</label>
                        <input type="tel" name="phone_number" required placeholder="For your doctor to reach you" style="width: 100%; padding: 10px; border: 1px solid #cbd5e1; border-radius: 6px; font-size: 0.95rem; color: #0f172a; background-color: #ffffff;">
                    </div>
                </div>
                <div style="margin-bottom: 20px;">
                    <label style="display: block; font-size: 0.85rem; font-weight: 600; color: #334155; margin-bottom: 6px;">Reason for Visit</label>
                    <input type="text" name="reason" placeholder="e.g. Annual checkup, flu symptoms, consultation..." style="width: 100%; padding: 10px; border: 1px solid #cbd5e1; border-radius: 6px; font-size: 0.95rem; color: #0f172a; background-color: #ffffff;">
                </div>
                <div style="text-align: right;">
                    <button type="submit" style="padding: 10px 24px; background-color: #2563eb; color: #ffffff; border: none; border-radius: 6px; font-weight: 600; font-size: 0.95rem; cursor: pointer;">Request Appointment</button>
                </div>
            </form>
            {% else %}
            <p style="color: #64748b; margin: 0;">No verified doctors are currently available for booking{% if specialty_filter %} in this specialty{% endif %}.</p>
            {% endif %}
        </div>

        <!-- Section 2: My Scheduled Appointments Card -->
        <div style="background-color: #ffffff; border: 1px solid #cbd5e1; border-radius: 8px; padding: 24px; box-shadow: 0 1px 3px rgba(0,0,0,0.05);">
            <h2 style="font-size: 1.25rem; margin-top: 0; margin-bottom: 20px; color: #0f172a;">📋 My Appointments</h2>
            {% if appointments %}
            <div style="overflow-x: auto;">
                <table style="width: 100%; border-collapse: collapse; text-align: left;">
                    <thead>
                        <tr style="border-bottom: 2px solid #e2e8f0; color: #64748b; font-size: 0.85rem;">
                            <th style="padding: 12px 10px;">Doctor</th>
                            <th style="padding: 12px 10px;">Date & Time</th>
                            <th style="padding: 12px 10px;">Reason</th>
                            <th style="padding: 12px 10px;">Status</th>
                            <th style="padding: 12px 10px; text-align: right;">Action</th>
                        </tr>
                    </thead>
                    <tbody>
                        {% for appt in appointments %}
                        <tr style="border-bottom: 1px solid #f1f5f9;">
                            <td style="padding: 14px 10px; font-weight: 600; color: #0f172a;">Dr. {{ appt.doctor.full_name.replace('Dr. ', '').replace('Dr ', '') }}</td>
                            <td style="padding: 14px 10px; color: #334155;">{{ appt.appointment_date }} at {{ appt.appointment_time }}</td>
                            <td style="padding: 14px 10px; color: #334155;">{{ appt.reason or 'General Consultation' }}</td>
                            <td style="padding: 14px 10px;">
                                {% if appt.status == 'accepted' %}
                                <span style="background-color: #dcfce7; color: #15803d; padding: 4px 12px; border-radius: 12px; font-weight: 600; font-size: 0.85rem; display: inline-block;">Accepted</span>
                                {% elif appt.status == 'declined' %}
                                <span style="background-color: #fee2e2; color: #b91c1c; padding: 4px 12px; border-radius: 12px; font-weight: 600; font-size: 0.85rem; display: inline-block;">Declined</span>
                                {% elif appt.status == 'cancelled' %}
                                <span style="background-color: #f1f5f9; color: #64748b; padding: 4px 12px; border-radius: 12px; font-weight: 600; font-size: 0.85rem; display: inline-block;">Cancelled</span>
                                {% elif appt.status == 'completed' %}
                                <span style="background-color: #ede9fe; color: #6d28d9; padding: 4px 12px; border-radius: 12px; font-weight: 600; font-size: 0.85rem; display: inline-block;">Completed</span>
                                {% else %}
                                <span style="background-color: #fef3c7; color: #b45309; padding: 4px 12px; border-radius: 12px; font-weight: 600; font-size: 0.85rem; display: inline-block;">Pending</span>
                                {% endif %}
                            </td>
                            <td style="padding: 14px 10px; text-align: right;">
                                {% if appt.status == 'pending' %}
                                <form action="{{ url_for('cancel_appointment', app_id=appt.id) }}" method="POST" style="display: inline;" onsubmit="return confirm('Cancel this appointment request?');">
                                    <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
                                    <button type="submit" style="background: none; border: none; padding: 0; font-family: inherit; color: #dc2626; font-weight: 600; font-size: 0.85rem; cursor: pointer;">Cancel</button>
                                </form>
                                {% elif appt.status in ['accepted', 'completed'] %}
                                <a href="{{ url_for('chat_thread', appointment_id=appt.id) }}" style="color: #2563eb; text-decoration: none; font-weight: 600; font-size: 0.85rem;">💬 Chat</a>
                                {% endif %}
                            </td>
                        </tr>
                        {% endfor %}
                    </tbody>
                </table>
            </div>
            {% else %}
            <p style="color: #64748b; margin: 0;">You have not booked any appointments yet.</p>
            {% endif %}
        </div>
    </main>
</div>
{% endblock %}
FILEEOF_4
cat > templates/vitals.html << 'FILEEOF_5'
{% extends "base.html" %}
{% block title %}Vitals - MediBro{% endblock %}
{% block content %}
<div class="patient-layout">
    <aside class="patient-sidebar">
        <a href="{{ url_for('my_health') }}">🏠 My Health</a>
        <a href="{{ url_for('patient_dashboard') }}">📅 Appointments</a>
        <a href="{{ url_for('vitals') }}" class="active">💓 Vitals</a>
        <a href="{{ url_for('symptoms') }}">🩺 Symptom Checker</a>
        <a href="{{ url_for('medicines') }}">💊 Medicines</a>
        <a href="{{ url_for('chat_list') }}">💬 Chat</a>
        <a href="{{ url_for('sos') }}" class="sos-link">🚨 SOS</a>
        <a href="{{ url_for('profile') }}">⚙️ Profile</a>
        <a href="{{ url_for('logout') }}">🚪 Log Out</a>
    </aside>

    <main class="patient-main">
    <div style="margin-bottom: 24px; display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 12px;">
        <div>
            <h1 style="margin: 0; font-size: 1.8rem; color: #0f172a;">Vitals Tracking</h1>
            <p style="margin: 4px 0 0 0; color: #64748b;">Log and review your health readings</p>
        </div>
        <a href="{{ url_for('patient_dashboard') }}" style="color: #2563eb; text-decoration: none; font-weight: 600; font-size: 0.9rem;">&larr; Back to Portal</a>
    </div>

    <!-- Latest Snapshot -->
    <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 16px; margin-bottom: 32px;">
        <div style="background-color: #ffffff; border: 1px solid #cbd5e1; border-radius: 8px; padding: 18px; box-shadow: 0 1px 3px rgba(0,0,0,0.05);">
            <p style="margin: 0 0 6px 0; font-size: 0.8rem; color: #64748b; font-weight: 600;">Blood Pressure</p>
            <p style="margin: 0; font-size: 1.5rem; font-weight: 700; color: #0f172a;">
                {% if latest and latest.systolic and latest.diastolic %}{{ latest.systolic }}/{{ latest.diastolic }}{% else %}&mdash;{% endif %}
                <span style="font-size: 0.75rem; font-weight: 500; color: #94a3b8;">mmHg</span>
            </p>
        </div>
        <div style="background-color: #ffffff; border: 1px solid #cbd5e1; border-radius: 8px; padding: 18px; box-shadow: 0 1px 3px rgba(0,0,0,0.05);">
            <p style="margin: 0 0 6px 0; font-size: 0.8rem; color: #64748b; font-weight: 600;">Heart Rate</p>
            <p style="margin: 0; font-size: 1.5rem; font-weight: 700; color: #0f172a;">
                {% if latest and latest.heart_rate %}{{ latest.heart_rate }}{% else %}&mdash;{% endif %}
                <span style="font-size: 0.75rem; font-weight: 500; color: #94a3b8;">bpm</span>
            </p>
        </div>
        <div style="background-color: #ffffff; border: 1px solid #cbd5e1; border-radius: 8px; padding: 18px; box-shadow: 0 1px 3px rgba(0,0,0,0.05);">
            <p style="margin: 0 0 6px 0; font-size: 0.8rem; color: #64748b; font-weight: 600;">SpO2</p>
            <p style="margin: 0; font-size: 1.5rem; font-weight: 700; color: #0f172a;">
                {% if latest and latest.spo2 %}{{ latest.spo2 }}{% else %}&mdash;{% endif %}
                <span style="font-size: 0.75rem; font-weight: 500; color: #94a3b8;">%</span>
            </p>
        </div>
        <div style="background-color: #ffffff; border: 1px solid #cbd5e1; border-radius: 8px; padding: 18px; box-shadow: 0 1px 3px rgba(0,0,0,0.05);">
            <p style="margin: 0 0 6px 0; font-size: 0.8rem; color: #64748b; font-weight: 600;">Temperature</p>
            <p style="margin: 0; font-size: 1.5rem; font-weight: 700; color: #0f172a;">
                {% if latest and latest.temperature %}{{ latest.temperature }}{% else %}&mdash;{% endif %}
                <span style="font-size: 0.75rem; font-weight: 500; color: #94a3b8;">&deg;F</span>
            </p>
        </div>
    </div>

    <!-- Log New Reading -->
    <div style="background-color: #ffffff; border: 1px solid #cbd5e1; border-radius: 8px; padding: 24px; margin-bottom: 32px; box-shadow: 0 1px 3px rgba(0,0,0,0.05);">
        <h2 style="font-size: 1.25rem; margin-top: 0; margin-bottom: 20px; color: #0f172a;">💓 Log New Reading</h2>
        <form action="{{ url_for('vitals') }}" method="POST">
            <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
            <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 16px; margin-bottom: 16px;">
                <div>
                    <label style="display: block; font-size: 0.85rem; font-weight: 600; color: #334155; margin-bottom: 6px;">Systolic (mmHg)</label>
                    <input type="number" name="systolic" min="0" max="300" placeholder="120" style="width: 100%; padding: 10px; border: 1px solid #cbd5e1; border-radius: 6px; font-size: 0.95rem; color: #0f172a; background-color: #ffffff;">
                </div>
                <div>
                    <label style="display: block; font-size: 0.85rem; font-weight: 600; color: #334155; margin-bottom: 6px;">Diastolic (mmHg)</label>
                    <input type="number" name="diastolic" min="0" max="200" placeholder="80" style="width: 100%; padding: 10px; border: 1px solid #cbd5e1; border-radius: 6px; font-size: 0.95rem; color: #0f172a; background-color: #ffffff;">
                </div>
                <div>
                    <label style="display: block; font-size: 0.85rem; font-weight: 600; color: #334155; margin-bottom: 6px;">Heart Rate (bpm)</label>
                    <input type="number" name="heart_rate" min="0" max="300" placeholder="72" style="width: 100%; padding: 10px; border: 1px solid #cbd5e1; border-radius: 6px; font-size: 0.95rem; color: #0f172a; background-color: #ffffff;">
                </div>
                <div>
                    <label style="display: block; font-size: 0.85rem; font-weight: 600; color: #334155; margin-bottom: 6px;">SpO2 (%)</label>
                    <input type="number" name="spo2" min="0" max="100" placeholder="98" style="width: 100%; padding: 10px; border: 1px solid #cbd5e1; border-radius: 6px; font-size: 0.95rem; color: #0f172a; background-color: #ffffff;">
                </div>
                <div>
                    <label style="display: block; font-size: 0.85rem; font-weight: 600; color: #334155; margin-bottom: 6px;">Temperature (&deg;F)</label>
                    <input type="number" step="0.1" name="temperature" min="80" max="115" placeholder="98.6" style="width: 100%; padding: 10px; border: 1px solid #cbd5e1; border-radius: 6px; font-size: 0.95rem; color: #0f172a; background-color: #ffffff;">
                </div>
            </div>
            <div style="margin-bottom: 20px;">
                <label style="display: block; font-size: 0.85rem; font-weight: 600; color: #334155; margin-bottom: 6px;">Notes (optional)</label>
                <input type="text" name="notes" placeholder="e.g. Measured after morning walk" style="width: 100%; padding: 10px; border: 1px solid #cbd5e1; border-radius: 6px; font-size: 0.95rem; color: #0f172a; background-color: #ffffff;">
            </div>
            <div style="text-align: right;">
                <button type="submit" style="padding: 10px 24px; background-color: #2563eb; color: #ffffff; border: none; border-radius: 6px; font-weight: 600; font-size: 0.95rem; cursor: pointer;">Save Reading</button>
            </div>
        </form>
    </div>

    <!-- History -->
    <div style="background-color: #ffffff; border: 1px solid #cbd5e1; border-radius: 8px; padding: 24px; box-shadow: 0 1px 3px rgba(0,0,0,0.05);">
        <h2 style="font-size: 1.25rem; margin-top: 0; margin-bottom: 20px; color: #0f172a;">📈 Recent History</h2>
        {% if history %}
        <div style="overflow-x: auto;">
            <table style="width: 100%; border-collapse: collapse; text-align: left;">
                <thead>
                    <tr style="border-bottom: 2px solid #e2e8f0; color: #64748b; font-size: 0.85rem;">
                        <th style="padding: 12px 10px;">Date</th>
                        <th style="padding: 12px 10px;">BP</th>
                        <th style="padding: 12px 10px;">HR</th>
                        <th style="padding: 12px 10px;">SpO2</th>
                        <th style="padding: 12px 10px;">Temp</th>
                        <th style="padding: 12px 10px;">Notes</th>
                    </tr>
                </thead>
                <tbody>
                    {% for v in history %}
                    <tr style="border-bottom: 1px solid #f1f5f9;">
                        <td style="padding: 14px 10px; color: #334155; white-space: nowrap;">{{ v.recorded_at.strftime('%b %d, %Y %I:%M %p') }}</td>
                        <td style="padding: 14px 10px; color: #0f172a; font-weight: 600;">{% if v.systolic and v.diastolic %}{{ v.systolic }}/{{ v.diastolic }}{% else %}&mdash;{% endif %}</td>
                        <td style="padding: 14px 10px; color: #334155;">{{ v.heart_rate or '—' }}</td>
                        <td style="padding: 14px 10px; color: #334155;">{{ v.spo2 or '—' }}</td>
                        <td style="padding: 14px 10px; color: #334155;">{{ v.temperature or '—' }}</td>
                        <td style="padding: 14px 10px; color: #64748b;">{{ v.notes or '' }}</td>
                    </tr>
                    {% endfor %}
                </tbody>
            </table>
        </div>
        {% else %}
        <p style="color: #64748b; margin: 0;">No vitals logged yet. Add your first reading above.</p>
        {% endif %}
    </div>
</main>
</div>
{% endblock %}
FILEEOF_5
cat > templates/symptoms.html << 'FILEEOF_6'
{% extends "base.html" %}
{% block title %}Symptom Checker - MediBro{% endblock %}
{% block content %}
<div class="patient-layout">
    <aside class="patient-sidebar">
        <a href="{{ url_for('my_health') }}">🏠 My Health</a>
        <a href="{{ url_for('patient_dashboard') }}">📅 Appointments</a>
        <a href="{{ url_for('vitals') }}">💓 Vitals</a>
        <a href="{{ url_for('symptoms') }}" class="active">🩺 Symptom Checker</a>
        <a href="{{ url_for('medicines') }}">💊 Medicines</a>
        <a href="{{ url_for('chat_list') }}">💬 Chat</a>
        <a href="{{ url_for('sos') }}" class="sos-link">🚨 SOS</a>
        <a href="{{ url_for('profile') }}">⚙️ Profile</a>
        <a href="{{ url_for('logout') }}">🚪 Log Out</a>
    </aside>

    <main class="patient-main">
    <div style="margin-bottom: 24px; display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 12px;">
        <div>
            <h1 style="margin: 0; font-size: 1.8rem; color: var(--text-dark);">Symptom Checker</h1>
            <p style="margin: 4px 0 0 0; color: var(--text-sub);">Log your symptoms and get basic guidance</p>
        </div>
        <a href="{{ url_for('patient_dashboard') }}" style="color: var(--primary); text-decoration: none; font-weight: 600; font-size: 0.9rem;">&larr; Back to Portal</a>
    </div>

    <div style="background-color: var(--warning-light, #fef3c7); border: 1px solid #fde68a; border-radius: var(--radius); padding: 14px 18px; margin-bottom: 24px; color: #92400e; font-size: 0.85rem;">
        &#9888; This tool gives general guidance only and is not a medical diagnosis. If you believe you are having a medical emergency, use the SOS page or call your local emergency number immediately.
    </div>

    <!-- Log Symptoms -->
    <div style="background-color: var(--surface); border: 1px solid var(--border); border-radius: var(--radius); padding: 24px; margin-bottom: 32px; box-shadow: 0 1px 3px rgba(0,0,0,0.05);">
        <h2 style="font-size: 1.25rem; margin-top: 0; margin-bottom: 20px; color: var(--text-dark);">🩺 What are you experiencing?</h2>
        <form action="{{ url_for('symptoms') }}" method="POST">
            <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
            <div style="margin-bottom: 20px;">
                <label style="display: block; font-size: 0.85rem; font-weight: 600; color: #334155; margin-bottom: 10px;">Select any symptoms that apply</label>
                <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 10px;">
                    {% for symptom in symptom_options %}
                    <label style="display: flex; align-items: center; gap: 8px; padding: 10px; border: 1px solid var(--border); border-radius: 6px; font-size: 0.9rem; color: var(--text-dark); cursor: pointer;">
                        <input type="checkbox" name="symptoms" value="{{ symptom }}">
                        {{ symptom }}
                    </label>
                    {% endfor %}
                </div>
            </div>
            <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 16px; margin-bottom: 20px;">
                <div>
                    <label style="display: block; font-size: 0.85rem; font-weight: 600; color: #334155; margin-bottom: 6px;">Severity</label>
                    <select name="severity" style="width: 100%; padding: 10px; border: 1px solid var(--border); border-radius: 6px; background-color: var(--surface); font-size: 0.95rem; color: var(--text-dark);">
                        <option value="mild">Mild</option>
                        <option value="moderate">Moderate</option>
                        <option value="severe">Severe</option>
                    </select>
                </div>
                <div>
                    <label style="display: block; font-size: 0.85rem; font-weight: 600; color: #334155; margin-bottom: 6px;">Additional details (optional)</label>
                    <input type="text" name="description" placeholder="e.g. Started 2 days ago, worse at night" style="width: 100%; padding: 10px; border: 1px solid var(--border); border-radius: 6px; font-size: 0.95rem; color: var(--text-dark); background-color: var(--surface);">
                </div>
            </div>
            <div style="text-align: right;">
                <button type="submit" style="padding: 10px 24px; background-color: var(--primary); color: #ffffff; border: none; border-radius: 6px; font-weight: 600; font-size: 0.95rem; cursor: pointer;">Check Symptoms</button>
            </div>
        </form>
    </div>

    <!-- History -->
    <div style="background-color: var(--surface); border: 1px solid var(--border); border-radius: var(--radius); padding: 24px; box-shadow: 0 1px 3px rgba(0,0,0,0.05);">
        <h2 style="font-size: 1.25rem; margin-top: 0; margin-bottom: 20px; color: var(--text-dark);">📋 Recent Logs</h2>
        {% if history %}
        <div style="overflow-x: auto;">
            <table style="width: 100%; border-collapse: collapse; text-align: left;">
                <thead>
                    <tr style="border-bottom: 2px solid #e2e8f0; color: var(--text-sub); font-size: 0.85rem;">
                        <th style="padding: 12px 10px;">Date</th>
                        <th style="padding: 12px 10px;">Symptoms</th>
                        <th style="padding: 12px 10px;">Severity</th>
                        <th style="padding: 12px 10px;">Guidance</th>
                    </tr>
                </thead>
                <tbody>
                    {% for log in history %}
                    <tr style="border-bottom: 1px solid #f1f5f9;">
                        <td style="padding: 14px 10px; color: #334155; white-space: nowrap;">{{ log.created_at.strftime('%b %d, %Y %I:%M %p') }}</td>
                        <td style="padding: 14px 10px; color: var(--text-dark); font-weight: 600;">{{ log.symptoms }}</td>
                        <td style="padding: 14px 10px; color: #334155; text-transform: capitalize;">{{ log.severity }}</td>
                        <td style="padding: 14px 10px; color: var(--text-sub);">{{ log.guidance }}</td>
                    </tr>
                    {% endfor %}
                </tbody>
            </table>
        </div>
        {% else %}
        <p style="color: var(--text-sub); margin: 0;">No symptoms logged yet.</p>
        {% endif %}
    </div>
</main>
</div>
{% endblock %}
FILEEOF_6
cat > templates/sos.html << 'FILEEOF_7'
{% extends "base.html" %}
{% block title %}SOS - MediBro{% endblock %}
{% block content %}
<div class="patient-layout">
    <aside class="patient-sidebar">
        <a href="{{ url_for('my_health') }}">🏠 My Health</a>
        <a href="{{ url_for('patient_dashboard') }}">📅 Appointments</a>
        <a href="{{ url_for('vitals') }}">💓 Vitals</a>
        <a href="{{ url_for('symptoms') }}">🩺 Symptom Checker</a>
        <a href="{{ url_for('medicines') }}">💊 Medicines</a>
        <a href="{{ url_for('chat_list') }}">💬 Chat</a>
        <a href="{{ url_for('sos') }}" class="sos-link active">🚨 SOS</a>
        <a href="{{ url_for('profile') }}">⚙️ Profile</a>
        <a href="{{ url_for('logout') }}">🚪 Log Out</a>
    </aside>

    <main class="patient-main">
    <div style="margin-bottom: 24px; display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 12px;">
        <div>
            <h1 style="margin: 0; font-size: 1.8rem; color: var(--text-dark);">Emergency SOS</h1>
            <p style="margin: 4px 0 0 0; color: var(--text-sub);">Quick access to help when you need it</p>
        </div>
        <a href="{{ url_for('patient_dashboard') }}" style="color: var(--primary); text-decoration: none; font-weight: 600; font-size: 0.9rem;">&larr; Back to Portal</a>
    </div>

    <!-- Big SOS trigger -->
    <div style="background-color: var(--danger-light); border: 1px solid #fecaca; border-radius: var(--radius); padding: 28px; margin-bottom: 24px; text-align: center;">
        <p style="margin: 0 0 16px 0; color: var(--danger); font-weight: 600; font-size: 0.95rem;">
            If this is a life-threatening emergency, call your local emergency number now.
        </p>
        <form action="{{ url_for('sos') }}" method="POST" style="display: inline;">
            <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
            <input type="hidden" name="action" value="trigger_sos">
            <button type="submit" style="padding: 16px 40px; background-color: var(--danger); color: #ffffff; border: none; border-radius: 8px; font-weight: 700; font-size: 1.1rem; cursor: pointer;">🚨 Trigger SOS Alert</button>
        </form>
        {% if contact %}
        <p style="margin: 16px 0 0 0; color: #7f1d1d; font-size: 0.9rem;">
            Your emergency contact: <strong>{{ contact.contact_name }}</strong>
            &mdash; <a href="tel:{{ contact.contact_phone }}" style="color: var(--danger); font-weight: 700;">{{ contact.contact_phone }}</a>
        </p>
        {% else %}
        <p style="margin: 16px 0 0 0; color: #7f1d1d; font-size: 0.9rem;">
            No emergency contact saved yet. Add one below so it's ready when you need it.
        </p>
        {% endif %}
    </div>

    <!-- Emergency contact form -->
    <div style="background-color: var(--surface); border: 1px solid var(--border); border-radius: var(--radius); padding: 24px; box-shadow: 0 1px 3px rgba(0,0,0,0.05);">
        <h2 style="font-size: 1.15rem; margin-top: 0; margin-bottom: 20px; color: var(--text-dark);">📇 Emergency Contact</h2>
        <form action="{{ url_for('sos') }}" method="POST">
            <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
            <input type="hidden" name="action" value="save_contact">
            <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 16px; margin-bottom: 16px;">
                <div>
                    <label style="display: block; font-size: 0.85rem; font-weight: 600; color: #334155; margin-bottom: 6px;">Name</label>
                    <input type="text" name="contact_name" required value="{{ contact.contact_name if contact else '' }}" style="width: 100%; padding: 10px; border: 1px solid var(--border); border-radius: 6px; font-size: 0.95rem; color: var(--text-dark); background-color: var(--surface);">
                </div>
                <div>
                    <label style="display: block; font-size: 0.85rem; font-weight: 600; color: #334155; margin-bottom: 6px;">Phone Number</label>
                    <input type="tel" name="contact_phone" required value="{{ contact.contact_phone if contact else '' }}" style="width: 100%; padding: 10px; border: 1px solid var(--border); border-radius: 6px; font-size: 0.95rem; color: var(--text-dark); background-color: var(--surface);">
                </div>
                <div>
                    <label style="display: block; font-size: 0.85rem; font-weight: 600; color: #334155; margin-bottom: 6px;">Relationship</label>
                    <input type="text" name="relation" placeholder="e.g. Spouse, Parent" value="{{ contact.relation if contact else '' }}" style="width: 100%; padding: 10px; border: 1px solid var(--border); border-radius: 6px; font-size: 0.95rem; color: var(--text-dark); background-color: var(--surface);">
                </div>
            </div>
            <div style="text-align: right;">
                <button type="submit" style="padding: 10px 24px; background-color: var(--primary); color: #ffffff; border: none; border-radius: 6px; font-weight: 600; font-size: 0.95rem; cursor: pointer;">Save Contact</button>
            </div>
        </form>
    </div>
</main>
</div>
{% endblock %}
FILEEOF_7
cat > templates/medicines.html << 'FILEEOF_8'
{% extends "base.html" %}
{% block title %}Medicines - MediBro{% endblock %}
{% block content %}
<style>
    details > summary::-webkit-details-marker { display: none; }
    details > summary::marker { content: ""; }
</style>
<div class="patient-layout">
    <aside class="patient-sidebar">
        <a href="{{ url_for('my_health') }}">🏠 My Health</a>
        <a href="{{ url_for('patient_dashboard') }}">📅 Appointments</a>
        <a href="{{ url_for('vitals') }}">💓 Vitals</a>
        <a href="{{ url_for('symptoms') }}">🩺 Symptom Checker</a>
        <a href="{{ url_for('medicines') }}" class="active">💊 Medicines</a>
        <a href="{{ url_for('chat_list') }}">💬 Chat</a>
        <a href="{{ url_for('sos') }}" class="sos-link">🚨 SOS</a>
        <a href="{{ url_for('profile') }}">⚙️ Profile</a>
        <a href="{{ url_for('logout') }}">🚪 Log Out</a>
    </aside>

    <main class="patient-main">
        <div style="margin-bottom: 24px;">
            <h1 style="margin: 0; font-size: 1.8rem; color: #0f172a;">Medicine Reminders</h1>
            <p style="margin: 4px 0 0 0; color: #64748b;">Keep track of what you're taking and when</p>
        </div>

        <div style="background-color: #eff6ff; border: 1px solid #bfdbfe; border-radius: 6px; padding: 10px 14px; margin-bottom: 24px; font-size: 0.8rem; color: #1e40af;">
            ℹ️ This tracker shows an on-page alert while this tab is open near a scheduled time. It cannot send notifications when the app is closed.
        </div>

        <div id="reminder-banner" style="display: none; background-color: #fef3c7; border: 1px solid #fde68a; border-radius: 8px; padding: 14px 18px; margin-bottom: 24px; font-weight: 700; color: #92400e;"></div>

        <!-- Today's Schedule -->
        <div style="background: linear-gradient(135deg, #eff6ff 0%, #ffffff 100%); border: 1px solid #cbd5e1; border-radius: 12px; padding: 24px; margin-bottom: 28px; box-shadow: 0 1px 3px rgba(0,0,0,0.05);">
            <h2 style="font-size: 1.2rem; margin-top: 0; margin-bottom: 20px; color: #0f172a;">📅 Today's Schedule</h2>
            {% if todays_schedule %}
            {% set periods = ['Morning', 'Afternoon', 'Evening', 'Other'] %}
            {% set period_colors = {'Morning': '#f59e0b', 'Afternoon': '#0ea5e9', 'Evening': '#6366f1', 'Other': '#64748b'} %}
            {% for period in periods %}
            {% set period_items = todays_schedule | selectattr('period', 'equalto', period) | list %}
            {% if period_items %}
            <div style="margin-bottom: 18px;">
                <p style="margin: 0 0 10px 0; font-weight: 700; font-size: 0.8rem; text-transform: uppercase; letter-spacing: 0.05em; color: {{ period_colors[period] }};">{{ period_items[0].icon }} {{ period }}</p>
                <div style="display: grid; gap: 10px;">
                    {% for item in period_items %}
                    <div style="display: flex; justify-content: space-between; align-items: center; background-color: #ffffff; border: 1px solid #e2e8f0; border-left: 4px solid {{ period_colors[period] }}; border-radius: 10px; padding: 14px 18px; {% if item.taken %}opacity: 0.6;{% endif %} box-shadow: 0 1px 2px rgba(0,0,0,0.03);">
                        <div style="display: flex; align-items: center; gap: 14px;">
                            <form action="{{ url_for('toggle_dose_taken', dose_id=item.dose.id) }}" method="POST" style="display: inline; flex-shrink: 0;">
                                <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
                                <button type="submit" style="width: 30px; height: 30px; border-radius: 50%; border: 2px solid {% if item.taken %}#16a34a{% else %}#cbd5e1{% endif %}; background-color: {% if item.taken %}#16a34a{% else %}#ffffff{% endif %}; display: flex; align-items: center; justify-content: center; padding: 0; cursor: pointer; transition: all 0.15s;">
                                    {% if item.taken %}<span style="color: white; font-size: 1rem;">✓</span>{% endif %}
                                </button>
                            </form>
                            <div>
                                <p style="margin: 0; font-weight: 700; color: #0f172a; font-size: 1rem; {% if item.taken %}text-decoration: line-through;{% endif %}">{{ item.medicine.name }}</p>
                                {% if item.medicine.dosage %}<p style="margin: 2px 0 0 0; color: #64748b; font-size: 0.8rem;">{{ item.medicine.dosage }}</p>{% endif %}
                            </div>
                        </div>
                        <span style="background-color: {{ period_colors[period] }}1a; color: {{ period_colors[period] }}; padding: 6px 14px; border-radius: 20px; font-weight: 700; font-size: 0.85rem; white-space: nowrap;">{{ item.dose.time }}</span>
                    </div>
                    {% endfor %}
                </div>
            </div>
            {% endif %}
            {% endfor %}
            {% else %}
            <p style="color: #64748b; margin: 0;">No scheduled doses for today. Add a medicine below to get started.</p>
            {% endif %}
        </div>

        <!-- Add Medicine (collapsible) -->
        <details style="background-color: #ffffff; border: 1px solid #cbd5e1; border-radius: 12px; margin-bottom: 28px; box-shadow: 0 1px 3px rgba(0,0,0,0.05); overflow: hidden;">
            <summary style="padding: 18px 24px; cursor: pointer; font-weight: 700; font-size: 1.05rem; color: #0f172a; list-style: none; display: flex; align-items: center; justify-content: space-between;">
                <span>➕ Add a New Medicine</span>
                <span style="color: #2563eb; font-size: 0.85rem; font-weight: 600;">Tap to expand</span>
            </summary>
            <div style="padding: 4px 24px 24px 24px; border-top: 1px solid #f1f5f9;">
                <form action="{{ url_for('medicines') }}" method="POST">
            <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
                    <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 16px; margin: 20px 0 16px 0;">
                        <div>
                            <label style="display: block; font-size: 0.85rem; font-weight: 600; color: #334155; margin-bottom: 6px;">Medicine Name</label>
                            <input type="text" name="name" required placeholder="e.g. Metformin" style="width: 100%; padding: 10px; border: 1px solid #cbd5e1; border-radius: 6px; font-size: 0.95rem; color: #0f172a; background-color: #ffffff;">
                        </div>
                        <div>
                            <label style="display: block; font-size: 0.85rem; font-weight: 600; color: #334155; margin-bottom: 6px;">Dosage</label>
                            <input type="text" name="dosage" placeholder="e.g. 500mg" style="width: 100%; padding: 10px; border: 1px solid #cbd5e1; border-radius: 6px; font-size: 0.95rem; color: #0f172a; background-color: #ffffff;">
                        </div>
                        <div>
                            <label style="display: block; font-size: 0.85rem; font-weight: 600; color: #334155; margin-bottom: 6px;">Frequency Note (optional)</label>
                            <input type="text" name="frequency" placeholder="e.g. With food" style="width: 100%; padding: 10px; border: 1px solid #cbd5e1; border-radius: 6px; font-size: 0.95rem; color: #0f172a; background-color: #ffffff;">
                        </div>
                    </div>

                    <div style="margin-bottom: 16px;">
                        <label style="display: block; font-size: 0.85rem; font-weight: 600; color: #334155; margin-bottom: 10px;">When do you take it?</label>
                        <div style="display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 12px;">
                            <label id="chip-morning" style="display:inline-flex; align-items:center; gap:6px; padding:10px 18px; border:2px solid #cbd5e1; border-radius:24px; cursor:pointer; font-weight:600; font-size:0.9rem; color:#334155; background:#ffffff; transition: all 0.15s;">
                                <input type="checkbox" name="dose_time" value="08:00" style="display:none;" onchange="updateChip('chip-morning', this.checked)">
                                ☀️ Morning
                            </label>
                            <label id="chip-afternoon" style="display:inline-flex; align-items:center; gap:6px; padding:10px 18px; border:2px solid #cbd5e1; border-radius:24px; cursor:pointer; font-weight:600; font-size:0.9rem; color:#334155; background:#ffffff; transition: all 0.15s;">
                                <input type="checkbox" name="dose_time" value="14:00" style="display:none;" onchange="updateChip('chip-afternoon', this.checked)">
                                🌤️ Afternoon
                            </label>
                            <label id="chip-evening" style="display:inline-flex; align-items:center; gap:6px; padding:10px 18px; border:2px solid #cbd5e1; border-radius:24px; cursor:pointer; font-weight:600; font-size:0.9rem; color:#334155; background:#ffffff; transition: all 0.15s;">
                                <input type="checkbox" name="dose_time" value="20:00" style="display:none;" onchange="updateChip('chip-evening', this.checked)">
                                🌙 Evening
                            </label>
                        </div>
                        <div style="display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 12px;">
                            <button type="button" onclick="quickSelect(['chip-morning','chip-evening'])" style="padding: 6px 14px; background-color: #f1f5f9; color: #334155; border: none; border-radius: 6px; font-size: 0.8rem; font-weight: 600; cursor: pointer;">⚡ Twice Daily</button>
                            <button type="button" onclick="quickSelect(['chip-morning','chip-afternoon','chip-evening'])" style="padding: 6px 14px; background-color: #f1f5f9; color: #334155; border: none; border-radius: 6px; font-size: 0.8rem; font-weight: 600; cursor: pointer;">⚡ Thrice Daily</button>
                            <button type="button" onclick="clearChips(['chip-morning','chip-afternoon','chip-evening'])" style="padding: 6px 14px; background-color: #f1f5f9; color: #94a3b8; border: none; border-radius: 6px; font-size: 0.8rem; font-weight: 600; cursor: pointer;">Clear</button>
                        </div>
                        <div>
                            <label style="display: block; font-size: 0.8rem; color: #64748b; margin-bottom: 6px;">Need a different time? Add a custom one:</label>
                            <input type="time" name="dose_time" style="padding: 8px 10px; border: 1px solid #cbd5e1; border-radius: 6px; font-size: 0.9rem; color: #0f172a; background-color: #ffffff;">
                        </div>
                    </div>

                    <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 16px; margin-bottom: 16px;">
                        <div>
                            <label style="display: block; font-size: 0.85rem; font-weight: 600; color: #334155; margin-bottom: 6px;">Start Date</label>
                            <input type="date" name="start_date" style="width: 100%; padding: 10px; border: 1px solid #cbd5e1; border-radius: 6px; font-size: 0.95rem; color: #0f172a; background-color: #ffffff;">
                        </div>
                        <div>
                            <label style="display: block; font-size: 0.85rem; font-weight: 600; color: #334155; margin-bottom: 6px;">End Date (leave blank if ongoing)</label>
                            <input type="date" name="end_date" style="width: 100%; padding: 10px; border: 1px solid #cbd5e1; border-radius: 6px; font-size: 0.95rem; color: #0f172a; background-color: #ffffff;">
                        </div>
                    </div>
                    <div style="margin-bottom: 20px;">
                        <label style="display: block; font-size: 0.85rem; font-weight: 600; color: #334155; margin-bottom: 6px;">Notes (optional)</label>
                        <input type="text" name="notes" placeholder="e.g. Take with a full glass of water" style="width: 100%; padding: 10px; border: 1px solid #cbd5e1; border-radius: 6px; font-size: 0.95rem; color: #0f172a; background-color: #ffffff;">
                    </div>
                    <div style="text-align: right;">
                        <button type="submit" style="padding: 10px 24px; background-color: #2563eb; color: #ffffff; border: none; border-radius: 6px; font-weight: 600; font-size: 0.95rem; cursor: pointer;">Add Medicine</button>
                    </div>
                </form>
            </div>
        </details>

        <!-- Medicine List -->
        <div style="background-color: #ffffff; border: 1px solid #cbd5e1; border-radius: 12px; padding: 24px; box-shadow: 0 1px 3px rgba(0,0,0,0.05);">
            <h2 style="font-size: 1.15rem; margin-top: 0; margin-bottom: 20px; color: #0f172a;">💊 Your Medicines</h2>
            {% if medicines %}
            <div style="display: grid; gap: 12px;">
                {% for med in medicines %}
                <div style="border: 1px solid #e2e8f0; border-radius: 10px; padding: 16px 18px; display: flex; justify-content: space-between; align-items: flex-start; flex-wrap: wrap; gap: 10px;">
                    <div>
                        <p style="margin: 0; font-weight: 700; color: #0f172a; font-size: 1.02rem;">{{ med.name }}{% if med.dosage %} <span style="font-weight: 500; color: #64748b;">&middot; {{ med.dosage }}</span>{% endif %}</p>
                        {% if med.frequency %}<p style="margin: 4px 0 0 0; color: #334155; font-size: 0.85rem;">{{ med.frequency }}</p>{% endif %}
                        {% if med.doses %}
                        <p style="margin: 6px 0 0 0;">
                            {% for dose in med.doses %}<span style="background-color: #eff6ff; color: #1e40af; padding: 3px 10px; border-radius: 12px; font-size: 0.78rem; font-weight: 600; margin-right: 4px; display: inline-block;">{{ dose.time }}</span>{% endfor %}
                        </p>
                        {% elif med.time_of_day %}
                        <p style="margin: 6px 0 0 0; color: #2563eb; font-size: 0.85rem; font-weight: 600;">⏰ {{ med.time_of_day }}</p>
                        {% endif %}
                        {% if med.start_date or med.end_date %}
                        <p style="margin: 6px 0 0 0; color: #94a3b8; font-size: 0.78rem;">
                            {% if med.start_date %}From {{ med.start_date.strftime('%b %d, %Y') }}{% endif %}
                            {% if med.end_date %} to {{ med.end_date.strftime('%b %d, %Y') }}{% else %}{% if med.start_date %} (ongoing){% endif %}{% endif %}
                        </p>
                        {% endif %}
                        {% if med.notes %}<p style="margin: 4px 0 0 0; color: #94a3b8; font-size: 0.78rem;">📝 {{ med.notes }}</p>{% endif %}
                    </div>
                    <div style="display: flex; gap: 12px; flex-shrink: 0; align-items: center;">
                        <a href="{{ url_for('edit_medicine', med_id=med.id) }}" style="color: #2563eb; text-decoration: none; font-weight: 600; font-size: 0.85rem;">Edit</a>
                        <form action="{{ url_for('delete_medicine', med_id=med.id) }}" method="POST" style="display: inline;" onsubmit="return confirm('Remove this medicine reminder?');">
                            <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
                            <button type="submit" style="background: none; border: none; padding: 0; font-family: inherit; color: #dc2626; font-weight: 600; font-size: 0.85rem; cursor: pointer;">Remove</button>
                        </form>
                    </div>
                </div>
                {% endfor %}
            </div>
            {% else %}
            <p style="color: #64748b; margin: 0;">No medicines added yet.</p>
            {% endif %}
        </div>
    </main>
</div>

<script>
function updateChip(id, checked) {
    var el = document.getElementById(id);
    if (checked) {
        el.style.background = '#2563eb';
        el.style.borderColor = '#2563eb';
        el.style.color = '#ffffff';
    } else {
        el.style.background = '#ffffff';
        el.style.borderColor = '#cbd5e1';
        el.style.color = '#334155';
    }
}
function quickSelect(ids) {
    ids.forEach(function(id) {
        var cb = document.querySelector('#' + id + ' input');
        cb.checked = true;
        updateChip(id, true);
    });
}
function clearChips(ids) {
    ids.forEach(function(id) {
        var cb = document.querySelector('#' + id + ' input');
        cb.checked = false;
        updateChip(id, false);
    });
}

(function() {
    var schedule = [
        {% for item in todays_schedule %}
        {% if not item.taken %}
        { name: {{ item.medicine.name | tojson }}, time: {{ item.dose.time | tojson }} },
        {% endif %}
        {% endfor %}
    ];
    var alerted = {};

    function checkReminders() {
        var now = new Date();
        var hh = String(now.getHours()).padStart(2, '0');
        var mm = String(now.getMinutes()).padStart(2, '0');
        var current = hh + ':' + mm;

        schedule.forEach(function(item) {
            if (item.time === current && !alerted[item.time + item.name]) {
                alerted[item.time + item.name] = true;
                var banner = document.getElementById('reminder-banner');
                banner.textContent = '⏰ Time to take ' + item.name + ' (' + item.time + ')';
                banner.style.display = 'block';
            }
        });
    }

    if (schedule.length > 0) {
        checkReminders();
        setInterval(checkReminders, 30000);
    }
})();
</script>
{% endblock %}
FILEEOF_8
cat > templates/medicine_edit.html << 'FILEEOF_9'
{% extends "base.html" %}
{% block title %}Edit {{ med.name }} - MediBro{% endblock %}
{% block content %}
<div class="patient-layout">
    <aside class="patient-sidebar">
        <a href="{{ url_for('my_health') }}">🏠 My Health</a>
        <a href="{{ url_for('patient_dashboard') }}">📅 Appointments</a>
        <a href="{{ url_for('vitals') }}">💓 Vitals</a>
        <a href="{{ url_for('symptoms') }}">🩺 Symptom Checker</a>
        <a href="{{ url_for('medicines') }}" class="active">💊 Medicines</a>
        <a href="{{ url_for('chat_list') }}">💬 Chat</a>
        <a href="{{ url_for('sos') }}" class="sos-link">🚨 SOS</a>
        <a href="{{ url_for('profile') }}">⚙️ Profile</a>
        <a href="{{ url_for('logout') }}">🚪 Log Out</a>
    </aside>

    <main class="patient-main">
        <div style="margin-bottom: 24px; display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 12px;">
            <div>
                <h1 style="margin: 0; font-size: 1.6rem; color: #0f172a;">Edit Medicine</h1>
                <p style="margin: 4px 0 0 0; color: #64748b;">{{ med.name }}</p>
            </div>
            <a href="{{ url_for('medicines') }}" style="color: #2563eb; text-decoration: none; font-weight: 600; font-size: 0.9rem;">&larr; Back to Medicines</a>
        </div>

        <!-- Edit basic details -->
        <div style="background-color: #ffffff; border: 1px solid #cbd5e1; border-radius: 8px; padding: 24px; margin-bottom: 24px; box-shadow: 0 1px 3px rgba(0,0,0,0.05);">
            <h2 style="font-size: 1.1rem; margin-top: 0; margin-bottom: 20px; color: #0f172a;">Details</h2>
            <form action="{{ url_for('edit_medicine', med_id=med.id) }}" method="POST">
            <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
                <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 16px; margin-bottom: 16px;">
                    <div>
                        <label style="display: block; font-size: 0.85rem; font-weight: 600; color: #334155; margin-bottom: 6px;">Medicine Name</label>
                        <input type="text" name="name" required value="{{ med.name }}" style="width: 100%; padding: 10px; border: 1px solid #cbd5e1; border-radius: 6px; font-size: 0.95rem; color: #0f172a; background-color: #ffffff;">
                    </div>
                    <div>
                        <label style="display: block; font-size: 0.85rem; font-weight: 600; color: #334155; margin-bottom: 6px;">Dosage</label>
                        <input type="text" name="dosage" value="{{ med.dosage or '' }}" style="width: 100%; padding: 10px; border: 1px solid #cbd5e1; border-radius: 6px; font-size: 0.95rem; color: #0f172a; background-color: #ffffff;">
                    </div>
                    <div>
                        <label style="display: block; font-size: 0.85rem; font-weight: 600; color: #334155; margin-bottom: 6px;">Frequency</label>
                        <input type="text" name="frequency" value="{{ med.frequency or '' }}" style="width: 100%; padding: 10px; border: 1px solid #cbd5e1; border-radius: 6px; font-size: 0.95rem; color: #0f172a; background-color: #ffffff;">
                    </div>
                </div>
                <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 16px; margin-bottom: 16px;">
                    <div>
                        <label style="display: block; font-size: 0.85rem; font-weight: 600; color: #334155; margin-bottom: 6px;">Start Date</label>
                        <input type="date" name="start_date" value="{{ med.start_date.strftime('%Y-%m-%d') if med.start_date else '' }}" style="width: 100%; padding: 10px; border: 1px solid #cbd5e1; border-radius: 6px; font-size: 0.95rem; color: #0f172a; background-color: #ffffff;">
                    </div>
                    <div>
                        <label style="display: block; font-size: 0.85rem; font-weight: 600; color: #334155; margin-bottom: 6px;">End Date (leave blank if ongoing)</label>
                        <input type="date" name="end_date" value="{{ med.end_date.strftime('%Y-%m-%d') if med.end_date else '' }}" style="width: 100%; padding: 10px; border: 1px solid #cbd5e1; border-radius: 6px; font-size: 0.95rem; color: #0f172a; background-color: #ffffff;">
                    </div>
                </div>
                <div style="margin-bottom: 20px;">
                    <label style="display: block; font-size: 0.85rem; font-weight: 600; color: #334155; margin-bottom: 6px;">Notes</label>
                    <input type="text" name="notes" value="{{ med.notes or '' }}" style="width: 100%; padding: 10px; border: 1px solid #cbd5e1; border-radius: 6px; font-size: 0.95rem; color: #0f172a; background-color: #ffffff;">
                </div>
                <div style="margin-bottom: 20px;">
                    <label style="display: block; font-size: 0.85rem; font-weight: 600; color: #334155; margin-bottom: 10px;">Add Reminder Time(s)</label>
                    <div style="display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 12px;">
                        <label id="chip-morning" style="display:inline-flex; align-items:center; gap:6px; padding:10px 18px; border:2px solid #cbd5e1; border-radius:24px; cursor:pointer; font-weight:600; font-size:0.9rem; color:#334155; background:#ffffff; transition: all 0.15s;">
                            <input type="checkbox" name="new_dose_time" value="08:00" style="display:none;" onchange="updateChip('chip-morning', this.checked)">
                            ☀️ Morning
                        </label>
                        <label id="chip-afternoon" style="display:inline-flex; align-items:center; gap:6px; padding:10px 18px; border:2px solid #cbd5e1; border-radius:24px; cursor:pointer; font-weight:600; font-size:0.9rem; color:#334155; background:#ffffff; transition: all 0.15s;">
                            <input type="checkbox" name="new_dose_time" value="14:00" style="display:none;" onchange="updateChip('chip-afternoon', this.checked)">
                            🌤️ Afternoon
                        </label>
                        <label id="chip-evening" style="display:inline-flex; align-items:center; gap:6px; padding:10px 18px; border:2px solid #cbd5e1; border-radius:24px; cursor:pointer; font-weight:600; font-size:0.9rem; color:#334155; background:#ffffff; transition: all 0.15s;">
                            <input type="checkbox" name="new_dose_time" value="20:00" style="display:none;" onchange="updateChip('chip-evening', this.checked)">
                            🌙 Evening
                        </label>
                    </div>
                    <div style="display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 12px;">
                        <button type="button" onclick="quickSelect(['chip-morning','chip-evening'])" style="padding: 6px 14px; background-color: #f1f5f9; color: #334155; border: none; border-radius: 6px; font-size: 0.8rem; font-weight: 600; cursor: pointer;">⚡ Twice Daily</button>
                        <button type="button" onclick="quickSelect(['chip-morning','chip-afternoon','chip-evening'])" style="padding: 6px 14px; background-color: #f1f5f9; color: #334155; border: none; border-radius: 6px; font-size: 0.8rem; font-weight: 600; cursor: pointer;">⚡ Thrice Daily</button>
                    </div>
                    <div>
                        <label style="display: block; font-size: 0.8rem; color: #64748b; margin-bottom: 6px;">Custom time:</label>
                        <input type="time" name="new_dose_time" style="padding: 8px 10px; border: 1px solid #cbd5e1; border-radius: 6px; font-size: 0.9rem; color: #0f172a; background-color: #ffffff;">
                    </div>
                </div>
                <div style="text-align: right;">
                    <button type="submit" style="padding: 10px 24px; background-color: #2563eb; color: #ffffff; border: none; border-radius: 6px; font-weight: 600; font-size: 0.95rem; cursor: pointer;">Save Changes</button>
                </div>
            </form>
        </div>

        <script>
        function updateChip(id, checked) {
            var el = document.getElementById(id);
            if (checked) {
                el.style.background = '#2563eb';
                el.style.borderColor = '#2563eb';
                el.style.color = '#ffffff';
            } else {
                el.style.background = '#ffffff';
                el.style.borderColor = '#cbd5e1';
                el.style.color = '#334155';
            }
        }
        function quickSelect(ids) {
            ids.forEach(function(id) {
                var cb = document.querySelector('#' + id + ' input');
                cb.checked = true;
                updateChip(id, true);
            });
        }
        </script>

        <!-- Manage times -->
        <div style="background-color: #ffffff; border: 1px solid #cbd5e1; border-radius: 8px; padding: 24px; box-shadow: 0 1px 3px rgba(0,0,0,0.05);">
            <h2 style="font-size: 1.1rem; margin-top: 0; margin-bottom: 16px; color: #0f172a;">Reminder Times</h2>
            {% if med.doses %}
            <div style="display: flex; flex-wrap: wrap; gap: 10px;">
                {% for dose in med.doses %}
                <div style="display: flex; align-items: center; gap: 8px; background-color: #eff6ff; border: 1px solid #bfdbfe; border-radius: 6px; padding: 6px 10px 6px 14px;">
                    <span style="color: #1e40af; font-weight: 600; font-size: 0.9rem;">{{ dose.time }}</span>
                    <form action="{{ url_for('delete_dose', dose_id=dose.id) }}" method="POST" style="display: inline;">
                        <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
                        <button type="submit" style="background: none; border: none; padding: 0; font-family: inherit; color: #dc2626; font-weight: 700; font-size: 0.9rem; cursor: pointer;">&times;</button>
                    </form>
                </div>
                {% endfor %}
            </div>
            {% else %}
            <p style="color: #64748b; margin: 0;">No reminder times set. Add one above.</p>
            {% endif %}
        </div>
    </main>
</div>
{% endblock %}
FILEEOF_9
cat > templates/chat_list.html << 'FILEEOF_10'
{% extends "base.html" %}
{% block title %}Chat - MediBro{% endblock %}
{% block content %}
{% if session.role == 'patient' %}
<div class="patient-layout">
    <aside class="patient-sidebar">
        <a href="{{ url_for('my_health') }}">🏠 My Health</a>
        <a href="{{ url_for('patient_dashboard') }}">📅 Appointments</a>
        <a href="{{ url_for('vitals') }}">💓 Vitals</a>
        <a href="{{ url_for('symptoms') }}">🩺 Symptom Checker</a>
        <a href="{{ url_for('medicines') }}">💊 Medicines</a>
        <a href="{{ url_for('chat_list') }}" class="active">💬 Chat</a>
        <a href="{{ url_for('sos') }}" class="sos-link">🚨 SOS</a>
        <a href="{{ url_for('profile') }}">⚙️ Profile</a>
        <a href="{{ url_for('logout') }}">🚪 Log Out</a>
    </aside>
    <main class="patient-main">
{% else %}
<div style="max-width: 700px; margin: 30px auto; padding: 0 20px;">
    <div style="margin-bottom: 20px;">
        <a href="{{ url_for('doctor_dashboard') }}" style="color: var(--primary); text-decoration: none; font-weight: 600; font-size: 0.9rem;">&larr; Back to Workspace</a>
    </div>
{% endif %}

        <div style="margin-bottom: 24px;">
            <h1 style="margin: 0; font-size: 1.6rem; color: var(--text-dark);">Messages</h1>
        </div>

        <div style="background-color: var(--surface); border: 1px solid var(--border); border-radius: var(--radius); box-shadow: 0 1px 3px rgba(0,0,0,0.05); overflow: hidden;">
            {% if conversations %}
            {% for convo in conversations %}
            <a href="{{ url_for('chat_thread', appointment_id=convo.appointment.id) }}" style="display: flex; justify-content: space-between; align-items: center; padding: 16px 20px; text-decoration: none; border-bottom: 1px solid #f1f5f9; color: inherit;">
                <div>
                    <p style="margin: 0; font-weight: 700; color: var(--text-dark);">
                        {% if session.role == 'patient' %}Dr. {{ convo.partner.full_name.replace('Dr. ', '').replace('Dr ', '') }}{% else %}{{ convo.partner.full_name }}{% endif %}
                        <span style="font-weight: 500; color: var(--text-sub); font-size: 0.8rem;">&middot; {{ convo.appointment.appointment_date }}</span>
                    </p>
                    {% if convo.last_message %}
                    <p style="margin: 4px 0 0 0; color: var(--text-sub); font-size: 0.85rem; max-width: 400px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;">{{ convo.last_message.content }}</p>
                    {% else %}
                    <p style="margin: 4px 0 0 0; color: var(--text-sub); font-size: 0.85rem;">No messages yet</p>
                    {% endif %}
                </div>
                <span style="color: var(--primary); font-size: 0.85rem; font-weight: 600;">Open →</span>
            </a>
            {% endfor %}
            {% else %}
            <p style="color: var(--text-sub); margin: 0; padding: 24px;">No conversations yet. Chat opens up once an appointment is accepted.</p>
            {% endif %}
        </div>

{% if session.role == 'patient' %}
    </main>
</div>
{% else %}
</div>
{% endif %}
{% endblock %}
FILEEOF_10
cat > templates/chat_thread.html << 'FILEEOF_11'
{% extends "base.html" %}
{% block title %}Chat with {{ other_user.full_name }} - MediBro{% endblock %}
{% block content %}
{% if session.role == 'patient' %}
<div class="patient-layout">
    <aside class="patient-sidebar">
        <a href="{{ url_for('my_health') }}">🏠 My Health</a>
        <a href="{{ url_for('patient_dashboard') }}">📅 Appointments</a>
        <a href="{{ url_for('vitals') }}">💓 Vitals</a>
        <a href="{{ url_for('symptoms') }}">🩺 Symptom Checker</a>
        <a href="{{ url_for('medicines') }}">💊 Medicines</a>
        <a href="{{ url_for('chat_list') }}" class="active">💬 Chat</a>
        <a href="{{ url_for('sos') }}" class="sos-link">🚨 SOS</a>
        <a href="{{ url_for('profile') }}">⚙️ Profile</a>
        <a href="{{ url_for('logout') }}">🚪 Log Out</a>
    </aside>
    <main class="patient-main">
{% else %}
<div style="max-width: 700px; margin: 30px auto; padding: 0 20px;">
{% endif %}

        <div style="margin-bottom: 20px;">
            <a href="{{ url_for('chat_list') }}" style="color: var(--primary); text-decoration: none; font-weight: 600; font-size: 0.9rem;">&larr; All Conversations</a>
        </div>

        <div style="margin-bottom: 16px;">
            <h1 style="margin: 0; font-size: 1.4rem; color: var(--text-dark);">
                {% if session.role == 'patient' %}Dr. {{ other_user.full_name.replace('Dr. ', '').replace('Dr ', '') }}{% else %}{{ other_user.full_name }}{% endif %}
            </h1>
            <p style="margin: 4px 0 0 0; color: var(--text-sub); font-size: 0.85rem;">Appointment on {{ appointment.appointment_date }} at {{ appointment.appointment_time }} &middot; {{ appointment.reason or 'General Consultation' }}</p>
        </div>

        <div style="background-color: var(--surface); border: 1px solid var(--border); border-radius: var(--radius); box-shadow: 0 1px 3px rgba(0,0,0,0.05); padding: 20px; margin-bottom: 16px; min-height: 300px; max-height: 500px; overflow-y: auto; display: flex; flex-direction: column; gap: 10px;">
            {% if messages %}
            {% for msg in messages %}
            <div style="display: flex; {% if msg.sender_id == my_user_id %}justify-content: flex-end;{% else %}justify-content: flex-start;{% endif %}">
                <div style="max-width: 75%; padding: 10px 14px; border-radius: 12px; {% if msg.sender_id == my_user_id %}background-color: var(--primary); color: #ffffff;{% else %}background-color: #f1f5f9; color: var(--text-dark);{% endif %}">
                    <p style="margin: 0; font-size: 0.9rem;">{{ msg.content }}</p>
                    <p style="margin: 4px 0 0 0; font-size: 0.7rem; opacity: 0.8;">{{ msg.created_at.strftime('%b %d, %I:%M %p') }}</p>
                </div>
            </div>
            {% endfor %}
            {% else %}
            <p style="color: var(--text-sub); margin: auto;">No messages yet. Say hello!</p>
            {% endif %}
        </div>

        <form action="{{ url_for('chat_thread', appointment_id=appointment.id) }}" method="POST" style="display: flex; gap: 10px;">
            <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
            <input type="text" name="content" required placeholder="Type a message..." style="flex: 1; padding: 10px 14px; border: 1px solid var(--border); border-radius: 6px; font-size: 0.95rem; color: var(--text-dark); background-color: var(--surface);">
            <button type="submit" style="padding: 10px 24px; background-color: var(--primary); color: #ffffff; border: none; border-radius: 6px; font-weight: 600; font-size: 0.95rem; cursor: pointer;">Send</button>
        </form>

{% if session.role == 'patient' %}
    </main>
</div>
{% else %}
</div>
{% endif %}
{% endblock %}
FILEEOF_11

echo "All files written."
echo ""
echo "=== git status ==="
git status

echo ""
read -p "Press Enter to commit and push now (or Ctrl+C to stop and test first): "

git add app.py templates/base.html templates/my_health.html templates/patient_dashboard.html templates/vitals.html templates/symptoms.html templates/sos.html templates/medicines.html templates/medicine_edit.html templates/chat_list.html templates/chat_thread.html
git commit -m "Add unified My Health dashboard as new patient landing page; add sidebar to Vitals/Symptoms/SOS pages"
git push origin main

echo ""
echo "=== Done. Check Render dashboard for the new deploy. ==="
echo "No database changes in this batch - purely new routes and templates."
echo "After logging in as a patient, you should land on the new My Health"
echo "page instead of the old appointments page (which is now under the"
echo "'Appointments' sidebar link)."
