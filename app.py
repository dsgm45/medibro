import os
from datetime import datetime
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, session
from flask_sqlalchemy import SQLAlchemy
from flask_bcrypt import Bcrypt

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'medibro-super-secret-key-2026')
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL')
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
    role = db.Column(db.String(20), default='patient')
    specialty = db.Column(db.String(100), nullable=True)

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

# Ensure tables are created safely
with app.app_context():
    try:
        db.create_all()
    except Exception as e:
        print("Database sync info:", e)

# ---------------------------------------------------------------------------
# Core Routes
# ---------------------------------------------------------------------------
@app.route('/')
def index():
    if 'user_id' in session:
        return redirect(url_for('dashboard'))
    return render_template('login.html')

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
                flash('Email already registered!', 'danger')
                return redirect(url_for('register'))

            hashed_pw = bcrypt.generate_password_hash(password).decode('utf-8')
            user = User(full_name=full_name, email=email, password=hashed_pw, role=role, specialty=specialty)
            db.session.add(user)
            db.session.commit()

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
                flash('Welcome back to MediBro!', 'success')
                return redirect(url_for('dashboard'))
            else:
                flash('Invalid email or password.', 'danger')
        except Exception as e:
            flash('Database connecting... please refresh in a moment.', 'warning')

    return render_template('login.html')

@app.route('/logout')
def logout():
    session.clear()
    flash('Logged out successfully.', 'info')
    return redirect(url_for('login'))

@app.route('/dashboard')
def dashboard():
    if 'user_id' not in session:
        return redirect(url_for('login'))

    user_id = session['user_id']
    try:
        vitals = Vital.query.filter_by(patient_id=user_id).order_by(Vital.recorded_at.desc()).limit(10).all()
        appointments = Appointment.query.filter_by(patient_id=user_id).all()
        doctors = User.query.filter_by(role='doctor').all()
    except Exception:
        vitals, appointments, doctors = [], [], []

    return render_template('dashboard.html', vitals=vitals, appointments=appointments, doctors=doctors)

# ---------------------------------------------------------------------------
# API Endpoints
# ---------------------------------------------------------------------------
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
    flash('Vitals recorded!', 'success')
    return redirect(url_for('dashboard'))

@app.route('/api/vitals-chart-data')
def vitals_chart_data():
    if 'user_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401

    try:
        vitals = Vital.query.filter_by(patient_id=session['user_id']).order_by(Vital.recorded_at.asc()).limit(10).all()
        labels = [v.recorded_at.strftime('%b %d %H:%M') for v in vitals]
        systolic_data = [v.systolic for v in vitals]
        diastolic_data = [v.diastolic for v in vitals]
        heart_rate_data = [v.heart_rate for v in vitals]
    except Exception:
        labels, systolic_data, diastolic_data, heart_rate_data = [], [], [], []

    return jsonify({
        'labels': labels,
        'systolic': systolic_data,
        'diastolic': diastolic_data,
        'heart_rate': heart_rate_data
    })

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
        reply = "A headache can be caused by stress, dehydration, or tension. Stay hydrated and rest in a dim room. If severe, consult a physician."
    elif 'fever' in message or 'temperature' in message:
        reply = "A fever indicates your body is fighting off an infection. Rest, stay hydrated, and monitor temperature. If it exceeds 102°F, consult a doctor."
    elif 'bp' in message or 'blood pressure' in message:
        reply = "Normal blood pressure is around 120/80 mmHg. You can log your readings directly on your MediBro dashboard chart!"
    else:
        reply = "Hello! I am MediBro AI. How can I assist with your health questions today?"

    return jsonify({'reply': reply})

if __name__ == '__main__':
    app.run(debug=True)
