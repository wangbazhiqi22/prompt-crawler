"""Public distribution: fresh state, access control and portable adapters."""
import os
import io
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import local_settings
import media_services


class DistributionTests(unittest.TestCase):
    def test_unique_persistent_session_secret(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(local_settings, 'DATA', Path(tmp)), \
                patch.dict(os.environ, {}, clear=True):
            first = local_settings.session_secret()
            self.assertEqual(first, local_settings.session_secret())
            self.assertGreaterEqual(len(first), 64)
            (Path(tmp) / 'session-secret.txt').unlink()
            self.assertNotEqual(first, local_settings.session_secret())

    def test_random_admin_password_and_config_validation(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(local_settings, 'DATA', Path(tmp)), \
                patch.dict(os.environ, {}, clear=True):
            # English Windows runners use cp1252; startup must not crash on a log message.
            with io.TextIOWrapper(io.BytesIO(), encoding='cp1252') as output, patch('sys.stdout', output):
                password = local_settings.initial_admin_password()
            self.assertGreaterEqual(len(password), 20)
            self.assertIn(password, (Path(tmp) / 'first-run-admin.txt').read_text())
            with patch.dict(os.environ, {'PROMPT_CRAWLER_ADMIN_PASSWORD': 'admin'}):
                with self.assertRaises(ValueError):
                    local_settings.initial_admin_password()

    def test_fresh_application_authentication_and_pages(self):
        script = r'''
import tempfile
from pathlib import Path
import config, local_settings
with tempfile.TemporaryDirectory() as tmp:
    config.DB_PATH = str(Path(tmp) / 'prompts.db')
    local_settings.DATA = Path(tmp)
    import run_server_with_extensions as runtime
    app = runtime.server.app
    client = app.test_client()
    assert client.get('/').status_code == 302
    assert client.get('/api/ai/status').status_code == 401
    assert client.get('/login').status_code == 200
    assert client.post('/api/login', json={'username':'admin','password':'admin'}).status_code == 401
    credentials = (Path(tmp) / 'first-run-admin.txt').read_text()
    password = credentials.split('Password: ')[1].splitlines()[0]
    assert client.post('/api/login', json={'username':'admin','password':password}).status_code == 200
    for path in ['/', '/api/ai/status', '/static/ambient/scene.js']:
        response = client.get(path)
        assert response.status_code == 200, (path, response.status_code)
    for path in ['/workspace', '/browse', '/douyin']:
        response = client.get(path)
        assert response.status_code == 302 and response.headers['Location'] == '/'
    from database import get_conn
    conn = get_conn()
    assert conn.execute('SELECT COUNT(*) FROM items').fetchone()[0] == 0
    assert conn.execute('SELECT COUNT(*) FROM users').fetchone()[0] == 1
    conn.close()
    assert client.get('/logout').status_code == 302
    assert client.get('/api/ai/status').status_code == 401
'''
        environment = dict(os.environ)
        environment.pop('PROMPT_CRAWLER_ADMIN_PASSWORD', None)
        result = subprocess.run([sys.executable, '-X', 'utf8', '-c', script],
            cwd=Path(__file__).resolve().parents[1], env=environment,
            capture_output=True, text=True, encoding='utf-8', timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_resolved_download_rejects_error_page_without_partial_file(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(media_services, 'ROOT', Path(tmp)):
            response = MagicMock()
            response.headers = {'Content-Type': 'text/html'}
            response.__enter__.return_value = response
            with patch.object(media_services.requests, 'get', return_value=response):
                with self.assertRaises(RuntimeError):
                    media_services.MultiPlatformDownloadService().download_douyin_from_resolved_media(
                        'https://www.douyin.com/video/1', 'https://example.com/video.mp4')
            self.assertEqual(list(Path(tmp).rglob('*.mp4')), [])
            self.assertEqual(list(Path(tmp).rglob('*.part')), [])

    def test_resolved_download_is_written_atomically(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(media_services, 'ROOT', Path(tmp)):
            response = MagicMock()
            response.headers = {'Content-Type': 'video/mp4'}
            response.iter_content.return_value = [b'video bytes']
            response.__enter__.return_value = response
            with patch.object(media_services.requests, 'get', return_value=response):
                result = media_services.MultiPlatformDownloadService().download_douyin_from_resolved_media(
                    'https://www.douyin.com/video/1', 'https://example.com/video.mp4')
            self.assertEqual(Path(result.local_path).read_bytes(), b'video bytes')
            self.assertEqual(list(Path(tmp).rglob('*.part')), [])

    def test_asr_adapter_preserves_timecodes_and_converts_to_simplified(self):
        model = MagicMock()
        model.transcribe.return_value = (iter([
            SimpleNamespace(start=1.25, end=2.5, text=' 雖然我們都知道 ')]), None)
        with tempfile.TemporaryDirectory() as tmp, patch.object(media_services, 'ROOT', Path(tmp)), \
                patch.object(media_services.subprocess, 'run') as ffmpeg, \
                patch('faster_whisper.WhisperModel', return_value=model):
            prepared, trans = media_services.AudioTranscriptionService().transcribe_source(Path(tmp) / 'input.mp4')
            self.assertEqual(trans.text, '虽然我们都知道')
            self.assertEqual(trans.segments[0].start_ms, 1250)
            self.assertIn('00:00:01,250 --> 00:00:02,500', trans.srt_text)
            self.assertTrue(prepared.audio_path.endswith('_asr.wav'))
            self.assertIn('16000', ffmpeg.call_args.args[0])


if __name__ == '__main__':
    unittest.main()
