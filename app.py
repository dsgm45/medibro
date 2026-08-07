"""
MediMind AI - Flask application
Integrates: patient dashboard, doctor dashboard, appointment chat,
medicine reminders, vitals tracking, and booking.
"""
import sqlite3
from datetime import datetime, date
from functools import wraps

from flask import (
    Flask, render_template, request, redirect, url_for,
    session, flash, g
)
from werkzeug.security import generate_password_hash, check_password_hash

DATABASE = "medimind.db"

app = Flask(__name__)
app.config["SECRET_KEY"] = "dev-secret-key-change-in-production"


# ------------------------------------------------------------------
# DB HELPERS
# ------------------------------------------------------------------
def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DATABASE)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(exception=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    db = get_db()

    # 1. USER table
    db.execute('''
        CREATE TABLE IF NOT EXISTS user (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            full_name TEXT NOT NULL,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL CHECK(role IN ('patient', 'doctor', 'hospital', 'clinic')),
            specialty TEXT,
            phone TEXT
        )
    ''')

    # 2. APPOINTMENT table
    db.execute('''
        CREATE TABLE IF NOT EXISTS appointment (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            patient_id INTEGER NOT NULL,
            doctor_id INTEGER NOT NULL,
            appt_date TEXT NOT NULL,
            appt_time TEXT NOT NULL,
            status TEXT DEFAULT 'Confirmed',
            follow_up_date TEXT,
            FOREIGN KEY (patient_id) REFERENCES user (id),
            FOREIGN KEY (doctor_id) REFERENCES user (id)
        )
    ''')

    # 3. MEDICINE table
    db.execute('''
        CREATE TABLE IF NOT EXISTS medicine (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            patient_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            dosage TEXT NOT NULL,
            reminder_time TEXT NOT NULL,
            instructions TEXT,
            recurrence TEXT DEFAULT 'daily',
            days_of_week TEXT,
            taken_today INTEGER DEFAULT 0,
            FOREIGN KEY (patient_id) REFERENCES user (id)
        )
    ''')

    # 4. MESSAGE table
    db.execute('''
        CREATE TABLE IF NOT EXISTS message (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            appointment_id INTEGER NOT NULL,
            sender_id INTEGER NOT NULL,
            body TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (appointment_id) REFERENCES appointment (id),
            FOREIGN KEY (sender_id) REFERENCES user (id)
        )
    ''')

    # 5. VITALS table
    db.execute('''
        CREATE TABLE IF NOT EXISTS vitals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            patient_id INTEGER NOT NULL,
            heart_rate INTEGER,
            blood_pressure_systolic INTEGER,
            blood_pressure_diastolic INTEGER,
            blood_sugar INTEGER,
            weight_kg REAL,
            recorded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (patient_id) REFERENCES user (id)
        )
    ''')

    db.commit()


# ------------------------------------------------------------------
# AUTH HELPERS
# ------------------------------------------------------------------
def current_user():
    uid = session.get("user_id")
    if not uid:
        return None
    db = get_db()
    return db.execute("SELECT * FROM user WHERE id = ?", (uid,)).fetchone()


def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("user_id"):
            flash("Please log in to continue.", "error")
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return wrapper


def doctor_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        user = current_user()
        if not user or user["role"] != "doctor":
            flash("That page is only available to doctors.", "error")
            return redirect(url_for("dashboard"))
        return f(*args, **kwargs)
    return wrapper


def appointment_participant_required(appt_row, user):
    """Return True only if the current user is the patient or doctor on this appointment."""
    return user and (user["id"] == appt_row["patient_id"] or user["id"] == appt_row["doctor_id"])


# ------------------------------------------------------------------
# AUTH ROUTES
# ------------------------------------------------------------------
@app.route("/")
def index():
    if session.get("user_id"):
        return redirect(url_for("dashboard"))
    return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")

        db = get_db()
        user = db.execute("SELECT * FROM user WHERE email = ?", (email,)).fetchone()

        if user is None or not check_password_hash(user["password_hash"], password):
            flash("Invalid email or password.", "error")
            return render_template("login.html")

        session.clear()
        session["user_id"] = user["id"]
        flash(f"Welcome back, {user['full_name']}!", "success")

        if user["role"] == "doctor":
            return redirect(url_for("doctor_dashboard"))
        return redirect(url_for("dashboard"))

    return render_template("login.html")


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        full_name = request.form.get("full_name", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        role = request.form.get("role", "patient")
        specialty = request.form.get("specialty", "").strip() or None
        phone = request.form.get("phone", "").strip() or None

        errors = []
        if not full_name:
            errors.append("Full name is required.")
        if not email:
            errors.append("Email is required.")
        if len(password) < 6:
            errors.append("Password must be at least 6 characters.")
        if role not in ("patient", "doctor", "hospital", "clinic"):
            errors.append("Invalid role selected.")

        db = get_db()
        if not errors and db.execute("SELECT id FROM user WHERE email = ?", (email,)).fetchone():
            errors.append("An account with that email already exists.")

        if errors:
            for e in errors:
                flash(e, "error")
            return render_template("register.html")

        # Explicitly specify pbkdf2:sha256 method to prevent hashlib.scrypt AttributeErrors
        hashed_password = generate_password_hash(password, method="pbkdf2:sha256")

        db.execute(
            "INSERT INTO user (full_name, email, password_hash, role, specialty, phone) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (full_name, email, hashed_password, role, specialty, phone),
        )
        db.commit()
        flash("Account created. Please log in.", "success")
        return redirect(url_for("login"))

    return render_template("register.html")


@app.route("/logout")
def logout():
    session.clear()
    flash("You have been logged out.", "success")
    return redirect(url_for("login"))


# ------------------------------------------------------------------
# PATIENT DASHBOARD
# ------------------------------------------------------------------
@app.route("/dashboard")
@login_required
def dashboard():
    user = current_user()
    if user["role"] == "doctor":
        return redirect(url_for("doctor_dashboard"))

    db = get_db()
    today_str = date.today().strftime("%Y-%m-%d")

    latest_vitals = db.execute('''
        SELECT * FROM vitals
        WHERE patient_id = ?
        ORDER BY recorded_at DESC, id DESC
        LIMIT 1
    ''', (user["id"],)).fetchone()

    vitals_data = None
    abnormal_vitals = []

    if latest_vitals:
        hr = latest_vitals["heart_rate"]
        sys = latest_vitals["blood_pressure_systolic"]
        dia = latest_vitals["blood_pressure_diastolic"]
        sugar = latest_vitals["blood_sugar"]

        hr_status, hr_flag = "Normal", "normal"
        if hr is not None:
            if hr > 100:
                hr_status, hr_flag = "Tachycardia (High)", "abnormal"
                abnormal_vitals.append("Heart Rate (Tachycardia)")
            elif hr < 60:
                hr_status, hr_flag = "Bradycardia (Low)", "abnormal"
                abnormal_vitals.append("Heart Rate (Bradycardia)")

        bp_status, bp_flag = "Optimal", "normal"
        if sys is not None and dia is not None:
            if sys >= 140 or dia >= 90:
                bp_status, bp_flag = "High (Abnormal)", "abnormal"
                abnormal_vitals.append("Blood Pressure (High)")
            elif sys >= 120 or dia >= 80:
                bp_status, bp_flag = "Elevated", "warning"

        sugar_status, sugar_flag = "Normal", "normal"
        if sugar is not None:
            if sugar < 70:
                sugar_status, sugar_flag = "Low (Hypoglycemia)", "abnormal"
                abnormal_vitals.append("Blood Sugar (Low)")
            elif sugar > 125:
                sugar_status, sugar_flag = "High (Abnormal)", "abnormal"
                abnormal_vitals.append("Blood Sugar (High)")
            elif 100 <= sugar <= 125:
                sugar_status, sugar_flag = "Elevated (Prediabetes)", "warning"

        vitals_data = {
            "heart_rate": hr, "hr_status": hr_status, "hr_flag": hr_flag,
            "bp_systolic": sys, "bp_diastolic": dia, "bp_status": bp_status, "bp_flag": bp_flag,
            "blood_sugar": sugar, "sugar_status": sugar_status, "sugar_flag": sugar_flag,
            "weight_kg": latest_vitals["weight_kg"],
            "recorded_at": latest_vitals["recorded_at"],
        }

    all_appointments = db.execute('''
        SELECT
            a.id, a.appt_date, a.appt_time, a.status, a.follow_up_date,
            d.full_name AS doctor_name, d.specialty, d.phone AS doctor_phone
        FROM appointment a
        JOIN user d ON a.doctor_id = d.id
        WHERE a.patient_id = ?
        ORDER BY a.appt_date ASC, a.appt_time ASC
    ''', (user["id"],)).fetchall()

    today = date.today()
    processed_appointments = []
    next_appointment = None

    for appt in all_appointments:
        appt_dict = dict(appt)
        doc_name = appt["doctor_name"] or "Doctor"
        parts = doc_name.replace("Dr.", "").strip().split()
        initials = "".join([p[0].upper() for p in parts[:2]]) if parts else "DR"
        appt_dict["initials"] = initials

        if appt["follow_up_date"]:
            try:
                fu_dt = datetime.strptime(appt["follow_up_date"], "%Y-%m-%d").date()
                appt_dict["days_until_follow_up"] = (fu_dt - today).days
            except ValueError:
                appt_dict["days_until_follow_up"] = None
        else:
            appt_dict["days_until_follow_up"] = None

        processed_appointments.append(appt_dict)

        if not next_appointment and appt["status"] != "Completed" and appt["appt_date"] >= today_str:
            next_appointment = appt_dict

    medicines = db.execute('''
        SELECT * FROM medicine WHERE patient_id = ? ORDER BY reminder_time ASC
    ''', (user["id"],)).fetchall()

    doctors = db.execute('''
        SELECT id, full_name, specialty, phone FROM user WHERE role = 'doctor'
    ''').fetchall()

    return render_template(
        "dashboard.html",
        user=user,
        vitals=vitals_data,
        abnormal_vitals=abnormal_vitals,
        next_appointment=next_appointment,
        appointments=processed_appointments,
        medicines=medicines,
        doctors=doctors,
        today_str=today_str,
    )


@app.route("/vitals/add", methods=["POST"])
@login_required
def add_vitals():
    user = current_user()

    heart_rate = request.form.get("heart_rate", type=int)
    systolic = request.form.get("blood_pressure_systolic", type=int)
    diastolic = request.form.get("blood_pressure_diastolic", type=int)
    blood_sugar = request.form.get("blood_sugar", type=int)
    weight_kg = request.form.get("weight_kg", type=float)

    db = get_db()
    db.execute('''
        INSERT INTO vitals (patient_id, heart_rate, blood_pressure_systolic, blood_pressure_diastolic, blood_sugar, weight_kg)
        VALUES (?, ?, ?, ?, ?, ?)
    ''', (user["id"], heart_rate, systolic, diastolic, blood_sugar, weight_kg))
    db.commit()

    flash("Vitals entry logged successfully!", "success")
    return redirect(url_for("dashboard"))


# ------------------------------------------------------------------
# BOOKING
# ------------------------------------------------------------------
@app.route("/book/<int:doctor_id>", methods=["POST"])
@login_required
def book(doctor_id):
    user = current_user()
    if user["role"] != "patient":
        flash("Only patients can book appointments.", "error")
        return redirect(url_for("dashboard"))

    appt_date = request.form.get("appt_date", "").strip()
    appt_time = request.form.get("appt_time", "").strip()

    if not appt_date or not appt_time:
        flash("Please choose a date and time.", "error")
        return redirect(url_for("dashboard"))

    db = get_db()
    doctor = db.execute("SELECT id FROM user WHERE id = ? AND role = 'doctor'", (doctor_id,)).fetchone()
    if not doctor:
        flash("Selected physician was not found.", "error")
        return redirect(url_for("dashboard"))

    db.execute('''
        INSERT INTO appointment (patient_id, doctor_id, appt_date, appt_time, status)
        VALUES (?, ?, ?, ?, 'Confirmed')
    ''', (user["id"], doctor_id, appt_date, appt_time))
    db.commit()

    flash("Appointment booked successfully!", "success")
    return redirect(url_for("dashboard"))


# ------------------------------------------------------------------
# MEDICINE CRUD
# ------------------------------------------------------------------
@app.route("/medicine/add", methods=["POST"])
@login_required
def add_medicine():
    user = current_user()

    name = request.form.get("name", "").strip()
    dosage = request.form.get("dosage", "").strip()
    reminder_time = request.form.get("reminder_time", "").strip()
    instructions = request.form.get("instructions", "").strip() or None
    recurrence = request.form.get("recurrence", "daily")
    days_of_week = ",".join(request.form.getlist("days_of_week")) or None

    if not name or not dosage or not reminder_time:
        flash("Medication name, dosage, and reminder time are required.", "error")
        return redirect(url_for("dashboard"))

    db = get_db()
    db.execute('''
        INSERT INTO medicine (patient_id, name, dosage, reminder_time, instructions, recurrence, days_of_week)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    ''', (user["id"], name, dosage, reminder_time, instructions, recurrence, days_of_week))
    db.commit()

    flash("Medication added.", "success")
    return redirect(url_for("dashboard"))


@app.route("/medicine/<int:med_id>/edit", methods=["GET", "POST"])
@login_required
def edit_medicine(med_id):
    user = current_user()
    db = get_db()
    med = db.execute(
        "SELECT * FROM medicine WHERE id = ? AND patient_id = ?", (med_id, user["id"])
    ).fetchone()

    if med is None:
        flash("Medication not found.", "error")
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        dosage = request.form.get("dosage", "").strip()
        reminder_time = request.form.get("reminder_time", "").strip()
        instructions = request.form.get("instructions", "").strip() or None
        recurrence = request.form.get("recurrence", "daily")
        days_of_week = ",".join(request.form.getlist("days_of_week")) or None

        if not name or not dosage or not reminder_time:
            flash("Medication name, dosage, and reminder time are required.", "error")
            selected_days = (med["days_of_week"] or "").split(",")
            return render_template("edit_medicine.html", med=med, selected_days=selected_days)

        db.execute('''
            UPDATE medicine
            SET name = ?, dosage = ?, reminder_time = ?, instructions = ?, recurrence = ?, days_of_week = ?
            WHERE id = ? AND patient_id = ?
        ''', (name, dosage, reminder_time, instructions, recurrence, days_of_week, med_id, user["id"]))
        db.commit()

        flash("Medication updated.", "success")
        return redirect(url_for("dashboard"))

    selected_days = (med["days_of_week"] or "").split(",")
    return render_template("edit_medicine.html", med=med, selected_days=selected_days)


@app.route("/medicine/<int:med_id>/delete", methods=["POST"])
@login_required
def delete_medicine(med_id):
    user = current_user()
    db = get_db()
    db.execute("DELETE FROM medicine WHERE id = ? AND patient_id = ?", (med_id, user["id"]))
    db.commit()
    flash("Medication removed.", "success")
    return redirect(url_for("dashboard"))


# ------------------------------------------------------------------
# DOCTOR DASHBOARD
# ------------------------------------------------------------------
@app.route("/doctor/dashboard")
@login_required
@doctor_required
def doctor_dashboard():
    doctor = current_user()
    db = get_db()
    today_str = date.today().strftime("%Y-%m-%d")

    rows = db.execute('''
        SELECT
            a.id, a.appt_date, a.appt_time, a.status, a.follow_up_date,
            p.full_name AS patient_name, p.email AS patient_email, p.phone AS patient_phone
        FROM appointment a
        JOIN user p ON a.patient_id = p.id
        WHERE a.doctor_id = ?
        ORDER BY a.appt_date ASC, a.appt_time ASC
    ''', (doctor["id"],)).fetchall()

    today = date.today()
    appointments = []
    for appt in rows:
        appt_dict = dict(appt)
        if appt["follow_up_date"]:
            try:
                fu_dt = datetime.strptime(appt["follow_up_date"], "%Y-%m-%d").date()
                appt_dict["days_until_follow_up"] = (fu_dt - today).days
            except ValueError:
                appt_dict["days_until_follow_up"] = None
        else:
            appt_dict["days_until_follow_up"] = None
        appointments.append(appt_dict)

    return render_template(
        "doctor_dashboard.html",
        doctor=doctor,
        appointments=appointments,
        today_str=today_str,
    )


@app.route("/appointment/<int:appointment_id>/complete", methods=["POST"])
@login_required
@doctor_required
def complete_appointment(appointment_id):
    doctor = current_user()
    db = get_db()
    appt = db.execute(
        "SELECT * FROM appointment WHERE id = ? AND doctor_id = ?", (appointment_id, doctor["id"])
    ).fetchone()

    if appt is None:
        flash("Appointment not found.", "error")
        return redirect(url_for("doctor_dashboard"))

    follow_up_date = request.form.get("follow_up_date", "").strip() or None

    db.execute(
        "UPDATE appointment SET status = 'Completed', follow_up_date = ? WHERE id = ?",
        (follow_up_date, appointment_id),
    )
    db.commit()

    flash("Appointment marked as completed.", "success")
    return redirect(url_for("doctor_dashboard"))


# ------------------------------------------------------------------
# CONSULTATION CHAT
# ------------------------------------------------------------------
@app.route("/appointment/<int:appointment_id>/messages", methods=["GET", "POST"])
@login_required
def appointment_messages(appointment_id):
    user = current_user()
    db = get_db()

    appt_row = db.execute('''
        SELECT
            a.id, a.patient_id, a.doctor_id, a.appt_date, a.appt_time, a.status,
            p.full_name AS patient_name,
            d.full_name AS doctor_name
        FROM appointment a
        JOIN user p ON a.patient_id = p.id
        JOIN user d ON a.doctor_id = d.id
        WHERE a.id = ?
    ''', (appointment_id,)).fetchone()

    if appt_row is None:
        flash("Appointment not found.", "error")
        return redirect(url_for("dashboard"))

    # Security: only the patient or doctor on this appointment may view/post
    if not appointment_participant_required(appt_row, user):
        flash("You do not have access to that consultation.", "error")
        return redirect(url_for("doctor_dashboard") if user["role"] == "doctor" else url_for("dashboard"))

    if request.method == "POST":
        body = request.form.get("message_body", "").strip()
        if body:
            db.execute('''
                INSERT INTO message (appointment_id, sender_id, body)
                VALUES (?, ?, ?)
            ''', (appointment_id, user["id"], body))
            db.commit()
        return redirect(url_for("appointment_messages", appointment_id=appointment_id))

    rows = db.execute('''
        SELECT m.id, m.body, m.created_at, m.sender_id,
               u.full_name AS sender_name, u.role AS sender_role
        FROM message m
        JOIN user u ON m.sender_id = u.id
        WHERE m.appointment_id = ?
        ORDER BY m.created_at ASC, m.id ASC
    ''', (appointment_id,)).fetchall()

    return render_template(
        "appointment_messages.html",
        appt=appt_row,
        messages=rows,
        user=user,
    )


# ------------------------------------------------------------------
# ENTRYPOINT
# ------------------------------------------------------------------
with app.app_context():
    init_db()

if __name__ == "__main__":
    app.run(debug=True, port=5000)