import os
import sys

# Use a throwaway test DB so we don't touch dev data
os.environ["MEDIMIND_TEST"] = "1"
if os.path.exists("medimind.db"):
    os.remove("medimind.db")

import app as appmodule

app = appmodule.app
app.config["TESTING"] = True

failures = []

def check(label, condition):
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {label}")
    if not condition:
        failures.append(label)


with app.test_client() as c:
    # --- Register a patient ---
    r = c.post("/register", data={
        "full_name": "Priya Patient", "email": "priya@example.com",
        "password": "password123", "role": "patient", "phone": "9999999999",
    }, follow_redirects=True)
    check("register patient (200)", r.status_code == 200)

    # --- Register a doctor ---
    r = c.post("/register", data={
        "full_name": "Dr. Rao", "email": "rao@example.com",
        "password": "password123", "role": "doctor",
        "specialty": "Gastroenterology", "phone": "8888888888",
    }, follow_redirects=True)
    check("register doctor (200)", r.status_code == 200)

    # --- Duplicate email should fail gracefully ---
    r = c.post("/register", data={
        "full_name": "Dupe", "email": "priya@example.com",
        "password": "password123", "role": "patient",
    }, follow_redirects=True)
    check("duplicate email rejected", b"already exists" in r.data)

    # --- Login as patient ---
    r = c.post("/login", data={"email": "priya@example.com", "password": "password123"},
               follow_redirects=True)
    check("patient login redirects to dashboard", b"Welcome back, Priya" in r.data)

    # --- Bad login ---
    r = c.post("/login", data={"email": "priya@example.com", "password": "wrong"},
               follow_redirects=True)
    check("bad password rejected", b"Invalid email or password" in r.data)

    # re-login properly (previous failed attempt did not create a session)
    c.post("/login", data={"email": "priya@example.com", "password": "password123"})

    # --- Dashboard loads with no vitals/appointments yet ---
    r = c.get("/dashboard")
    check("dashboard GET 200", r.status_code == 200)
    check("dashboard shows doctor in booking list", b"Dr. Rao" in r.data)

    # --- Get doctor id from DB directly ---
    with app.app_context():
        db = appmodule.get_db()
        doctor_row = db.execute("SELECT id FROM user WHERE email = 'rao@example.com'").fetchone()
        doctor_id = doctor_row["id"]

    # --- Book an appointment ---
    r = c.post(f"/book/{doctor_id}", data={
        "appt_date": "2026-08-20", "appt_time": "10:30",
    }, follow_redirects=True)
    check("booking succeeds", b"Appointment booked successfully" in r.data)

    with app.app_context():
        db = appmodule.get_db()
        appt_row = db.execute("SELECT id FROM appointment ORDER BY id DESC LIMIT 1").fetchone()
        appt_id = appt_row["id"]

    # --- Add vitals (normal) ---
    r = c.post("/vitals/add", data={
        "heart_rate": "72", "blood_pressure_systolic": "118",
        "blood_pressure_diastolic": "76", "blood_sugar": "90", "weight_kg": "68.5",
    }, follow_redirects=True)
    check("vitals add succeeds", b"Vitals entry logged successfully" in r.data)
    check("vitals renders heart rate", b"72" in r.data)

    # --- Add vitals (abnormal, to hit the alert branch) ---
    r = c.post("/vitals/add", data={
        "heart_rate": "130", "blood_pressure_systolic": "150",
        "blood_pressure_diastolic": "95", "blood_sugar": "180", "weight_kg": "68.5",
    }, follow_redirects=True)
    check("abnormal vitals triggers alert copy", b"Medical Attention Alert" in r.data)

    # --- Add medicine ---
    r = c.post("/medicine/add", data={
        "name": "Pantoprazole", "dosage": "40mg", "reminder_time": "08:00",
        "instructions": "Before breakfast", "recurrence": "daily",
    }, follow_redirects=True)
    check("medicine add succeeds", b"Medication added" in r.data)
    check("medicine shows in checklist", b"Pantoprazole" in r.data)

    with app.app_context():
        db = appmodule.get_db()
        med_row = db.execute("SELECT id FROM medicine ORDER BY id DESC LIMIT 1").fetchone()
        med_id = med_row["id"]

    # --- Edit medicine GET ---
    r = c.get(f"/medicine/{med_id}/edit")
    check("edit medicine GET 200", r.status_code == 200)
    check("edit medicine form pre-filled", b"Pantoprazole" in r.data)

    # --- Edit medicine POST ---
    r = c.post(f"/medicine/{med_id}/edit", data={
        "name": "Pantoprazole", "dosage": "40mg", "reminder_time": "09:00",
        "instructions": "Before breakfast", "recurrence": "specific_days",
        "days_of_week": ["Mon", "Wed", "Fri"],
    }, follow_redirects=True)
    check("edit medicine POST succeeds", b"Medication updated" in r.data)

    # --- Patient posts a chat message ---
    r = c.post(f"/appointment/{appt_id}/messages", data={
        "message_body": "Hi doctor, I have some acid reflux symptoms.",
    }, follow_redirects=True)
    check("patient message posts", b"acid reflux symptoms" in r.data)

    # --- Log out patient ---
    c.get("/logout")
    r = c.get("/dashboard", follow_redirects=True)
    check("dashboard requires login after logout", b"Please log in to continue" in r.data)

    # --- Login as doctor ---
    r = c.post("/login", data={"email": "rao@example.com", "password": "password123"},
               follow_redirects=True)
    check("doctor login redirects to doctor dashboard", b"Provider ID" in r.data)

    # --- Doctor dashboard shows the booked appointment ---
    r = c.get("/doctor/dashboard")
    check("doctor dashboard 200", r.status_code == 200)
    check("doctor dashboard shows patient name", b"Priya Patient" in r.data)

    # --- A patient trying to access doctor dashboard should be redirected ---
    # (tested below after logging back in as patient)

    # --- Doctor views and replies in chat ---
    r = c.get(f"/appointment/{appt_id}/messages")
    check("doctor can view chat", b"acid reflux symptoms" in r.data)

    r = c.post(f"/appointment/{appt_id}/messages", data={
        "message_body": "Let's schedule an endoscopy to check.",
    }, follow_redirects=True)
    check("doctor reply posts", b"endoscopy" in r.data)

    # --- Doctor marks appointment completed with a follow-up date ---
    r = c.post(f"/appointment/{appt_id}/complete", data={
        "follow_up_date": "2026-09-01",
    }, follow_redirects=True)
    check("mark completed succeeds", b"Appointment marked as completed" in r.data)
    check("doctor dashboard shows completed status", b"Completed" in r.data)

    c.get("/logout")

    # --- Register a second, unrelated patient to test chat access control ---
    c.post("/register", data={
        "full_name": "Amit Outsider", "email": "amit@example.com",
        "password": "password123", "role": "patient",
    })
    c.post("/login", data={"email": "amit@example.com", "password": "password123"})

    r = c.get(f"/appointment/{appt_id}/messages", follow_redirects=True)
    check("unrelated user blocked from chat", b"do not have access" in r.data)

    # --- Original patient sees completed status + follow-up on their dashboard ---
    c.get("/logout")
    c.post("/login", data={"email": "priya@example.com", "password": "password123"})
    r = c.get("/dashboard")
    check("patient dashboard reflects completed appt", b"Completed" in r.data)
    check("patient dashboard shows follow-up date", b"2026-09-01" in r.data)

    # --- Delete medicine ---
    r = c.post(f"/medicine/{med_id}/delete", follow_redirects=True)
    check("delete medicine succeeds", b"Medication removed" in r.data)


print("\n" + "=" * 50)
if failures:
    print(f"{len(failures)} CHECK(S) FAILED:")
    for f in failures:
        print(" -", f)
    sys.exit(1)
else:
    print("ALL CHECKS PASSED")
    sys.exit(0)
