# MediMind AI

A patient/doctor telemedicine app: appointments, vitals tracking with
abnormal-value flagging, medicine reminders, and per-appointment chat.

## Run it

```
pip install -r requirements.txt
python app.py
```

Then open http://127.0.0.1:5000 — it will redirect to /register on first run
(SQLite DB `medimind.db` is created automatically).

## Test it

```
python test_app.py
```

Runs an end-to-end integration test against every route (auth, booking,
vitals, medicines, doctor completion flow, chat, and the access-control
check that blocks a third party from reading someone else's consultation).

## Notes
- SECRET_KEY in app.py is a placeholder — replace before any real deployment.
- Passwords are hashed with werkzeug's generate_password_hash/check_password_hash.
