import os
import secrets
from datetime import datetime
from functools import wraps
from flask import Flask, render_template, request, redirect, url_for, flash, session
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'medibro_secret_key_2026')

db_url = os.environ.get('DATABASE_URL', 'sqlite:///medibro.db')
if db_url.startswith('postgres://'):
    db_url = db_url.replace('postgres://', 'postgresql://', 1)

app.config['SQLALCHEMY_DATABASE_URI'] = db_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)

# --- DATABASE MODELS ---

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
    time_slot = db.Column(db.String(50), nullable=False)
    reason = db.Column(db.Text, nullable=True)
    status = db.Column(db.String(20), default='pending') # pending, confirmed, completed, canceled
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    patient = db.relationship('User', foreign_keys=[patient_id])
    doctor = db.relationship('User', foreign_keys=[doctor_id])

class Prescription(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    appointment_id = db.Column(db.Integer, db.ForeignKey('appointment.id'), nullable=False)
    patient_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    doctor_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    medicines = db.Column(db.Text, nullable=False)
    instructions = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    doctor = db.relationship('User', foreign_keys=[doctor_id])
    patient = db.relationship('User', foreign_keys=[patient_id])

class Vitals(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    patient_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    sys_bp = db.Column(db.Integer, nullable=False)
    dia_bp = db.Column(db.Integer, nullable=False)
    heart_rate = db.Column(db.Integer, nullable=False)
    spo2 = db.Column(db.Integer, nullable=False)
    temp = db.Column(db.Float, nullable=False)
    is_abnormal = db.Column(db.Boolean, default=False)
    warnings = db.Column(db.String(250), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

class EmergencyAlert(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    patient_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    patient_name = db.Column(db.String(120), nullable=False)
    phone = db.Column(db.String(20), nullable=True)
    status = db.Column(db.String(20), default='active') # active, resolved
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

class Message(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    appointment_id = db.Column(db.Integer, db.ForeignKey('appointment.id'), nullable=False)
    sender_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    sender_name = db.Column(db.String(120), nullable=False)
    content = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

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
            print("All database tables verified/created on Render.")
        except Exception as e:
            db.session.rollback()
            print(f"Database init error: {e}")

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

# --- ROUTES ---

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
                    flash('Your account is suspended or rejected. Please contact support.', 'error')
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

@app.route('/patient')
@login_required
@role_required('patient')
def patient_dashboard():
    doctors = User.query.filter_by(role='doctor', status='approved').all()
    appointments = Appointment.query.filter_by(patient_id=session['user_id']).order_by(Appointment.created_at.desc()).all()
    vitals_history = Vitals.query.filter_by(patient_id=session['user_id']).order_by(Vitals.created_at.desc()).all()
    prescriptions = Prescription.query.filter_by(patient_id=session['user_id']).order_by(Prescription.created_at.desc()).all()
    return render_template('patient_dashboard.html', doctors=doctors, appointments=appointments, vitals_history=vitals_history, prescriptions=prescriptions)

@app.route('/book-appointment', methods=['POST'])
@login_required
@role_required('patient')
def book_appointment():
    doctor_id = request.form.get('doctor_id')
    appointment_date = request.form.get('appointment_date')
    time_slot = request.form.get('time_slot')
    reason = request.form.get('reason')

    appt = Appointment(
        patient_id=session['user_id'],
        doctor_id=doctor_id,
        appointment_date=appointment_date,
        time_slot=time_slot,
        reason=reason,
        status='pending'
    )
    db.session.add(appt)
    db.session.commit()
    flash('Appointment requested successfully! Awaiting doctor confirmation.', 'success')
    return redirect(url_for('patient_dashboard'))

@app.route('/log-vitals', methods=['POST'])
@login_required
@role_required('patient')
def log_vitals():
    try:
        sys_bp = int(request.form.get('sys_bp', 120))
        dia_bp = int(request.form.get('dia_bp', 80))
        heart_rate = int(request.form.get('heart_rate', 72))
        spo2 = int(request.form.get('spo2', 98))
        temp = float(request.form.get('temp', 98.6))

        warn_list = []
        if sys_bp > 140 or dia_bp > 90: warn_list.append("High Blood Pressure")
        elif sys_bp < 90 or dia_bp < 60: warn_list.append("Low Blood Pressure")
        if heart_rate > 100 or heart_rate < 60: warn_list.append("Abnormal Heart Rate")
        if spo2 < 95: warn_list.append("Low Oxygen Level (SpO2)")
        if temp > 99.5: warn_list.append("Fever / Elevated Temp")

        is_abnormal = len(warn_list) > 0
        warnings = ", ".join(warn_list) if is_abnormal else "Normal Range"

        vital = Vitals(
            patient_id=session['user_id'],
            sys_bp=sys_bp,
            dia_bp=dia_bp,
            heart_rate=heart_rate,
            spo2=spo2,
            temp=temp,
            is_abnormal=is_abnormal,
            warnings=warnings
        )
        db.session.add(vital)
        db.session.commit()
        
        if is_abnormal:
            flash(f'Vitals Logged! Alert: {warnings}', 'error')
        else:
            flash('Vitals Logged Successfully! All metrics within normal range.', 'success')
    except Exception as e:
        flash(f'Error logging vitals: {str(e)}', 'error')

    return redirect(url_for('patient_dashboard'))

@app.route('/symptom-checker', methods=['POST'])
@login_required
@role_required('patient')
def symptom_checker():
    symptoms = request.form.get('symptoms', '').lower()
    
    urgency = "Low / Standard"
    specialty = "General Physician"
    advice = "Monitor your symptoms and maintain hydration. Consider scheduling a routine consultation."

    if any(k in symptoms for k in ['chest pain', 'breath', 'shortness', 'faint', 'severe']):
        urgency = "🚨 HIGH / URGENT"
        specialty = "Cardiology / Emergency Care"
        advice = "Your symptoms require urgent clinical evaluation. Please seek emergency medical care or trigger Emergency SOS."
    elif any(k in symptoms for k in ['fever', 'cough', 'cold', 'sore throat', 'flu']):
        urgency = "Moderate"
        specialty = "Pulmonology / General Medicine"
        advice = "Common respiratory or viral symptoms. Rest and consult a general physician if symptoms persist beyond 3 days."
    elif any(k in symptoms for k in ['stomach', 'vomit', 'nausea', 'diarrhea', 'acid']):
        urgency = "Moderate"
        specialty = "Gastroenterology"
        advice = "Digestive system discomfort. Drink electrolyte fluids and consult a gastroenterologist if pain worsens."
    elif any(k in symptoms for k in ['skin', 'rash', 'itch']):
        urgency = "Low"
        specialty = "Dermatology"
        advice = "Dermatological symptom. Avoid irritation and schedule a routine dermatology checkup."

    session['symptom_result'] = {
        'urgency': urgency,
        'specialty': specialty,
        'advice': advice,
        'query': symptoms
    }
    return redirect(url_for('patient_dashboard'))

@app.route('/trigger-sos', methods=['POST'])
@login_required
@role_required('patient')
def trigger_sos():
    user = User.query.get(session['user_id'])
    sos = EmergencyAlert(
        patient_id=user.id,
        patient_name=user.full_name,
        phone=user.phone or "Not provided",
        status='active'
    )
    db.session.add(sos)
    db.session.commit()
    flash('🚨 EMERGENCY SOS ACTIVATED! Hospital Admin team has been notified immediately.', 'error')
    return redirect(url_for('patient_dashboard'))

@app.route('/doctor')
@login_required
@role_required('doctor')
def doctor_dashboard():
    doctor = User.query.get(session['user_id'])
    appointments = Appointment.query.filter_by(doctor_id=doctor.id).order_by(Appointment.created_at.desc()).all()
    return render_template('doctor_dashboard.html', doctor=doctor, appointments=appointments)

@app.route('/appointment/status/<int:appt_id>/<status>')
@login_required
@role_required('doctor')
def update_appointment_status(appt_id, status):
    appt = Appointment.query.get_or_404(appt_id)
    if appt.doctor_id == session['user_id']:
        appt.status = status
        db.session.commit()
        flash(f'Appointment status updated to {status}.', 'success')
    return redirect(url_for('doctor_dashboard'))

@app.route('/appointment/<int:appt_id>/prescription', methods=['POST'])
@login_required
@role_required('doctor')
def add_prescription(appt_id):
    appt = Appointment.query.get_or_404(appt_id)
    medicines = request.form.get('medicines')
    instructions = request.form.get('instructions')

    presc = Prescription(
        appointment_id=appt.id,
        patient_id=appt.patient_id,
        doctor_id=session['user_id'],
        medicines=medicines,
        instructions=instructions
    )
    appt.status = 'completed'
    db.session.add(presc)
    db.session.commit()
    flash('Digital Prescription sent to patient successfully!', 'success')
    return redirect(url_for('doctor_dashboard'))

@app.route('/chat/<int:appt_id>', methods=['GET', 'POST'])
@login_required
def chat(appt_id):
    appt = Appointment.query.get_or_404(appt_id)
    if session['user_id'] not in [appt.patient_id, appt.doctor_id]:
        flash('Access denied to this consultation thread.', 'error')
        return redirect(url_for('index'))

    if request.method == 'POST':
        content = request.form.get('content', '').strip()
        if content:
            msg = Message(
                appointment_id=appt.id,
                sender_id=session['user_id'],
                sender_name=session['full_name'],
                content=content
            )
            db.session.add(msg)
            db.session.commit()
            return redirect(url_for('chat', appt_id=appt.id))

    messages = Message.query.filter_by(appointment_id=appt.id).order_by(Message.created_at.asc()).all()
    return render_template('chat.html', appointment=appt, messages=messages)

@app.route('/admin')
@login_required
@role_required('hospital', 'admin')
def admin_dashboard():
    pending_doctors = User.query.filter_by(role='doctor', status='pending').all()
    approved_doctors = User.query.filter_by(role='doctor', status='approved').all()
    patients = User.query.filter_by(role='patient').all()
    emergency_alerts = EmergencyAlert.query.filter_by(status='active').order_by(EmergencyAlert.created_at.desc()).all()

    stats = {
        'total_patients': len(patients),
        'active_doctors': len(approved_doctors),
        'pending_approvals': len(pending_doctors),
        'active_sos': len(emergency_alerts)
    }

    return render_template(
        'admin.html',
        pending_doctors=pending_doctors,
        approved_doctors=approved_doctors,
        patients=patients,
        emergency_alerts=emergency_alerts,
        stats=stats
    )

@app.route('/admin/resolve-sos/<int:sos_id>')
@login_required
@role_required('hospital', 'admin')
def resolve_sos(sos_id):
    sos = EmergencyAlert.query.get_or_404(sos_id)
    sos.status = 'resolved'
    db.session.commit()
    flash('Emergency SOS alert resolved.', 'success')
    return redirect(url_for('admin_dashboard'))

@app.route('/admin/verify/<int:doctor_id>/<action>')
@login_required
@role_required('hospital', 'admin')
def verify_doctor(doctor_id, action):
    doctor = User.query.get_or_404(doctor_id)
    if action == 'approve':
        doctor.status = 'approved'
        flash(f'Doctor {doctor.full_name} approved successfully!', 'success')
    elif action == 'reject':
        doctor.status = 'rejected'
        flash(f'Doctor {doctor.full_name} rejected.', 'error')
    db.session.commit()
    return redirect(url_for('admin_dashboard'))

@app.route('/admin/toggle-user/<int:user_id>')
@login_required
@role_required('hospital', 'admin')
def toggle_user_status(user_id):
    user = User.query.get_or_404(user_id)
    if user.role in ['hospital', 'admin']:
        flash('Cannot suspend system admin account.', 'error')
        return redirect(url_for('admin_dashboard'))

    if user.status == 'approved':
        user.status = 'suspended'
        flash(f'Account for {user.full_name} has been suspended.', 'error')
    else:
        user.status = 'approved'
        flash(f'Account for {user.full_name} is now active.', 'success')
    
    db.session.commit()
    return redirect(url_for('admin_dashboard'))

@app.route('/logout')
def logout():
    session.clear()
    flash('Logged out successfully.', 'success')
    return redirect(url_for('login'))

if __name__ == '__main__':
    app.run(debug=True)
