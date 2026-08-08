import os
from datetime import datetime
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, session
from flask_sqlalchemy import SQLAlchemy
from flask_bcrypt import Bcrypt

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'medibro-super-secret-key-2026')
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL', 'sqlite:///medibro.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)
bcrypt = Bcrypt(app)

# ---------------------------------------------------------------------------
# Database Models
# ---------------------------------------------------------------------------
class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    full_name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password = db.Column(db.String(200), nullable=False)
    role = db.Column(db.String(20), default='patient')  # 'patient', 'doctor', 'admin'
    specialty = db.Column(db.String(100), nullable=True)
    status = db.Column(db.String(20), default='Approved')  # 'Approved', 'Pending Verification', 'Rejected'
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

class Vital(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    patient_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    systolic = db.Column(db.Integer, nullable=False)
    diastolic = db.Column(db.Integer, nullable=False)
    heart_rate = db.Column(db.Integer, nullable=False)
    recorded_at = db.Column(db.DateTime, default=datetime.utcnow)

class Appointment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    patient_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    doctor_name = db.Column(db.String(120), nullable=False)
    date = db.Column(db.String(50), nullable=False)
    time = db.Column(db.String(50), nullable=False)
    notes = db.Column(db.Text, nullable=True)
    status = db.Column(db.String(20), default='Confirmed')

with app.app_context():
    try:
        db.create_all()
        # Seed initial admin if not exists
        if not User.query.filter_by(email='admin@medibro.com').first():
            hashed_admin = bcrypt.generate_password_hash('admin123').decode('utf-8')
            admin_user = User(
                full_name='System Administrator',
                email='admin@medibro.com',
                password=hashed_admin,
                role='admin',
                status='Approved'
            )
            db.session.add(admin_user)
            db.session.commit()
    except Exception as e:
        print("DB Initialization info:", e)

# ---------------------------------------------------------------------------
# Core Routes
# ---------------------------------------------------------------------------
@app.route('/', methods=['GET'])
def index():
    return render_template('index.html')

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        full_name = request.form.get('full_name')
        email = request.form.get('email')
        password = request.form.get('password')
        role = request.form.get('role', 'patient')
        specialty = request.form.get('specialty', '')

        try:
            existing_user = User.query.filter_by(email=email).first()
            if existing_user:
                flash('Email is already registered. Please log in.', 'danger')
                return redirect(url_for('register'))

            hashed_pw = bcrypt.generate_password_hash(password).decode('utf-8')
            
            # Doctors require admin verification by default
            initial_status = 'Pending Verification' if role == 'doctor' else 'Approved'

            user = User(
                full_name=full_name,
                email=email,
                password=hashed_pw,
                role=role,
                specialty=specialty,
                status=initial_status
            )
            db.session.add(user)
            db.session.commit()

            if role == 'doctor':
                flash('Account created! Your doctor profile is currently pending Admin verification.', 'info')
            else:
                flash('Account created successfully! Please log in.', 'success')
            return redirect(url_for('login'))

        except Exception as e:
            db.session.rollback()
            flash('Error creating account. Please try again.', 'danger')

    return render_template('register.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form.get('email')
        password = request.form.get('password')
        
        try:
            user = User.query.filter_by(email=email).first()
            if user and bcrypt.check_password_hash(user.password, password):
                session['user_id'] = user.id
                session['user_name'] = user.full_name
                session['user_role'] = user.role
                session['user_status'] = user.status

                if user.role == 'admin':
                    return redirect(url_for('admin_dashboard'))

                flash('Welcome back to MediBro!', 'success')
                return redirect(url_for('dashboard'))
            else:
                flash('Invalid email or password.', 'danger')
        except Exception as e:
            flash('Database connecting... please try logging in again.', 'warning')

    return render_template('login.html')

@app.route('/logout', methods=['GET', 'POST'])
def logout():
    session.clear()
    flash('Logged out successfully.', 'info')
    return redirect(url_for('login'))

@app.route('/dashboard', methods=['GET'])
def dashboard():
    if 'user_id' not in session:
        return redirect(url_for('login'))

    user_id = session['user_id']
    user_role = session.get('user_role', 'patient')

    try:
        user = User.query.get(user_id)
        if user_role == 'doctor':
            appointments = Appointment.query.all()
            vitals = Vital.query.order_by(Vital.recorded_at.desc()).limit(15).all()
            doctors = []
        else:
            vitals = Vital.query.filter_by(patient_id=user_id).order_by(Vital.recorded_at.desc()).limit(10).all()
            appointments = Appointment.query.filter_by(patient_id=user_id).all()
            doctors = User.query.filter_by(role='doctor', status='Approved').all()
    except Exception:
        user, vitals, appointments, doctors = None, [], [], []

    return render_template('dashboard.html', user=user, vitals=vitals, appointments=appointments, doctors=doctors)

@app.route('/admin', methods=['GET'])
def admin_dashboard():
    if 'user_id' not in session or session.get('user_role') != 'admin':
        flash('Unauthorized access. Admin privileges required.', 'danger')
        return redirect(url_for('login'))

    users = User.query.all()
    pending_doctors = User.query.filter_by(role='doctor', status='Pending Verification').all()
    verified_doctors = User.query.filter_by(role='doctor', status='Approved').all()
    total_patients = User.query.filter_by(role='patient').count()
    total_appointments = Appointment.query.count()

    return render_template(
        'admin.html',
        users=users,
        pending_doctors=pending_doctors,
        verified_doctors=verified_doctors,
        total_patients=total_patients,
        total_appointments=total_appointments
    )

# ---------------------------------------------------------------------------
# API Endpoints
# ---------------------------------------------------------------------------
@app.route('/api/approve-doctor/<int:doctor_id>', methods=['POST'])
def approve_doctor(doctor_id):
    if 'user_id' not in session or session.get('user_role') != 'admin':
        return jsonify({'error': 'Unauthorized'}), 401

    doctor = User.query.get(doctor_id)
    if doctor:
        doctor.status = 'Approved'
        db.session.commit()
        flash(f'Dr. {doctor.full_name} has been verified and approved!', 'success')
    return redirect(url_for('admin_dashboard'))

@app.route('/api/reject-doctor/<int:doctor_id>', methods=['POST'])
def reject_doctor(doctor_id):
    if 'user_id' not in session or session.get('user_role') != 'admin':
        return jsonify({'error': 'Unauthorized'}), 401

    doctor = User.query.get(doctor_id)
    if doctor:
        doctor.status = 'Rejected'
        db.session.commit()
        flash(f'Doctor registration for {doctor.full_name} was rejected.', 'info')
    return redirect(url_for('admin_dashboard'))

@app.route('/api/add-vital', methods=['POST'])
def add_vital():
    if 'user_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    
    systolic = int(request.form.get('systolic', 120))
    diastolic = int(request.form.get('diastolic', 80))
    heart_rate = int(request.form.get('heart_rate', 72))

    vital = Vital(patient_id=session['user_id'], systolic=systolic, diastolic=diastolic, heart_rate=heart_rate)
    db.session.add(vital)
    db.session.commit()
    flash('Vitals recorded successfully!', 'success')
    return redirect(url_for('dashboard'))

@app.route('/api/book-appointment', methods=['POST'])
def book_appointment():
    if 'user_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401

    doctor_name = request.form.get('doctor_name')
    date = request.form.get('date')
    time = request.form.get('time')
    notes = request.form.get('notes', '')

    appt = Appointment(patient_id=session['user_id'], doctor_name=doctor_name, date=date, time=time, notes=notes)
    db.session.add(appt)
    db.session.commit()
    flash(f'Appointment booked with {doctor_name} for {date} at {time}!', 'success')
    return redirect(url_for('dashboard'))

@app.route('/api/ai-assistant', methods=['POST'])
def ai_assistant():
    data = request.json or {}
    message = data.get('message', '').lower()

    if 'headache' in message or 'migraine' in message:
        reply = "A headache can stem from stress, dehydration, or tension. Stay hydrated and rest in a dim room. If severe or accompanied by nausea, consult a physician."
    elif 'fever' in message or 'temperature' in message:
        reply = "A fever indicates an immune response. Rest, stay hydrated with fluids, and monitor your temperature. Seek care if it exceeds 102°F (38.9°C)."
    elif 'bp' in message or 'blood pressure' in message:
        reply = "Normal blood pressure is around 120/80 mmHg. You can log new readings directly on your MediBro Vitals panel!"
    else:
        reply = "Hello! I am your MediBro AI Assistant. Describe any symptoms or questions you have today."

    return jsonify({'reply': reply})

if __name__ == '__main__':
    app.run(debug=True)
