import concurrent.futures
import hashlib
import hmac
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import unittest
import uuid

from tools.pc_compare import make_server
from test_pc_test import SECRET, PCM, wire


class CompareTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        (root / 'catalog.json').write_text(json.dumps({'default_voice_id': 'a', 'voices': [
            {'id': 'a', 'name': '测试音色', 'language': 'zh', 'reference_text': 'private-reference-content'}]}), encoding='utf-8')
        self.config = root / 'settings.json'
        self.config.write_text(json.dumps({'secret': SECRET, 'catalog_path': str(root / 'catalog.json'),
                                           'model_dir': 'private-model-path'}), encoding='utf-8')
        self.observed = {'foreign': [], 'domestic': []}
        self.upstreams = {}
        self.threads = []
        for side in ('foreign', 'domestic'):
            observed = self.observed[side]

            def handler_factory(identity, records):
                class Upstream(BaseHTTPRequestHandler):
                    def log_message(self, *args):
                        pass

                    def do_GET(self):
                        records.append(('GET', self.path))
                        body = json.dumps({'ready': True, 'active': 0, 'runtime': {'target': identity}}).encode()
                        self.send_response(200)
                        self.send_header('Content-Length', str(len(body)))
                        self.end_headers()
                        self.wfile.write(body)

                    def do_POST(self):
                        raw = self.rfile.read(int(self.headers['Content-Length']))
                        message = '\n'.join(['POST', self.path, self.headers['X-Hht-Timestamp'], self.headers['X-Hht-Nonce'],
                                             hashlib.sha256(raw).hexdigest()])
                        valid = hmac.compare_digest(self.headers['X-Hht-Signature'],
                                                    hmac.new(SECRET.encode(), message.encode(), hashlib.sha256).hexdigest())
                        records.append(('POST', self.path, json.loads(raw), valid))
                        if not valid:
                            self.send_error(403)
                            return
                        data = wire()
                        self.send_response(200)
                        self.send_header('Content-Type', 'application/vnd.yilian.xiaoya-voice')
                        self.send_header('Content-Length', str(len(data)))
                        self.send_header('Connection', 'close')
                        self.end_headers()
                        self.wfile.write(data)
                return Upstream

            upstream = ThreadingHTTPServer(('127.0.0.1', 0), handler_factory(side, observed))
            self.upstreams[side] = upstream
            thread = threading.Thread(target=upstream.serve_forever, kwargs={'poll_interval': .02}, daemon=True)
            thread.start()
            self.threads.append(thread)
        self.server = make_server(self.config, 0, **{side: 'http://127.0.0.1:' + str(upstream.server_address[1])
                                                    for side, upstream in self.upstreams.items()})
        self.worker = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': .02}, daemon=True)
        self.worker.start()
        self.port = self.server.server_address[1]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.worker.join(2)
        for upstream in self.upstreams.values():
            upstream.shutdown()
            upstream.server_close()
        for thread in self.threads:
            thread.join(2)
        self.temp.cleanup()

    def request(self, path, body=None, token=None, headers=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=3)
        sent_headers = dict(headers or {})
        if body is not None:
            sent_headers['Content-Type'] = 'application/json'
        if token is not None:
            sent_headers['X-Voice-Test-Token'] = token
        connection.request('POST' if body is not None else 'GET', path,
                           None if body is None else json.dumps(body).encode(), sent_headers)
        response = connection.getresponse()
        data = response.read()
        result = response.status, data
        connection.close()
        return result

    def test_two_fixed_sides_and_shared_voice_list_do_not_expose_private_config(self):
        status, raw = self.request('/api/config')
        self.assertEqual(status, 200)
        config = json.loads(raw)
        self.assertEqual([item['id'] for item in config['backends']], ['foreign', 'domestic'])
        self.assertEqual(config['default_voice_id'], 'a')
        self.assertNotEqual(config['backends'][0]['token'], config['backends'][1]['token'])
        self.assertTrue(config['comparison'])
        for path in ('/', '/api/config'):
            status, raw = self.request(path)
            self.assertEqual(status, 200)
            for value in (SECRET.encode(), b'private-reference-content', b'private-model-path'):
                self.assertNotIn(value, raw)

    def test_health_routes_reach_the_correct_fixed_upstream(self):
        for side in ('foreign', 'domestic'):
            status, raw = self.request('/api/' + side + '/health')
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(raw)['runtime']['target'], side)
            self.assertEqual(self.observed[side], [('GET', '/health')])

    def test_two_simultaneous_runs_keep_signatures_and_jobs_independent(self):
        identity = uuid.uuid4().hex
        body = {'run_id': identity, 'text': '相同文字两侧独立合成', 'voice_id': 'a', 'mode': 'fresh'}
        def generate(side):
            return self.request('/api/' + side + '/synthesize', body, self.server.backends[side].token)
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(generate, ('foreign', 'domestic')))
        for side, (status, raw) in zip(('foreign', 'domestic'), results):
            self.assertEqual(status, 200)
            events = [json.loads(line) for line in raw.splitlines()]
            self.assertEqual(events[0]['endpoint'], self.server.backends[side].upstream)
            self.assertEqual(events[-1]['type'], 'done')
            self.assertEqual(events[-1]['pcm_sha256'], hashlib.sha256(PCM * 2).hexdigest())
            self.assertTrue(self.observed[side][-1][3])
            self.assertEqual(self.observed[side][-1][2]['text'], body['text'])
        self.assertIsNot(self.server.backends['foreign'].jobs, self.server.backends['domestic'].jobs)
        self.assertNotEqual(self.server.backends['foreign'].jobs.items[identity].uid,
                            self.server.backends['domestic'].jobs.items[identity].uid)

    def test_cross_side_tokens_and_client_upstream_override_are_rejected(self):
        body = {'run_id': uuid.uuid4().hex, 'text': 'test', 'voice_id': 'a', 'mode': 'fresh'}
        status, _ = self.request('/api/domestic/synthesize', body, self.server.backends['foreign'].token)
        self.assertEqual(status, 403)
        for key, value in (('url', 'https://attacker.invalid'), ('user_id', 1), ('endpoint', 'https://attacker.invalid')):
            status, _ = self.request('/api/foreign/synthesize', dict(body, **{key: value}), self.server.backends['foreign'].token)
            self.assertEqual(status, 400)
        self.assertEqual(self.observed, {'foreign': [], 'domestic': []})

    def test_cancel_only_affects_chosen_side(self):
        identity = uuid.uuid4().hex
        jobs = {side: self.server.backends[side].jobs.begin(identity, 'test', 'a', 'fresh', None)
                for side in ('foreign', 'domestic')}
        status, _ = self.request('/api/foreign/cancel', {'run_id': identity}, self.server.backends['foreign'].token)
        self.assertEqual(status, 200)
        self.assertTrue(jobs['foreign'].cancelled.is_set())
        self.assertFalse(jobs['domestic'].cancelled.is_set())

    def test_rebinding_cross_origin_and_unknown_routes_stay_blocked(self):
        self.assertEqual(self.request('/api/config', headers={'Host': 'attacker.invalid'})[0], 421)
        self.assertEqual(self.request('/api/config', headers={'Origin': 'https://attacker.invalid'})[0], 403)
        for path in ('/api/unknown/health', '/api/foreign/health?url=bad', '/api/config.json', '/api/health'):
            self.assertEqual(self.request(path)[0], 404)
        self.assertEqual(self.request('/api/domestic/health', {'run_id': uuid.uuid4().hex})[0], 404)


if __name__ == '__main__':
    unittest.main()
