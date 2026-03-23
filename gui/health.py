"""Minimal health-check HTTP server for GUI smoke tests."""

import http.server
import json
import threading

HEALTH_PORT = 19876


def start_health_server(api):
    """Start a health endpoint on localhost:HEALTH_PORT in a daemon thread."""

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            status = api.get_status()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(status).encode())

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", HEALTH_PORT), Handler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return server
