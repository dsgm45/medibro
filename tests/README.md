# MediBro Test Suite

## What this covers

- **Registration**: valid signup, doctor accounts auto-pending, invalid email
  rejected, weak/short passwords rejected, duplicate emails rejected
- **Login**: correct/incorrect credentials, pending doctors blocked,
  suspended accounts blocked, role-based redirect after login
- **Rate limiting**: repeated failed logins get locked out (even with the
  correct password once locked), and the lockout doesn't leak across
  different accounts
- **Access control**: every role boundary (patient/doctor/admin can't reach
  each other's dashboards), and an ownership check (one patient can't cancel
  another patient's appointment)
- **Booking**: successful booking, required-field validation, and the
  double-booking prevention logic (same slot blocked, different time/doctor
  allowed, a declined appointment frees the slot back up)

This is a first pass covering the core flows, not exhaustive coverage of
every route in the app.

## Important: I could not run these tests myself

Every other batch tonight got verified against Render's actual build logs
or by executing the logic directly in this sandbox. This is different -
`pytest`, `Flask-SQLAlchemy`, and `Flask-WTF` aren't available in this
sandbox (no network access to install them), so **this test suite has only
been checked for Python syntax validity and manually cross-referenced
against the actual routes in `app.py` - it has never actually been run.**

Please run it and paste back the output, including any failures. That's
the only way to actually confirm this works, and I'd rather fix real
failures than have you discover a broken test suite later.

## How to run it

```
cd /Users/dpk/Downloads/medimind
pip install -r requirements-dev.txt --break-system-packages
pytest -v
```

If a test fails, the `-v` flag will show you exactly which one and why -
paste that output back and I'll fix it.

## Notes on how this is built

- `app.py` doesn't use an application-factory pattern - it creates the
  Flask app and runs database setup as side effects at import time. To test
  this safely, `conftest.py` points `DATABASE_URL` at a temporary SQLite
  file *before* importing `app.py`, so your real database is never touched
  by running these tests.
- Every test gets a completely fresh database (all tables dropped and
  recreated) before it runs, so tests can't leak state into each other via
  the database.
- Two in-memory rate-limiter dictionaries in `app.py` (`LOGIN_ATTEMPTS`,
  `REGISTER_ATTEMPTS`) live outside the database and don't get cleared by
  a fresh database - they're explicitly reset before every test too, or
  tests that make several login/registration attempts would start failing
  each other for unrelated reasons.
