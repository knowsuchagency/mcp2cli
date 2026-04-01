"""Tests for OAuth support."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import mcp2cli


class TestResolveSecret:
    """Tests for resolve_secret helper."""

    def test_literal_value(self):
        assert mcp2cli.resolve_secret("my-secret") == "my-secret"

    def test_env_prefix(self, monkeypatch):
        monkeypatch.setenv("TEST_SECRET_VAR", "from-env")
        assert mcp2cli.resolve_secret("env:TEST_SECRET_VAR") == "from-env"

    def test_env_prefix_missing_var(self, monkeypatch):
        monkeypatch.delenv("NONEXISTENT_VAR_12345", raising=False)
        with pytest.raises(SystemExit):
            mcp2cli.resolve_secret("env:NONEXISTENT_VAR_12345")

    def test_file_prefix(self, tmp_path):
        secret_file = tmp_path / "secret.txt"
        secret_file.write_text("file-secret\n")
        assert mcp2cli.resolve_secret(f"file:{secret_file}") == "file-secret"

    def test_file_prefix_missing_file(self):
        with pytest.raises(SystemExit):
            mcp2cli.resolve_secret("file:/nonexistent/path/secret.txt")

    def test_file_prefix_strips_trailing_newline(self, tmp_path):
        secret_file = tmp_path / "secret.txt"
        secret_file.write_text("no-newline")
        assert mcp2cli.resolve_secret(f"file:{secret_file}") == "no-newline"


class TestFileTokenStorage:
    """Tests for FileTokenStorage persistence."""

    def test_roundtrip_tokens(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mcp2cli, "OAUTH_DIR", tmp_path / "oauth")
        storage = mcp2cli.FileTokenStorage("https://example.com/mcp")

        import anyio

        async def _test():
            # Initially empty
            assert await storage.get_tokens() is None
            assert await storage.get_client_info() is None

            # Store tokens
            from mcp.shared.auth import OAuthToken

            token = OAuthToken(access_token="test-access", token_type="Bearer", refresh_token="test-refresh")
            await storage.set_tokens(token)

            # Retrieve tokens
            loaded = await storage.get_tokens()
            assert loaded is not None
            assert loaded.access_token == "test-access"
            assert loaded.refresh_token == "test-refresh"

        anyio.run(_test)

    def test_roundtrip_client_info(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mcp2cli, "OAUTH_DIR", tmp_path / "oauth")
        storage = mcp2cli.FileTokenStorage("https://example.com/mcp")

        import anyio

        async def _test():
            from mcp.shared.auth import OAuthClientInformationFull

            info = OAuthClientInformationFull(
                client_id="my-client",
                client_secret="my-secret",
                redirect_uris=["http://127.0.0.1:9999/callback"],
            )
            await storage.set_client_info(info)

            loaded = await storage.get_client_info()
            assert loaded is not None
            assert loaded.client_id == "my-client"
            assert loaded.client_secret == "my-secret"

        anyio.run(_test)

    def test_different_servers_get_different_storage(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mcp2cli, "OAUTH_DIR", tmp_path / "oauth")
        s1 = mcp2cli.FileTokenStorage("https://server-a.com/mcp")
        s2 = mcp2cli.FileTokenStorage("https://server-b.com/mcp")
        assert s1._dir != s2._dir

    def test_corrupt_token_file_returns_none(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mcp2cli, "OAUTH_DIR", tmp_path / "oauth")
        storage = mcp2cli.FileTokenStorage("https://example.com/mcp")
        storage._tokens_path.write_text("not valid json{{{")

        import anyio

        async def _test():
            assert await storage.get_tokens() is None

        anyio.run(_test)


class TestBuildOAuthProvider:
    """Tests for build_oauth_provider factory."""

    def test_client_credentials_returns_provider(self):
        provider = mcp2cli.build_oauth_provider(
            "https://example.com/mcp",
            client_id="my-id",
            client_secret="my-secret",
            scope="read write",
        )
        from mcp.client.auth.extensions.client_credentials import ClientCredentialsOAuthProvider

        assert isinstance(provider, ClientCredentialsOAuthProvider)

    def test_auth_code_returns_provider(self):
        provider = mcp2cli.build_oauth_provider(
            "https://example.com/mcp",
            scope="read",
        )
        from mcp.client.auth.oauth2 import OAuthClientProvider

        assert isinstance(provider, OAuthClientProvider)

    def test_auth_code_uses_custom_redirect_uri(self):
        """When redirect_uri is given the provider uses it verbatim."""
        from mcp.client.auth.oauth2 import OAuthClientProvider

        custom_uri = "http://localhost:19876/oauth/callback"
        provider = mcp2cli.build_oauth_provider(
            "https://example.com/mcp",
            redirect_uri=custom_uri,
        )
        assert isinstance(provider, OAuthClientProvider)
        redirect_uris = [str(u) for u in provider.context.client_metadata.redirect_uris]
        assert custom_uri in redirect_uris

    def test_redirect_uri_https_rejected(self):
        with pytest.raises(SystemExit):
            mcp2cli.build_oauth_provider(
                "https://example.com/mcp",
                redirect_uri="https://localhost:3334/callback",
            )

    def test_redirect_uri_no_port_rejected(self):
        with pytest.raises(SystemExit):
            mcp2cli.build_oauth_provider(
                "https://example.com/mcp",
                redirect_uri="http://localhost/callback",
            )

    def test_redirect_uri_non_loopback_rejected(self):
        with pytest.raises(SystemExit):
            mcp2cli.build_oauth_provider(
                "https://example.com/mcp",
                redirect_uri="http://example.com:3334/callback",
            )

    def test_redirect_uri_ipv6_loopback_accepted(self):
        """::1 (IPv6 loopback) should be accepted as a valid redirect host."""
        from mcp.client.auth.oauth2 import OAuthClientProvider

        provider = mcp2cli.build_oauth_provider(
            "https://example.com/mcp",
            redirect_uri="http://[::1]:19878/callback",
        )
        assert isinstance(provider, OAuthClientProvider)

    def test_auth_code_random_port_when_no_redirect_uri(self, monkeypatch):
        """Without redirect_uri, _find_free_port() is called and the default URI is built."""
        called_with = []

        original = mcp2cli._find_free_port

        def patched():
            port = original()
            called_with.append(port)
            return port

        monkeypatch.setattr(mcp2cli, "_find_free_port", patched)
        from mcp.client.auth.oauth2 import OAuthClientProvider

        provider = mcp2cli.build_oauth_provider("https://example.com/mcp")
        assert isinstance(provider, OAuthClientProvider)
        assert len(called_with) == 1
        expected_uri = f"http://127.0.0.1:{called_with[0]}/callback"
        redirect_uris = [str(u) for u in provider.context.client_metadata.redirect_uris]
        assert expected_uri in redirect_uris

    def test_client_id_only_preseeds_storage(self, tmp_path, monkeypatch):
        """client_id without client_secret pre-seeds client.json to skip DCR."""
        monkeypatch.setattr(mcp2cli, "OAUTH_DIR", tmp_path / "oauth")
        from mcp.client.auth.oauth2 import OAuthClientProvider

        provider = mcp2cli.build_oauth_provider(
            "https://example.com/mcp",
            client_id="pre-configured-id",
            redirect_uri="http://localhost:19877/oauth/callback",
        )
        assert isinstance(provider, OAuthClientProvider)

        storage = mcp2cli.FileTokenStorage("https://example.com/mcp")
        assert storage._client_path.exists()
        import json
        data = json.loads(storage._client_path.read_text())
        assert data["client_id"] == "pre-configured-id"
        assert data.get("client_secret") is None
        assert data.get("token_endpoint_auth_method") == "none"

    def test_flow_authorization_code_with_secret_returns_auth_code_provider(self, tmp_path, monkeypatch):
        """flow='authorization_code' with both client_id and client_secret
        must return OAuthClientProvider (not ClientCredentialsOAuthProvider)."""
        monkeypatch.setattr(mcp2cli, "OAUTH_DIR", tmp_path / "oauth")
        from mcp.client.auth.oauth2 import OAuthClientProvider

        provider = mcp2cli.build_oauth_provider(
            "https://example.com/mcp",
            client_id="my-id",
            client_secret="my-secret",
            scope="read write",
            redirect_uri="http://localhost:19879/callback",
            flow="authorization_code",
        )
        assert isinstance(provider, OAuthClientProvider)

    def test_flow_authorization_code_preseeds_confidential_client(self, tmp_path, monkeypatch):
        """flow='authorization_code' with client_secret pre-seeds storage with
        client_secret_post auth method."""
        monkeypatch.setattr(mcp2cli, "OAUTH_DIR", tmp_path / "oauth")

        mcp2cli.build_oauth_provider(
            "https://example.com/mcp",
            client_id="slack-client-id",
            client_secret="slack-client-secret",
            redirect_uri="http://localhost:19880/callback",
            flow="authorization_code",
        )

        storage = mcp2cli.FileTokenStorage("https://example.com/mcp")
        assert storage._client_path.exists()
        data = json.loads(storage._client_path.read_text())
        assert data["client_id"] == "slack-client-id"
        assert data["client_secret"] == "slack-client-secret"
        assert data["token_endpoint_auth_method"] == "client_secret_post"

    def test_flow_auto_with_id_and_secret_returns_client_credentials(self):
        """flow='auto' (default) with both id+secret → client credentials."""
        from mcp.client.auth.extensions.client_credentials import ClientCredentialsOAuthProvider

        provider = mcp2cli.build_oauth_provider(
            "https://example.com/mcp",
            client_id="my-id",
            client_secret="my-secret",
            flow="auto",
        )
        assert isinstance(provider, ClientCredentialsOAuthProvider)

    def test_flow_client_credentials_explicit(self):
        """flow='client_credentials' explicit returns client credentials provider."""
        from mcp.client.auth.extensions.client_credentials import ClientCredentialsOAuthProvider

        provider = mcp2cli.build_oauth_provider(
            "https://example.com/mcp",
            client_id="my-id",
            client_secret="my-secret",
            flow="client_credentials",
        )
        assert isinstance(provider, ClientCredentialsOAuthProvider)

    def test_find_free_port(self):
        port = mcp2cli._find_free_port()
        assert isinstance(port, int)
        assert 1024 <= port <= 65535


class TestOAuthCLIValidation:
    """Tests for OAuth CLI argument validation."""

    def _run(self, *args) -> subprocess.CompletedProcess:
        cmd = [sys.executable, "-m", "mcp2cli", *args]
        return subprocess.run(cmd, capture_output=True, text=True, timeout=10)

    def test_client_id_without_secret_accepted(self):
        """--oauth-client-id alone is valid (pre-configured client, no DCR)."""
        r = self._run("--mcp", "https://example.com/mcp", "--oauth-client-id", "my-id", "--list")
        # Flag combination itself must not produce a validation error
        assert "--oauth-client-secret" not in r.stderr

    def test_client_secret_without_id_errors(self):
        r = self._run("--mcp", "https://example.com/mcp", "--oauth-client-secret", "secret", "--list")
        assert r.returncode != 0
        assert "--oauth-client-id" in r.stderr

    def test_oauth_with_stdio_errors(self):
        r = self._run("--mcp-stdio", "echo test", "--oauth", "--list")
        assert r.returncode != 0
        assert "not supported with --mcp-stdio" in r.stderr

    def test_oauth_with_spec_accepted(self):
        """--oauth with --spec should not error on the flag itself (may fail on connection)."""
        r = self._run("--spec", "https://example.com/openapi.json", "--oauth", "--list")
        # Should NOT contain the old MCP-only error
        assert "not supported" not in r.stderr

    def test_oauth_with_graphql_accepted(self):
        """--oauth with --graphql should not error on the flag itself (may fail on connection)."""
        r = self._run("--graphql", "https://example.com/graphql", "--oauth", "--list")
        assert "not supported" not in r.stderr

    def test_oauth_with_local_spec_needs_base_url(self):
        """--oauth with a local spec file requires --base-url for OAuth discovery."""
        r = self._run("--spec", "./local.json", "--oauth", "--list")
        assert r.returncode != 0
        assert "--base-url" in r.stderr

    def test_oauth_flags_in_help(self):
        r = self._run("--help")
        assert "--oauth" in r.stdout
        assert "--oauth-client-id" in r.stdout
        assert "--oauth-client-secret" in r.stdout
        assert "--oauth-scope" in r.stdout
        assert "--oauth-redirect-uri" in r.stdout
        assert "--oauth-flow" in r.stdout

    def test_env_secret_in_client_id(self):
        """--oauth-client-id env:VAR should resolve from environment."""
        env = {**os.environ, "MCP2CLI_TEST_ID": "resolved-id"}
        cmd = [
            sys.executable, "-m", "mcp2cli",
            "--mcp", "https://example.com/mcp",
            "--oauth-client-id", "env:MCP2CLI_TEST_ID",
            "--oauth-client-secret", "literal-secret",
            "--list",
        ]
        # Will fail to connect but should not error on secret resolution
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=10, env=env)
        # Should NOT contain "environment variable" error
        assert "environment variable" not in r.stderr

    def test_env_secret_missing_var_errors(self):
        r = self._run(
            "--mcp", "https://example.com/mcp",
            "--oauth-client-id", "env:NONEXISTENT_VAR_99999",
            "--oauth-client-secret", "secret",
            "--list",
        )
        assert r.returncode != 0
        assert "NONEXISTENT_VAR_99999" in r.stderr


class TestCallbackHandler:
    """Tests for the OAuth callback HTTP handler."""

    def test_callback_captures_code(self):
        import threading
        from http.server import HTTPServer
        from urllib.request import urlopen

        # Reset handler state
        mcp2cli._CallbackHandler.auth_code = None
        mcp2cli._CallbackHandler.state = None
        mcp2cli._CallbackHandler.error = None
        mcp2cli._CallbackHandler.done = threading.Event()

        port = mcp2cli._find_free_port()
        server = HTTPServer(("127.0.0.1", port), mcp2cli._CallbackHandler)
        thread = threading.Thread(target=server.handle_request, daemon=True)
        thread.start()

        urlopen(f"http://127.0.0.1:{port}/callback?code=test-code&state=test-state")
        mcp2cli._CallbackHandler.done.wait(timeout=5)
        server.server_close()

        assert mcp2cli._CallbackHandler.auth_code == "test-code"
        assert mcp2cli._CallbackHandler.state == "test-state"

    def test_callback_captures_error(self):
        import threading
        from http.server import HTTPServer
        from urllib.request import urlopen

        mcp2cli._CallbackHandler.auth_code = None
        mcp2cli._CallbackHandler.state = None
        mcp2cli._CallbackHandler.error = None
        mcp2cli._CallbackHandler.done = threading.Event()

        port = mcp2cli._find_free_port()
        server = HTTPServer(("127.0.0.1", port), mcp2cli._CallbackHandler)
        thread = threading.Thread(target=server.handle_request, daemon=True)
        thread.start()

        urlopen(f"http://127.0.0.1:{port}/callback?error=access_denied")
        mcp2cli._CallbackHandler.done.wait(timeout=5)
        server.server_close()

        assert mcp2cli._CallbackHandler.error == "access_denied"
        assert mcp2cli._CallbackHandler.auth_code is None
