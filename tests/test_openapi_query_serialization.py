"""Query arrays must follow the serialization declared by the OpenAPI endpoint."""

import json
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest


@pytest.fixture
def query_endpoint():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            query = parse_qs(urlsplit(self.path).query, keep_blank_values=True)
            payload = json.dumps(
                {"query": query, "raw_query": urlsplit(self.path).query}
            ).encode()
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


@pytest.mark.parametrize(
    "serialization,value,expected,raw_query",
    [
        ({}, '["blue","black"]', ["blue", "black"], "tags=blue&tags=black"),
        (
            {"style": "form", "explode": True},
            '["blue","black"]',
            ["blue", "black"],
            "tags=blue&tags=black",
        ),
        (
            {"style": "form", "explode": False},
            '["blue","black"]',
            ["blue,black"],
            "tags=blue,black",
        ),
        (
            {"style": "spaceDelimited", "explode": False},
            '["blue","black"]',
            ["blue black"],
            "tags=blue%20black",
        ),
        (
            {"style": "pipeDelimited", "explode": False},
            '["blue","black"]',
            ["blue|black"],
            "tags=blue%7Cblack",
        ),
        (
            {"style": "spaceDelimited"},
            '["blue","black"]',
            ["blue black"],
            "tags=blue%20black",
        ),
        ({"explode": False}, "[true,false,2]", ["true,false,2"], "tags=true,false,2"),
        (
            {"explode": False},
            '["a&b","c+d","雪"]',
            ["a&b,c+d,雪"],
            "tags=a%26b,c%2Bd,%E9%9B%AA",
        ),
        ({"explode": False}, '["a,b","c"]', ["a,b,c"], "tags=a%2Cb,c"),
        ({"explode": False}, "[]", None, ""),
    ],
)
def test_query_array_wire_values(
    tmp_path, query_endpoint, serialization, value, expected, raw_query
):
    spec = {
        "openapi": "3.0.4",
        "info": {"title": "Query arrays", "version": "1"},
        "servers": [{"url": query_endpoint}],
        "paths": {
            "/items": {
                "get": {
                    "operationId": "listItems",
                    "parameters": [
                        {
                            "name": "tags",
                            "in": "query",
                            "schema": {"type": "array", "items": {}},
                            **serialization,
                        }
                    ],
                    "responses": {"200": {"description": "Observed query parameters"}},
                },
            },
        },
    }
    spec_path = tmp_path / "openapi.json"
    spec_path.write_text(json.dumps(spec), encoding="utf-8")
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "mcp2cli",
            "--spec",
            str(spec_path),
            "--json",
            "list-items",
            "--tags",
            value,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["query"].get("tags") == expected
    assert data["raw_query"] == raw_query


def test_query_params_extend_a_query_already_in_the_path(tmp_path, query_endpoint):
    spec = {
        "openapi": "3.0.4",
        "info": {"title": "Fixed query", "version": "1"},
        "servers": [{"url": query_endpoint}],
        "paths": {
            "/items?kind=widget": {
                "get": {
                    "operationId": "listWidgets",
                    "parameters": [
                        {
                            "name": "tags",
                            "in": "query",
                            "explode": False,
                            "schema": {"type": "array", "items": {}},
                        }
                    ],
                    "responses": {"200": {"description": "Observed query parameters"}},
                },
            },
        },
    }
    spec_path = tmp_path / "openapi.json"
    spec_path.write_text(json.dumps(spec), encoding="utf-8")
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "mcp2cli",
            "--spec",
            str(spec_path),
            "--json",
            "list-widgets",
            "--tags",
            '["a","b"]',
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["raw_query"] == "kind=widget&tags=a,b"


@pytest.mark.parametrize(
    "serialization,value,raw_query",
    [
        (
            {"style": "deepObject", "explode": True},
            '{"status":"open","limit":5}',
            "filter[status]=open&filter[limit]=5",
        ),
        (
            {"style": "deepObject"},
            '{"q":"a&b c","active":true,"skip":null}',
            "filter[q]=a%26b%20c&filter[active]=true",
        ),
        ({}, '{"status":"open","limit":5}', "status=open&limit=5"),
        (
            {"style": "form", "explode": False},
            '{"status":"open","limit":5}',
            "filter=status,open,limit,5",
        ),
        (
            {"style": "spaceDelimited", "explode": False},
            '{"status":"open","limit":5}',
            "filter=status%20open%20limit%205",
        ),
        (
            {"style": "pipeDelimited", "explode": False},
            '{"status":"open","limit":5}',
            "filter=status%7Copen%7Climit%7C5",
        ),
        ({"style": "deepObject"}, "{}", ""),
    ],
)
def test_query_object_wire_values(tmp_path, query_endpoint, serialization, value, raw_query):
    spec = {
        "openapi": "3.0.4",
        "info": {"title": "Query objects", "version": "1"},
        "servers": [{"url": query_endpoint}],
        "paths": {
            "/items": {
                "get": {
                    "operationId": "listItems",
                    "parameters": [
                        {
                            "name": "filter",
                            "in": "query",
                            "schema": {"type": "object"},
                            **serialization,
                        }
                    ],
                    "responses": {"200": {"description": "Observed query parameters"}},
                },
            },
        },
    }
    spec_path = tmp_path / "openapi.json"
    spec_path.write_text(json.dumps(spec), encoding="utf-8")
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "mcp2cli",
            "--spec",
            str(spec_path),
            "--json",
            "list-items",
            "--filter",
            value,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["raw_query"] == raw_query
