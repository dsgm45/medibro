#!/bin/bash
set -e

echo "=== MediBro: Closing remaining CSRF gaps (action links -> POST forms) ==="

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
        return 'patient_dashboard'

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
                    return redirect(url_for('patient_dashboard'))
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
    app.run(debug=True)
FILEEOF_1
cat > templates/admin.html << 'FILEEOF_2'
{% extends "base.html" %}
{% block title %}Admin Dashboard - MediBro{% endblock %}
{% block content %}
<div style="max-width: 1100px; margin: 30px auto; padding: 0 20px;">

    <div style="margin-bottom: 20px; text-align: right;">
        <a href="{{ url_for('admin_audit_log') }}" style="color: var(--primary); text-decoration: none; font-weight: 600; font-size: 0.9rem;">📜 View Audit Log</a>
    </div>
    
    <!-- Emergency SOS High-Priority Banner -->
    {% if emergency_alerts %}
    <div style="background: var(--danger-light); border: 2px solid var(--danger); border-radius: var(--radius); padding: 20px; margin-bottom: 32px;">
        <h2 style="color: var(--danger); margin-top: 0; display: flex; align-items: center; gap: 8px;">🚨 ACTIVE EMERGENCY SOS ALERTS ({{ emergency_alerts | length }})</h2>
        <div style="display: grid; gap: 12px; margin-top: 12px;">
            {% for sos in emergency_alerts %}
            <div style="background: white; padding: 14px; border-radius: 6px; display: flex; justify-content: space-between; align-items: center;">
                <div>
                    <strong style="color: var(--danger); font-size: 1.1rem;">{{ sos.patient_name }}</strong>
                    <div style="font-size: 0.9rem; color: var(--text-dark);">Contact Phone: <strong>{{ sos.phone }}</strong></div>
                    <div style="font-size: 0.8rem; color: var(--text-sub);">Alert Time: {{ sos.created_at.strftime('%Y-%m-%d %H:%M:%S') }}</div>
                </div>
                <a href="{{ url_for('resolve_sos', sos_id=sos.id) }}" style="padding: 8px 16px; background: var(--success); color: white; text-decoration: none; border-radius: 6px; font-weight: 700;">Mark Resolved</a>
            </div>
            {% endfor %}
        </div>
    </div>
    {% endif %}

    <!-- Analytics Stats Cards -->
    <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 16px; margin-bottom: 32px;">
        <div style="background: var(--surface); border: 1px solid var(--border); padding: 20px; border-radius: var(--radius);">
            <div style="color: var(--text-sub); font-size: 0.875rem; font-weight: 600;">Total Patients</div>
            <div style="font-size: 2rem; font-weight: 700; color: var(--primary); margin-top: 4px;">{{ stats.total_patients }}</div>
        </div>
        <div style="background: var(--surface); border: 1px solid var(--border); padding: 20px; border-radius: var(--radius);">
            <div style="color: var(--text-sub); font-size: 0.875rem; font-weight: 600;">Active Doctors</div>
            <div style="font-size: 2rem; font-weight: 700; color: var(--success); margin-top: 4px;">{{ stats.active_doctors }}</div>
        </div>
        <div style="background: var(--surface); border: 1px solid var(--border); padding: 20px; border-radius: var(--radius);">
            <div style="color: var(--text-sub); font-size: 0.875rem; font-weight: 600;">Pending Approvals</div>
            <div style="font-size: 2rem; font-weight: 700; color: var(--warning); margin-top: 4px;">{{ stats.pending_approvals }}</div>
        </div>
    </div>

    <!-- Doctor Verification Queue -->
    <div style="background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius); padding: 24px; margin-bottom: 32px;">
        <h2 style="font-size: 1.25rem; margin-top: 0;">⏳ Pending Doctor Approvals</h2>
        {% if pending_doctors %}
        <input type="text" id="doctorSearch" onkeyup="filterTable('doctorSearch', 'doctorTable')" placeholder="Search by name or email..." style="width: 100%; padding: 8px 12px; border: 1px solid var(--border); border-radius: 6px; font-size: 0.85rem; margin-bottom: 12px; margin-top: 12px;">
        <table id="doctorTable" style="width: 100%; border-collapse: collapse; text-align: left;">
            <thead>
                <tr style="border-bottom: 2px solid var(--border); color: var(--text-sub); font-size: 0.85rem;">
                    <th style="padding: 10px;">Name</th>
                    <th style="padding: 10px;">Email</th>
                    <th style="padding: 10px;">Specialty</th>
                    <th style="padding: 10px; text-align: right;">Actions</th>
                </tr>
            </thead>
            <tbody>
                {% for doc in pending_doctors %}
                <tr style="border-bottom: 1px solid var(--border);">
                    <td style="padding: 12px 10px; font-weight: 600;">Dr. {{ doc.full_name }}</td>
                    <td style="padding: 12px 10px;">{{ doc.email }}</td>
                    <td style="padding: 12px 10px;">{{ doc.specialty or 'General' }}</td>
                    <td style="padding: 12px 10px; text-align: right;">
                        <form action="{{ url_for('verify_doctor', doctor_id=doc.id, action='approve') }}" method="POST" style="display: inline;">
                            <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
                            <button type="submit" style="padding: 6px 12px; background: var(--success); color: white; border-radius: 6px; border: none; font-size: 0.85rem; font-weight: 600; font-family: inherit; cursor: pointer;">Approve</button>
                        </form>
                        <form action="{{ url_for('verify_doctor', doctor_id=doc.id, action='reject') }}" method="POST" style="display: inline;">
                            <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
                            <button type="submit" style="padding: 6px 12px; background: var(--danger); color: white; border-radius: 6px; border: none; font-size: 0.85rem; font-weight: 600; font-family: inherit; cursor: pointer;">Reject</button>
                        </form>
                    </td>
                </tr>
                {% endfor %}
            </tbody>
        </table>
        {% else %}
        <p style="color: var(--text-sub); margin: 0;">No pending doctor approvals.</p>
        {% endif %}
    </div>

    <!-- Active Doctors Directory -->
    <div style="background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius); padding: 24px; margin-bottom: 32px;">
        <h2 style="font-size: 1.25rem; margin-top: 0;">👨‍⚕️ Active Doctors</h2>
        {% if approved_doctors %}
        <input type="text" id="activeDoctorSearch" onkeyup="filterTable('activeDoctorSearch', 'activeDoctorTable')" placeholder="Search by name or email..." style="width: 100%; padding: 8px 12px; border: 1px solid var(--border); border-radius: 6px; font-size: 0.85rem; margin-bottom: 12px; margin-top: 12px;">
        <table id="activeDoctorTable" style="width: 100%; border-collapse: collapse; text-align: left;">
            <thead>
                <tr style="border-bottom: 2px solid var(--border); color: var(--text-sub); font-size: 0.85rem;">
                    <th style="padding: 10px;">Name</th>
                    <th style="padding: 10px;">Email</th>
                    <th style="padding: 10px;">Specialty</th>
                    <th style="padding: 10px;">Status</th>
                    <th style="padding: 10px; text-align: right;">Account Control</th>
                </tr>
            </thead>
            <tbody>
                {% for doc in approved_doctors %}
                <tr style="border-bottom: 1px solid var(--border);">
                    <td style="padding: 12px 10px; font-weight: 600;">Dr. {{ doc.full_name }}</td>
                    <td style="padding: 12px 10px;">{{ doc.email }}</td>
                    <td style="padding: 12px 10px;">{{ doc.specialty or 'General' }}</td>
                    <td style="padding: 12px 10px;">
                        <span style="padding: 4px 8px; border-radius: 4px; font-size: 0.8rem; font-weight: 600; background: var(--success-light); color: var(--success);">Approved</span>
                    </td>
                    <td style="padding: 12px 10px; text-align: right;">
                        <form action="{{ url_for('toggle_user_status', user_id=doc.id) }}" method="POST" style="display: inline;">
                            <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
                            <button type="submit" style="padding: 6px 12px; background: var(--danger-light); color: var(--danger); border-radius: 6px; border: none; font-size: 0.85rem; font-weight: 600; font-family: inherit; cursor: pointer;">Suspend</button>
                        </form>
                    </td>
                </tr>
                {% endfor %}
            </tbody>
        </table>
        {% else %}
        <p style="color: var(--text-sub); margin: 0;">No active doctors yet.</p>
        {% endif %}
    </div>

    <!-- Patient Directory -->
    <div style="background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius); padding: 24px;">
        <h2 style="font-size: 1.25rem; margin-top: 0;">👥 Patient Directory</h2>
        {% if patients %}
        <input type="text" id="patientSearch" onkeyup="filterTable('patientSearch', 'patientTable')" placeholder="Search by name or email..." style="width: 100%; padding: 8px 12px; border: 1px solid var(--border); border-radius: 6px; font-size: 0.85rem; margin-bottom: 12px; margin-top: 12px;">
        <table id="patientTable" style="width: 100%; border-collapse: collapse; text-align: left;">
            <thead>
                <tr style="border-bottom: 2px solid var(--border); color: var(--text-sub); font-size: 0.85rem;">
                    <th style="padding: 10px;">Name</th>
                    <th style="padding: 10px;">Email</th>
                    <th style="padding: 10px;">Status</th>
                    <th style="padding: 10px; text-align: right;">Account Control</th>
                </tr>
            </thead>
            <tbody>
                {% for p in patients %}
                <tr style="border-bottom: 1px solid var(--border);">
                    <td style="padding: 12px 10px; font-weight: 600;">{{ p.full_name }}</td>
                    <td style="padding: 12px 10px;">{{ p.email }}</td>
                    <td style="padding: 12px 10px;">
                        <span style="padding: 4px 8px; border-radius: 4px; font-size: 0.8rem; font-weight: 600; background: {{ 'var(--success-light)' if p.status == 'approved' else 'var(--danger-light)' }}; color: {{ 'var(--success)' if p.status == 'approved' else 'var(--danger)' }};">
                            {{ p.status | capitalize }}
                        </span>
                    </td>
                    <td style="padding: 12px 10px; text-align: right;">
                        <form action="{{ url_for('toggle_user_status', user_id=p.id) }}" method="POST" style="display: inline;">
                            <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
                            <button type="submit" style="padding: 6px 12px; background: {{ 'var(--danger-light)' if p.status == 'approved' else 'var(--success-light)' }}; color: {{ 'var(--danger)' if p.status == 'approved' else 'var(--success)' }}; border-radius: 6px; border: none; font-size: 0.85rem; font-weight: 600; font-family: inherit; cursor: pointer;">
                                {{ 'Suspend' if p.status == 'approved' else 'Activate' }}
                            </button>
                        </form>
                    </td>
                </tr>
                {% endfor %}
            </tbody>
        </table>
        {% else %}
        <p style="color: var(--text-sub); margin: 0;">No patients registered yet.</p>
        {% endif %}
    </div>
</div>

<script>
function filterTable(inputId, tableId) {
    const query = document.getElementById(inputId).value.toLowerCase();
    const rows = document.getElementById(tableId).getElementsByTagName('tbody')[0].getElementsByTagName('tr');
    for (let i = 0; i < rows.length; i++) {
        const text = rows[i].textContent.toLowerCase();
        rows[i].style.display = text.includes(query) ? '' : 'none';
    }
}
</script>
{% endblock %}
FILEEOF_2
cat > templates/doctor_dashboard.html << 'FILEEOF_3'
{% extends "base.html" %}
{% block title %}Doctor Workspace - MediBro{% endblock %}
{% block content %}
<div style="max-width: 1100px; margin: 30px auto; padding: 0 20px;">
    <div style="margin-bottom: 24px; display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 12px;">
        <div>
            <h1 style="margin: 0; font-size: 1.8rem; color: #0f172a;">Doctor Workspace</h1>
            <p style="margin: 4px 0 0 0; color: #64748b;">Welcome, Dr. {{ doctor.full_name.replace('Dr. ', '').replace('Dr ', '') }} ({{ doctor.specialty or 'General Medicine' }})</p>
        </div>
        <a href="{{ url_for('doctor_profile') }}" style="padding: 10px 20px; background-color: #2563eb; color: #ffffff; border-radius: 6px; text-decoration: none; font-weight: 600; font-size: 0.9rem;">⚙️ Edit Profile</a>
    </div>

    <!-- Appointments grouped by day -->
    <div style="background-color: #ffffff; border: 1px solid #cbd5e1; border-radius: 8px; padding: 24px; box-shadow: 0 1px 3px rgba(0,0,0,0.05);">
        <h2 style="font-size: 1.25rem; margin-top: 0; margin-bottom: 20px; color: #0f172a;">🩺 Patient Appointment Requests</h2>
        {% if grouped_appointments %}
        {% for date_label, day_appointments in grouped_appointments %}
        <div style="margin-bottom: 24px;">
            <h3 style="font-size: 0.95rem; font-weight: 700; color: #2563eb; margin: 0 0 10px 0; padding-bottom: 6px; border-bottom: 2px solid #e2e8f0;">{{ date_label }}</h3>
            <div style="overflow-x: auto;">
                <table style="width: 100%; border-collapse: collapse; text-align: left;">
                    <thead>
                        <tr style="border-bottom: 1px solid #e2e8f0; color: #64748b; font-size: 0.8rem;">
                            <th style="padding: 10px;">Patient Name</th>
                            <th style="padding: 10px;">Phone</th>
                            <th style="padding: 10px;">Time</th>
                            <th style="padding: 10px;">Reason</th>
                            <th style="padding: 10px;">Status</th>
                            <th style="padding: 10px; text-align: right;">Action</th>
                        </tr>
                    </thead>
                    <tbody>
                        {% for appt in day_appointments %}
                        <tr style="border-bottom: 1px solid #f1f5f9;">
                            <td style="padding: 12px 10px; font-weight: 600; color: #0f172a;">
                                <a href="{{ url_for('view_patient_history', patient_id=appt.patient.id) }}" style="color: #0f172a; text-decoration: none;">{{ appt.patient.full_name }}</a>
                            </td>
                            <td style="padding: 12px 10px; color: #334155;">{{ appt.phone_number or appt.patient.phone or 'N/A' }}</td>
                            <td style="padding: 12px 10px; color: #334155;">{{ appt.appointment_time }}</td>
                            <td style="padding: 12px 10px; color: #334155;">{{ appt.reason or 'General Consultation' }}</td>
                            <td style="padding: 12px 10px;">
                                {% if appt.status == 'accepted' %}
                                <span style="background-color: #dcfce7; color: #15803d; padding: 4px 12px; border-radius: 12px; font-weight: 600; font-size: 0.85rem; display: inline-block;">Accepted</span>
                                {% elif appt.status == 'declined' %}
                                <span style="background-color: #fee2e2; color: #b91c1c; padding: 4px 12px; border-radius: 12px; font-weight: 600; font-size: 0.85rem; display: inline-block;">Declined</span>
                                {% elif appt.status == 'cancelled' %}
                                <span style="background-color: #f1f5f9; color: #64748b; padding: 4px 12px; border-radius: 12px; font-weight: 600; font-size: 0.85rem; display: inline-block;">Cancelled</span>
                                {% elif appt.status == 'completed' %}
                                <span style="background-color: #ede9fe; color: #6d28d9; padding: 4px 12px; border-radius: 12px; font-weight: 600; font-size: 0.85rem; display: inline-block;">Completed{% if appt.follow_up_requested %} &middot; Follow-up sent{% endif %}</span>
                                {% else %}
                                <span style="background-color: #fef3c7; color: #b45309; padding: 4px 12px; border-radius: 12px; font-weight: 600; font-size: 0.85rem; display: inline-block;">Pending</span>
                                {% endif %}
                            </td>
                            <td style="padding: 12px 10px; text-align: right;">
                                {% if appt.status == 'pending' %}
                                <form action="{{ url_for('handle_appointment', app_id=appt.id, action='accept') }}" method="POST" style="display: inline;">
                                    <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
                                    <button type="submit" style="padding: 6px 14px; background-color: #16a34a; color: #ffffff; border-radius: 6px; border: none; font-size: 0.85rem; font-weight: 600; font-family: inherit; cursor: pointer; margin-right: 6px;">Accept</button>
                                </form>
                                <form action="{{ url_for('handle_appointment', app_id=appt.id, action='decline') }}" method="POST" style="display: inline;">
                                    <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
                                    <button type="submit" style="padding: 6px 14px; background-color: #dc2626; color: #ffffff; border-radius: 6px; border: none; font-size: 0.85rem; font-weight: 600; font-family: inherit; cursor: pointer;">Decline</button>
                                </form>
                                {% elif appt.status == 'accepted' %}
                                <a href="{{ url_for('chat_thread', appointment_id=appt.id) }}" style="color: #2563eb; font-size: 0.85rem; font-weight: 600; text-decoration: none; margin-right: 10px;">💬 Chat</a>
                                <form action="{{ url_for('handle_appointment', app_id=appt.id, action='complete') }}" method="POST" style="display: inline;">
                                    <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
                                    <button type="submit" style="padding: 6px 14px; background-color: #6d28d9; color: #ffffff; border-radius: 6px; border: none; font-size: 0.85rem; font-weight: 600; font-family: inherit; cursor: pointer;">Mark Completed</button>
                                </form>
                                {% elif appt.status == 'completed' %}
                                <a href="{{ url_for('chat_thread', appointment_id=appt.id) }}" style="color: #2563eb; font-size: 0.85rem; font-weight: 600; text-decoration: none; margin-right: 10px;">💬 Chat</a>
                                {% if not appt.follow_up_requested %}
                                <form action="{{ url_for('request_follow_up', app_id=appt.id) }}" method="POST" style="display: inline;">
                                    <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
                                    <button type="submit" style="padding: 6px 14px; background-color: #2563eb; color: #ffffff; border-radius: 6px; border: none; font-size: 0.85rem; font-weight: 600; font-family: inherit; cursor: pointer;">Request Follow-up</button>
                                </form>
                                {% endif %}
                                {% else %}
                                <a href="{{ url_for('view_patient_history', patient_id=appt.patient.id) }}" style="color: #2563eb; font-size: 0.85rem; font-weight: 600; text-decoration: none;">View History</a>
                                {% endif %}
                            </td>
                        </tr>
                        {% endfor %}
                    </tbody>
                </table>
            </div>
        </div>
        {% endfor %}
        {% else %}
        <p style="color: #64748b; margin: 0;">No appointment requests received yet.</p>
        {% endif %}
    </div>
</div>
{% endblock %}
FILEEOF_3
cat > templates/patient_dashboard.html << 'FILEEOF_4'
{% extends "base.html" %}
{% block title %}Patient Portal - MediBro{% endblock %}
{% block content %}
<div class="patient-layout">
    <aside class="patient-sidebar">
        <a href="{{ url_for('patient_dashboard') }}" class="active">🏠 Dashboard</a>
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
cat > templates/medicines.html << 'FILEEOF_5'
{% extends "base.html" %}
{% block title %}Medicines - MediBro{% endblock %}
{% block content %}
<style>
    details > summary::-webkit-details-marker { display: none; }
    details > summary::marker { content: ""; }
</style>
<div class="patient-layout">
    <aside class="patient-sidebar">
        <a href="{{ url_for('patient_dashboard') }}">🏠 Dashboard</a>
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
FILEEOF_5
cat > templates/medicine_edit.html << 'FILEEOF_6'
{% extends "base.html" %}
{% block title %}Edit {{ med.name }} - MediBro{% endblock %}
{% block content %}
<div class="patient-layout">
    <aside class="patient-sidebar">
        <a href="{{ url_for('patient_dashboard') }}">🏠 Dashboard</a>
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
FILEEOF_6

echo "All files written."
echo ""
echo "=== git status ==="
git status

echo ""
read -p "Press Enter to commit and push now (or Ctrl+C to stop and test first): "

git add app.py templates/admin.html templates/doctor_dashboard.html templates/patient_dashboard.html templates/medicines.html templates/medicine_edit.html
git commit -m "Close CSRF gaps: convert remaining action links to CSRF-protected POST forms"
git push origin main

echo ""
echo "=== Done. Check Render dashboard for the new deploy. ==="
echo "8 routes were changed to accept POST instead of GET: accept/decline/"
echo "complete appointment, request follow-up, cancel appointment, approve/"
echo "reject doctor, suspend/activate user, delete medicine, delete dose,"
echo "and mark dose taken. All buttons look and behave the same as before -"
echo "this is a backend security change, not a visible one."
