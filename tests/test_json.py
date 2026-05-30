"""Tests for the --json flag: forces valid JSON output across all modes and paths."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from mcp2cli import (
    CommandDef,
    ParamDef,
    command_to_dict,
    output_result,
    print_commands_json,
)

MCP_SERVER = str(Path(__file__).parent / "mcp_test_server.py")


# ---------------------------------------------------------------------------
# Unit tests: output_result(json_output=True)
# ---------------------------------------------------------------------------


class TestOutputResultJson:
    def test_dict_emitted_as_json(self, capsys):
        output_result({"a": 1, "b": [1, 2]}, json_output=True)
        out = capsys.readouterr().out
        assert json.loads(out) == {"a": 1, "b": [1, 2]}

    def test_json_string_is_unwrapped(self, capsys):
        # A string that is itself JSON gets parsed, not double-encoded.
        output_result('{"x": 42}', json_output=True)
        out = capsys.readouterr().out
        assert json.loads(out) == {"x": 42}

    def test_plain_text_becomes_json_string(self, capsys):
        # Non-JSON prose is emitted as a valid JSON string literal.
        output_result("just some prose", json_output=True)
        out = capsys.readouterr().out
        assert json.loads(out) == "just some prose"

    def test_json_overrides_raw(self, capsys):
        # --json wins over --raw, still valid JSON.
        output_result("plain", json_output=True, raw=True)
        out = capsys.readouterr().out
        assert json.loads(out) == "plain"

    def test_json_overrides_toon(self, capsys):
        output_result([{"a": 1}], json_output=True, toon=True)
        out = capsys.readouterr().out
        assert json.loads(out) == [{"a": 1}]

    def test_head_applies_in_json_mode(self, capsys):
        output_result([1, 2, 3, 4, 5], json_output=True, head=2)
        out = capsys.readouterr().out
        assert json.loads(out) == [1, 2]

    def test_pretty_indented(self, capsys):
        output_result({"a": 1}, json_output=True, pretty=True)
        out = capsys.readouterr().out
        assert "\n  " in out
        assert json.loads(out) == {"a": 1}


# ---------------------------------------------------------------------------
# Unit tests: command serialization
# ---------------------------------------------------------------------------


class TestCommandSerialization:
    def _cmd(self):
        return CommandDef(
            name="list-pets",
            description="List all pets",
            method="get",
            path="/pets",
            params=[
                ParamDef(
                    name="limit",
                    original_name="limit",
                    python_type=int,
                    required=False,
                    description="Max items",
                    location="query",
                    choices=None,
                ),
                ParamDef(
                    name="status",
                    original_name="status",
                    python_type=str,
                    required=True,
                    description="Filter",
                    location="query",
                    choices=["available", "sold"],
                ),
            ],
        )

    def test_command_to_dict_shape(self):
        d = command_to_dict(self._cmd())
        assert d["name"] == "list-pets"
        assert d["description"] == "List all pets"
        assert d["method"] == "GET"
        assert d["path"] == "/pets"
        assert len(d["parameters"]) == 2
        p0 = d["parameters"][0]
        assert p0 == {
            "name": "limit",
            "type": "int",
            "required": False,
            "description": "Max items",
            "location": "query",
        }
        # choices included only when present
        assert d["parameters"][1]["choices"] == ["available", "sold"]

    def test_boolean_param_type(self):
        cmd = CommandDef(
            name="flag-cmd",
            params=[ParamDef(name="force", original_name="force", python_type=None)],
        )
        d = command_to_dict(cmd)
        assert d["parameters"][0]["type"] == "boolean"

    def test_mcp_command_includes_tool_name(self):
        cmd = CommandDef(name="echo", tool_name="echo", description="Echo")
        d = command_to_dict(cmd)
        assert d["toolName"] == "echo"
        assert "method" not in d  # mode-specific fields omitted when absent

    def test_graphql_command_includes_operation_type(self):
        cmd = CommandDef(name="users", graphql_operation_type="query")
        d = command_to_dict(cmd)
        assert d["operationType"] == "query"

    def test_print_commands_json_array(self, capsys):
        print_commands_json([self._cmd()])
        out = capsys.readouterr().out
        data = json.loads(out)
        assert isinstance(data, list)
        assert data[0]["name"] == "list-pets"

    def test_print_commands_json_compact_names(self, capsys):
        print_commands_json([self._cmd()], compact=True)
        out = capsys.readouterr().out
        assert json.loads(out) == ["list-pets"]


# ---------------------------------------------------------------------------
# Integration: OpenAPI
# ---------------------------------------------------------------------------


class TestOpenAPIJson:
    def _run(self, petstore_server, *args):
        cmd = [
            sys.executable, "-m", "mcp2cli",
            "--spec", f"{petstore_server}/openapi.json",
            "--base-url", f"{petstore_server}/api/v1",
            *args,
        ]
        return subprocess.run(cmd, capture_output=True, text=True, timeout=15)

    def test_list_json(self, petstore_server):
        r = self._run(petstore_server, "--list", "--json")
        assert r.returncode == 0
        data = json.loads(r.stdout)
        assert isinstance(data, list)
        names = [c["name"] for c in data]
        assert "list-pets" in names
        # each command carries structured parameter metadata
        listpets = next(c for c in data if c["name"] == "list-pets")
        assert "parameters" in listpets
        assert listpets["method"] == "GET"

    def test_list_json_compact(self, petstore_server):
        r = self._run(petstore_server, "--list", "--json", "--compact")
        assert r.returncode == 0
        data = json.loads(r.stdout)
        assert isinstance(data, list)
        assert all(isinstance(name, str) for name in data)
        assert "list-pets" in data

    def test_call_json(self, petstore_server):
        r = self._run(petstore_server, "--json", "list-pets")
        assert r.returncode == 0
        data = json.loads(r.stdout)
        assert isinstance(data, list)


# ---------------------------------------------------------------------------
# Integration: MCP stdio
# ---------------------------------------------------------------------------


class TestMCPStdioJson:
    def _run(self, *args):
        cmd = [
            sys.executable, "-m", "mcp2cli",
            "--mcp-stdio", f"{sys.executable} {MCP_SERVER}",
            *args,
        ]
        return subprocess.run(cmd, capture_output=True, text=True, timeout=30)

    def test_list_json(self):
        r = self._run("--list", "--json")
        assert r.returncode == 0
        data = json.loads(r.stdout)
        names = [c["name"] for c in data]
        assert "echo" in names
        assert "add-numbers" in names

    def test_call_json_emits_full_envelope(self):
        # --json surfaces the full MCP CallToolResult envelope, not just text.
        r = self._run("--json", "echo", "--message", "hi there")
        assert r.returncode == 0
        data = json.loads(r.stdout)
        assert isinstance(data, dict)
        assert data["isError"] is False
        assert "content" in data
        texts = [c.get("text") for c in data["content"]]
        assert "hi there" in texts

    def test_call_json_structured_content_key_present(self):
        # The envelope always includes the structuredContent key (closing the
        # gap from the report, even when the value is null for this tool).
        r = self._run("--json", "add-numbers", "--a", "2", "--b", "3")
        assert r.returncode == 0
        data = json.loads(r.stdout)
        assert "structuredContent" in data
        texts = [c.get("text") for c in data["content"]]
        assert "5" in texts

    def test_json_overrides_raw(self):
        r = self._run("--json", "--raw", "echo", "--message", "x")
        assert r.returncode == 0
        # still a valid JSON envelope despite --raw
        data = json.loads(r.stdout)
        assert isinstance(data, dict)
        assert "content" in data


# ---------------------------------------------------------------------------
# Integration: MCP HTTP
# ---------------------------------------------------------------------------


class TestMCPHttpJson:
    @pytest.fixture(scope="class")
    def mcp_http_server(self):
        import time

        server_script = Path(__file__).parent / "_mcp_http_server.py"
        proc = subprocess.Popen(
            [sys.executable, str(server_script)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        port = None
        deadline = time.time() + 10
        while time.time() < deadline:
            line = proc.stdout.readline().strip()
            if line.startswith("PORT="):
                port = int(line.split("=")[1])
                break
            if proc.poll() is not None:
                pytest.skip(f"MCP HTTP server failed: {proc.stderr.read()}")
                return
        if port is None:
            proc.kill()
            pytest.skip("MCP HTTP server did not report port in time")
            return
        yield f"http://127.0.0.1:{port}/sse"
        proc.terminate()
        proc.wait(timeout=5)

    def _run(self, url, *args):
        cmd = [sys.executable, "-m", "mcp2cli", "--mcp", url, *args]
        return subprocess.run(cmd, capture_output=True, text=True, timeout=30)

    def test_list_json(self, mcp_http_server):
        r = self._run(mcp_http_server, "--list", "--json")
        assert r.returncode == 0
        data = json.loads(r.stdout)
        names = [c["name"] for c in data]
        assert "echo" in names

    def test_call_json_envelope(self, mcp_http_server):
        r = self._run(mcp_http_server, "--json", "echo", "--message", "http json")
        assert r.returncode == 0
        data = json.loads(r.stdout)
        assert data["isError"] is False
        texts = [c.get("text") for c in data["content"]]
        assert "http json" in texts


# ---------------------------------------------------------------------------
# Integration: GraphQL
# ---------------------------------------------------------------------------


def _run_gql(args):
    cmd = [sys.executable, "-m", "mcp2cli"] + args
    return subprocess.run(cmd, capture_output=True, text=True, timeout=30)


class TestGraphQLJson:
    def test_list_json(self, graphql_server):
        r = _run_gql(["--graphql", graphql_server, "--list", "--json"])
        assert r.returncode == 0
        data = json.loads(r.stdout)
        names = [c["name"] for c in data]
        assert "users" in names
        users = next(c for c in data if c["name"] == "users")
        assert users["operationType"] == "query"

    def test_call_json(self, graphql_server):
        r = _run_gql(["--graphql", graphql_server, "--json", "users"])
        assert r.returncode == 0
        data = json.loads(r.stdout)
        assert isinstance(data, list)
        assert data[0]["name"] == "Alice"
