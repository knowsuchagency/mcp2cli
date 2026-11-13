"""Tests for OAuth support."""

import contextlib
import io
import json
import os
import socket
import subprocess
import sys

import pytest

import mcp2cli


def _sdk_httpx():
    """The httpx flavour the installed SDK is built on (v2 uses httpx2)."""
    from mcp.shared import _httpx_utils

    return getattr(_httpx_utils, "httpx2", None) or _httpx_utils.httpx


def _token_endpoint_response(status_code, body=b""):
    """A real response of the httpx flavour the installed SDK reads.

    The SDK's refresh handling does more than look at ``status_code`` and
    ``aread()``: its log line appends ``redirect_note(response)``, which
    reaches for ``response.next_request``. A two-attribute stub therefore
    raises AttributeError on every SDK carrying that diagnostic, so hand the
    provider the type it actually expects.
    """
    httpx = _sdk_httpx()
    return httpx.Response(
        status_code,
        content=body,
        request=httpx.Request("POST", "https://example.com/oauth/token"),
    )


def _code_state(result):
    """Normalize a callback_handler result across SDK majors.

    v1 returns a plain ``(code, state)`` tuple; v2 returns an
    ``AuthorizationCodeResult`` model.
    """
    if isinstance(result, tuple):
        return result
    return (result.code, result.state)


def _sdk_carries_iss():
    """Whether the installed SDK's callback contract can carry an issuer.

    Asked of the SDK directly, never of ``_authorization_code_result``: if that
    helper regressed to a tuple (or dropped ``iss``) under v2, the issuer
    assertions must fail, not quietly switch themselves off.
    """
    try:
        from mcp.shared.auth import AuthorizationCodeResult
    except ImportError:  # v1 hands back a plain (code, state) tuple
        return False
    return "iss" in AuthorizationCodeResult.model_fields


_SDK_CARRIES_ISS = _sdk_carries_iss()


@contextlib.contextmanager
def _listening_socket(host="127.0.0.1"):
    """Hold a loopback port the way another process would; yield its number."""
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as s:
        s.bind((host, 0))
        s.listen()
        yield s.getsockname()[1]


@contextlib.contextmanager
def _parallel_login_listener(host="127.0.0.1"):
    """Hold a loopback port the way a parallel mcp2cli login would; yield its number.

    Unlike a plain socket, this one binds with mcp2cli's own options, so on
    Windows it is the case SO_REUSEADDR on both sides would silently share.
    """
    server = mcp2cli._CallbackHTTPServer(host, 0)
    try:
        yield server.server_address[1]
    finally:
        server.server_close()


def _port_bindable(host, port):
    """Whether the next login's listener can bind *port*.

    Probed with that listener itself: a probe with other bind options answers
    a different question (on Windows SO_REUSEADDR binds over a live listener;
    on POSIX a bare bind fails while the last login's connection is in
    TIME_WAIT).
    """
    try:
        mcp2cli._CallbackHTTPServer(host, port).server_close()
    except OSError:
        return False
    return True


# A busy callback port must be detected whoever holds it. The parallel login
# matters on Windows: there SO_REUSEADDR on both sockets shares the port.
_BUSY_PORT_OCCUPANTS = pytest.mark.parametrize(
    "occupy",
    [_listening_socket, _parallel_login_listener],
    ids=["foreign-listener", "parallel-login"],
)


def _ipv6_loopback_available():
    if not socket.has_ipv6:
        return False
    try:
        with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as s:
            s.bind(("::1", 0))
    except OSError:
        return False
    return True


def _seed_dcr_client(storage, redirect_uri, client_id="cached-dcr-id"):
    """Persist a client.json as a prior Dynamic Client Registration would."""
    import anyio
    from mcp.shared.auth import OAuthClientInformationFull

    async def _seed():
        await storage.set_client_info(
            OAuthClientInformationFull(client_id=client_id, redirect_uris=[redirect_uri])
        )

    anyio.run(_seed)


def _run_browser_login(provider, query, monkeypatch):
    """Run the default flow's redirect + callback handlers against a fake browser.

    The fake browser GETs the provider's redirect_uri with ``query`` from a
    thread, the way the real one lands on the callback after consent. It only
    connects once ``webbrowser.open`` is called, so the request reaches a
    listener only if ``redirect_handler`` bound it before opening the browser.
    """
    import threading
    from urllib.request import urlopen

    import anyio

    redirect_uri = str(provider.context.client_metadata.redirect_uris[0])
    browsers = []

    def _browser(url):
        thread = threading.Thread(
            target=lambda: urlopen(f"{redirect_uri}?{query}", timeout=5).read(),
            daemon=True,
        )
        thread.start()
        browsers.append(thread)
        return True

    monkeypatch.setattr(mcp2cli.webbrowser, "open", _browser)

    async def _login():
        await provider.context.redirect_handler("https://auth.example.com/authorize")
        return await provider.context.callback_handler()

    try:
        return anyio.run(_login)
    finally:
        for thread in browsers:
            thread.join(timeout=5)


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

    def test_set_tokens_writes_expires_at_sidecar(self, tmp_path, monkeypatch):
        """set_tokens persists an absolute expiry timestamp (issue #50)."""
        import time

        monkeypatch.setattr(mcp2cli, "OAUTH_DIR", tmp_path / "oauth")
        storage = mcp2cli.FileTokenStorage("https://example.com/mcp")

        import anyio
        from mcp.shared.auth import OAuthToken

        async def _test():
            before = time.time()
            token = OAuthToken(
                access_token="a", token_type="Bearer",
                refresh_token="r", expires_in=3600,
            )
            await storage.set_tokens(token)
            after = time.time()

            expires_at = storage.get_expires_at()
            assert expires_at is not None
            assert before + 3600 - 1 <= expires_at <= after + 3600 + 1

        anyio.run(_test)

    def test_set_tokens_without_expires_in_clears_sidecar(self, tmp_path, monkeypatch):
        """When expires_in is None, any prior sidecar is removed."""
        monkeypatch.setattr(mcp2cli, "OAUTH_DIR", tmp_path / "oauth")
        storage = mcp2cli.FileTokenStorage("https://example.com/mcp")
        storage._tokens_meta_path.write_text(json.dumps({"expires_at": 1.0}))

        import anyio
        from mcp.shared.auth import OAuthToken

        async def _test():
            await storage.set_tokens(
                OAuthToken(access_token="a", token_type="Bearer")
            )
            assert storage.get_expires_at() is None
            assert not storage._tokens_meta_path.exists()

        anyio.run(_test)

    def test_get_expires_at_missing_sidecar(self, tmp_path, monkeypatch):
        """Older caches with no sidecar return None (backward-compat)."""
        monkeypatch.setattr(mcp2cli, "OAUTH_DIR", tmp_path / "oauth")
        storage = mcp2cli.FileTokenStorage("https://example.com/mcp")
        assert storage.get_expires_at() is None

    def test_clear_client_info_removes_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mcp2cli, "OAUTH_DIR", tmp_path / "oauth")
        storage = mcp2cli.FileTokenStorage("https://example.com/mcp")
        storage._client_path.write_text("{}")
        storage.clear_client_info()
        assert not storage._client_path.exists()
        # Idempotent
        storage.clear_client_info()

    def test_clear_tokens_removes_token_files(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mcp2cli, "OAUTH_DIR", tmp_path / "oauth")
        storage = mcp2cli.FileTokenStorage("https://example.com/mcp")
        storage._tokens_path.write_text("{}")
        storage._tokens_meta_path.write_text("{}")
        storage.clear_tokens()
        assert not storage._tokens_path.exists()
        assert not storage._tokens_meta_path.exists()


class TestRobustOAuthClientProvider:
    """Behavior of the _RobustOAuthClientProvider subclass (issue #50).

    These tests drive ``_initialize`` and ``_handle_refresh_response`` only, so
    they build the provider with ``manual_callback=True``: the default flow
    binds its loopback listener in ``build_oauth_provider`` and only closes it
    once a callback is served, which leaks a bound port for the rest of the
    session (and made these fixed ports collide once a failing test kept the
    provider alive in its traceback).
    """

    def test_initialize_restores_token_expiry_from_sidecar(self, tmp_path, monkeypatch):
        """A fresh process restoring tokens picks up the persisted expiry,
        so an expired access token correctly fails is_token_valid()."""
        import time
        import anyio
        from mcp.shared.auth import OAuthToken

        monkeypatch.setattr(mcp2cli, "OAUTH_DIR", tmp_path / "oauth")
        storage = mcp2cli.FileTokenStorage("https://example.com/mcp")

        async def _setup():
            # Persist a token whose expires_in places expiry in the past
            await storage.set_tokens(
                OAuthToken(
                    access_token="stale",
                    token_type="Bearer",
                    refresh_token="r",
                    expires_in=3600,
                )
            )
            # Rewrite the sidecar so expires_at is firmly in the past.
            storage._tokens_meta_path.write_text(
                json.dumps({"expires_at": time.time() - 60})
            )

        anyio.run(_setup)

        provider = mcp2cli.build_oauth_provider(
            "https://example.com/mcp",
            redirect_uri="http://localhost:19881/callback",
            manual_callback=True,
        )

        async def _drive():
            await provider._initialize()
            # token_expiry_time must be restored (and in the past)
            assert provider.context.token_expiry_time is not None
            assert provider.context.token_expiry_time < time.time()
            # Therefore the access token is not considered valid …
            assert not provider.context.is_token_valid()
            # … but refresh is possible because client_info was pre-seeded
            # by build_oauth_provider's client_id branch? No — without an
            # explicit client_id we have no client_info yet, so the SDK
            # would do a full re-auth. The point of this test is the
            # expiry restoration; a separate test covers DCR recovery.

        anyio.run(_drive)

    def test_initialize_without_sidecar_leaves_expiry_unset(self, tmp_path, monkeypatch):
        """Backward compat: caches written by older versions (no sidecar)
        still load — token_expiry_time stays None (legacy behavior)."""
        import anyio
        from mcp.shared.auth import OAuthToken

        monkeypatch.setattr(mcp2cli, "OAUTH_DIR", tmp_path / "oauth")
        storage = mcp2cli.FileTokenStorage("https://example.com/mcp")
        # Write tokens.json directly (no sidecar) — simulates old cache.
        storage._tokens_path.write_text(
            OAuthToken(
                access_token="a", token_type="Bearer",
                refresh_token="r", expires_in=3600,
            ).model_dump_json()
        )

        provider = mcp2cli.build_oauth_provider(
            "https://example.com/mcp",
            redirect_uri="http://localhost:19882/callback",
            manual_callback=True,
        )

        async def _drive():
            await provider._initialize()
            assert provider.context.current_tokens is not None
            assert provider.context.token_expiry_time is None

        anyio.run(_drive)

    def test_refresh_failure_clears_client_info(self, tmp_path, monkeypatch):
        """A definitive invalid_grant rejection clears the cached DCR
        client_id so the subsequent re-auth performs fresh registration."""
        import anyio
        from mcp.shared.auth import OAuthClientInformationFull

        monkeypatch.setattr(mcp2cli, "OAUTH_DIR", tmp_path / "oauth")
        storage = mcp2cli.FileTokenStorage("https://example.com/mcp")

        async def _setup():
            await storage.set_client_info(
                OAuthClientInformationFull(
                    client_id="stale-dcr-id",
                    redirect_uris=["http://localhost:19883/callback"],
                )
            )

        anyio.run(_setup)
        assert storage._client_path.exists()

        provider = mcp2cli.build_oauth_provider(
            "https://example.com/mcp",
            redirect_uri="http://localhost:19883/callback",
            manual_callback=True,
        )

        # A definitive rejection from the token endpoint.
        response = _token_endpoint_response(400, b'{"error":"invalid_grant"}')

        async def _drive():
            await provider._initialize()
            provider.context.client_info = await storage.get_client_info()
            assert provider.context.client_info is not None

            ok = await provider._handle_refresh_response(response)
            assert ok is False
            # In-memory and on-disk client_info both cleared
            assert provider.context.client_info is None
            assert not storage._client_path.exists()

        anyio.run(_drive)

    def test_transient_refresh_failure_preserves_cache(self, tmp_path, monkeypatch):
        """Issue #59: a transient refresh failure (5xx, network blip) must
        NOT erase client.json/tokens.json, otherwise the next headless run
        is forced into an interactive consent it can never complete."""
        import anyio
        from mcp.shared.auth import OAuthClientInformationFull
        from mcp.shared.auth import OAuthToken

        monkeypatch.setattr(mcp2cli, "OAUTH_DIR", tmp_path / "oauth")
        storage = mcp2cli.FileTokenStorage("https://example.com/mcp")

        async def _setup():
            await storage.set_client_info(
                OAuthClientInformationFull(
                    client_id="dcr-id",
                    redirect_uris=["http://localhost:19883/callback"],
                )
            )
            await storage.set_tokens(
                OAuthToken(
                    access_token="a",
                    token_type="Bearer",
                    refresh_token="r",
                    expires_in=3600,
                )
            )

        anyio.run(_setup)
        assert storage._client_path.exists()
        assert storage._tokens_path.exists()

        provider = mcp2cli.build_oauth_provider(
            "https://example.com/mcp",
            redirect_uri="http://localhost:19883/callback",
            manual_callback=True,
        )

        # A 503 from the token endpoint — transient, recoverable on retry.
        response = _token_endpoint_response(503, b"<html>Service Unavailable</html>")

        async def _drive():
            await provider._initialize()
            provider.context.client_info = await storage.get_client_info()

            ok = await provider._handle_refresh_response(response)
            assert ok is False
            # Persisted OAuth state survives so a later run can refresh again.
            assert provider.context.client_info is not None
            assert storage._client_path.exists()
            assert storage._tokens_path.exists()

        anyio.run(_drive)

    def test_unauthorized_refresh_failure_clears_cache(self, tmp_path, monkeypatch):
        """A bare 401 from the token endpoint means client authentication
        failed (RFC 6749 §5.2) — definitive, so the cache is wiped."""
        import anyio
        from mcp.shared.auth import OAuthClientInformationFull

        monkeypatch.setattr(mcp2cli, "OAUTH_DIR", tmp_path / "oauth")
        storage = mcp2cli.FileTokenStorage("https://example.com/mcp")

        async def _setup():
            await storage.set_client_info(
                OAuthClientInformationFull(
                    client_id="stale-dcr-id",
                    redirect_uris=["http://localhost:19883/callback"],
                )
            )

        anyio.run(_setup)

        provider = mcp2cli.build_oauth_provider(
            "https://example.com/mcp",
            redirect_uri="http://localhost:19883/callback",
            manual_callback=True,
        )

        response = _token_endpoint_response(401)

        async def _drive():
            await provider._initialize()
            provider.context.client_info = await storage.get_client_info()

            ok = await provider._handle_refresh_response(response)
            assert ok is False
            assert provider.context.client_info is None
            assert not storage._client_path.exists()

        anyio.run(_drive)

    def test_refresh_preserves_refresh_token_when_omitted(self, tmp_path, monkeypatch):
        """Issue #58: a successful refresh whose response omits refresh_token
        must carry the previously cached refresh token forward (RFC 6749
        §5.1) and re-persist it, so subsequent refreshes still work."""
        import anyio
        from mcp.shared.auth import OAuthToken

        monkeypatch.setattr(mcp2cli, "OAUTH_DIR", tmp_path / "oauth")
        storage = mcp2cli.FileTokenStorage("https://example.com/mcp")

        async def _setup():
            await storage.set_tokens(
                OAuthToken(
                    access_token="old-access",
                    token_type="Bearer",
                    refresh_token="keep-me",
                    expires_in=3600,
                )
            )

        anyio.run(_setup)

        provider = mcp2cli.build_oauth_provider(
            "https://example.com/mcp",
            redirect_uri="http://localhost:19884/callback",
            manual_callback=True,
        )

        # Refresh response that rotates the access token but omits refresh_token
        response = _token_endpoint_response(
            200,
            b'{"access_token":"new-access","token_type":"Bearer","expires_in":3600}',
        )

        async def _drive():
            await provider._initialize()
            ok = await provider._handle_refresh_response(response)
            assert ok is True
            # Old refresh token carried forward in memory …
            assert provider.context.current_tokens.access_token == "new-access"
            assert provider.context.current_tokens.refresh_token == "keep-me"
            # … and persisted to disk for the next process.
            persisted = await storage.get_tokens()
            assert persisted.refresh_token == "keep-me"

        anyio.run(_drive)

    def test_refresh_uses_new_refresh_token_when_present(self, tmp_path, monkeypatch):
        """When the server DOES issue a new refresh token, it replaces the
        old one (the old token must be discarded per RFC 6749 §5.1)."""
        import anyio
        from mcp.shared.auth import OAuthToken

        monkeypatch.setattr(mcp2cli, "OAUTH_DIR", tmp_path / "oauth")
        storage = mcp2cli.FileTokenStorage("https://example.com/mcp")

        async def _setup():
            await storage.set_tokens(
                OAuthToken(
                    access_token="old-access",
                    token_type="Bearer",
                    refresh_token="old-refresh",
                    expires_in=3600,
                )
            )

        anyio.run(_setup)

        provider = mcp2cli.build_oauth_provider(
            "https://example.com/mcp",
            redirect_uri="http://localhost:19885/callback",
            manual_callback=True,
        )

        response = _token_endpoint_response(
            200,
            b'{"access_token":"new-access","token_type":"Bearer",'
            b'"refresh_token":"new-refresh","expires_in":3600}',
        )

        async def _drive():
            await provider._initialize()
            ok = await provider._handle_refresh_response(response)
            assert ok is True
            assert provider.context.current_tokens.refresh_token == "new-refresh"

        anyio.run(_drive)


class TestRobustClientCredentialsProvider:
    """Behavior of the client-credentials provider subclass (issue #57)."""

    def test_initialize_restores_token_expiry_from_sidecar(self, tmp_path, monkeypatch):
        """A fresh process using the client-credentials flow restores the
        persisted expiry, so an expired access token fails is_token_valid()
        and re-auth happens proactively instead of after a wasted 401."""
        import time
        import anyio
        from mcp.shared.auth import OAuthToken

        monkeypatch.setattr(mcp2cli, "OAUTH_DIR", tmp_path / "oauth")
        storage = mcp2cli.FileTokenStorage("https://example.com/mcp")

        async def _setup():
            await storage.set_tokens(
                OAuthToken(
                    access_token="stale",
                    token_type="Bearer",
                    expires_in=3600,
                )
            )
            storage._tokens_meta_path.write_text(
                json.dumps({"expires_at": time.time() - 60})
            )

        anyio.run(_setup)

        provider = mcp2cli.build_oauth_provider(
            "https://example.com/mcp",
            client_id="my-id",
            client_secret="my-secret",
        )

        async def _drive():
            await provider._initialize()
            assert provider.context.token_expiry_time is not None
            assert provider.context.token_expiry_time < time.time()
            assert not provider.context.is_token_valid()

        anyio.run(_drive)


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

    def test_auth_code_random_port_when_no_redirect_uri(self, tmp_path, monkeypatch):
        """Without redirect_uri and no cached client, _find_free_port() is called and the default URI is built."""
        # Isolate OAUTH_DIR so no stale client.json interferes
        monkeypatch.setattr(mcp2cli, "OAUTH_DIR", tmp_path / "oauth")

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
        assert "--oauth-manual-callback" in r.stdout

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


class TestLocalListenerCallbackResult:
    """The default flow's callback_handler, driven by a real redirect request.

    ``redirect_handler`` binds the loopback listener when the login starts; the
    handler serves one request and turns it into whatever the installed SDK
    expects. These tests issue the redirect over HTTP the way a browser would,
    so the whole path -- lazy bind, handler, class state, result
    construction -- is exercised.
    """

    def test_redirect_with_iss_is_forwarded(self, tmp_path, monkeypatch):
        """RFC 9207: an issuer that reaches the listener must reach the SDK, or
        the flow dies with 'Authorization response missing iss parameter'."""
        monkeypatch.setattr(mcp2cli, "OAUTH_DIR", tmp_path / "oauth")
        provider = mcp2cli.build_oauth_provider("https://example.com/mcp")

        result = _run_browser_login(
            provider,
            "code=live-code&state=live-state&iss=https%3A%2F%2Fissuer.example.com",
            monkeypatch,
        )

        assert _code_state(result) == ("live-code", "live-state")
        if _SDK_CARRIES_ISS:
            assert result.iss == "https://issuer.example.com"

    def test_redirect_without_iss_stays_absent(self, tmp_path, monkeypatch):
        """Servers that omit iss must not be handed an empty one: the SDK only
        rejects a missing issuer when the metadata advertised it."""
        monkeypatch.setattr(mcp2cli, "OAUTH_DIR", tmp_path / "oauth")
        provider = mcp2cli.build_oauth_provider("https://example.com/mcp")

        result = _run_browser_login(provider, "code=plain-code&state=plain-state", monkeypatch)

        assert _code_state(result) == ("plain-code", "plain-state")
        if _SDK_CARRIES_ISS:
            assert result.iss is None


class TestCachedRedirectUriReuse:
    """Tests for issue #54 fix: reuse cached redirect_uri when port is free."""

    def test_no_cache_picks_random_port(self, tmp_path, monkeypatch):
        """When no client.json exists, a random port is chosen as before."""
        monkeypatch.setattr(mcp2cli, "OAUTH_DIR", tmp_path / "oauth")

        # Ensure no cached client
        storage = mcp2cli.FileTokenStorage("https://example.com/mcp")
        assert not storage._client_path.exists()

        provider = mcp2cli.build_oauth_provider("https://example.com/mcp")
        redirect_uris = [str(u) for u in provider.context.client_metadata.redirect_uris]
        # Should be a loopback URI with some port
        assert len(redirect_uris) == 1
        assert redirect_uris[0].startswith("http://127.0.0.1:")
        assert redirect_uris[0].endswith("/callback")

    def test_cached_redirect_uri_reused(self, tmp_path, monkeypatch):
        """When client.json exists with a redirect_uri, that port is reused."""
        import anyio
        from mcp.shared.auth import OAuthClientInformationFull

        monkeypatch.setattr(mcp2cli, "OAUTH_DIR", tmp_path / "oauth")
        storage = mcp2cli.FileTokenStorage("https://example.com/mcp")

        # Pick a port that is free right now
        port = mcp2cli._find_free_port()

        async def _seed():
            await storage.set_client_info(
                OAuthClientInformationFull(
                    client_id="cached-dcr-id",
                    redirect_uris=[f"http://127.0.0.1:{port}/callback"],
                )
            )

        anyio.run(_seed)

        # Now build the provider — should reuse the cached port
        provider = mcp2cli.build_oauth_provider("https://example.com/mcp")
        redirect_uris = [str(u) for u in provider.context.client_metadata.redirect_uris]
        assert redirect_uris[0] == f"http://127.0.0.1:{port}/callback"

    def test_cached_redirect_uri_kept_when_port_busy_at_build(self, tmp_path, monkeypatch):
        """A busy cached port at build time must not discard the registration.

        Another mcp2cli run for the same server may be holding the port for
        its own login. Wiping client.json here orphans tokens.json: with no
        client_info the SDK cannot refresh, so the next expiry forces a full
        DCR + browser login although the refresh token is still valid.
        """
        monkeypatch.setattr(mcp2cli, "OAUTH_DIR", tmp_path / "oauth")
        storage = mcp2cli.FileTokenStorage("https://example.com/mcp")

        with _listening_socket() as taken_port:
            cached_uri = f"http://127.0.0.1:{taken_port}/callback"
            _seed_dcr_client(storage, cached_uri)

            provider = mcp2cli.build_oauth_provider("https://example.com/mcp")

        redirect_uris = [str(u) for u in provider.context.client_metadata.redirect_uris]
        assert redirect_uris == [cached_uri]
        assert storage._client_path.exists()
        assert json.loads(storage._client_path.read_text())["client_id"] == "cached-dcr-id"

    def test_get_cached_redirect_uri_no_file(self, tmp_path, monkeypatch):
        """_get_cached_redirect_uri returns None when client.json absent."""
        monkeypatch.setattr(mcp2cli, "OAUTH_DIR", tmp_path / "oauth")
        storage = mcp2cli.FileTokenStorage("https://example.com/mcp")
        assert mcp2cli._get_cached_redirect_uri(storage) is None

    def test_get_cached_redirect_uri_with_file(self, tmp_path, monkeypatch):
        """_get_cached_redirect_uri reads the first redirect_uri from client.json."""
        monkeypatch.setattr(mcp2cli, "OAUTH_DIR", tmp_path / "oauth")
        storage = mcp2cli.FileTokenStorage("https://example.com/mcp")
        storage._client_path.write_text(
            json.dumps({"redirect_uris": ["http://127.0.0.1:43210/callback"]})
        )
        assert mcp2cli._get_cached_redirect_uri(storage) == "http://127.0.0.1:43210/callback"

    def test_get_cached_redirect_uri_corrupt_file(self, tmp_path, monkeypatch):
        """_get_cached_redirect_uri returns None for corrupt client.json."""
        monkeypatch.setattr(mcp2cli, "OAUTH_DIR", tmp_path / "oauth")
        storage = mcp2cli.FileTokenStorage("https://example.com/mcp")
        storage._client_path.write_text("not json{{{")
        assert mcp2cli._get_cached_redirect_uri(storage) is None

    def test_get_cached_redirect_uri_empty_uris(self, tmp_path, monkeypatch):
        """_get_cached_redirect_uri returns None when redirect_uris is empty."""
        monkeypatch.setattr(mcp2cli, "OAUTH_DIR", tmp_path / "oauth")
        storage = mcp2cli.FileTokenStorage("https://example.com/mcp")
        storage._client_path.write_text(json.dumps({"redirect_uris": []}))
        assert mcp2cli._get_cached_redirect_uri(storage) is None

    def test_explicit_redirect_uri_not_affected_by_cache(self, tmp_path, monkeypatch):
        """An explicit redirect_uri is not overridden by a cached client."""
        import anyio
        from mcp.shared.auth import OAuthClientInformationFull

        monkeypatch.setattr(mcp2cli, "OAUTH_DIR", tmp_path / "oauth")
        storage = mcp2cli.FileTokenStorage("https://example.com/mcp")

        async def _seed():
            await storage.set_client_info(
                OAuthClientInformationFull(
                    client_id="cached-dcr-id",
                    redirect_uris=["http://127.0.0.1:54321/callback"],
                )
            )

        anyio.run(_seed)

        # Pass an explicit redirect_uri — should be used as-is
        provider = mcp2cli.build_oauth_provider(
            "https://example.com/mcp",
            redirect_uri="http://localhost:19890/callback",
        )
        redirect_uris = [str(u) for u in provider.context.client_metadata.redirect_uris]
        assert "http://localhost:19890/callback" in redirect_uris
        assert "http://127.0.0.1:54321/callback" not in redirect_uris


class TestLazyCallbackListener:
    """The loopback listener is bound when a browser login starts, not before.

    Every CLI invocation builds a provider, but most never log in (the token is
    valid or refreshable). Binding at build time made parallel invocations for
    the same server collide on the callback port: with a cached registration
    the loser wiped client.json, orphaning tokens.json; with a fixed
    --oauth-redirect-uri it died with "Address already in use".
    """

    URL = "https://example.com/mcp"

    def test_build_binds_no_listener(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mcp2cli, "OAUTH_DIR", tmp_path / "oauth")

        def _explode(*args, **kwargs):
            raise AssertionError("building a provider must not bind a listener")

        monkeypatch.setattr(mcp2cli, "_CallbackHTTPServer", _explode)
        mcp2cli.build_oauth_provider(self.URL)

    def test_parallel_builds_with_explicit_redirect_uri(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mcp2cli, "OAUTH_DIR", tmp_path / "oauth")
        port = mcp2cli._find_free_port()
        uri = f"http://127.0.0.1:{port}/callback"

        first = mcp2cli.build_oauth_provider(self.URL, redirect_uri=uri)
        second = mcp2cli.build_oauth_provider(self.URL, redirect_uri=uri)

        assert first is not second
        assert _port_bindable("127.0.0.1", port)

    def test_parallel_builds_share_cached_registration(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mcp2cli, "OAUTH_DIR", tmp_path / "oauth")
        storage = mcp2cli.FileTokenStorage(self.URL)
        port = mcp2cli._find_free_port()
        cached_uri = f"http://127.0.0.1:{port}/callback"
        _seed_dcr_client(storage, cached_uri)

        providers = [mcp2cli.build_oauth_provider(self.URL) for _ in range(2)]

        for provider in providers:
            uris = [str(u) for u in provider.context.client_metadata.redirect_uris]
            assert uris == [cached_uri]
        assert json.loads(storage._client_path.read_text())["client_id"] == "cached-dcr-id"

    def test_login_serves_redirect_and_releases_port(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mcp2cli, "OAUTH_DIR", tmp_path / "oauth")
        provider = mcp2cli.build_oauth_provider(self.URL)
        port = mcp2cli.urlparse(
            str(provider.context.client_metadata.redirect_uris[0])
        ).port

        result = _run_browser_login(provider, "code=c1&state=s1", monkeypatch)

        assert _code_state(result) == ("c1", "s1")
        assert _port_bindable("127.0.0.1", port)

    def test_each_login_binds_its_own_listener(self, tmp_path, monkeypatch):
        """A provider that logs in twice (a 401 after the first login) rebinds."""
        monkeypatch.setattr(mcp2cli, "OAUTH_DIR", tmp_path / "oauth")
        provider = mcp2cli.build_oauth_provider(self.URL)

        first = _run_browser_login(provider, "code=c1&state=s1", monkeypatch)
        second = _run_browser_login(provider, "code=c2&state=s2", monkeypatch)

        assert _code_state(first) == ("c1", "s1")
        assert _code_state(second) == ("c2", "s2")

    @pytest.mark.skipif(not _ipv6_loopback_available(), reason="no IPv6 loopback")
    def test_ipv6_redirect_uri_login(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mcp2cli, "OAUTH_DIR", tmp_path / "oauth")
        with _listening_socket("::1") as probe:
            port = probe
        provider = mcp2cli.build_oauth_provider(
            self.URL, redirect_uri=f"http://[::1]:{port}/callback"
        )

        result = _run_browser_login(provider, "code=v6&state=s6", monkeypatch)

        assert _code_state(result) == ("v6", "s6")

    def test_callback_timeout_closes_listener(self, tmp_path, monkeypatch):
        import anyio

        monkeypatch.setattr(mcp2cli, "OAUTH_DIR", tmp_path / "oauth")
        monkeypatch.setattr(mcp2cli.webbrowser, "open", lambda url: True)
        port = mcp2cli._find_free_port()
        provider = mcp2cli.build_oauth_provider(
            self.URL, redirect_uri=f"http://127.0.0.1:{port}/callback"
        )
        anyio.run(provider.context.redirect_handler, "https://auth.example.com/a")

        class _NeverSet:
            def wait(self, timeout=None):
                return False

        monkeypatch.setattr(mcp2cli._CallbackHandler, "done", _NeverSet())
        with pytest.raises(TimeoutError):
            anyio.run(provider.context.callback_handler)
        assert _port_bindable("127.0.0.1", port)

    @_BUSY_PORT_OCCUPANTS
    def test_busy_port_at_login_discards_cached_registration(
        self, tmp_path, monkeypatch, occupy
    ):
        """Issue #54 recovery, moved to the moment a listener is needed.

        The registration is pinned to a port something else holds, so it is
        dropped and the next run registers a fresh port. Tokens are kept.
        """
        import anyio
        from mcp.shared.auth import OAuthToken

        monkeypatch.setattr(mcp2cli, "OAUTH_DIR", tmp_path / "oauth")
        opened = []
        monkeypatch.setattr(mcp2cli.webbrowser, "open", opened.append)
        storage = mcp2cli.FileTokenStorage(self.URL)
        anyio.run(
            storage.set_tokens,
            OAuthToken(access_token="a", token_type="Bearer", refresh_token="r"),
        )

        with occupy() as taken_port:
            _seed_dcr_client(storage, f"http://127.0.0.1:{taken_port}/callback")
            provider = mcp2cli.build_oauth_provider(self.URL)
            with pytest.raises(
                mcp2cli.OAuthCallbackPortInUseError, match=str(taken_port)
            ):
                anyio.run(provider.context.redirect_handler, "https://auth.example.com/a")

            assert opened == [], "no browser for a login that cannot complete"
            assert not storage._client_path.exists()
            assert storage._tokens_path.exists()

            fresh = mcp2cli.build_oauth_provider(self.URL)
            fresh_uri = str(fresh.context.client_metadata.redirect_uris[0])
            assert mcp2cli.urlparse(fresh_uri).port != taken_port

    @_BUSY_PORT_OCCUPANTS
    @pytest.mark.parametrize("client_id", [None, "fixed-client-id"])
    def test_busy_port_at_login_keeps_client_for_explicit_redirect_uri(
        self, tmp_path, monkeypatch, client_id, occupy
    ):
        """The caller fixed the port, so a new registration would hit the same
        busy port: report it and keep the registration."""
        import anyio

        monkeypatch.setattr(mcp2cli, "OAUTH_DIR", tmp_path / "oauth")
        monkeypatch.setattr(mcp2cli.webbrowser, "open", lambda url: True)
        storage = mcp2cli.FileTokenStorage(self.URL)

        with occupy() as taken_port:
            uri = f"http://127.0.0.1:{taken_port}/callback"
            if client_id is None:
                _seed_dcr_client(storage, uri)
            provider = mcp2cli.build_oauth_provider(
                self.URL, client_id=client_id, redirect_uri=uri
            )
            with pytest.raises(
                mcp2cli.OAuthCallbackPortInUseError, match=str(taken_port)
            ):
                anyio.run(provider.context.redirect_handler, "https://auth.example.com/a")

        expected = client_id or "cached-dcr-id"
        assert json.loads(storage._client_path.read_text())["client_id"] == expected


class TestCallbackServerBindOptions:
    """The callback listener's bind options, checked for both platforms anywhere.

    HTTPServer sets SO_REUSEADDR. On POSIX that only lets a port in TIME_WAIT
    be rebound, which the cached callback port needs. On Windows it lets a
    socket bind a port another one is listening on, so a busy port would go
    unnoticed and the redirect could reach the other listener; there the port
    must be held with SO_EXCLUSIVEADDRUSE instead. The real-socket tests above
    only see the semantics of the platform they run on, so the selection is
    pinned here against a socket stub.
    """

    class _SocketStub:
        def __init__(self, family, kind):
            self.family = family
            self.options = {}
            self.address = None

        def setsockopt(self, level, option, value):
            self.options[(level, option)] = value

        def bind(self, address):
            self.address = address

        def getsockname(self):
            return self.address

        def listen(self, backlog):
            pass

        def close(self):
            pass

    @pytest.fixture
    def bind(self, monkeypatch):
        """Construct the listener on a stub socket; return the stub."""
        monkeypatch.setattr(socket, "socket", self._SocketStub)

        def _bind(host):
            server = mcp2cli._CallbackHTTPServer(host, 5311)
            server.server_close()
            return server.socket

        return _bind

    @pytest.mark.parametrize(
        ("host", "family"),
        [("127.0.0.1", socket.AF_INET), ("::1", socket.AF_INET6)],
        ids=["ipv4", "ipv6"],
    )
    def test_windows_holds_the_port_exclusively(self, bind, monkeypatch, host, family):
        exclusive = getattr(socket, "SO_EXCLUSIVEADDRUSE", -5)
        monkeypatch.setattr(socket, "SO_EXCLUSIVEADDRUSE", exclusive, raising=False)
        monkeypatch.setattr(mcp2cli, "IS_WINDOWS", True)

        stub = bind(host)

        assert stub.family == family
        assert stub.address == (host, 5311)
        assert stub.options == {(socket.SOL_SOCKET, exclusive): 1}

    @pytest.mark.parametrize(
        ("host", "family"),
        [("127.0.0.1", socket.AF_INET), ("::1", socket.AF_INET6)],
        ids=["ipv4", "ipv6"],
    )
    def test_posix_reuses_a_time_wait_port(self, bind, monkeypatch, host, family):
        monkeypatch.setattr(mcp2cli, "IS_WINDOWS", False)

        stub = bind(host)

        assert stub.family == family
        assert stub.address == (host, 5311)
        assert stub.options == {(socket.SOL_SOCKET, socket.SO_REUSEADDR): 1}


class TestParseOAuthCallbackInput:
    """Tests for parsing a pasted OAuth callback URL (issue #71)."""

    def test_full_url(self):
        code, state, iss = mcp2cli._parse_oauth_callback_input(
            "http://127.0.0.1:5311/callback?code=abc123&state=xyz789"
        )
        assert (code, state, iss) == ("abc123", "xyz789", None)

    def test_query_string_only(self):
        """A user who copied only the query part still gets through."""
        assert mcp2cli._parse_oauth_callback_input("code=abc&state=xyz") == (
            "abc",
            "xyz",
            None,
        )

    def test_strips_surrounding_whitespace_and_quotes(self):
        code, state, _ = mcp2cli._parse_oauth_callback_input(
            '  "http://127.0.0.1:1/callback?code=a&state=b"\n'
        )
        assert (code, state) == ("a", "b")

    def test_percent_encoded_code_is_decoded(self):
        code, _, _ = mcp2cli._parse_oauth_callback_input(
            "http://127.0.0.1:1/callback?code=a%2Fb%3Dc&state=s"
        )
        assert code == "a/b=c"

    def test_iss_is_extracted_and_decoded(self):
        """RFC 9207: the SDK validates iss when the server advertises it, so a
        dropped iss fails the flow with 'Authorization response missing iss'."""
        code, state, iss = mcp2cli._parse_oauth_callback_input(
            "http://127.0.0.1:1/callback?code=a&state=b"
            "&iss=https%3A%2F%2Fclerk.example.com"
        )
        assert (code, state, iss) == ("a", "b", "https://clerk.example.com")

    def test_empty_iss_is_reported_as_absent(self):
        """An empty ``iss=`` must reach the SDK as ``None``: absent is accepted
        unless the server advertised the parameter, whereas ``""`` would be
        rejected as an issuer mismatch."""
        assert mcp2cli._parse_oauth_callback_input(
            "http://127.0.0.1:1/callback?code=a&state=b&iss="
        ) == ("a", "b", None)

    def test_error_redirect_raises_with_description(self):
        with pytest.raises(RuntimeError, match=r"access_denied \(user said no\)"):
            mcp2cli._parse_oauth_callback_input(
                "http://127.0.0.1:1/callback?error=access_denied"
                "&error_description=user+said+no"
            )

    def test_missing_code_rejected(self):
        with pytest.raises(ValueError, match="no 'code' parameter"):
            mcp2cli._parse_oauth_callback_input("http://127.0.0.1:1/callback?state=xyz")

    def test_missing_state_rejected(self):
        """The SDK compares state with compare_digest and treats None as a
        mismatch, so demand it here instead of emitting its opaque error."""
        with pytest.raises(ValueError, match="no 'state' parameter"):
            mcp2cli._parse_oauth_callback_input("http://127.0.0.1:1/callback?code=abc")

    def test_empty_input_rejected(self):
        with pytest.raises(ValueError, match="No callback URL"):
            mcp2cli._parse_oauth_callback_input("   \n")


class TestPromptOAuthCallback:
    """Tests for reading the callback URL from stdin (issue #71)."""

    def test_reads_url_from_stdin(self, monkeypatch, capsys):
        monkeypatch.setattr(
            sys, "stdin", io.StringIO("http://127.0.0.1:1/callback?code=c1&state=s1\n")
        )
        assert mcp2cli._prompt_oauth_callback() == ("c1", "s1", None)
        assert "Paste the full callback URL" in capsys.readouterr().err

    def test_reprompts_after_malformed_paste(self, monkeypatch, capsys):
        """A typo must not burn the still-valid authorization code."""
        monkeypatch.setattr(
            sys,
            "stdin",
            io.StringIO("not-a-url\nhttp://127.0.0.1:1/callback?code=c2&state=s2\n"),
        )
        assert mcp2cli._prompt_oauth_callback() == ("c2", "s2", None)
        assert "attempt(s) left" in capsys.readouterr().err

    def test_gives_up_after_exhausting_attempts(self, monkeypatch):
        monkeypatch.setattr(sys, "stdin", io.StringIO("junk\njunk\njunk\nignored\n"))
        with pytest.raises(ValueError):
            mcp2cli._prompt_oauth_callback(attempts=3)

    def test_eof_raises_runtime_error(self, monkeypatch):
        monkeypatch.setattr(sys, "stdin", io.StringIO(""))
        with pytest.raises(RuntimeError, match="stdin closed"):
            mcp2cli._prompt_oauth_callback()

    def test_server_side_error_is_not_reprompted(self, monkeypatch):
        """An error= redirect is terminal: re-asking the user cannot help."""
        monkeypatch.setattr(
            sys,
            "stdin",
            io.StringIO("http://127.0.0.1:1/callback?error=access_denied\n"),
        )
        with pytest.raises(RuntimeError, match="access_denied"):
            mcp2cli._prompt_oauth_callback()


class TestManualCallbackProvider:
    """--oauth-manual-callback binds no local listener (issue #71)."""

    def test_manual_callback_binds_no_local_server(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mcp2cli, "OAUTH_DIR", tmp_path / "oauth")

        def _explode(*args, **kwargs):
            raise AssertionError("manual callback must not bind a local HTTP server")

        monkeypatch.setattr(mcp2cli, "_CallbackHTTPServer", _explode)
        from mcp.client.auth.oauth2 import OAuthClientProvider

        provider = mcp2cli.build_oauth_provider(
            "https://example.com/mcp", manual_callback=True
        )
        assert isinstance(provider, OAuthClientProvider)

    def test_default_flow_binds_a_server_only_when_login_starts(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(mcp2cli, "OAUTH_DIR", tmp_path / "oauth")
        monkeypatch.setattr(mcp2cli.webbrowser, "open", lambda url: True)
        original = mcp2cli._CallbackHTTPServer
        servers = []

        def _spy(host, port):
            server = original(host, port)
            servers.append(server)
            return server

        monkeypatch.setattr(mcp2cli, "_CallbackHTTPServer", _spy)
        try:
            provider = mcp2cli.build_oauth_provider("https://example.com/mcp")
            assert servers == []
            import anyio

            anyio.run(provider.context.redirect_handler, "https://auth.example.com/a")
            assert len(servers) == 1
        finally:
            for server in servers:
                server.server_close()

    def test_manual_callback_handler_reads_stdin(self, tmp_path, monkeypatch):
        import anyio

        monkeypatch.setattr(mcp2cli, "OAUTH_DIR", tmp_path / "oauth")
        provider = mcp2cli.build_oauth_provider(
            "https://example.com/mcp", manual_callback=True
        )
        monkeypatch.setattr(
            sys, "stdin", io.StringIO("http://127.0.0.1:1/callback?code=zz&state=yy\n")
        )
        assert _code_state(anyio.run(provider.context.callback_handler)) == ("zz", "yy")

    def test_manual_callback_handler_forwards_iss(self, tmp_path, monkeypatch):
        """Servers that send iss (RFC 9207) fail the token exchange unless the
        handler forwards it to the SDK."""
        import anyio

        monkeypatch.setattr(mcp2cli, "OAUTH_DIR", tmp_path / "oauth")
        provider = mcp2cli.build_oauth_provider(
            "https://example.com/mcp", manual_callback=True
        )
        monkeypatch.setattr(
            sys,
            "stdin",
            io.StringIO(
                "http://127.0.0.1:1/callback?code=zz&state=yy"
                "&iss=https%3A%2F%2Fissuer.example.com\n"
            ),
        )
        result = anyio.run(provider.context.callback_handler)

        assert _code_state(result) == ("zz", "yy")
        if _SDK_CARRIES_ISS:
            assert result.iss == "https://issuer.example.com"

    def test_manual_redirect_handler_prints_url_and_skips_browser(
        self, tmp_path, monkeypatch, capsys
    ):
        import anyio

        monkeypatch.setattr(mcp2cli, "OAUTH_DIR", tmp_path / "oauth")
        opened = []
        monkeypatch.setattr(mcp2cli.webbrowser, "open", opened.append)
        provider = mcp2cli.build_oauth_provider(
            "https://example.com/mcp", manual_callback=True
        )
        auth_url = "https://auth.example.com/authorize?client_id=x"
        anyio.run(provider.context.redirect_handler, auth_url)
        err = capsys.readouterr().err
        assert auth_url in err
        assert opened == [], "must not try to open a browser on a headless host"

    def test_baked_config_round_trips_flag(self):
        argv = mcp2cli._baked_to_argv(
            {
                "source_type": "mcp",
                "source": "https://example.com/mcp",
                "oauth": True,
                "oauth_manual_callback": True,
            }
        )
        assert "--oauth-manual-callback" in argv

    def test_baked_config_omits_flag_when_unset(self):
        argv = mcp2cli._baked_to_argv(
            {
                "source_type": "mcp",
                "source": "https://example.com/mcp",
                "oauth": True,
            }
        )
        assert "--oauth-manual-callback" not in argv


def _fake_authorization_server(token_forms):
    """An httpx responder playing MCP server + authorization server (with DCR).

    Every token-endpoint form is appended to ``token_forms``.
    """
    httpx = _sdk_httpx()

    def respond(request):
        path = request.url.path
        if path == "/mcp" and "authorization" not in request.headers:
            return httpx.Response(
                401,
                headers={
                    "WWW-Authenticate": "Bearer resource_metadata="
                    '"https://as.example.com/.well-known/oauth-protected-resource"'
                },
                request=request,
            )
        if path == "/.well-known/oauth-protected-resource":
            payload = {
                "resource": "https://as.example.com/mcp",
                "authorization_servers": ["https://as.example.com"],
            }
        elif "oauth-authorization-server" in path or path.endswith(
            "openid-configuration"
        ):
            payload = {
                "issuer": "https://as.example.com",
                "authorization_endpoint": "https://as.example.com/authorize",
                "token_endpoint": "https://as.example.com/token",
                "registration_endpoint": "https://as.example.com/register",
                "response_types_supported": ["code"],
                "grant_types_supported": ["authorization_code", "refresh_token"],
                "code_challenge_methods_supported": ["S256"],
            }
        elif path == "/register":
            body = json.loads(request.content)
            return httpx.Response(
                201,
                json={
                    "client_id": "dcr-client",
                    "redirect_uris": body["redirect_uris"],
                    "grant_types": body.get(
                        "grant_types", ["authorization_code", "refresh_token"]
                    ),
                },
                request=request,
            )
        elif path == "/token":
            form = httpx.QueryParams(request.content.decode())
            token_forms.append(form)
            payload = {
                "access_token": "TOKEN-OK",
                "token_type": "Bearer",
                "expires_in": 3600,
            }
        else:
            payload = {"ok": True}
        return httpx.Response(200, json=payload, request=request)

    return respond


def _drive_auth_flow(provider, respond):
    """Run the provider's auth flow for one MCP request; return the final request."""
    import anyio

    httpx = _sdk_httpx()

    async def drive():
        request = httpx.Request("GET", "https://as.example.com/mcp")
        flow = provider.async_auth_flow(request)
        outbound = await flow.__anext__()
        while True:
            try:
                outbound = await flow.asend(respond(outbound))
            except StopAsyncIteration:
                return outbound

    return anyio.run(drive)


class TestManualCallbackEndToEnd:
    """The pasted URL must actually complete the PKCE exchange (issue #71)."""

    def test_pasted_callback_completes_token_exchange(self, tmp_path, monkeypatch):
        httpx = _sdk_httpx()

        monkeypatch.setattr(mcp2cli, "OAUTH_DIR", tmp_path / "oauth")
        provider = mcp2cli.build_oauth_provider(
            "https://as.example.com/mcp", manual_callback=True
        )

        # Stand in for the browser on the user's laptop: take the URL mcp2cli
        # printed, then type back exactly what that browser's address bar
        # would show. State comes from the real auth URL, so the SDK's
        # compare_digest check is genuinely exercised.
        original_redirect = provider.context.redirect_handler

        async def redirect_handler(url):
            await original_redirect(url)
            state = httpx.URL(url).params["state"]
            monkeypatch.setattr(
                sys,
                "stdin",
                io.StringIO(f"http://127.0.0.1:9/callback?code=THE-CODE&state={state}\n"),
            )

        provider.context.redirect_handler = redirect_handler

        token_forms = []
        final = _drive_auth_flow(provider, _fake_authorization_server(token_forms))

        assert len(token_forms) == 1, "expected exactly one token exchange"
        assert token_forms[0].get("code") == "THE-CODE"
        assert token_forms[0].get("code_verifier"), "PKCE verifier must be sent"
        assert final.headers.get("authorization") == "Bearer TOKEN-OK"


class TestBrowserCallbackEndToEnd:
    """The default flow completes a real PKCE exchange with a lazily bound listener."""

    def test_browser_redirect_completes_token_exchange(self, tmp_path, monkeypatch):
        import threading
        from urllib.request import urlopen

        httpx = _sdk_httpx()

        monkeypatch.setattr(mcp2cli, "OAUTH_DIR", tmp_path / "oauth")
        provider = mcp2cli.build_oauth_provider("https://as.example.com/mcp")
        redirect_uri = str(provider.context.client_metadata.redirect_uris[0])
        port = mcp2cli.urlparse(redirect_uri).port
        browsers = []

        def _browser(url):
            state = httpx.URL(url).params["state"]
            thread = threading.Thread(
                target=lambda: urlopen(
                    f"{redirect_uri}?code=THE-CODE&state={state}", timeout=5
                ).read(),
                daemon=True,
            )
            thread.start()
            browsers.append(thread)
            return True

        monkeypatch.setattr(mcp2cli.webbrowser, "open", _browser)
        token_forms = []
        try:
            final = _drive_auth_flow(provider, _fake_authorization_server(token_forms))
        finally:
            for thread in browsers:
                thread.join(timeout=5)

        assert len(browsers) == 1
        assert [form.get("code") for form in token_forms] == ["THE-CODE"]
        assert final.headers.get("authorization") == "Bearer TOKEN-OK"
        assert _port_bindable("127.0.0.1", port), "listener closed after the login"
        storage = mcp2cli.FileTokenStorage("https://as.example.com/mcp")
        assert json.loads(storage._client_path.read_text())["redirect_uris"] == [
            redirect_uri
        ]
