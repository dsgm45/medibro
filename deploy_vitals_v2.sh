#!/bin/bash
set -e

echo "=== MediBro: Deploying Vitals feature ==="

# Make sure we're in the right folder
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

def init_db():
    with app.app_context():
        try:
            db.create_all()
            admin = User.query.filter_by(email='admin@medibro.com').first()
            if not admin:
                admin = User(
                    email='admin@medibro.com',
                    password_hash=generate_password_hash('admin123'),
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

@app.route('/symptoms', methods=['GET', 'POST'])
@login_required
def symptoms():
    return redirect(url_for('patient_dashboard'))

@app.route('/sos', methods=['GET', 'POST'])
@login_required
def sos():
    return redirect(url_for('patient_dashboard'))

@app.route('/profile')
@login_required
def profile():
    return redirect(url_for('patient_dashboard'))

@app.route('/logout')
def logout():
    session.clear()
    flash('Logged out successfully.', 'success')
    return redirect(url_for('login'))

if __name__ == '__main__':
    app.run(debug=True)
APP_PY_EOF
cat > templates/vitals.html << 'VITALS_HTML_EOF'
{% extends "base.html" %}
{% block title %}Vitals - MediBro{% endblock %}
{% block content %}
<div style="max-width: 1100px; margin: 30px auto; padding: 0 20px;">
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
</div>
{% endblock %}
VITALS_HTML_EOF
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
        <a href="{{ url_for('vitals') }}" style="padding: 10px 20px; background-color: #2563eb; color: #ffffff; border-radius: 6px; text-decoration: none; font-weight: 600; font-size: 0.9rem;">💓 Log Vitals</a>
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

echo "Files written."
echo ""
echo "=== git status ==="
git status

echo ""
read -p "Test the app locally now before committing? Run 'python3 app.py' in another terminal, then press Enter here to continue with git commit + push (or Ctrl+C to stop and test first): "

git add app.py templates/vitals.html templates/patient_dashboard.html
git commit -m "Add vitals tracking feature"
git push origin main

echo ""
echo "=== Done. Check Render dashboard for the new deploy. ==="
