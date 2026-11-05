from concurrent.futures import ThreadPoolExecutor
import hashlib
import hmac
import io
import time
import threading
import unittest
from unittest.mock import patch
import uuid
import wave

from fastapi.testclient import TestClient

from server import create_app
from test_server import FakeEngine, SECRET


def wav(rate):
    target = io.BytesIO()
    with wave.open(target, 'wb') as audio:
        audio.setparams((1, 2, rate, 0, 'NONE', 'not compressed'))
        audio.writeframes(b'\x00\x01' * 3200)
    return target.getvalue()


class FakeRecognizer:
    def warm(self):
        self.ready = True

    def recognize(self, samples):
        assert self.ready and len(samples) == 3200
        return '查询工单。'


class SignedRecognitionTests(unittest.TestCase):
    target = '/v1/transcribe?user_id=10133'

    def headers(self, raw):
        stamp, nonce = str(int(time.time())), uuid.uuid4().hex
        message = '\n'.join(('POST', self.target, stamp, nonce, hashlib.sha256(raw).hexdigest()))
        return {'x-hht-timestamp': stamp, 'x-hht-nonce': nonce,
                'x-hht-signature': hmac.new(SECRET.encode(), message.encode(), hashlib.sha256).hexdigest()}

    def test_signed_wav_returns_text_and_replay_is_rejected(self):
        with TestClient(create_app({'secret': SECRET}, FakeEngine(), FakeRecognizer())) as client:
            raw = wav(16000)
            headers = self.headers(raw)
            self.assertEqual(client.post(self.target, content=raw).status_code, 403)
            self.assertEqual(client.post(self.target, content=raw, headers=headers).json(), {'text': '查询工单。'})
            self.assertEqual(client.post(self.target, content=raw, headers=headers).status_code, 403)
            for bad in (wav(24000), raw[:-2], b'RIFF'):
                self.assertEqual(client.post(self.target, content=bad, headers=self.headers(bad)).status_code, 400)

    def test_optional_model_is_disabled_without_config(self):
        with TestClient(create_app({'secret': SECRET}, FakeEngine())) as client:
            raw = wav(16000)
            self.assertFalse(client.get('/health').json()['asr'])
            self.assertEqual(client.post(self.target, content=raw, headers=self.headers(raw)).status_code, 503)

    def test_two_recognizers_admit_twenty_and_reject_the_next(self):
        release = threading.Event()
        raw = wav(16000)

        class HeldRecognizer(FakeRecognizer):
            def recognize(self, samples):
                if not release.wait(8):
                    raise RuntimeError('test did not release inference')
                return super().recognize(samples)

        config = {'secret': SECRET, 'deployment_target': 'amd-4vcpu-12gb', 'asr_model_dir': 'unused'}
        with patch('server.VoiceRecognizer', side_effect=lambda _: HeldRecognizer()):
            app = create_app(config, FakeEngine())
        with TestClient(app) as client, ThreadPoolExecutor(max_workers=20) as submitters:
            jobs = [submitters.submit(client.post, self.target, content=raw, headers=self.headers(raw))
                    for _ in range(20)]
            try:
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    capacity = client.get('/health').json()['asr_capacity']
                    if capacity['active'] == 20 and capacity['running'] == 2:
                        break
                    time.sleep(.02)
                self.assertEqual(capacity, {'workers': 2, 'limit': 20, 'active': 20, 'running': 2})
                self.assertEqual(client.post(self.target, content=raw, headers=self.headers(raw)).status_code, 429)
            finally:
                release.set()
            for job in jobs:
                response = job.result(timeout=8)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json(), {'text': '查询工单。'})
            self.assertEqual(client.get('/health').json()['asr_capacity']['active'], 0)
