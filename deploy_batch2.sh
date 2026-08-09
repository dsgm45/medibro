#!/bin/bash
set -e

echo "=== MediBro: Deploying Batch 2 (Symptom Checker, SOS, UI fix, password fix, cleanup) ==="

if [ ! -f "app.py" ]; then
  echo "ERROR: app.py not found. cd into your medimind project folder first, then re-run this script."
  exit 1
fi

mkdir -p templates

cat > app.py << 'APP_PY_EOF'
import os
import secrets
from datetime import datetime
from functools import wraps
from flask import Flask, render_template, request, redirect, url_for, flash, session
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import text
from werkzeug.security import generate_password_hash, check_password_hash

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

# --- BASE / PUBLIC ROUTES ---
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')
        try:
            user = User.query.filter_by(email=email).first()
            if user and check_password_hash(user.password_hash, password):
                if user.status == 'pending':
                    flash('Your doctor account is pending verification by hospital admin.', 'error')
                    return redirect(url_for('login'))
                if user.status in ['rejected', 'suspended']:
                    flash('Your account is suspended or rejected.', 'error')
                    return redirect(url_for('login'))
                
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
                flash('Invalid email or password.', 'error')
        except Exception as e:
            app.logger.error(f"Login error: {e}")
            flash(f'Database error: {str(e)}', 'error')
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
            flash(f'Registration error: {str(e)}', 'error')

    return render_template('register.html')

# --- DASHBOARD ROUTES ---
@app.route('/patient')
@login_required
@role_required('patient')
def patient_dashboard():
    patient_id = session.get('user_id')
    doctors = User.query.filter_by(role='doctor', status='approved').all()
    my_appointments = Appointment.query.filter_by(patient_id=patient_id).order_by(Appointment.created_at.desc()).all()
    return render_template('patient_dashboard.html', doctors=doctors, appointments=my_appointments)

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
        flash(f'Error booking appointment: {str(e)}', 'error')

    return redirect(url_for('patient_dashboard'))

@app.route('/doctor')
@login_required
@role_required('doctor')
def doctor_dashboard():
    doctor_id = session.get('user_id')
    doctor = User.query.get_or_404(doctor_id)
    appointments = Appointment.query.filter_by(doctor_id=doctor_id).order_by(Appointment.created_at.desc()).all()
    return render_template('doctor_dashboard.html', doctor=doctor, appointments=appointments)

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
        db.session.commit()
    except Exception as e:
        db.session.rollback()
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
            db.session.commit()
    except Exception as e:
        db.session.rollback()
        flash('Error toggling user status.', 'error')
    return redirect(url_for('admin_dashboard'))

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

    return render_template('profile.html')

@app.route('/logout')
def logout():
    session.clear()
    flash('Logged out successfully.', 'success')
    return redirect(url_for('login'))

if __name__ == '__main__':
    app.run(debug=True)
APP_PY_EOF
cat > templates/base.html << 'BASE_HTML_EOF'
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
            padding: 12px 16px;
            border-radius: 6px;
            margin-bottom: 12px;
            font-weight: 600;
            font-size: 0.95rem;
        }
        .flash.success { background-color: #dcfce7; color: #15803d; border: 1px solid #bbf7d0; }
        .flash.error { background-color: #fee2e2; color: #b91c1c; border: 1px solid #fecaca; }
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
BASE_HTML_EOF
cat > templates/patient_dashboard.html << 'PATIENT_DASH_EOF'
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

    <!-- Section 1: Book Appointment Card -->
    <div style="background-color: #ffffff; border: 1px solid #cbd5e1; border-radius: 8px; padding: 24px; margin-bottom: 32px; box-shadow: 0 1px 3px rgba(0,0,0,0.05);">
        <h2 style="font-size: 1.25rem; margin-top: 0; margin-bottom: 20px; color: #0f172a;">📅 Book an Appointment</h2>
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
        <p style="color: #64748b; margin: 0;">No verified doctors are currently available for booking.</p>
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
                            {% else %}
                            <span style="background-color: #fef3c7; color: #b45309; padding: 4px 12px; border-radius: 12px; font-weight: 600; font-size: 0.85rem; display: inline-block;">Pending</span>
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
PATIENT_DASH_EOF
cat > templates/symptoms.html << 'SYMPTOMS_HTML_EOF'
{% extends "base.html" %}
{% block title %}Symptom Checker - MediBro{% endblock %}
{% block content %}
<div style="max-width: 1100px; margin: 30px auto; padding: 0 20px;">
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
</div>
{% endblock %}
SYMPTOMS_HTML_EOF
cat > templates/sos.html << 'SOS_HTML_EOF'
{% extends "base.html" %}
{% block title %}SOS - MediBro{% endblock %}
{% block content %}
<div style="max-width: 700px; margin: 30px auto; padding: 0 20px;">
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
</div>
{% endblock %}
SOS_HTML_EOF
cat > templates/profile.html << 'PROFILE_HTML_EOF'
{% extends "base.html" %}
{% block title %}Profile - MediBro{% endblock %}
{% block content %}
<div style="max-width: 480px; margin: 50px auto; padding: 0 20px;">
    <div style="margin-bottom: 24px;">
        <h1 style="margin: 0; font-size: 1.6rem; color: var(--text-dark);">Profile & Security</h1>
        <p style="margin: 4px 0 0 0; color: var(--text-sub);">{{ session.full_name }} ({{ session.email }})</p>
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
PROFILE_HTML_EOF

echo "All files written."
echo ""
echo "=== git status ==="
git status

echo ""
read -p "Press Enter to commit and push now (or Ctrl+C to stop and test first): "

git add app.py templates/base.html templates/patient_dashboard.html templates/symptoms.html templates/sos.html templates/profile.html
git commit -m "Add Symptom Checker, SOS, fix login button visibility, admin password fix, cleanup"
git push origin main

echo ""
echo "=== Done. Check Render dashboard for the new deploy. ==="
echo "Once live, log in as admin@medibro.com and go to /profile to change the admin password."
