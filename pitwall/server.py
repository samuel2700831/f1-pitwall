"""Single-process local web server; Python standard library, no install step."""
from __future__ import annotations

import argparse
import json
import mimetypes
import os
import re
import secrets
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit,parse_qs

from .data import ROOT, load_timeline
from .service import Conflict, PitwallService

WEB = ROOT / 'web'


def handler_for(service):
    class Handler(BaseHTTPRequestHandler):
        server_version = 'Pitwall/1.0'

        def log_message(self, fmt, *args):
            # Paths contain no credentials; request bodies and cookies stay private.
            super().log_message(fmt, *args)

        def owner(self):
            cookie = SimpleCookie()
            try:
                cookie.load(self.headers.get('Cookie', ''))
            except Exception:
                pass
            value = cookie.get('pitwall_session')
            if value and re.fullmatch(r'[a-f0-9]{64}', value.value):
                return value.value
            self.new_cookie = secrets.token_hex(32)
            return self.new_cookie

        def host_allowed(self):
            if self.server.server_address[0] not in ('127.0.0.1', '::1'):
                return True
            try:
                return urlsplit('http://' + self.headers.get('Host', '')).hostname in ('localhost', '127.0.0.1', '::1')
            except ValueError:
                return False

        def send(self, status, data, content_type='application/json; charset=utf-8', download=False):
            body = json.dumps(data, allow_nan=False).encode() if content_type.startswith('application/json') else data
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'same-origin')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
            if getattr(self, 'new_cookie', None):
                self.send_header('Set-Cookie', 'pitwall_session=' + self.new_cookie + '; HttpOnly; SameSite=Strict; Path=/; Max-Age=31536000')
            if download:
                self.send_header('Content-Disposition', 'attachment; filename="pitwall-run.json"')
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            self.new_cookie = None
            if not self.host_allowed():
                return self.send(421, {'error': 'Use the local application URL.'})
            path = urlsplit(self.path).path
            try:
                if path == '/health':
                    return self.send(200, {'status': 'ok'})
                if path.startswith('/api/'):
                    owner = self.owner()
                    if path == '/api/bootstrap':
                        return self.send(200, {'meta': service.meta(), 'runs': service.list_runs(owner)})
                    test=re.fullmatch(r'/api/agent-tests/([A-Za-z0-9_-]+)(/export)?',path)
                    if test:
                        return self.send(200,service.get_agent_test(owner,test[1]),download=bool(test[2]))
                    preview=re.fullmatch(r'/api/runs/([A-Za-z0-9_-]+)/preview',path)
                    if preview:
                        query=parse_qs(urlsplit(self.path).query)
                        wing=query.get('repair_wing',['true'])[0]
                        if wing not in ('true','false'):raise ValueError('Invalid repair option')
                        return self.send(200,service.preview(owner,preview[1],query.get('compound',['MEDIUM'])[0],wing=='true'))
                    match = re.fullmatch(r'/api/runs/([A-Za-z0-9_-]+)(/export)?', path)
                    if match:
                        if match[2]:
                            return self.send(200, service.export(owner, match[1]), download=True)
                        return self.send(200, service.get(owner, match[1]))
                    raise KeyError('Endpoint not found')
                assets = {'/': 'index.html', '/index.html': 'index.html', '/app.js': 'app.js',
                          '/styles.css': 'styles.css', '/favicon.svg': 'favicon.svg'}
                if path not in assets:
                    raise KeyError('Page not found')
                target = WEB / assets[path]
                content_type = mimetypes.guess_type(str(target))[0] or 'application/octet-stream'
                return self.send(200, target.read_bytes(), content_type + '; charset=utf-8')
            except ValueError as error:
                self.send(400,{'error':str(error)})
            except KeyError as error:
                self.send(404, {'error': str(error.args[0])})
            except Exception:
                self.send(500, {'error': 'The request could not finish. Refresh and try again.'})

        def do_POST(self):
            self.new_cookie = None
            if not self.host_allowed():
                return self.send(421, {'error': 'Use the local application URL.'})
            try:
                origin = self.headers.get('Origin')
                if self.headers.get('Sec-Fetch-Site') == 'cross-site' or (origin and urlsplit(origin).netloc != self.headers.get('Host')):
                    return self.send(403, {'error': 'Use the application in the same browser origin.'})
                if self.headers.get_content_type() != 'application/json':
                    return self.send(415, {'error': 'Expected application/json'})
                size = int(self.headers.get('Content-Length', '0'))
                if not 1 <= size <= 16000:
                    return self.send(413, {'error': 'Request must be between 1 and 16000 bytes'})
                data = json.loads(self.rfile.read(size))
                if not isinstance(data, dict):
                    raise ValueError('Expected a JSON object')
                owner = self.owner()
                path = urlsplit(self.path).path
                request_id = data.get('request_id')
                if path == '/api/runs':
                    return self.send(201, service.create(owner, data.get('lap'), request_id, data.get('label', '')))
                test=re.fullmatch(r'/api/runs/([A-Za-z0-9_-]+)/test',path)
                if test:
                    return self.send(202,service.test_agents(owner,test[1],request_id,data.get('expected_version'),
                                     data.get('case','current'),data.get('target','all'),data.get('mode','local')))
                match = re.fullmatch(r'/api/runs/([A-Za-z0-9_-]+)/(jump|remember|assess|decision|review|incident|mode|playback)', path)
                if not match:
                    raise KeyError('Endpoint not found')
                run_id, action = match.groups(); version = data.get('expected_version')
                if action == 'jump':
                    result = service.jump(owner, run_id, data.get('lap'), request_id, version, data.get('finish', False), data.get('monitor',False), data.get('refresh',True))
                elif action == 'remember':
                    result = service.remember(owner, run_id, data.get('text'), request_id, version)
                elif action == 'assess':
                    result = service.assess(owner, run_id, data.get('mode', 'local'), request_id, version,data.get('compound'),data.get('repair_wing',True))
                elif action == 'playback':
                    result = service.playback(owner,run_id,data.get('action'),data.get('speed',40),request_id,version)
                elif action == 'mode':
                    result = service.configure_mode(owner, run_id, data.get('mode'), request_id, version)
                elif action == 'incident':
                    result = service.incident(owner, run_id, data.get('type'), request_id, version)
                elif action == 'review':
                    result = service.request_review(owner, run_id, request_id, version)
                else:
                    result = service.decide(owner, run_id, data.get('choice'), data.get('compound'),
                                            data.get('rationale') or 'Confirmed by the strategy director.', request_id, version, data.get('repair_wing',True))
                self.send(200, result)
            except (ValueError, TypeError) as error:
                status = 409 if isinstance(error, Conflict) else 400
                self.send(status, {'error': str(error) if isinstance(error, ValueError) else 'Invalid request fields'})
            except KeyError as error:
                self.send(404, {'error': str(error.args[0])})
            except Exception:
                self.send(500, {'error': 'Action could not finish. Refresh before retrying; your last complete state is saved.'})

    return Handler


def make_server(service, host='127.0.0.1', port=8001):
    server = ThreadingHTTPServer((host, port), handler_for(service))
    server.daemon_threads = True
    return server


def main():
    parser = argparse.ArgumentParser(description='Run the F1 Pitwall web demo')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=int(os.environ.get('PORT', 8001)))
    parser.add_argument('--database', type=Path, default=ROOT / '.local' / 'austin-web.sqlite3')
    parser.add_argument('--open', action='store_true', help='Open the local demo in your browser')
    args = parser.parse_args()
    try:
        project=json.loads((ROOT/'project.json').read_text())
        timeline=load_timeline(ROOT/'data'/project.get('data_bundle','austin-2025-demo.json'))
    except FileNotFoundError:
        parser.exit(1, 'Race data is missing. Run: python3 -m pitwall.import_race\n')
    service = PitwallService(args.database, timeline, autoplay=True)
    server = make_server(service, args.host, args.port)
    print('F1 Pitwall ready: http://' + args.host + ':' + str(server.server_port), flush=True)
    if args.open:
        import webbrowser
        webbrowser.open('http://127.0.0.1:' + str(server.server_port))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close(); service.close()


if __name__ == '__main__':
    main()
