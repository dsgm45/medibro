"""
Tests for document uploads. Files are stored directly in the database
(Document.file_data) - no external service to mock, unlike the earlier
R2-based version of this feature.
"""
import io
import app as app_module
from conftest import login


def _pdf_file(name='report.pdf', content=b'%PDF-1.4 fake content'):
    return (io.BytesIO(content), name)


class TestValidation:
    def test_valid_pdf_passes(self):
        class FS:
            filename = 'report.pdf'
            content_type = 'application/pdf'
        assert app_module._validate_document_upload(FS(), 1000) is None

    def test_invalid_extension_rejected(self):
        class FS:
            filename = 'malware.exe'
            content_type = 'application/pdf'
        assert app_module._validate_document_upload(FS(), 1000) is not None

    def test_invalid_content_type_rejected(self):
        class FS:
            filename = 'report.pdf'
            content_type = 'application/x-executable'
        assert app_module._validate_document_upload(FS(), 1000) is not None

    def test_empty_file_rejected(self):
        class FS:
            filename = 'report.pdf'
            content_type = 'application/pdf'
        assert app_module._validate_document_upload(FS(), 0) is not None

    def test_oversized_file_rejected(self):
        class FS:
            filename = 'report.pdf'
            content_type = 'application/pdf'
        too_big = app_module.MAX_DOCUMENT_SIZE_BYTES + 1
        assert app_module._validate_document_upload(FS(), too_big) is not None

    def test_no_file_rejected(self):
        assert app_module._validate_document_upload(None, 0) is not None


class TestUpload:
    def test_successful_upload_creates_document_with_bytes_stored(self, client, make_user):
        patient_id = make_user('patient@example.com', 'password123', role='patient')
        login(client, 'patient@example.com', 'password123')

        client.post('/documents/upload', data={'document': _pdf_file()}, content_type='multipart/form-data', follow_redirects=True)

        with app_module.app.app_context():
            doc = app_module.Document.query.filter_by(patient_id=patient_id).first()
            assert doc is not None
            assert doc.original_filename == 'report.pdf'
            assert doc.file_data == b'%PDF-1.4 fake content'
            assert doc.file_size == len(b'%PDF-1.4 fake content')

    def test_invalid_file_type_rejected_at_route_level(self, client, make_user):
        make_user('patient@example.com', 'password123', role='patient')
        login(client, 'patient@example.com', 'password123')

        bad_file = (io.BytesIO(b'not really an exe but wrong extension'), 'virus.exe')
        client.post('/documents/upload', data={'document': bad_file}, content_type='multipart/form-data', follow_redirects=True)

        with app_module.app.app_context():
            assert app_module.Document.query.count() == 0

    def test_doctor_cannot_upload(self, client, make_user):
        make_user('doc@example.com', 'password123', role='doctor', status='approved')
        login(client, 'doc@example.com', 'password123')

        resp = client.post('/documents/upload', data={'document': _pdf_file()}, content_type='multipart/form-data', follow_redirects=False)
        assert resp.status_code == 302

    def test_upload_requires_login(self, client):
        resp = client.post('/documents/upload', data={'document': _pdf_file()}, content_type='multipart/form-data', follow_redirects=False)
        assert resp.status_code == 302


class TestDownload:
    def _upload_one(self, client, patient_email='patient@example.com'):
        login(client, patient_email, 'password123')
        client.post('/documents/upload', data={'document': _pdf_file()}, content_type='multipart/form-data', follow_redirects=True)
        with app_module.app.app_context():
            return app_module.Document.query.first().id

    def test_patient_can_download_own_document(self, client, make_user):
        make_user('patient@example.com', 'password123', role='patient')
        doc_id = self._upload_one(client)

        resp = client.get(f'/documents/{doc_id}/download')
        assert resp.status_code == 200
        assert resp.data == b'%PDF-1.4 fake content'
        assert 'attachment' in resp.headers.get('Content-Disposition', '')
        assert 'report.pdf' in resp.headers.get('Content-Disposition', '')

    def test_other_patient_cannot_download(self, client, make_user):
        make_user('patient@example.com', 'password123', role='patient')
        make_user('otherpatient@example.com', 'password123', role='patient')
        doc_id = self._upload_one(client)

        client.post('/logout', follow_redirects=True)
        login(client, 'otherpatient@example.com', 'password123')
        resp = client.get(f'/documents/{doc_id}/download', follow_redirects=False)
        assert resp.status_code == 302  # redirected away, not served the file

    def test_treating_doctor_can_download(self, client, make_user):
        patient_id = make_user('patient@example.com', 'password123', role='patient')
        doctor_id = make_user('doc@example.com', 'password123', role='doctor', status='approved')
        doc_id = self._upload_one(client)

        with app_module.app.app_context():
            appt = app_module.Appointment(
                patient_id=patient_id, doctor_id=doctor_id,
                appointment_date='2026-08-01', appointment_time='10:00',
                phone_number='555', status='completed'
            )
            app_module.db.session.add(appt)
            app_module.db.session.commit()

        client.post('/logout', follow_redirects=True)
        login(client, 'doc@example.com', 'password123')
        resp = client.get(f'/documents/{doc_id}/download')
        assert resp.status_code == 200
        assert resp.data == b'%PDF-1.4 fake content'

    def test_unrelated_doctor_cannot_download(self, client, make_user):
        make_user('patient@example.com', 'password123', role='patient')
        make_user('otherdoc@example.com', 'password123', role='doctor', status='approved')
        doc_id = self._upload_one(client)

        client.post('/logout', follow_redirects=True)
        login(client, 'otherdoc@example.com', 'password123')
        resp = client.get(f'/documents/{doc_id}/download', follow_redirects=False)
        assert resp.status_code == 302  # redirected away, not served the file


class TestDelete:
    def test_patient_can_delete_own_document(self, client, make_user):
        make_user('patient@example.com', 'password123', role='patient')
        login(client, 'patient@example.com', 'password123')
        client.post('/documents/upload', data={'document': _pdf_file()}, content_type='multipart/form-data', follow_redirects=True)
        with app_module.app.app_context():
            doc_id = app_module.Document.query.first().id

        client.post(f'/documents/{doc_id}/delete', follow_redirects=True)

        with app_module.app.app_context():
            assert app_module.Document.query.count() == 0

    def test_other_patient_cannot_delete(self, client, make_user):
        make_user('patient@example.com', 'password123', role='patient')
        make_user('otherpatient@example.com', 'password123', role='patient')
        login(client, 'patient@example.com', 'password123')
        client.post('/documents/upload', data={'document': _pdf_file()}, content_type='multipart/form-data', follow_redirects=True)
        with app_module.app.app_context():
            doc_id = app_module.Document.query.first().id

        client.post('/logout', follow_redirects=True)
        login(client, 'otherpatient@example.com', 'password123')
        client.post(f'/documents/{doc_id}/delete', follow_redirects=True)

        with app_module.app.app_context():
            assert app_module.Document.query.count() == 1  # unchanged
