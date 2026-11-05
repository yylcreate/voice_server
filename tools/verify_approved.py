"""Check the signed preview, live PCM stream, and optional recognition after release."""
import argparse
import hashlib
import hmac
import http.client
import json
from pathlib import Path
import time
from urllib.parse import urlsplit
import uuid

from pc_test import AGENT, VoiceWire, sign


def request(endpoint, path, body=None, headers=None):
    transport = http.client.HTTPSConnection if endpoint.scheme == 'https' else http.client.HTTPConnection
    conn = transport(endpoint.hostname, endpoint.port, timeout=90)
    conn.request('GET' if body is None else 'POST', path, body, {'User-Agent': AGENT, **(headers or {})})
    return conn, conn.getresponse()


def fetch(endpoint, path, body=None, headers=None):
    conn, response = request(endpoint, path, body, headers)
    try:
        return response.status, response.headers.get_content_type(), response.read()
    finally:
        conn.close()


def signature(secret, path, body, content_type):
    stamp, nonce = str(int(time.time())), uuid.uuid4().hex
    message = '\n'.join(('POST', path, stamp, nonce, hashlib.sha256(body).hexdigest()))
    return {'Content-Type': content_type, 'X-Hht-Timestamp': stamp, 'X-Hht-Nonce': nonce,
            'X-Hht-Signature': hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--url', default='https://voice.6xmu.com')
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--streams', action='store_true')
    parser.add_argument('--asr-wav', type=Path)
    args = parser.parse_args()
    endpoint = urlsplit(args.url)
    if not (args.url == 'https://voice.6xmu.com' or (
            endpoint.scheme == 'http' and endpoint.hostname == '127.0.0.1' and endpoint.port
            and not (endpoint.path or endpoint.query or endpoint.fragment or endpoint.username))):
        parser.error('Use the verified voice HTTPS host or a local test port')
    secret = json.loads(args.config.read_text(encoding='utf-8-sig'))['secret']
    voices = json.loads(args.manifest.read_text(encoding='utf-8'))['voices']
    status, _, body = fetch(endpoint, '/health')
    health = json.loads(body)
    assert status == 200 and health['ready'] and health['voice_count'] == health['preview_count'] == len(voices)
    owner = 1800000000 + uuid.uuid4().int % 100000000
    preview = f'/v1/preview?user_id={owner}'
    body = json.dumps({'voice_id': voices[0]['id']}).encode()
    assert fetch(endpoint, preview, body)[0] == 403
    for row in voices:
        body = json.dumps({'voice_id': row['id']}).encode()
        headers = signature(secret, preview, body, 'application/json')
        started = time.perf_counter()
        status, kind, wav = fetch(endpoint, preview, body, headers)
        assert status == 200 and kind == 'audio/wav' and len(wav) == row['bytes'], row['id']
        assert hashlib.sha256(wav).hexdigest() == row['sha256'], row['id']
        print(json.dumps({'preview': row['id'], 'exact_bytes': len(wav), 'seconds': round(time.perf_counter()-started, 3)}), flush=True)
    assert fetch(endpoint, preview, body, headers)[0] == 403
    for retired in ('female-03', 'lauren'):
        body = json.dumps({'voice_id': retired}).encode()
        assert fetch(endpoint, preview, body, signature(secret, preview, body, 'application/json'))[0] == 404
    if args.streams:
        for row in voices:
            path, body, headers = sign(secret, owner, '请核对工单 A-034425。', row['id'])
            started = time.perf_counter()
            conn, response = request(endpoint, path, body, headers)
            try:
                assert response.status == 200, (row['id'], response.status)
                wire = VoiceWire(row['id'])
                first = None
                for chunk in iter(lambda: response.read1(8192), b''):
                    for event, _ in wire.feed(chunk):
                        if event == 'pcm' and first is None:
                            first = round(time.perf_counter()-started, 3)
                metrics = wire.finish()
                assert not any(metrics['cached'])
                print(json.dumps({'stream': row['id'], 'first_pcm_s': first,
                                  'total_s': round(time.perf_counter()-started, 3), **metrics}), flush=True)
            finally:
                conn.close()
    if args.asr_wav:
        path = f'/v1/transcribe?user_id={owner}'
        body = args.asr_wav.read_bytes()
        status, _, result = fetch(endpoint, path, body, signature(secret, path, body, 'audio/wav'))
        assert status == 200 and json.loads(result)['text'].strip()
        print(json.dumps({'asr': 'pass', 'characters': len(json.loads(result)['text'])}), flush=True)


if __name__ == '__main__':
    main()
