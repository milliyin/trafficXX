"""User-run temporary Web Preview cookie probe; no Azure/Grafana credentials."""
import json
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, HTTPServer


class Probe(BaseHTTPRequestHandler):
    def log_message(self, *args):
        # Do not log request paths, headers, cookies or relay session identifiers.
        pass

    def do_GET(self):
        if self.path.split('?', 1)[0].rstrip('/').endswith('/check'):
            cookies = SimpleCookie()
            try:
                cookies.load(self.headers.get('Cookie', ''))
            except CookieError:
                pass
            present = (cookies.get('trafficxx_preview_probe') is not None
                       and cookies['trafficxx_preview_probe'].value == 'dummy')
            js_present = (cookies.get('trafficxx_preview_js_probe') is not None
                          and cookies['trafficxx_preview_js_probe'].value == 'dummy')
            body = json.dumps({'result': 'COOKIE_FORWARDED' if present
                               else 'COOKIE_MISSING_AT_PROBE',
                               'server_cookie_received': present,
                               'browser_cookie_received': js_present}).encode()
            content_type = 'application/json'
        else:
            body = b'''<!doctype html><meta charset="utf-8">
<title>trafficXX preview cookie test</title>
<h1>Preview cookie test</h1><pre id="result">Checking...</pre>
<script>
document.cookie = 'trafficxx_preview_js_probe=dummy; Path=/; Max-Age=120; Secure; SameSite=None';
const browserStored = document.cookie.split(';').some(
 entry => entry.trim() === 'trafficxx_preview_js_probe=dummy');
const endpoint = new URL(location.href);
endpoint.search = ''; endpoint.hash = '';
if (!endpoint.pathname.endsWith('/')) endpoint.pathname += '/';
endpoint.pathname += 'check';
fetch(endpoint, {credentials: 'include', cache: 'no-store'})
 .then(r => r.json()).then(value => {
   value.browser_cookie_stored = browserStored;
   document.getElementById('result').textContent = JSON.stringify(value);
 }).catch(() => {
   document.getElementById('result').textContent = 'PROBE_REQUEST_FAILED';
 });
</script>'''
            content_type = 'text/html; charset=utf-8'
        self.send_response(200)
        self.send_header('Content-Type', content_type)
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(body)))
        if content_type.startswith('text/html'):
            self.send_header('Set-Cookie', 'trafficxx_preview_probe=dummy; '
                             'Path=/; Max-Age=120; HttpOnly; Secure; SameSite=None')
        self.end_headers()
        self.wfile.write(body)


if __name__ == '__main__':
    print('Open Cloud Shell Web Preview on port 8092. Ctrl+C stops the probe.', flush=True)
    try:
        with HTTPServer(('0.0.0.0', 8092), Probe) as server:
            server.serve_forever()
    except KeyboardInterrupt:
        pass
