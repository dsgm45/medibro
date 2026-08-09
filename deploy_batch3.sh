#!/bin/bash
set -e

echo "=== MediBro: Deploying Batch 3 (14 improvements + profile back-link) ==="

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
from sqlalchemy import text
from werkzeug.security import generate_password_hash, check_password_hash
from email_validator import validate_email, EmailNotValidError

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'medibro_secret_key_2026')

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
    status = db.Column(db.String(20), nullable=False, default='pending')
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

def migrate_schema():
    """Adds any new columns to existing tables. Safe to run on every startup."""
    with app.app_context():
        migrations = [
            'ALTER TABLE "user" ADD COLUMN IF NOT EXISTS bio TEXT',
            'ALTER TABLE "user" ADD COLUMN IF NOT EXISTS hours VARCHAR(200)',
        ]
        for stmt in migrations:
            try:
                db.session.execute(text(stmt))
                db.session.commit()
            except Exception as e:
                db.session.rollback()
                app.logger.warning(f"Migration statement skipped (may already be applied): {e}")

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
migrate_schema()

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

    return render_template(
        'patient_dashboard.html',
        doctors=doctors,
        appointments=my_appointments,
        all_specialties=all_specialties,
        specialty_filter=specialty_filter,
        upcoming=upcoming
    )

@app.route('/book-appointment', methods=['POST'])
@login_required
@role_required('patient')
def book_appointment():
    doctor_id = request.form.get('doctor_id')
    appointment_date = request.form.get('appointment_date')
    appointment_time = request.form.get('appointment_time')
    reason = request.form.get('reason', '').strip()

    if not doctor_id or not appointment_date or not appointment_time:
        flash('Please fill in all required appointment fields.', 'error')
        return redirect(url_for('patient_dashboard'))

    try:
        new_app = Appointment(
            patient_id=session['user_id'],
            doctor_id=int(doctor_id),
            appointment_date=appointment_date,
            appointment_time=appointment_time,
            reason=reason,
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

@app.route('/my-appointment/<int:app_id>/cancel')
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

@app.route('/appointment/<int:app_id>/<action>')
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

        db.session.commit()
    except Exception as e:
        db.session.rollback()
        app.logger.error(f"Appointment handle error: {e}")
        flash('Database error updating appointment.', 'error')

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

    return render_template(
        'patient_history_view.html',
        patient=patient,
        vitals_history=vitals_history,
        symptom_history=symptom_history
    )

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

@app.route('/admin/verify/<int:doctor_id>/<action>')
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

@app.route('/admin/toggle-user/<int:user_id>')
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
cat > templates/patient_dashboard.html << 'FILEEOF_3'
{% extends "base.html" %}
{% block title %}Patient Portal - MediBro{% endblock %}
{% block content %}
<div style="max-width: 1100px; margin: 30px auto; padding: 0 20px;">
    <div style="margin-bottom: 24px; display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 12px;">
        <div>
            <h1 style="margin: 0; font-size: 1.8rem; color: #0f172a;">Patient Portal</h1>
            <p style="margin: 4px 0 0 0; color: #64748b;">Welcome back, {{ session.full_name }}</p>
        </div>
        <div style="display: flex; gap: 10px; flex-wrap: wrap;">
            <a href="{{ url_for('vitals') }}" style="padding: 10px 20px; background-color: #2563eb; color: #ffffff; border-radius: 6px; text-decoration: none; font-weight: 600; font-size: 0.9rem;">💓 Log Vitals</a>
            <a href="{{ url_for('symptoms') }}" style="padding: 10px 20px; background-color: #059669; color: #ffffff; border-radius: 6px; text-decoration: none; font-weight: 600; font-size: 0.9rem;">🩺 Symptom Checker</a>
            <a href="{{ url_for('sos') }}" style="padding: 10px 20px; background-color: #dc2626; color: #ffffff; border-radius: 6px; text-decoration: none; font-weight: 600; font-size: 0.9rem;">🚨 SOS</a>
        </div>
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
        <form action="{{ url_for('book_appointment') }}" method="POST">
            <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 16px; margin-bottom: 16px;">
                <div>
                    <label style="display: block; font-size: 0.85rem; font-weight: 600; color: #334155; margin-bottom: 6px;">Select Doctor</label>
                    <select name="doctor_id" required style="width: 100%; padding: 10px; border: 1px solid #cbd5e1; border-radius: 6px; background-color: #ffffff; font-size: 0.95rem; color: #0f172a;">
                        <option value="">-- Choose Specialist --</option>
                        {% for doc in doctors %}
                        <option value="{{ doc.id }}">Dr. {{ doc.full_name.replace('Dr. ', '').replace('Dr ', '') }} ({{ doc.specialty or 'General Practice' }})</option>
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
                            {% else %}
                            <span style="background-color: #fef3c7; color: #b45309; padding: 4px 12px; border-radius: 12px; font-weight: 600; font-size: 0.85rem; display: inline-block;">Pending</span>
                            {% endif %}
                        </td>
                        <td style="padding: 14px 10px; text-align: right;">
                            {% if appt.status == 'pending' %}
                            <a href="{{ url_for('cancel_appointment', app_id=appt.id) }}" onclick="return confirm('Cancel this appointment request?');" style="color: #dc2626; text-decoration: none; font-weight: 600; font-size: 0.85rem;">Cancel</a>
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
</div>
{% endblock %}
FILEEOF_3
cat > templates/doctor_dashboard.html << 'FILEEOF_4'
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
                            <td style="padding: 12px 10px; color: #334155;">{{ appt.patient.phone or 'N/A' }}</td>
                            <td style="padding: 12px 10px; color: #334155;">{{ appt.appointment_time }}</td>
                            <td style="padding: 12px 10px; color: #334155;">{{ appt.reason or 'General Consultation' }}</td>
                            <td style="padding: 12px 10px;">
                                {% if appt.status == 'accepted' %}
                                <span style="background-color: #dcfce7; color: #15803d; padding: 4px 12px; border-radius: 12px; font-weight: 600; font-size: 0.85rem; display: inline-block;">Accepted</span>
                                {% elif appt.status == 'declined' %}
                                <span style="background-color: #fee2e2; color: #b91c1c; padding: 4px 12px; border-radius: 12px; font-weight: 600; font-size: 0.85rem; display: inline-block;">Declined</span>
                                {% elif appt.status == 'cancelled' %}
                                <span style="background-color: #f1f5f9; color: #64748b; padding: 4px 12px; border-radius: 12px; font-weight: 600; font-size: 0.85rem; display: inline-block;">Cancelled</span>
                                {% else %}
                                <span style="background-color: #fef3c7; color: #b45309; padding: 4px 12px; border-radius: 12px; font-weight: 600; font-size: 0.85rem; display: inline-block;">Pending</span>
                                {% endif %}
                            </td>
                            <td style="padding: 12px 10px; text-align: right;">
                                {% if appt.status == 'pending' %}
                                <a href="{{ url_for('handle_appointment', app_id=appt.id, action='accept') }}" style="padding: 6px 14px; background-color: #16a34a; color: #ffffff; border-radius: 6px; text-decoration: none; font-size: 0.85rem; font-weight: 600; margin-right: 6px; display: inline-block;">Accept</a>
                                <a href="{{ url_for('handle_appointment', app_id=appt.id, action='decline') }}" style="padding: 6px 14px; background-color: #dc2626; color: #ffffff; border-radius: 6px; text-decoration: none; font-size: 0.85rem; font-weight: 600; display: inline-block;">Decline</a>
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
FILEEOF_4
cat > templates/doctor_profile.html << 'FILEEOF_5'
{% extends "base.html" %}
{% block title %}Edit Profile - MediBro{% endblock %}
{% block content %}
<div style="max-width: 640px; margin: 30px auto; padding: 0 20px;">
    <div style="margin-bottom: 24px; display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 12px;">
        <div>
            <h1 style="margin: 0; font-size: 1.6rem; color: var(--text-dark);">Edit Profile</h1>
            <p style="margin: 4px 0 0 0; color: var(--text-sub);">Dr. {{ doctor.full_name.replace('Dr. ', '').replace('Dr ', '') }}</p>
        </div>
        <a href="{{ url_for('doctor_dashboard') }}" style="color: var(--primary); text-decoration: none; font-weight: 600; font-size: 0.9rem;">&larr; Back to Portal</a>
    </div>

    <div style="background-color: var(--surface); border: 1px solid var(--border); border-radius: var(--radius); padding: 24px; box-shadow: 0 1px 3px rgba(0,0,0,0.05);">
        <form method="POST" style="display: grid; gap: 16px;">
            <div>
                <label style="display: block; font-size: 0.85rem; font-weight: 600; color: #334155; margin-bottom: 6px;">Specialty</label>
                <input type="text" name="specialty" value="{{ doctor.specialty or '' }}" placeholder="e.g. Cardiology" style="width: 100%; padding: 10px; border: 1px solid var(--border); border-radius: 6px; font-size: 0.95rem; color: var(--text-dark); background-color: var(--surface);">
            </div>
            <div>
                <label style="display: block; font-size: 0.85rem; font-weight: 600; color: #334155; margin-bottom: 6px;">Phone</label>
                <input type="text" name="phone" value="{{ doctor.phone or '' }}" style="width: 100%; padding: 10px; border: 1px solid var(--border); border-radius: 6px; font-size: 0.95rem; color: var(--text-dark); background-color: var(--surface);">
            </div>
            <div>
                <label style="display: block; font-size: 0.85rem; font-weight: 600; color: #334155; margin-bottom: 6px;">Working Hours</label>
                <input type="text" name="hours" value="{{ doctor.hours or '' }}" placeholder="e.g. Mon-Fri, 9:00 AM - 5:00 PM" style="width: 100%; padding: 10px; border: 1px solid var(--border); border-radius: 6px; font-size: 0.95rem; color: var(--text-dark); background-color: var(--surface);">
            </div>
            <div>
                <label style="display: block; font-size: 0.85rem; font-weight: 600; color: #334155; margin-bottom: 6px;">Bio</label>
                <textarea name="bio" rows="5" placeholder="A short introduction patients will see..." style="width: 100%; padding: 10px; border: 1px solid var(--border); border-radius: 6px; font-size: 0.95rem; color: var(--text-dark); background-color: var(--surface); font-family: inherit; resize: vertical;">{{ doctor.bio or '' }}</textarea>
            </div>
            <div style="text-align: right;">
                <button type="submit" style="padding: 10px 24px; background-color: var(--primary); color: #ffffff; border: none; border-radius: 6px; font-weight: 600; font-size: 0.95rem; cursor: pointer;">Save Profile</button>
            </div>
        </form>
    </div>
</div>
{% endblock %}
FILEEOF_5
cat > templates/patient_history_view.html << 'FILEEOF_6'
{% extends "base.html" %}
{% block title %}{{ patient.full_name }} - Patient History{% endblock %}
{% block content %}
<div style="max-width: 1000px; margin: 30px auto; padding: 0 20px;">
    <div style="margin-bottom: 24px; display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 12px;">
        <div>
            <h1 style="margin: 0; font-size: 1.6rem; color: var(--text-dark);">{{ patient.full_name }}</h1>
            <p style="margin: 4px 0 0 0; color: var(--text-sub);">{{ patient.phone or 'No phone on file' }} &middot; {{ patient.email }}</p>
        </div>
        <a href="{{ url_for('doctor_dashboard') }}" style="color: var(--primary); text-decoration: none; font-weight: 600; font-size: 0.9rem;">&larr; Back to Workspace</a>
    </div>

    <!-- Vitals History -->
    <div style="background-color: var(--surface); border: 1px solid var(--border); border-radius: var(--radius); padding: 24px; margin-bottom: 24px; box-shadow: 0 1px 3px rgba(0,0,0,0.05);">
        <h2 style="font-size: 1.15rem; margin-top: 0; margin-bottom: 16px; color: var(--text-dark);">💓 Vitals History</h2>
        {% if vitals_history %}
        <div style="overflow-x: auto;">
            <table style="width: 100%; border-collapse: collapse; text-align: left;">
                <thead>
                    <tr style="border-bottom: 2px solid #e2e8f0; color: var(--text-sub); font-size: 0.85rem;">
                        <th style="padding: 10px;">Date</th>
                        <th style="padding: 10px;">BP</th>
                        <th style="padding: 10px;">HR</th>
                        <th style="padding: 10px;">SpO2</th>
                        <th style="padding: 10px;">Temp</th>
                    </tr>
                </thead>
                <tbody>
                    {% for v in vitals_history %}
                    <tr style="border-bottom: 1px solid #f1f5f9;">
                        <td style="padding: 10px; color: #334155; white-space: nowrap;">{{ v.recorded_at.strftime('%b %d, %Y %I:%M %p') }}</td>
                        <td style="padding: 10px; color: var(--text-dark); font-weight: 600;">{% if v.systolic and v.diastolic %}{{ v.systolic }}/{{ v.diastolic }}{% else %}&mdash;{% endif %}</td>
                        <td style="padding: 10px; color: #334155;">{{ v.heart_rate or '—' }}</td>
                        <td style="padding: 10px; color: #334155;">{{ v.spo2 or '—' }}</td>
                        <td style="padding: 10px; color: #334155;">{{ v.temperature or '—' }}</td>
                    </tr>
                    {% endfor %}
                </tbody>
            </table>
        </div>
        {% else %}
        <p style="color: var(--text-sub); margin: 0;">No vitals logged by this patient.</p>
        {% endif %}
    </div>

    <!-- Symptom History -->
    <div style="background-color: var(--surface); border: 1px solid var(--border); border-radius: var(--radius); padding: 24px; box-shadow: 0 1px 3px rgba(0,0,0,0.05);">
        <h2 style="font-size: 1.15rem; margin-top: 0; margin-bottom: 16px; color: var(--text-dark);">🩺 Symptom History</h2>
        {% if symptom_history %}
        <div style="overflow-x: auto;">
            <table style="width: 100%; border-collapse: collapse; text-align: left;">
                <thead>
                    <tr style="border-bottom: 2px solid #e2e8f0; color: var(--text-sub); font-size: 0.85rem;">
                        <th style="padding: 10px;">Date</th>
                        <th style="padding: 10px;">Symptoms</th>
                        <th style="padding: 10px;">Severity</th>
                        <th style="padding: 10px;">Notes</th>
                    </tr>
                </thead>
                <tbody>
                    {% for log in symptom_history %}
                    <tr style="border-bottom: 1px solid #f1f5f9;">
                        <td style="padding: 10px; color: #334155; white-space: nowrap;">{{ log.created_at.strftime('%b %d, %Y %I:%M %p') }}</td>
                        <td style="padding: 10px; color: var(--text-dark); font-weight: 600;">{{ log.symptoms }}</td>
                        <td style="padding: 10px; color: #334155; text-transform: capitalize;">{{ log.severity }}</td>
                        <td style="padding: 10px; color: var(--text-sub);">{{ log.description or '' }}</td>
                    </tr>
                    {% endfor %}
                </tbody>
            </table>
        </div>
        {% else %}
        <p style="color: var(--text-sub); margin: 0;">No symptoms logged by this patient.</p>
        {% endif %}
    </div>
</div>
{% endblock %}
FILEEOF_6
cat > templates/audit_log.html << 'FILEEOF_7'
{% extends "base.html" %}
{% block title %}Audit Log - MediBro{% endblock %}
{% block content %}
<div style="max-width: 1000px; margin: 30px auto; padding: 0 20px;">
    <div style="margin-bottom: 24px; display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 12px;">
        <div>
            <h1 style="margin: 0; font-size: 1.6rem; color: var(--text-dark);">Admin Audit Log</h1>
            <p style="margin: 4px 0 0 0; color: var(--text-sub);">Most recent 100 admin actions</p>
        </div>
        <a href="{{ url_for('admin_dashboard') }}" style="color: var(--primary); text-decoration: none; font-weight: 600; font-size: 0.9rem;">&larr; Back to Admin</a>
    </div>

    <div style="background-color: var(--surface); border: 1px solid var(--border); border-radius: var(--radius); padding: 24px; box-shadow: 0 1px 3px rgba(0,0,0,0.05);">
        {% if logs %}
        <div style="overflow-x: auto;">
            <table style="width: 100%; border-collapse: collapse; text-align: left;">
                <thead>
                    <tr style="border-bottom: 2px solid #e2e8f0; color: var(--text-sub); font-size: 0.85rem;">
                        <th style="padding: 10px;">Date</th>
                        <th style="padding: 10px;">Admin</th>
                        <th style="padding: 10px;">Action</th>
                        <th style="padding: 10px;">Target</th>
                        <th style="padding: 10px;">Details</th>
                    </tr>
                </thead>
                <tbody>
                    {% for log in logs %}
                    <tr style="border-bottom: 1px solid #f1f5f9;">
                        <td style="padding: 10px; color: #334155; white-space: nowrap;">{{ log.created_at.strftime('%b %d, %Y %I:%M %p') }}</td>
                        <td style="padding: 10px; color: var(--text-dark); font-weight: 600;">{{ log.admin.full_name if log.admin else 'Unknown' }}</td>
                        <td style="padding: 10px; color: #334155;">{{ log.action }}</td>
                        <td style="padding: 10px; color: #334155;">{{ log.target_name or '—' }}</td>
                        <td style="padding: 10px; color: var(--text-sub);">{{ log.details or '' }}</td>
                    </tr>
                    {% endfor %}
                </tbody>
            </table>
        </div>
        {% else %}
        <p style="color: var(--text-sub); margin: 0;">No admin actions logged yet.</p>
        {% endif %}
    </div>
</div>
{% endblock %}
FILEEOF_7
cat > templates/error.html << 'FILEEOF_8'
{% extends "base.html" %}
{% block title %}{{ code }} - MediBro{% endblock %}
{% block content %}
<div style="max-width: 480px; margin: 80px auto; padding: 0 20px; text-align: center;">
    <p style="font-size: 3rem; font-weight: 800; color: var(--primary); margin: 0;">{{ code }}</p>
    <p style="font-size: 1.1rem; color: var(--text-dark); margin: 8px 0 24px 0;">{{ message }}</p>
    <a href="{{ url_for('index') }}" style="padding: 10px 24px; background-color: var(--primary); color: #ffffff; border-radius: 6px; text-decoration: none; font-weight: 600; font-size: 0.95rem;">Go to Homepage</a>
</div>
{% endblock %}
FILEEOF_8
cat > templates/profile.html << 'FILEEOF_9'
{% extends "base.html" %}
{% block title %}Profile - MediBro{% endblock %}
{% block content %}
<div style="max-width: 480px; margin: 50px auto; padding: 0 20px;">
    <div style="margin-bottom: 24px; display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 12px;">
        <div>
            <h1 style="margin: 0; font-size: 1.6rem; color: var(--text-dark);">Profile & Security</h1>
            <p style="margin: 4px 0 0 0; color: var(--text-sub);">{{ session.full_name }} ({{ session.email }})</p>
        </div>
        <a href="{{ url_for(back_endpoint) }}" style="color: var(--primary); text-decoration: none; font-weight: 600; font-size: 0.9rem;">&larr; Back to Portal</a>
    </div>

    <div style="background-color: var(--surface); border: 1px solid var(--border); border-radius: var(--radius); padding: 24px; box-shadow: 0 1px 3px rgba(0,0,0,0.05);">
        <h2 style="font-size: 1.15rem; margin-top: 0; margin-bottom: 20px; color: var(--text-dark);">🔒 Change Password</h2>
        <form method="POST" style="display: grid; gap: 14px;">
            <div>
                <label style="display:block; font-weight:600; font-size:0.875rem; margin-bottom:4px;">Current Password</label>
                <input type="password" name="current_password" required style="width:100%; padding:10px; border-radius:6px; border:1px solid var(--border);">
            </div>
            <div>
                <label style="display:block; font-weight:600; font-size:0.875rem; margin-bottom:4px;">New Password</label>
                <input type="password" name="new_password" required minlength="6" style="width:100%; padding:10px; border-radius:6px; border:1px solid var(--border);">
            </div>
            <div>
                <label style="display:block; font-weight:600; font-size:0.875rem; margin-bottom:4px;">Confirm New Password</label>
                <input type="password" name="confirm_password" required minlength="6" style="width:100%; padding:10px; border-radius:6px; border:1px solid var(--border);">
            </div>
            <button type="submit" style="padding:10px; border:none; border-radius:6px; background:var(--primary); color:white; font-weight:600; cursor:pointer;">Update Password</button>
        </form>
    </div>
</div>
{% endblock %}
FILEEOF_9
cat > templates/admin.html << 'FILEEOF_10'
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
                        <a href="{{ url_for('verify_doctor', doctor_id=doc.id, action='approve') }}" style="padding: 6px 12px; background: var(--success); color: white; border-radius: 6px; text-decoration: none; font-size: 0.85rem; font-weight: 600;">Approve</a>
                        <a href="{{ url_for('verify_doctor', doctor_id=doc.id, action='reject') }}" style="padding: 6px 12px; background: var(--danger); color: white; border-radius: 6px; text-decoration: none; font-size: 0.85rem; font-weight: 600;">Reject</a>
                    </td>
                </tr>
                {% endfor %}
            </tbody>
        </table>
        {% else %}
        <p style="color: var(--text-sub); margin: 0;">No pending doctor approvals.</p>
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
                        <a href="{{ url_for('toggle_user_status', user_id=p.id) }}" style="padding: 6px 12px; background: {{ 'var(--danger-light)' if p.status == 'approved' else 'var(--success-light)' }}; color: {{ 'var(--danger)' if p.status == 'approved' else 'var(--success)' }}; border-radius: 6px; text-decoration: none; font-size: 0.85rem; font-weight: 600;">
                            {{ 'Suspend' if p.status == 'approved' else 'Activate' }}
                        </a>
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
FILEEOF_10
cat > templates/index.html << 'FILEEOF_11'
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>MediBro | Intelligent Healthcare Platform</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <link href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700;800&display=swap" rel="stylesheet">
    <style>
        body { font-family: 'Plus Jakarta Sans', sans-serif; }
    </style>
</head>
<body class="bg-[#f8fafc] text-slate-800">

    <header class="bg-white border-b border-slate-200/80 sticky top-0 z-50">
        <div class="max-w-7xl mx-auto px-6 h-20 flex items-center justify-between">
            <div class="flex items-center space-x-3">
                <div class="w-10 h-10 rounded-xl bg-[#2d76c9] text-white flex items-center justify-center font-extrabold text-xl shadow-md">
                    M
                </div>
                <span class="text-2xl font-bold text-[#1e56a0] tracking-tight">MediBro</span>
            </div>

            <nav class="hidden md:flex items-center space-x-8 text-sm font-semibold text-slate-600">
                <a href="#home" class="hover:text-[#1e56a0] transition">Home</a>
                <a href="#features" class="hover:text-[#1e56a0] transition">Features</a>
                <a href="#how-it-works" class="hover:text-[#1e56a0] transition">How It Works</a>
                <a href="/login" class="hover:text-[#1e56a0] transition">Demo Dashboard</a>
            </nav>

            <div class="flex items-center space-x-3">
                <a href="/login" class="px-5 py-2.5 rounded-xl border border-slate-300 text-slate-700 font-bold text-sm hover:bg-slate-50 transition">
                    Log In
                </a>
                <a href="/register" class="px-5 py-2.5 rounded-xl bg-[#2d76c9] hover:bg-[#2363ad] text-white font-bold text-sm shadow-md transition">
                    Sign Up
                </a>
            </div>
        </div>
    </header>

    <section id="home" class="max-w-7xl mx-auto px-6 py-16 md:py-24 grid grid-cols-1 lg:grid-cols-12 gap-12 items-center">
        <div class="lg:col-span-7 space-y-6">
            <span class="inline-block bg-[#e3f2fd] text-[#1e56a0] text-xs font-bold px-4 py-1.5 rounded-full border border-[#bbdefb]">
                ✨ Next-Generation Smart Healthcare
            </span>
            <h1 class="text-4xl md:text-5xl lg:text-6xl font-black text-slate-900 leading-tight tracking-tight">
                Connecting Patients, Doctors & Clinics Through <span class="text-[#2d76c9]">Intelligent AI</span>
            </h1>
            <p class="text-slate-600 text-base md:text-lg leading-relaxed max-w-2xl font-medium">
                Streamline consultations, triage symptoms instantly, track medical history, and access verified healthcare providers — all in one secure platform.
            </p>
            <div class="flex flex-wrap gap-4 pt-2">
                <a href="/register" class="px-7 py-3.5 bg-[#2d76c9] hover:bg-[#2363ad] text-white font-bold rounded-2xl shadow-lg transition text-sm">
                    Find a Doctor Now
                </a>
                <a href="#how-it-works" class="px-7 py-3.5 bg-white border border-slate-300 text-slate-700 font-bold rounded-2xl hover:bg-slate-50 transition text-sm">
                    See How It Works
                </a>
            </div>
        </div>

        <div class="lg:col-span-5">
            <div class="bg-white p-6 rounded-3xl border border-slate-200/80 shadow-xl space-y-4">
                <div class="bg-[#e8f5e9] border border-[#a5d6a7] p-5 rounded-2xl space-y-1">
                    <h3 class="text-xl font-extrabold text-[#2e7d32]">Track Your Health</h3>
                    <p class="text-xs text-slate-600 font-medium">Log vitals and symptoms, and get matched with verified doctors.</p>
                </div>
                <div class="p-4 bg-slate-50 rounded-2xl border border-slate-100 text-xs text-slate-600 space-y-2">
                    <p><strong class="text-slate-800">Get started:</strong> Register as a patient or doctor and start using MediBro today.</p>
                    <a href="/login" class="inline-block text-[#2d76c9] font-bold hover:underline">View Patient Dashboard →</a>
                </div>
            </div>
        </div>
    </section>

    <section id="features" class="max-w-7xl mx-auto px-6 py-16 space-y-10">
        <div class="text-center max-w-2xl mx-auto space-y-3">
            <h2 class="text-3xl font-extrabold text-slate-900">Comprehensive AI Healthcare Suite</h2>
            <p class="text-slate-500 text-sm font-medium">
                Everything you need for preventative care, rapid diagnosis, and seamless medical team coordination.
            </p>
        </div>

        <div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-6">
            <div class="bg-white p-6 rounded-2xl border border-slate-200/80 shadow-sm space-y-3 hover:shadow-md transition relative">
                <span class="absolute top-4 right-4 text-[10px] font-bold text-slate-400 bg-slate-100 px-2 py-0.5 rounded-full">Coming Soon</span>
                <div class="w-12 h-12 rounded-xl bg-blue-50 text-2xl flex items-center justify-center">💬</div>
                <h3 class="font-bold text-slate-900 text-base">AI Health Chat</h3>
                <p class="text-slate-500 text-xs leading-relaxed">24/7 interactive virtual health assistant for instant answers to medical questions.</p>
            </div>

            <div class="bg-white p-6 rounded-2xl border border-slate-200/80 shadow-sm space-y-3 hover:shadow-md transition">
                <div class="w-12 h-12 rounded-xl bg-emerald-50 text-2xl flex items-center justify-center">🩺</div>
                <h3 class="font-bold text-slate-900 text-base">Symptom Checker</h3>
                <p class="text-slate-500 text-xs leading-relaxed">Log your symptoms and severity to get basic guidance on next steps.</p>
            </div>

            <div class="bg-white p-6 rounded-2xl border border-[#bbdefb] shadow-sm space-y-3 hover:shadow-md transition">
                <div class="w-12 h-12 rounded-xl bg-amber-50 text-2xl flex items-center justify-center">👨‍⚕️</div>
                <h3 class="font-bold text-slate-900 text-base">Find Verified Doctors</h3>
                <p class="text-slate-500 text-xs leading-relaxed">Search hospital-verified specialists by specialty and request appointments.</p>
            </div>

            <div class="bg-white p-6 rounded-2xl border border-slate-200/80 shadow-sm space-y-3 hover:shadow-md transition">
                <div class="w-12 h-12 rounded-xl bg-indigo-50 text-2xl flex items-center justify-center">📅</div>
                <h3 class="font-bold text-slate-900 text-base">Appointment Booking</h3>
                <p class="text-slate-500 text-xs leading-relaxed">Request appointments with your doctor and track their status in real time.</p>
            </div>

            <div class="bg-white p-6 rounded-2xl border border-slate-200/80 shadow-sm space-y-3 hover:shadow-md transition relative">
                <span class="absolute top-4 right-4 text-[10px] font-bold text-slate-400 bg-slate-100 px-2 py-0.5 rounded-full">Coming Soon</span>
                <div class="w-12 h-12 rounded-xl bg-teal-50 text-2xl flex items-center justify-center">📹</div>
                <h3 class="font-bold text-slate-900 text-base">Video Consultation</h3>
                <p class="text-slate-500 text-xs leading-relaxed">HD encrypted telehealth visits directly from your smartphone or desktop browser.</p>
            </div>

            <div class="bg-white p-6 rounded-2xl border border-slate-200/80 shadow-sm space-y-3 hover:shadow-md transition relative">
                <span class="absolute top-4 right-4 text-[10px] font-bold text-slate-400 bg-slate-100 px-2 py-0.5 rounded-full">Coming Soon</span>
                <div class="w-12 h-12 rounded-xl bg-rose-50 text-2xl flex items-center justify-center">💊</div>
                <h3 class="font-bold text-slate-900 text-base">Medicine Reminders</h3>
                <p class="text-slate-500 text-xs leading-relaxed">Smart prescription alerts and dosage tracking to ensure regimen adherence.</p>
            </div>

            <div class="bg-white p-6 rounded-2xl border border-slate-200/80 shadow-sm space-y-3 hover:shadow-md transition">
                <div class="w-12 h-12 rounded-xl bg-purple-50 text-2xl flex items-center justify-center">📊</div>
                <h3 class="font-bold text-slate-900 text-base">Vitals Tracking</h3>
                <p class="text-slate-500 text-xs leading-relaxed">Log blood pressure, heart rate, SpO2, and temperature, and track trends over time.</p>
            </div>

            <div class="bg-white p-6 rounded-2xl border border-slate-200/80 shadow-sm space-y-3 hover:shadow-md transition">
                <div class="w-12 h-12 rounded-xl bg-red-50 text-2xl flex items-center justify-center">🚨</div>
                <h3 class="font-bold text-slate-900 text-base">Emergency SOS</h3>
                <p class="text-slate-500 text-xs leading-relaxed">Save an emergency contact and trigger a one-tap SOS alert when you need help fast.</p>
            </div>
        </div>
    </section>

    <section id="how-it-works" class="max-w-7xl mx-auto px-6 py-16 space-y-12">
        <div class="text-center max-w-2xl mx-auto space-y-3">
            <h2 class="text-3xl font-extrabold text-slate-900">How MediBro Works</h2>
            <p class="text-slate-500 text-sm font-medium">Four effortless steps to personalized healthcare management.</p>
        </div>

        <div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-8">
            <div class="text-center space-y-3">
                <div class="w-12 h-12 bg-[#2d76c9] text-white font-bold rounded-full flex items-center justify-center mx-auto text-lg shadow-md">1</div>
                <h3 class="font-bold text-slate-900 text-base">Describe Symptoms</h3>
                <p class="text-slate-500 text-xs leading-relaxed">Chat with our AI or input your current symptoms to get preliminary medical insights.</p>
            </div>

            <div class="text-center space-y-3">
                <div class="w-12 h-12 bg-[#2d76c9] text-white font-bold rounded-full flex items-center justify-center mx-auto text-lg shadow-md">2</div>
                <h3 class="font-bold text-slate-900 text-base">Match Specialist</h3>
                <p class="text-slate-500 text-xs leading-relaxed">Get matched with highly rated, verified doctors or clinics tailored to your needs.</p>
            </div>

            <div class="text-center space-y-3">
                <div class="w-12 h-12 bg-[#2d76c9] text-white font-bold rounded-full flex items-center justify-center mx-auto text-lg shadow-md">3</div>
                <h3 class="font-bold text-slate-900 text-base">Consult & Care</h3>
                <p class="text-slate-500 text-xs leading-relaxed">Attend a telehealth video call or visit in-person with automatic digital prescriptions.</p>
            </div>

            <div class="text-center space-y-3">
                <div class="w-12 h-12 bg-[#2d76c9] text-white font-bold rounded-full flex items-center justify-center mx-auto text-lg shadow-md">4</div>
                <h3 class="font-bold text-slate-900 text-base">Track & Recover</h3>
                <p class="text-slate-500 text-xs leading-relaxed">Receive intelligent medicine reminders and monitor continuous vital progress.</p>
            </div>
        </div>
    </section>

    <footer class="bg-[#0f172a] text-slate-400 py-16 border-t border-slate-800">
        <div class="max-w-7xl mx-auto px-6 grid grid-cols-1 md:grid-cols-4 gap-10 text-xs">
            <div class="space-y-3">
                <div class="flex items-center space-x-2 text-white font-bold text-lg">
                    <div class="w-7 h-7 rounded-lg bg-[#2d76c9] flex items-center justify-center text-sm">M</div>
                    <span>MediBro</span>
                </div>
                <p class="leading-relaxed">Empowering modern healthcare through artificial intelligence and seamless clinical connections.</p>
            </div>

            <div class="space-y-2">
                <h4 class="text-white font-bold uppercase tracking-wider text-[11px]">Platform</h4>
                <p><a href="/login" class="hover:text-white transition">AI Symptom Checker</a></p>
                <p><a href="/login" class="hover:text-white transition">Find Doctors</a></p>
                <p><a href="/login" class="hover:text-white transition">Telehealth Video</a></p>
                <p><a href="/login" class="hover:text-white transition">Hospitals & Clinics</a></p>
            </div>

            <div class="space-y-2">
                <h4 class="text-white font-bold uppercase tracking-wider text-[11px]">Company</h4>
                <p><a href="#" class="hover:text-white transition">About Us</a></p>
                <p><a href="#" class="hover:text-white transition">Medical Board</a></p>
                <p><a href="#" class="hover:text-white transition">Careers</a></p>
                <p><a href="#" class="hover:text-white transition">Privacy & HIPAA</a></p>
            </div>

            <div class="space-y-2">
                <h4 class="text-white font-bold uppercase tracking-wider text-[11px]">Contact Support</h4>
                <p>Emergency: 911 / Local Hotline</p>
                <p>Support: care@medibro.com</p>
                <p>Phone: +1 (800) 555-0199</p>
            </div>
        </div>
    </footer>

</body>
</html>
FILEEOF_11

echo "All files written."
echo ""
echo "=== git status ==="
git status

echo ""
read -p "Press Enter to commit and push now (or Ctrl+C to stop and test first): "

git add app.py templates/base.html templates/patient_dashboard.html templates/doctor_dashboard.html templates/doctor_profile.html templates/patient_history_view.html templates/audit_log.html templates/error.html templates/profile.html templates/admin.html templates/index.html
git commit -m "Batch 3: doctor profile/history, cancel appointments, specialty filter, reminders, audit log, rate limiting, validation, mobile+flash polish, accurate landing page"
git push origin main

echo ""
echo "=== Done. Check Render dashboard for the new deploy. ==="
echo "This batch adds new columns to the existing 'user' table (bio, hours)."
echo "app.py now runs an automatic migration on startup to add them safely -"
echo "no manual database fix should be needed this time."
