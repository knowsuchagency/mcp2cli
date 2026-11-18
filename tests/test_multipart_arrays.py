"""Multiple document uploads through an OpenAPI-generated CLI."""

import argparse
import json
import subprocess
import sys
import threading
from email import policy
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from mcp2cli import _collect_openapi_params, build_argparse, extract_openapi_commands


def batch_spec(base_url="http://example.test"):
    return {
        "openapi": "3.0.3",
        "info": {"title": "Document ingestion", "version": "1"},
        "servers": [{"url": base_url}],
        "paths": {"/documents": {"post": {
            "operationId": "ingestDocuments",
            "requestBody": {"content": {
                "application/json": {"schema": {"type": "object", "properties": {"url": {"type": "string"}}}},
                "multipart/form-data": {"schema": {
                    "type": "object",
                    "required": ["files"],
                    "properties": {
                        "files": {"type": "array", "items": {"type": "string", "format": "binary"}},
                        "collection": {"type": "string"},
                    },
                }},
            }},
            "responses": {"200": {"description": "Uploaded"}},
        }}},
    }


def test_repeated_file_flags_and_required_array():
    cmd = extract_openapi_commands(batch_spec())[0]
    assert cmd.content_type == "multipart/form-data"
    files = next(p for p in cmd.params if p.original_name == "files")
    assert files.location == "file"
    parser = build_argparse([cmd], argparse.ArgumentParser(add_help=False))
    args = parser.parse_args(["ingest-documents", "--files", "first.md", "--files", "second.md"])
    assert args.files == ["first.md", "second.md"]
    with pytest.raises(SystemExit):
        parser.parse_args(["ingest-documents"])


def test_uploads_two_documents_under_the_same_field(tmp_path):
    received = []
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            data = self.rfile.read(int(self.headers["Content-Length"]))
            message = BytesParser(policy=policy.default).parsebytes(
                ("Content-Type: " + self.headers["Content-Type"] + "\r\n\r\n").encode() + data
            )
            for part in message.iter_parts():
                received.append((
                    part.get_param("name", header="content-disposition"),
                    part.get_filename(),
                    part.get_payload(decode=True),
                ))
            body = json.dumps({"accepted": len(received)}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    spec = tmp_path / "api.json"
    spec.write_text(json.dumps(batch_spec(f"http://127.0.0.1:{server.server_port}")))
    first = tmp_path / "first note.md"
    second = tmp_path / "second,notes.md"
    first.write_bytes(b"Deployment notes")
    second.write_bytes(b"Incident report")
    try:
        result = subprocess.run([
            sys.executable, "-m", "mcp2cli", "--spec", str(spec), "ingest-documents",
            "--files", str(first), "--files", str(second), "--collection", "research",
        ], capture_output=True, text=True, timeout=15)
        assert result.returncode == 0, result.stderr
        assert ("files", first.name, first.read_bytes()) in received
        assert ("files", second.name, second.read_bytes()) in received
        assert ("collection", None, b"research") in received
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_later_missing_file_closes_already_open_documents(tmp_path, monkeypatch):
    import builtins
    first = tmp_path / "first.md"
    first.write_text("Notes")
    opened = []
    original_open = builtins.open
    def tracking_open(*args, **kwargs):
        handle = original_open(*args, **kwargs)
        opened.append(handle)
        return handle
    monkeypatch.setattr(builtins, "open", tracking_open)
    cmd = extract_openapi_commands(batch_spec())[0]
    args = argparse.Namespace(files=[str(first), str(tmp_path / "missing.md")], collection=None, stdin=False)
    with pytest.raises(SystemExit):
        _collect_openapi_params(cmd, args)
    assert opened
    assert all(handle.closed for handle in opened)
