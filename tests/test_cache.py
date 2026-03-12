"""Integration tests for CLI caching behavior.

Verifies that specs/tool lists are cached, --refresh bypasses cache,
--cache-key overrides the key, --cache-ttl controls expiry, and
local files are never cached.
"""

import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

import mcp2cli

from conftest import PETSTORE_SPEC


class CountingHandler(BaseHTTPRequestHandler):
    """Petstore handler that counts spec fetches."""

    spec_fetch_count = 0
    api_call_count = 0

    def log_message(self, format, *args):
        pass

    def _send_json(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/openapi.json":
            CountingHandler.spec_fetch_count += 1
            self._send_json(PETSTORE_SPEC)
            return
        if path == "/api/v1/pets":
            CountingHandler.api_call_count += 1
            self._send_json([{"id": 1, "name": "Fido"}])
            return
        if path.startswith("/api/v1/pets/"):
            CountingHandler.api_call_count += 1
            self._send_json({"id": 1, "name": "Fido"})
            return
        self._send_json({"error": "not found"}, 404)


@pytest.fixture(autouse=True)
def _reset_counters():
    CountingHandler.spec_fetch_count = 0
    CountingHandler.api_call_count = 0


@pytest.fixture()
def counting_server():
    server = HTTPServer(("127.0.0.1", 0), CountingHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{port}"
    server.shutdown()


def _run_cli(*args, cache_dir=None, stdin_data=None):
    """Run mcp2cli as subprocess with a controlled cache dir."""
    cmd = [sys.executable, "-m", "mcp2cli", *args]
    env = {**os.environ}
    if cache_dir:
        # We can't monkeypatch a subprocess, so we inject via env
        env["MCP2CLI_CACHE_DIR"] = str(cache_dir)
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        input=stdin_data,
        timeout=15,
        env=env,
    )


class TestOpenAPICaching:
    """Verify OpenAPI spec caching end-to-end."""

    def test_spec_cached_on_second_call(self, counting_server, tmp_path):
        """First call fetches spec from server, second uses cache."""
        cd = tmp_path / "cache"
        url = f"{counting_server}/openapi.json"

        r1 = _run_cli(
            "--spec",
            url,
            "--base-url",
            f"{counting_server}/api/v1",
            "--list",
            cache_dir=cd,
        )
        assert r1.returncode == 0
        assert CountingHandler.spec_fetch_count == 1

        r2 = _run_cli(
            "--spec",
            url,
            "--base-url",
            f"{counting_server}/api/v1",
            "--list",
            cache_dir=cd,
        )
        assert r2.returncode == 0
        # Should NOT have fetched again
        assert CountingHandler.spec_fetch_count == 1

    def test_refresh_bypasses_cache(self, counting_server, tmp_path):
        cd = tmp_path / "cache"
        url = f"{counting_server}/openapi.json"

        _run_cli(
            "--spec",
            url,
            "--base-url",
            f"{counting_server}/api/v1",
            "--list",
            cache_dir=cd,
        )
        assert CountingHandler.spec_fetch_count == 1

        _run_cli(
            "--spec",
            url,
            "--base-url",
            f"{counting_server}/api/v1",
            "--list",
            "--refresh",
            cache_dir=cd,
        )
        assert CountingHandler.spec_fetch_count == 2

    def test_custom_cache_key(self, counting_server, tmp_path):
        """--cache-key overrides the auto-generated key."""
        cd = tmp_path / "cache"
        url = f"{counting_server}/openapi.json"

        _run_cli(
            "--spec",
            url,
            "--base-url",
            f"{counting_server}/api/v1",
            "--cache-key",
            "my-petstore",
            "--list",
            cache_dir=cd,
        )
        assert CountingHandler.spec_fetch_count == 1

        # Verify cache file uses our custom key
        cache_files = list(cd.glob("*.json"))
        assert any("my-petstore" in f.name for f in cache_files)

        # Second call with same key should hit cache
        _run_cli(
            "--spec",
            url,
            "--base-url",
            f"{counting_server}/api/v1",
            "--cache-key",
            "my-petstore",
            "--list",
            cache_dir=cd,
        )
        assert CountingHandler.spec_fetch_count == 1

    def test_cache_ttl_expiry(self, counting_server, tmp_path):
        """Expired cache triggers re-fetch."""
        cd = tmp_path / "cache"
        url = f"{counting_server}/openapi.json"

        _run_cli(
            "--spec",
            url,
            "--base-url",
            f"{counting_server}/api/v1",
            "--list",
            cache_dir=cd,
        )
        assert CountingHandler.spec_fetch_count == 1

        # Manually age the cache file
        for f in cd.glob("*.json"):
            old = time.time() - 7200
            os.utime(f, (old, old))

        # With short TTL, should re-fetch
        _run_cli(
            "--spec",
            url,
            "--base-url",
            f"{counting_server}/api/v1",
            "--cache-ttl",
            "1",
            "--list",
            cache_dir=cd,
        )
        assert CountingHandler.spec_fetch_count == 2

    def test_corrupt_cached_spec_refetches_instead_of_crashing(
        self, counting_server, tmp_path
    ):
        cd = tmp_path / "cache"
        cd.mkdir(parents=True, exist_ok=True)
        url = f"{counting_server}/openapi.json"
        cache_key = mcp2cli.cache_key_for(url)
        (cd / f"{cache_key}.json").write_text('{"broken":', encoding="utf-8")

        r = _run_cli(
            "--spec",
            url,
            "--base-url",
            f"{counting_server}/api/v1",
            "--list",
            cache_dir=cd,
        )

        assert r.returncode == 0
        assert CountingHandler.spec_fetch_count == 1
        repaired = json.loads((cd / f"{cache_key}.json").read_text(encoding="utf-8"))
        assert "/pets" in repaired["paths"]

    def test_local_file_not_cached(self, tmp_path):
        """Loading from a local file should not create cache entries."""
        cd = tmp_path / "cache"
        spec_file = tmp_path / "petstore.json"
        spec_file.write_text(json.dumps(PETSTORE_SPEC))

        r = _run_cli(
            "--spec",
            str(spec_file),
            "--base-url",
            "http://unused",
            "--list",
            cache_dir=cd,
        )
        assert r.returncode == 0
        # No cache files should be created for local files
        if cd.exists():
            cache_files = list(cd.glob("*.json"))
            assert len(cache_files) == 0

    def test_execution_uses_cached_spec(self, counting_server, tmp_path):
        """Actual API calls work after spec is served from cache."""
        cd = tmp_path / "cache"
        url = f"{counting_server}/openapi.json"
        base = f"{counting_server}/api/v1"

        # Prime the cache
        _run_cli("--spec", url, "--base-url", base, "--list", cache_dir=cd)
        assert CountingHandler.spec_fetch_count == 1

        # Execute a command — should use cached spec, not re-fetch
        r = _run_cli("--spec", url, "--base-url", base, "list-pets", cache_dir=cd)
        assert r.returncode == 0
        assert CountingHandler.spec_fetch_count == 1  # still 1
        assert CountingHandler.api_call_count == 1  # but API was called

    def test_different_urls_different_cache(self, counting_server, tmp_path):
        """Different spec URLs should not share cache entries."""
        cd = tmp_path / "cache"
        url = f"{counting_server}/openapi.json"

        _run_cli(
            "--spec",
            url,
            "--base-url",
            f"{counting_server}/api/v1",
            "--list",
            cache_dir=cd,
        )
        assert CountingHandler.spec_fetch_count == 1

        # Same server, different path — should not reuse cache
        # (it would 404, but the point is it tries to fetch)
        r = _run_cli(
            "--spec",
            url + "?v=2",
            "--base-url",
            f"{counting_server}/api/v1",
            "--list",
            cache_dir=cd,
        )
        assert CountingHandler.spec_fetch_count == 2


class TestMCPStdioCaching:
    """Verify MCP stdio tool list caching."""

    MCP_SERVER = str(Path(__file__).parent / "mcp_test_server.py")

    def _run_mcp(self, *args, cache_dir=None):
        return _run_cli(
            "--mcp-stdio",
            f"{sys.executable} {self.MCP_SERVER}",
            *args,
            cache_dir=cache_dir,
        )

    def test_tool_list_cached(self, tmp_path):
        """Subcommand invocation caches the tool list for subsequent runs."""
        cd = tmp_path / "cache"

        # First run fetches tools and caches them
        r1 = self._run_mcp("echo", "--message", "prime", cache_dir=cd)
        assert r1.returncode == 0

        # Cache should now have a tools file
        assert cd.exists()
        tools_files = list(cd.glob("*_tools.json"))
        assert len(tools_files) == 1

        # Verify the cached content is valid
        cached = json.loads(tools_files[0].read_text())
        assert isinstance(cached, list)
        tool_names = {t["name"] for t in cached}
        assert "echo" in tool_names

    def test_mcp_refresh_clears_tool_cache(self, tmp_path):
        """--refresh should re-fetch tools from the server."""
        cd = tmp_path / "cache"

        # Prime cache with a subcommand invocation
        self._run_mcp("echo", "--message", "prime", cache_dir=cd)
        tools_files = list(cd.glob("*_tools.json"))
        assert len(tools_files) == 1
        mtime1 = tools_files[0].stat().st_mtime

        # Small delay to ensure mtime differs
        time.sleep(0.1)

        # --refresh should re-fetch and update the cache
        self._run_mcp("--refresh", "echo", "--message", "again", cache_dir=cd)
        tools_files = list(cd.glob("*_tools.json"))
        assert len(tools_files) == 1
        mtime2 = tools_files[0].stat().st_mtime
        assert mtime2 > mtime1

    def test_cached_tools_used_for_execution(self, tmp_path):
        """Tool invocation should work with cached tool list (no extra server connect for listing)."""
        cd = tmp_path / "cache"

        # Prime cache
        self._run_mcp("echo", "--message", "prime", cache_dir=cd)

        # Run again using cache
        r = self._run_mcp("echo", "--message", "cached", cache_dir=cd)
        assert r.returncode == 0
        assert "cached" in r.stdout

    def test_expired_tool_cache_triggers_reconnect(self, tmp_path):
        """Expired tool cache should cause re-fetch."""
        cd = tmp_path / "cache"

        self._run_mcp("echo", "--message", "first", cache_dir=cd)
        tools_files = list(cd.glob("*_tools.json"))
        assert len(tools_files) == 1

        # Age the cache
        old = time.time() - 7200
        os.utime(tools_files[0], (old, old))

        # With short TTL, should re-fetch
        r = self._run_mcp(
            "--cache-ttl", "1", "echo", "--message", "refreshed", cache_dir=cd
        )
        assert r.returncode == 0
        assert "refreshed" in r.stdout
