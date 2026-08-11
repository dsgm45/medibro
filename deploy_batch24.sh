#!/bin/bash
set -e

echo "=== MediBro: Patch vulnerable transitive dependencies (click, pillow) ==="

if [ ! -f "app.py" ]; then
  echo "ERROR: app.py not found. cd into your medimind project folder first, then re-run this script."
  exit 1
fi

cat > requirements.txt << 'REQ_EOF'
Flask==3.1.3
Werkzeug==3.1.8
Flask-SQLAlchemy==3.1.1
psycopg2-binary==2.9.12
gunicorn==26.0.0
Flask-Bcrypt==1.0.1
email-validator==2.3.0
Flask-Migrate==4.0.7
Flask-WTF==1.2.1
fpdf2==2.7.9
click>=8.3.3
pillow>=12.2.0
REQ_EOF

echo "requirements.txt updated."
echo ""
echo "=== Verifying it installs cleanly locally ==="
pip3 install -r requirements.txt --upgrade
echo ""
echo "=== Re-scanning to confirm the fix ==="
grep -v gunicorn requirements.txt > requirements-scan.txt
python3 -m pip_audit -r requirements-scan.txt || true

echo ""
echo "=== Review the scan output above - click/pillow findings should be gone ==="
read -p "Press Enter to commit and push, or Ctrl+C to stop here: "

git add requirements.txt
git commit -m "Pin click and pillow to patched versions (pip-audit findings)"
git push origin main

echo ""
echo "=== Done. Check Render dashboard for the new deploy. ==="
echo "Watch the BUILD logs specifically to confirm the new pillow/click"
echo "versions install cleanly there too, not just locally."
