"""Loopback-only, fixed-origin comparison of the foreign and domestic voice servers. --yyl"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re

try:
    from .pc_test import Handler, PCServer, Settings, TestError
except ImportError:
    from pc_test import Handler, PCServer, Settings, TestError


PAGE = Path(__file__).with_name('pc_compare.html')
FOREIGN = 'https://voice.6xmu.com'
DOMESTIC = 'https://voice.sh-yilian.com'
SIDES = ('foreign', 'domestic')
LABELS = {'foreign': '国外服务器', 'domestic': '国内服务器'}
ROUTE = re.compile(r'/api/(foreign|domestic)/(health|synthesize|cancel)')


class CompareHandler(Handler):
    """Keep the original signing, wire validation, cancellation and origin checks. --yyl"""

    @property
    def settings(self):
        return self.server.backends[getattr(self, 'backend_id', 'foreign')]

    def route(self, method):
        match = ROUTE.fullmatch(self.path)
        if match is None:
            return False
        side, action = match.groups()
        if (method == 'GET' and action != 'health') or (method == 'POST' and action == 'health'):
            return False
        self.backend_id = side
        self.path = '/api/' + action
        return True

    def do_GET(self):
        if self.route('GET'):
            return super().do_GET()
        try:
            self.safe_request()
            if self.path == '/':
                data = PAGE.read_bytes()
                self.send_headers(200, 'text/html; charset=utf-8', len(data))
                self.wfile.write(data)
            elif self.path == '/api/config':
                shared = self.settings.public()
                shared.pop('endpoint')
                shared.pop('token')
                if 'female-02' in self.settings.voices:
                    shared['default_voice_id'] = 'female-02'
                shared['backends'] = [dict(id=side, name=LABELS[side], endpoint=self.server.backends[side].upstream,
                                          token=self.server.backends[side].token) for side in SIDES]
                shared['comparison'] = True
                self.json(200, shared)
            else:
                self.json(404, {'error': 'Not found'})
        except TestError as error:
            self.json(error.status, {'error': str(error)})
        except OSError:
            self.json(500, {'error': '读取本机对比页面失败'})

    def do_POST(self):
        if self.route('POST'):
            return super().do_POST()
        try:
            self.safe_request()
            self.json(404, {'error': 'Not found'})
        except TestError as error:
            self.json(error.status, {'error': str(error)})


def make_server(config, port=8798, foreign=FOREIGN, domestic=DOMESTIC):
    backends = {'foreign': Settings(Path(config), foreign), 'domestic': Settings(Path(config), domestic)}
    if backends['foreign'].voices != backends['domestic'].voices:
        raise TestError('两侧必须使用同一份测试音色目录')
    server = PCServer(('127.0.0.1', port), CompareHandler)
    server.backends = backends
    return server


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--port', type=int, default=8798)
    parser.add_argument('--foreign-upstream', default=FOREIGN)
    parser.add_argument('--domestic-upstream', default=DOMESTIC)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error('Invalid loopback port')
    server = make_server(args.config, args.port, args.foreign_upstream, args.domestic_upstream)
    print(json.dumps({'listening': 'http://127.0.0.1:' + str(args.port), 'backends': [
        {'id': side, 'endpoint': server.backends[side].upstream} for side in SIDES],
        'voices': len(server.backends['foreign'].voices), 'signing_material_exposed': False}), flush=True)
    try:
        server.serve_forever(poll_interval=.25)
    except KeyboardInterrupt:
        pass
    finally:
        for settings in server.backends.values():
            for job in list(settings.jobs.items.values()):
                if job.state == 'running':
                    job.stop()
        server.server_close()


if __name__ == '__main__':
    main()
