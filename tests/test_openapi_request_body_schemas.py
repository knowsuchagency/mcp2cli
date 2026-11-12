"""Request bodies composed with allOf, or shaped without top-level properties."""

import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from mcp2cli import extract_openapi_commands


@pytest.fixture
def body_endpoint():
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"null")
            payload = json.dumps({"body": body}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _spec(server_url, body_schema):
    return {
        "openapi": "3.0.4",
        "info": {"title": "Bodies", "version": "1"},
        "servers": [{"url": server_url}],
        "paths": {
            "/pets": {
                "post": {
                    "operationId": "createPet",
                    "requestBody": {
                        "content": {"application/json": {"schema": body_schema}}
                    },
                    "responses": {"200": {"description": "Echoed body"}},
                }
            }
        },
        "components": {
            "schemas": {
                "Base": {
                    "type": "object",
                    "required": ["tag"],
                    "properties": {"tag": {"type": "string"}},
                },
            }
        },
    }


ALL_OF_BODY = {
    "allOf": [
        {"$ref": "#/components/schemas/Base"},
        {
            "type": "object",
            "required": ["name"],
            "properties": {
                "name": {"type": "string"},
                "age": {"type": "integer"},
            },
        },
    ]
}


def _run(tmp_path, spec, *argv, stdin=None):
    spec_path = tmp_path / "openapi.json"
    spec_path.write_text(json.dumps(spec), encoding="utf-8")
    env = {**os.environ, "MCP2CLI_CACHE_DIR": str(tmp_path / "cache")}
    return subprocess.run(
        [sys.executable, "-m", "mcp2cli", "--spec", str(spec_path), "--json", *argv],
        input=stdin,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=15,
        env=env,
    )


def test_all_of_branches_become_flags():
    from mcp2cli import resolve_refs

    spec = resolve_refs(_spec("http://unused", ALL_OF_BODY))
    [cmd] = extract_openapi_commands(spec)
    params = {p.name: p for p in cmd.params}
    assert set(params) == {"tag", "name", "age"}
    assert params["tag"].required and params["name"].required
    assert not params["age"].required
    assert params["age"].python_type is int
    assert cmd.has_body


def test_all_of_body_sent_from_flags(tmp_path, body_endpoint):
    result = _run(
        tmp_path,
        _spec(body_endpoint, ALL_OF_BODY),
        "create-pet", "--name", "rex", "--tag", "dog", "--age", "3",
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["body"] == {"tag": "dog", "name": "rex", "age": 3}


def test_all_of_body_sent_from_stdin(tmp_path, body_endpoint):
    result = _run(
        tmp_path,
        _spec(body_endpoint, ALL_OF_BODY),
        "create-pet", "--stdin",
        stdin='{"name": "rex", "tag": "dog"}',
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["body"] == {"name": "rex", "tag": "dog"}


@pytest.mark.parametrize(
    "body_schema,payload",
    [
        (
            {
                "oneOf": [
                    {"type": "object", "properties": {"cat": {"type": "string"}}},
                    {"type": "object", "properties": {"dog": {"type": "string"}}},
                ]
            },
            {"dog": "rex"},
        ),
        ({"type": "array", "items": {"type": "string"}}, ["rex", "tom"]),
    ],
)
def test_body_without_properties_still_accepts_json(
    tmp_path, body_endpoint, body_schema, payload
):
    result = _run(
        tmp_path,
        _spec(body_endpoint, body_schema),
        "create-pet", "--req-json", json.dumps(payload),
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["body"] == payload


def test_all_of_property_keeps_its_type():
    from mcp2cli import resolve_refs

    body = {
        "type": "object",
        "properties": {
            "owner": {
                "description": "Who owns it",
                "allOf": [{"$ref": "#/components/schemas/Base"}],
            }
        },
    }
    [cmd] = extract_openapi_commands(resolve_refs(_spec("http://unused", body)))
    [owner] = cmd.params
    assert owner.description == "Who owns it (JSON object)"
    assert owner.schema["type"] == "object"
