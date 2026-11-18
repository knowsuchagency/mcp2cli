"""OpenAPI download endpoints can redirect to the actual response body."""

import base64
import json
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest


@pytest.fixture
def redirect_endpoint(tmp_path):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.handle_request()

        def do_POST(self):
            self.handle_request()

        def handle_request(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            if self.path in ("/download", "/submit", "/upload", "/other-origin"):
                self.send_response(307 if self.path in ("/submit", "/upload") else 302)
                target = "/archive" if self.path == "/download" else "/echo"
                if self.path == "/other-origin":
                    target = f"http://localhost:{self.server.server_port}/echo"
                self.send_header("Location", target)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            payload = (b"PK\x03\x04\x00\xff\x80\x00" if self.path == "/archive" else json.dumps({
                "method": self.command, "body": body.decode(errors="replace"),
                "body_bytes": base64.b64encode(body).decode(),
                "authorization": self.headers.get("Authorization"),
                "api_key": self.headers.get("X-Api-Key"),
            }).encode())
            self.send_response(200)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    spec = {
        "openapi": "3.0.3", "info": {"title": "Downloads", "version": "1"},
        "servers": [{"url": f"http://127.0.0.1:{server.server_port}"}],
        "paths": {
            "/download": {"get": {"operationId": "downloadArchive", "responses": {}}},
            "/other-origin": {"get": {"operationId": "otherOrigin", "responses": {}}},
            "/upload": {"post": {"operationId": "upload", "requestBody": {"content": {
                "multipart/form-data": {"schema": {"type": "object", "properties": {
                    "file": {"type": "string", "format": "binary"}
                }}}
            }}, "responses": {}}},
            "/submit": {"post": {"operationId": "submit", "requestBody": {"content": {
                "application/json": {"schema": {"type": "object", "properties": {"name": {"type": "string"}}}}
            }}, "responses": {}}},
        },
    }
    path = tmp_path / "spec.json"
    path.write_text(json.dumps(spec))
    yield [sys.executable, "-m", "mcp2cli", "--spec", str(path)]
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


def test_redirected_download_is_opt_in_and_preserves_binary(redirect_endpoint):
    default = subprocess.run(redirect_endpoint + ["--raw", "download-archive"], capture_output=True, timeout=15)
    assert default.returncode == 1
    assert b"Error 302" in default.stderr
    followed = subprocess.run(redirect_endpoint + ["--follow-redirects", "--raw", "download-archive"], capture_output=True, timeout=15)
    assert followed.returncode == 0, followed.stderr
    assert followed.stdout == b"PK\x03\x04\x00\xff\x80\x00"


def test_307_keeps_the_method_and_body(redirect_endpoint):
    result = subprocess.run(redirect_endpoint + ["--follow-redirects", "submit", "--name", "report"], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["method"] == "POST"
    assert json.loads(data["body"]) == {"name": "report"}


def test_cross_origin_redirect_does_not_forward_authorization(redirect_endpoint):
    result = subprocess.run(redirect_endpoint + ["--follow-redirects", "--auth-header", "Authorization:Bearer test-only", "--auth-header", "X-Api-Key:test-key", "other-origin"], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["authorization"] is None
    assert data["api_key"] is None


def test_same_origin_redirect_keeps_custom_auth(redirect_endpoint):
    result = subprocess.run(redirect_endpoint + ["--follow-redirects", "--auth-header", "X-Api-Key:test-key", "submit", "--name", "report"], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["api_key"] == "test-key"


def test_307_replays_file_upload(redirect_endpoint, tmp_path):
    upload = tmp_path / "report.bin"
    payload = b"report\x00\xff\x80"
    upload.write_bytes(payload)
    result = subprocess.run(redirect_endpoint + ["--follow-redirects", "upload", "--file", str(upload)], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["method"] == "POST"
    multipart = base64.b64decode(data["body_bytes"])
    assert payload in multipart
    assert b'filename="report.bin"' in multipart
