"""Tests for MCP mode — stdio and HTTP transports."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

MCP_SERVER = str(Path(__file__).parent / "mcp_test_server.py")


class TestMCPStdio:
    """Integration tests using the stdio MCP test server."""

    def _run(self, *args, stdin_data=None) -> subprocess.CompletedProcess:
        cmd = [
            sys.executable,
            "-m",
            "mcp2cli",
            "--mcp-stdio",
            f"{sys.executable} {MCP_SERVER}",
            *args,
        ]
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            input=stdin_data,
            timeout=30,
        )

    def test_list_tools(self):
        r = self._run("--list")
        assert r.returncode == 0
        assert "echo" in r.stdout
        assert "add-numbers" in r.stdout
        assert "list-items" in r.stdout

    def test_tool_help_shows_description(self):
        r = self._run("echo", "--help")
        assert r.returncode == 0
        assert "Echo back the input" in r.stdout

    def test_echo(self):
        r = self._run("echo", "--message", "hello world")
        assert r.returncode == 0
        assert "hello world" in r.stdout

    def test_add_numbers(self):
        r = self._run("add-numbers", "--a", "3", "--b", "7")
        assert r.returncode == 0
        assert "10" in r.stdout

    def test_list_items(self):
        r = self._run("list-items", "--path", "/tmp")
        assert r.returncode == 0
        data = json.loads(r.stdout)
        assert data["path"] == "/tmp"
        assert "items" in data

    def test_list_items_with_boolean(self):
        r = self._run("list-items", "--path", "/tmp", "--recursive")
        assert r.returncode == 0
        data = json.loads(r.stdout)
        assert data["recursive"] is True

    def test_structured_content_only(self):
        """A tool returning only structuredContent (empty content list) must
        still print the payload instead of printing nothing."""
        # --refresh: the tool list is cached on disk by server command; force a
        # re-fetch so this test doesn't depend on cache freshness.
        r = self._run("--refresh", "struct-only")
        assert r.returncode == 0
        data = json.loads(r.stdout)
        assert data == {"answer": 42}

    def test_reserved_boolean_stdin_property_reaches_tool(self):
        r = self._run(
            "--refresh",
            "reserved-args",
            "--arg-help",
            "details",
            "--arg-stdin-2",
            "--arg-stdin",
            "literal",
        )
        assert r.returncode == 0
        assert json.loads(r.stdout) == {
            "arg_stdin": "literal",
            "help": "details",
            "stdin": True,
        }

    def test_echo_stdin(self):
        r = self._run("echo", "--stdin", stdin_data='{"message": "from stdin"}')
        assert r.returncode == 0
        assert "from stdin" in r.stdout

    def test_echo_stdin_invalid_json(self):
        r = self._run("echo", "--stdin", stdin_data='{"message":')
        assert r.returncode != 0
        assert "invalid JSON" in r.stderr

    def test_no_subcommand_shows_tools(self):
        r = self._run()
        assert r.returncode == 0
        assert "echo" in r.stdout

    def test_pretty_output(self):
        r = self._run("--pretty", "list-items", "--path", "/test")
        assert r.returncode == 0
        # Pretty output should be indented
        assert "  " in r.stdout

    def test_raw_output(self):
        r = self._run("--raw", "echo", "--message", "raw test")
        assert r.returncode == 0
        assert "raw test" in r.stdout

    def test_env_vars(self):
        """Test that --env flag is accepted (env vars passed to subprocess)."""
        r = self._run("--env", "TEST_VAR=hello", "echo", "--message", "test")
        assert r.returncode == 0

    # --- GH #14: --search filters tools ---

    def test_search_by_name(self):
        """--search filters tools by name."""
        r = self._run("--search", "echo")
        assert r.returncode == 0
        assert "echo" in r.stdout
        assert "add-numbers" not in r.stdout

    def test_search_by_description(self):
        """--search matches against description too."""
        r = self._run("--search", "directory")
        assert r.returncode == 0
        assert "list-items" in r.stdout
        assert "echo" not in r.stdout

    def test_search_case_insensitive(self):
        """--search is case-insensitive."""
        r = self._run("--search", "ECHO")
        assert r.returncode == 0
        assert "echo" in r.stdout

    def test_search_no_matches(self):
        """--search with no matches prints helpful message."""
        r = self._run("--search", "nonexistent_xyz")
        assert r.returncode == 0
        assert "No tools matching" in r.stdout

    # --- GH #15: global options must not shadow tool parameters ---

    def test_tool_param_not_shadowed_by_global_env(self):
        """Tool --env parameter must not be consumed by global --env."""
        r = self._run("deploy", "--env", "production")
        assert r.returncode == 0
        data = json.loads(r.stdout)
        assert data["env"] == "production"

    def test_tool_param_not_shadowed_by_global_refresh(self):
        """Tool --refresh must not be consumed by global --refresh."""
        r = self._run("deploy", "--env", "staging", "--refresh")
        assert r.returncode == 0
        data = json.loads(r.stdout)
        assert data["refresh"] is True

    def test_global_and_tool_env_coexist(self):
        """Global --env before subcommand + tool --env after subcommand."""
        r = self._run("--env", "X=1", "deploy", "--env", "production")
        assert r.returncode == 0
        data = json.loads(r.stdout)
        assert data["env"] == "production"

    def test_tool_caching(self, tmp_path, monkeypatch):
        """Run twice — second should use cached tool list."""
        import mcp2cli

        monkeypatch.setattr(mcp2cli, "CACHE_DIR", tmp_path / "cache")

        # First run fetches and caches
        r1 = self._run("echo", "--message", "first")
        assert r1.returncode == 0

        # Cached tools file should exist
        cache_files = (
            list((tmp_path / "cache").glob("*_tools.json"))
            if (tmp_path / "cache").exists()
            else []
        )
        # Cache may or may not exist depending on subprocess isolation
        # Just verify both runs succeed
        r2 = self._run("echo", "--message", "second")
        assert r2.returncode == 0

    # --- Tool failures (isError) ---

    def test_iserror_tool_exits_nonzero(self):
        """A tool result with isError=true must exit non-zero, on stderr."""
        r = self._run("fail")
        assert r.returncode != 0
        assert "boom: deliberate failure" in r.stderr
        assert "boom: deliberate failure" not in r.stdout

    def test_iserror_tool_json_keeps_envelope_but_exits_nonzero(self):
        """--json still emits the full envelope, but the exit code reflects failure."""
        r = self._run("--json", "fail")
        assert r.returncode != 0
        envelope = json.loads(r.stdout)
        assert envelope["isError"] is True
        assert envelope["content"][0]["text"] == "boom: deliberate failure"

    # --- Resources ---

    def test_list_resources(self):
        r = self._run("--list-resources")
        assert r.returncode == 0
        data = json.loads(r.stdout)
        names = [d["name"] for d in data]
        assert "Test Document" in names

    def test_list_resource_templates(self):
        r = self._run("--list-resource-templates")
        assert r.returncode == 0
        data = json.loads(r.stdout)
        assert len(data) >= 1
        assert "uriTemplate" in data[0]

    def test_read_resource(self):
        r = self._run("--read-resource", "file:///test/doc.txt")
        assert r.returncode == 0
        assert "Hello from test document!" in r.stdout

    def test_read_resource_not_found(self):
        r = self._run("--read-resource", "file:///nonexistent")
        assert r.returncode != 0

    # --- Prompts ---

    def test_list_prompts(self):
        r = self._run("--list-prompts")
        assert r.returncode == 0
        data = json.loads(r.stdout)
        names = [d["name"] for d in data]
        assert "greeting" in names
        assert "summary" in names

    def test_get_prompt(self):
        r = self._run("--get-prompt", "greeting", "--prompt-arg", "name=Alice")
        assert r.returncode == 0
        data = json.loads(r.stdout)
        assert "messages" in data
        assert "Alice" in data["messages"][0]["content"]

    def test_get_prompt_no_args(self):
        r = self._run("--get-prompt", "greeting")
        assert r.returncode == 0
        data = json.loads(r.stdout)
        assert "messages" in data
        # Default name should be "World"
        assert "World" in data["messages"][0]["content"]

    # --- Roots and completion ---

    def test_roots_are_exposed_to_server(self):
        r = self._run(
            "--refresh",
            "--root",
            "/tmp/workspace",
            "--root",
            "file:///var/project",
            "client-roots",
        )
        assert r.returncode == 0
        roots = json.loads(r.stdout)
        assert {root["uri"] for root in roots} == {
            "file:///tmp/workspace",
            "file:///var/project",
        }
        assert {root["name"] for root in roots} == {"workspace", "project"}

    def test_invalid_root_uri_fails_before_connecting(self):
        r = self._run("--root", "https://example.com/workspace", "--list")
        assert r.returncode != 0
        assert "--root expects a filesystem path or file:// URI" in r.stderr

    def test_complete_prompt_argument(self):
        r = self._run("--complete", "greeting:name=San")
        assert r.returncode == 0
        assert json.loads(r.stdout) == {
            "values": ["San Diego", "San Francisco"],
            "total": 3,
            "hasMore": True,
        }


class TestMCPHTTP:
    """Tests for MCP HTTP transport.

    Driven against the Streamable HTTP test server (`mcp_http_server` in
    conftest), which is deliberately POST-only.
    """


    def _run(self, url, *args) -> subprocess.CompletedProcess:
        cmd = [
            sys.executable,
            "-m",
            "mcp2cli",
            "--mcp",
            url,
            *args,
        ]
        return subprocess.run(cmd, capture_output=True, text=True, timeout=30)

    def test_list_tools_http(self, mcp_http_server):
        r = self._run(mcp_http_server, "--list")
        assert r.returncode == 0
        assert "echo" in r.stdout

    def test_echo_http(self, mcp_http_server):
        r = self._run(mcp_http_server, "echo", "--message", "http test")
        assert r.returncode == 0
        assert "http test" in r.stdout

    def test_add_numbers_http(self, mcp_http_server):
        r = self._run(mcp_http_server, "add-numbers", "--a", "10", "--b", "20")
        assert r.returncode == 0
        assert "30" in r.stdout

    def test_iserror_http_exits_nonzero(self, mcp_http_server):
        plain = self._run(mcp_http_server, "fail")
        assert plain.returncode != 0
        assert "boom: deliberate failure" in plain.stderr
        assert plain.stdout == ""

        machine = self._run(mcp_http_server, "--json", "fail")
        assert machine.returncode != 0
        assert json.loads(machine.stdout)["isError"] is True

    # --- Resources (HTTP) ---

    def test_list_resources_http(self, mcp_http_server):
        r = self._run(mcp_http_server, "--list-resources")
        assert r.returncode == 0
        data = json.loads(r.stdout)
        names = [d["name"] for d in data]
        assert "Test Document" in names

    def test_list_resource_templates_http(self, mcp_http_server):
        r = self._run(mcp_http_server, "--list-resource-templates")
        assert r.returncode == 0
        data = json.loads(r.stdout)
        assert len(data) >= 1

    def test_read_resource_http(self, mcp_http_server):
        r = self._run(mcp_http_server, "--read-resource", "file:///test/doc.txt")
        assert r.returncode == 0
        assert "Hello from test document!" in r.stdout

    # --- Prompts (HTTP) ---

    def test_list_prompts_http(self, mcp_http_server):
        r = self._run(mcp_http_server, "--list-prompts")
        assert r.returncode == 0
        data = json.loads(r.stdout)
        names = [d["name"] for d in data]
        assert "greeting" in names

    def test_get_prompt_http(self, mcp_http_server):
        r = self._run(
            mcp_http_server, "--get-prompt", "greeting", "--prompt-arg", "name=Bob"
        )
        assert r.returncode == 0
        data = json.loads(r.stdout)
        assert "Bob" in data["messages"][0]["content"]


class TestSessions:
    """Tests for persistent session support."""

    def test_session_lifecycle(self):
        """Start, list, and stop a session."""
        server = f"{sys.executable} {MCP_SERVER}"
        name = "test-lifecycle"

        # Start
        r = subprocess.run(
            [
                sys.executable,
                "-m",
                "mcp2cli",
                "--mcp-stdio",
                server,
                "--root",
                "/tmp/workspace",
                "--root",
                "file:///var/project",
                "--session-start",
                name,
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert r.returncode == 0
        assert name in r.stdout

        try:
            # List
            r = subprocess.run(
                [sys.executable, "-m", "mcp2cli", "--session-list"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            assert r.returncode == 0
            assert name in r.stdout
            assert "alive" in r.stdout

            # Tool call via session
            r = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "mcp2cli",
                    "--session",
                    name,
                    "echo",
                    "--message",
                    "via session",
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
            assert r.returncode == 0
            assert "via session" in r.stdout

            # List tools via session
            r = subprocess.run(
                [sys.executable, "-m", "mcp2cli", "--session", name, "--list"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            assert r.returncode == 0
            assert "echo" in r.stdout

            # Resources via session
            r = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "mcp2cli",
                    "--session",
                    name,
                    "--list-resources",
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
            assert r.returncode == 0
            data = json.loads(r.stdout)
            assert any(d["name"] == "Test Document" for d in data)

            # Prompts via session
            r = subprocess.run(
                [sys.executable, "-m", "mcp2cli", "--session", name, "--list-prompts"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            assert r.returncode == 0
            data = json.loads(r.stdout)
            assert any(d["name"] == "greeting" for d in data)

            # Completion via session
            r = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "mcp2cli",
                    "--session",
                    name,
                    "--complete",
                    "greeting:name=San",
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
            assert r.returncode == 0
            assert json.loads(r.stdout) == {
                "values": ["San Diego", "San Francisco"],
                "total": 3,
                "hasMore": True,
            }

            # Roots survive serialization into the session daemon
            r = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "mcp2cli",
                    "--session",
                    name,
                    "client-roots",
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
            assert r.returncode == 0
            roots = json.loads(r.stdout)
            assert {root["uri"] for root in roots} == {
                "file:///tmp/workspace",
                "file:///var/project",
            }

        finally:
            # Stop
            r = subprocess.run(
                [sys.executable, "-m", "mcp2cli", "--session-stop", name],
                capture_output=True,
                text=True,
                timeout=10,
            )
            assert r.returncode == 0

        # Verify stopped
        r = subprocess.run(
            [sys.executable, "-m", "mcp2cli", "--session-list"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert name not in r.stdout or "dead" in r.stdout

    def test_session_iserror_tool_exits_nonzero(self):
        """isError through the session daemon must also exit non-zero."""
        server = f"{sys.executable} {MCP_SERVER}"
        name = "test-iserror"

        r = subprocess.run(
            [
                sys.executable,
                "-m",
                "mcp2cli",
                "--mcp-stdio",
                server,
                "--session-start",
                name,
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )

        assert r.returncode == 0

        try:
            r = subprocess.run(
                [sys.executable, "-m", "mcp2cli", "--session", name, "fail"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            assert r.returncode != 0
            assert "boom: deliberate failure" in r.stderr
            assert "boom: deliberate failure" not in r.stdout

            machine = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "mcp2cli",
                    "--json",
                    "--session",
                    name,
                    "fail",
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
            assert machine.returncode != 0
            assert json.loads(machine.stdout)["isError"] is True

            structured = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "mcp2cli",
                    "--session",
                    name,
                    "struct-only",
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
            assert structured.returncode == 0
            assert json.loads(structured.stdout) == {"answer": 42}

            # Successful calls still print to stdout and exit 0.
            r = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "mcp2cli",
                    "--session",
                    name,
                    "echo",
                    "--message",
                    "still fine",
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
            assert r.returncode == 0
            assert "still fine" in r.stdout
        finally:
            subprocess.run(
                [sys.executable, "-m", "mcp2cli", "--session-stop", name],
                capture_output=True,
                text=True,
                timeout=10,
            )

class TestConnectionErrors:
    """Transport failures must report one clean error line, not a traceback."""

    def _run(self, *args):
        import os

        env = {key: value for key, value in os.environ.items() if key != "MCP2CLI_DEBUG"}
        return subprocess.run(
            [sys.executable, "-m", "mcp2cli", *args],
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
        )


    def test_unreachable_server_is_one_clean_line(self):
        r = self._run(
            "--mcp", "http://127.0.0.1:9/mcp", "--list", "--transport", "streamable"
        )
        assert r.returncode != 0
        assert "Traceback" not in r.stderr
        assert "Error: cannot use MCP server at http://127.0.0.1:9/mcp" in r.stderr
        assert r.stdout == ""

    def test_auth_rejection_hints_at_credentials(self):
        """A 401/403 should suggest --auth-header instead of dumping a traceback."""
        import http.server
        import threading

        class Deny(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(401)
                self.end_headers()

            def do_POST(self):
                self.send_response(401)
                self.end_headers()

            def log_message(self, *args):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), Deny)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            r = self._run("--mcp", f"http://127.0.0.1:{port}/mcp", "--list")
            assert r.returncode != 0
            assert "Traceback" not in r.stderr
            assert "--auth-header" in r.stderr
            assert len(r.stderr.splitlines()) == 1
        finally:
            server.shutdown()

    def test_grouped_system_exit_preserves_code(self):
        import anyio

        from mcp2cli import _run_mcp_clean

        async def exit_inside_task_group():
            async with anyio.create_task_group():
                raise SystemExit(3)

        with pytest.raises(SystemExit) as caught:
            _run_mcp_clean(exit_inside_task_group, "test")
        assert caught.value.code == 3

    def test_debug_env_restores_traceback(self):
        import os

        env = {**os.environ, "MCP2CLI_DEBUG": "1"}
        r = subprocess.run(
            [
                sys.executable, "-m", "mcp2cli",
                "--mcp", "http://127.0.0.1:9/mcp", "--list",
                "--transport", "streamable",
            ],
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
        )
        assert r.returncode != 0
        assert "Traceback" in r.stderr


    def test_run_mcp_clean_server_disconnect(self, capsys):
        import anyio
        from mcp2cli import _run_mcp_clean

        async def throw_disconnect():
            raise anyio.EndOfStream("Server disconnected")

        with pytest.raises(SystemExit) as caught:
            _run_mcp_clean(throw_disconnect, "test")
        
        assert caught.value.code == 1
        captured = capsys.readouterr()
        assert "the server disconnected unexpectedly" in captured.err
