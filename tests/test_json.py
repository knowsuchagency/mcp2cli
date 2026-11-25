"""Tests for the --json flag: forces valid JSON output across all modes and paths."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from mcp2cli import (
    CommandDef,
    ParamDef,
    _ensure_utf8_output,
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

    def test_non_ascii_emitted_as_utf8(self, capsys):
        output_result({"content": ["返回首页"]}, json_output=True)
        out = capsys.readouterr().out
        assert "\\u" not in out
        assert json.loads(out) == {"content": ["返回首页"]}


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
        return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", timeout=15)

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
        return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", timeout=30)

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
    def _run(self, url, *args):
        cmd = [sys.executable, "-m", "mcp2cli", "--mcp", url, *args]
        return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", timeout=30)

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
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", timeout=30)


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


class TestEnsureUtf8Output:
    """ensure_ascii=False (#62) must not become a crash on legacy consoles."""

    def test_reconfigures_stream_to_utf8(self, monkeypatch):
        import io

        stream = io.TextIOWrapper(io.BytesIO(), encoding="cp936")
        monkeypatch.setattr(sys, "stdout", stream)
        monkeypatch.setattr(sys, "stderr", stream)
        _ensure_utf8_output()
        assert stream.encoding.lower().replace("-", "") == "utf8"

    def test_non_ascii_survives_a_cp936_pipe(self, monkeypatch):
        """The regression this guards: a CJK payload down a non-UTF-8 pipe."""
        import io

        raw = io.BytesIO()
        monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(raw, encoding="cp936"))
        monkeypatch.setattr(sys, "stderr", sys.stdout)
        _ensure_utf8_output()
        output_result({"content": ["返回首页"]}, json_output=True)
        sys.stdout.flush()
        assert json.loads(raw.getvalue().decode("utf-8")) == {"content": ["返回首页"]}

    def test_stream_without_reconfigure_is_left_alone(self, monkeypatch):
        class Bare:
            encoding = "ascii"

        monkeypatch.setattr(sys, "stdout", Bare())
        monkeypatch.setattr(sys, "stderr", Bare())
        _ensure_utf8_output()  # must not raise

    def test_unreconfigurable_stream_falls_back_to_escapes(self, monkeypatch):
        """If UTF-8 is refused, degrade to the pre-#62 shape, never crash."""
        calls = []

        class Stubborn:
            encoding = "cp936"

            def reconfigure(self, **kwargs):
                calls.append(kwargs)
                if "encoding" in kwargs:
                    raise OSError("cannot change encoding")

        stream = Stubborn()
        monkeypatch.setattr(sys, "stdout", stream)
        monkeypatch.setattr(sys, "stderr", stream)
        _ensure_utf8_output()
        assert {"errors": "backslashreplace"} in calls
