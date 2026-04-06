"""mcp2cli — Turn any MCP server or OpenAPI spec into a CLI."""

from __future__ import annotations

__version__ = "2.7.0"

import argparse
import copy
import hashlib
import json
import mimetypes
import os
import fnmatch
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from dataclasses import dataclass, field
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from datetime import datetime, timezone

import anyio
import httpx

CACHE_DIR = Path(
    os.environ.get("MCP2CLI_CACHE_DIR", Path.home() / ".cache" / "mcp2cli")
)
DEFAULT_CACHE_TTL = 3600
USAGE_FILE = CACHE_DIR / "usage.json"
CONFIG_DIR = Path(
    os.environ.get("MCP2CLI_CONFIG_DIR", Path.home() / ".config" / "mcp2cli")
)
BAKED_FILE = CONFIG_DIR / "baked.json"
ARGPARSE_HELP_PERCENT_RE = re.compile(r"(?<!%)%(?![%\(])")


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class ParamDef:
    name: str  # kebab-case CLI flag
    original_name: str  # original name for API/tool call
    python_type: type | None  # None means boolean (store_true)
    required: bool = False
    description: str = ""
    choices: list | None = None
    location: str = "body"  # path|query|header|body|tool_input
    schema: dict = field(default_factory=dict)


@dataclass
class CommandDef:
    name: str
    description: str = ""
    params: list[ParamDef] = field(default_factory=list)
    has_body: bool = False
    # OpenAPI
    method: str | None = None
    path: str | None = None
    content_type: str | None = None  # None = json, "multipart/form-data", etc.
    # MCP
    tool_name: str | None = None
    # GraphQL
    graphql_operation_type: str | None = None  # "query" or "mutation"
    graphql_field_name: str | None = None      # original field name pre-kebab
    graphql_return_type: dict | None = None    # return type info for selection set


@dataclass
class BakeConfig:
    include: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)
    methods: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def resolve_secret(value: str) -> str:
    """Resolve a secret value from env var, file, or literal.

    Supports:
      env:VAR_NAME   — read from environment variable
      file:/path     — read from file (trailing newline stripped)
      literal value  — returned as-is
    """
    if value.startswith("env:"):
        var = value[4:]
        resolved = os.environ.get(var)
        if resolved is None:
            print(f"Error: environment variable {var!r} is not set", file=sys.stderr)
            sys.exit(1)
        return resolved
    if value.startswith("file:"):
        path = Path(value[5:])
        if not path.exists():
            print(f"Error: secret file not found: {path}", file=sys.stderr)
            sys.exit(1)
        return path.read_text().rstrip("\n")
    return value


def escape_argparse_help(help_text: str) -> str:
    """Escape literal percent signs in help text for argparse."""
    return ARGPARSE_HELP_PERCENT_RE.sub("%%", help_text)


def _parse_kv_list(
    items: list[str],
    delimiter: str,
    label: str,
    *,
    resolve_values: bool = False,
) -> list[tuple[str, str]]:
    """Parse a list of 'KEY<delimiter>VALUE' strings into (key, value) pairs.

    Exits with an error message if any item is missing the delimiter.
    When *resolve_values* is True, each value is passed through :func:`resolve_secret`.
    """
    result: list[tuple[str, str]] = []
    for item in items:
        if delimiter not in item:
            print(f"Error: invalid {label} format: {item!r}", file=sys.stderr)
            sys.exit(1)
        k, v = item.split(delimiter, 1)
        k, v = k.strip(), v.strip()
        if resolve_values:
            v = resolve_secret(v)
        result.append((k, v))
    return result


def read_stdin_json(context: str):
    raw = sys.stdin.read()
    if not raw.strip():
        print(
            f"Error: --stdin expects JSON for {context}, but stdin was empty.",
            file=sys.stderr,
        )
        sys.exit(1)
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        print(
            f"Error: invalid JSON on stdin for {context} "
            f"(line {exc.lineno}, column {exc.colno}).",
            file=sys.stderr,
        )
        sys.exit(1)


def schema_type_to_python(schema: dict) -> tuple[type | None, str]:
    t = schema.get("type")
    if t == "integer":
        return int, ""
    if t == "number":
        return float, ""
    if t == "boolean":
        return None, ""
    if t == "array":
        return str, " (JSON array)"
    if t == "object":
        return str, " (JSON object)"
    return str, ""


def _coerce_item(value: str, item_type: str | None):
    """Coerce a single string value to the given JSON schema type."""
    if item_type == "integer":
        return int(value)
    if item_type == "number":
        return float(value)
    if item_type == "boolean":
        return value.lower() in ("true", "1", "yes")
    return value


def coerce_value(value, schema: dict):
    if value is None:
        return None
    t = schema.get("type")
    if t == "array":
        if isinstance(value, list):
            return value
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
                if isinstance(parsed, list):
                    return parsed
            except (json.JSONDecodeError, TypeError):
                pass
            item_type = schema.get("items", {}).get("type")
            if "," in value:
                return [_coerce_item(v.strip(), item_type) for v in value.split(",")]
            return [_coerce_item(value, item_type)]
        return value
    if t == "object":
        try:
            return json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return value
    if t == "boolean":
        return bool(value)
    if t == "integer":
        return int(value)
    if t == "number":
        return float(value)
    return value


def to_kebab(name: str) -> str:
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1-\2", name)
    return s.replace("_", "-").lower()


def _find_toon_cli() -> str | None:
    """Return the command to invoke the TOON CLI, or None if unavailable."""
    if shutil.which("toon"):
        return "toon"
    # Check for npx (ships with Node.js)
    if shutil.which("npx"):
        return "npx @toon-format/cli"
    return None


def _toon_encode(json_str: str) -> str | None:
    """Pipe JSON through the TOON CLI. Returns TOON text or None on failure."""
    cmd = _find_toon_cli()
    if cmd is None:
        return None
    try:
        result = subprocess.run(
            cmd.split(),
            input=json_str,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            return result.stdout
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return None


def _run_jq(json_str: str, expr: str) -> str:
    """Pipe JSON through jq with the given expression. Exits on failure."""
    if not shutil.which("jq"):
        print(
            "Error: --jq requires jq to be installed. "
            "See https://jqlang.github.io/jq/",
            file=sys.stderr,
        )
        sys.exit(1)
    try:
        result = subprocess.run(
            ["jq", expr],
            input=json_str,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            print(f"jq error: {result.stderr.strip()}", file=sys.stderr)
            sys.exit(1)
        return result.stdout
    except subprocess.TimeoutExpired:
        print("Error: jq timed out", file=sys.stderr)
        sys.exit(1)


def _apply_head(data, n: int):
    """Truncate data to first N elements (array) or return as-is (dict/scalar)."""
    if isinstance(data, list):
        return data[:n]
    return data


def output_result(
    data,
    *,
    pretty: bool = False,
    raw: bool = False,
    toon: bool = False,
    jq_expr: str | None = None,
    head: int | None = None,
):
    if raw:
        if isinstance(data, str):
            print(data)
        else:
            print(json.dumps(data))
        return
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except (json.JSONDecodeError, TypeError):
            print(data)
            return
    if head is not None:
        data = _apply_head(data, head)
    if jq_expr:
        print(_run_jq(json.dumps(data), jq_expr), end="")
        return
    if toon:
        encoded = _toon_encode(json.dumps(data))
        if encoded is not None:
            print(encoded, end="")
            return
        print(
            "Warning: --toon requires the TOON CLI (@toon-format/cli). "
            "Install with: npm install -g @toon-format/cli",
            file=sys.stderr,
        )
        # Fall through to normal output
    if pretty or sys.stdout.isatty():
        print(json.dumps(data, indent=2))
    else:
        print(json.dumps(data))


def _build_http_headers(auth_headers: list[tuple[str, str]], multipart: bool = False) -> dict[str, str]:
    """Build HTTP headers dict from auth_headers with a Content-Type default."""
    headers = dict(auth_headers)
    if not multipart:
        headers.setdefault("Content-Type", "application/json")
    return headers


def _handle_http_error(resp) -> None:
    """Print error and exit(1) on non-2xx HTTP response."""
    try:
        resp.raise_for_status()
    except httpx.HTTPStatusError:
        print(f"Error {resp.status_code}: {resp.text}", file=sys.stderr)
        sys.exit(1)


# ---------------------------------------------------------------------------
# Caching
# ---------------------------------------------------------------------------


def cache_key_for(config: dict) -> str:
    """
    Generate cache key from tool configuration dict.

    Hashes all config fields that affect MCP server behavior to ensure
    unique caching for tools with the same URL but different configurations.
    """
    # Exclude fields that don't affect which tools are returned
    cache_config = {
        k: v for k, v in config.items()
        if k not in ('cache_ttl', 'description', 'include', 'exclude', 'methods')
    }
    # Ensure auth_headers are sorted for stable hashing
    if 'auth_headers' in cache_config and cache_config['auth_headers']:
        cache_config['auth_headers'] = sorted(cache_config['auth_headers'])

    return hashlib.sha256(
        json.dumps(cache_config, sort_keys=True).encode()
    ).hexdigest()[:16]

def load_cached(key: str, ttl: int) -> dict | None:
    path = CACHE_DIR / f"{key}.json"
    if not path.exists():
        return None
    age = time.time() - path.stat().st_mtime
    if age >= ttl:
        return None
    return json.loads(path.read_text())


def save_cache(key: str, data: dict):
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    (CACHE_DIR / f"{key}.json").write_text(json.dumps(data))


# ---------------------------------------------------------------------------
# Usage tracking
# ---------------------------------------------------------------------------


def _load_usage() -> dict:
    """Load the usage tracking file. Returns empty dict on any failure."""
    if not USAGE_FILE.exists():
        return {}
    try:
        return json.loads(USAGE_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _save_usage(data: dict) -> None:
    """Write usage data. Last-write-wins -- no file locking."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    USAGE_FILE.write_text(json.dumps(data, indent=2))


def record_usage(source_hash: str, tool_name: str) -> None:
    """Increment the call count and update last_used for a tool."""
    usage = _load_usage()
    bucket = usage.setdefault(source_hash, {})
    entry = bucket.setdefault(tool_name, {"count": 0, "last_used": ""})
    entry["count"] += 1
    entry["last_used"] = datetime.now(timezone.utc).isoformat()
    _save_usage(usage)


def _source_hash_for(source: str) -> str:
    """Derive a stable hash key from a source URL/command string."""
    return hashlib.sha256(source.encode()).hexdigest()[:16]


def sort_commands(
    commands: list["CommandDef"],
    sort_mode: str,
    source_hash: str,
) -> list["CommandDef"]:
    """Sort commands by the given mode using usage data.

    Modes:
        usage   -- most-called first (default when usage data exists)
        recent  -- most-recently-used first
        alpha   -- alphabetical by name
        default -- original insertion order
    """
    if sort_mode == "default":
        return commands
    if sort_mode == "alpha":
        return sorted(commands, key=lambda c: c.name)

    usage = _load_usage().get(source_hash, {})
    if not usage:
        return commands  # no data, keep insertion order

    def _usage_key(c: "CommandDef") -> str:
        return c.tool_name or c.graphql_field_name or c.name

    if sort_mode == "usage":
        return sorted(
            commands,
            key=lambda c: usage.get(_usage_key(c), {}).get("count", 0),
            reverse=True,
        )
    if sort_mode == "recent":
        return sorted(
            commands,
            key=lambda c: usage.get(_usage_key(c), {}).get("last_used", ""),
            reverse=True,
        )
    return commands


def _resolve_sort_mode(explicit_sort: str | None, source_hash: str) -> str:
    """Determine the effective sort mode.

    If the user passed --sort explicitly, use that. Otherwise, default to
    'usage' when usage data exists for this source, else 'default'.
    """
    if explicit_sort is not None:
        return explicit_sort
    usage = _load_usage().get(source_hash, {})
    return "usage" if usage else "default"


# ---------------------------------------------------------------------------
# OAuth support
# ---------------------------------------------------------------------------

OAUTH_DIR = CACHE_DIR / "oauth"


class FileTokenStorage:
    """File-based token storage for OAuth tokens and client info."""

    def __init__(self, server_url: str):
        key = hashlib.sha256(server_url.encode()).hexdigest()[:16]
        self._dir = OAUTH_DIR / key
        self._dir.mkdir(parents=True, exist_ok=True)
        self._tokens_path = self._dir / "tokens.json"
        self._client_path = self._dir / "client.json"

    async def get_tokens(self):
        from mcp.shared.auth import OAuthToken

        if not self._tokens_path.exists():
            return None
        try:
            data = json.loads(self._tokens_path.read_text())
            return OAuthToken(**data)
        except Exception:
            return None

    async def set_tokens(self, tokens) -> None:
        self._tokens_path.write_text(tokens.model_dump_json())

    async def get_client_info(self):
        from mcp.shared.auth import OAuthClientInformationFull

        if not self._client_path.exists():
            return None
        try:
            data = json.loads(self._client_path.read_text())
            return OAuthClientInformationFull(**data)
        except Exception:
            return None

    async def set_client_info(self, client_info) -> None:
        self._client_path.write_text(client_info.model_dump_json())


class _CallbackHandler(BaseHTTPRequestHandler):
    """HTTP handler that captures the OAuth authorization code callback."""

    auth_code: str | None = None
    state: str | None = None
    error: str | None = None
    done = threading.Event()

    def do_GET(self):
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)

        if "error" in params:
            _CallbackHandler.error = params["error"][0]
        elif "code" in params:
            _CallbackHandler.auth_code = params["code"][0]
            _CallbackHandler.state = params.get("state", [None])[0]

        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        if _CallbackHandler.error:
            self.wfile.write(
                b"<h1>Authorization failed</h1><p>You can close this tab.</p>"
            )
        else:
            self.wfile.write(
                b"<h1>Authorization successful</h1><p>You can close this tab.</p>"
            )
        _CallbackHandler.done.set()

    def log_message(self, format, *args):
        pass  # Suppress request logging


def _find_free_port() -> int:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def build_oauth_provider(
    server_url: str,
    *,
    client_id: str | None = None,
    client_secret: str | None = None,
    client_name: str = "mcp2cli",
    scope: str | None = None,
    redirect_uri: str | None = None,
    flow: str = "auto",
) -> "httpx.Auth":
    """Build an OAuth provider for HTTP connections.

    The ``flow`` parameter controls which grant type is used:

    - ``"auto"`` (default): client_id + client_secret → client credentials;
      client_id only → authorization code + PKCE (public client);
      neither → authorization code + PKCE with dynamic client registration.
    - ``"authorization_code"``: always use authorization code + PKCE.
      When a client_secret is also provided the token-endpoint request
      authenticates as a confidential client (``client_secret_post``).
      This is required by servers like Slack that issue confidential OAuth
      clients but only support the authorization-code grant.
    - ``"client_credentials"``: always use client credentials (requires both
      client_id and client_secret).

    redirect_uri controls the full callback URL (scheme, host, port, path).
    When None, defaults to http://127.0.0.1:<random-free-port>/callback.
    """
    storage = FileTokenStorage(server_url)

    use_client_credentials = (
        flow == "client_credentials"
        or (flow == "auto" and client_id and client_secret)
    )

    if use_client_credentials:
        from mcp.client.auth.extensions.client_credentials import (
            ClientCredentialsOAuthProvider,
        )

        return ClientCredentialsOAuthProvider(
            server_url=server_url,
            storage=storage,
            client_id=client_id,
            client_secret=client_secret,
            scopes=scope,
        )

    from mcp.client.auth.oauth2 import OAuthClientProvider
    from mcp.shared.auth import OAuthClientInformationFull, OAuthClientMetadata

    _LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}

    if redirect_uri is not None:
        parsed = urlparse(redirect_uri)
        if parsed.scheme != "http":
            print(
                f"Error: --oauth-redirect-uri must use http://, got '{parsed.scheme}://'. "
                "The local callback server is plain HTTP; use http://<host>:<port>/path.",
                file=sys.stderr,
            )
            sys.exit(1)
        if parsed.port is None:
            print(
                "Error: --oauth-redirect-uri must include an explicit port number "
                "(e.g. http://localhost:3334/oauth/callback).",
                file=sys.stderr,
            )
            sys.exit(1)
        if (parsed.hostname or "") not in _LOOPBACK_HOSTS:
            print(
                f"Error: --oauth-redirect-uri host must be a loopback address "
                f"(localhost, 127.0.0.1, or ::1), got '{parsed.hostname}'.",
                file=sys.stderr,
            )
            sys.exit(1)
        callback_host = parsed.hostname
        port = parsed.port
    else:
        port = _find_free_port()
        callback_host = "127.0.0.1"
        redirect_uri = f"http://127.0.0.1:{port}/callback"

    client_metadata = OAuthClientMetadata(
        client_name=client_name,
        redirect_uris=[redirect_uri],
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        scope=scope,
    )

    if client_id:
        # Pre-seed storage with the caller-supplied client_id so the OAuth
        # provider skips dynamic client registration entirely.  The write is
        # synchronous (plain file I/O) so no async context is needed here.
        #
        # When a client_secret is provided (confidential client, e.g. Slack),
        # include it and use client_secret_post so the token-endpoint request
        # sends the secret in the POST body.
        if client_secret:
            auth_method = "client_secret_post"
        else:
            auth_method = "none"
        pre_client_info = OAuthClientInformationFull(
            client_id=client_id,
            client_secret=client_secret,
            token_endpoint_auth_method=auth_method,
            redirect_uris=[redirect_uri],
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            scope=scope,
        )
        storage._client_path.write_text(pre_client_info.model_dump_json())

    # Reset callback handler state
    _CallbackHandler.auth_code = None
    _CallbackHandler.state = None
    _CallbackHandler.error = None
    _CallbackHandler.done = threading.Event()

    if callback_host == "::1":
        import socket as _socket

        class _IPv6HTTPServer(HTTPServer):
            address_family = _socket.AF_INET6

        server = _IPv6HTTPServer((callback_host, port), _CallbackHandler)
    else:
        server = HTTPServer((callback_host, port), _CallbackHandler)

    async def redirect_handler(auth_url: str) -> None:
        print(f"Opening browser for authorization...", file=sys.stderr)
        print(f"If browser doesn't open, visit: {auth_url}", file=sys.stderr)
        webbrowser.open(auth_url)

    async def callback_handler() -> tuple[str, str | None]:
        # Run the HTTP server in a thread, wait for the callback
        thread = threading.Thread(target=server.handle_request, daemon=True)
        thread.start()
        # Wait with timeout
        if not _CallbackHandler.done.wait(timeout=300):
            server.server_close()
            raise TimeoutError("OAuth callback timed out after 5 minutes")
        server.server_close()
        if _CallbackHandler.error:
            raise RuntimeError(f"OAuth error: {_CallbackHandler.error}")
        if not _CallbackHandler.auth_code:
            raise RuntimeError("No authorization code received")
        return (_CallbackHandler.auth_code, _CallbackHandler.state)

    return OAuthClientProvider(
        server_url=server_url,
        client_metadata=client_metadata,
        storage=storage,
        redirect_handler=redirect_handler,
        callback_handler=callback_handler,
    )


# ---------------------------------------------------------------------------
# OpenAPI: $ref resolution
# ---------------------------------------------------------------------------


def resolve_refs(spec: dict) -> dict:
    spec = copy.deepcopy(spec)

    def _resolve(node, root, seen):
        if isinstance(node, dict):
            if "$ref" in node:
                ref = node["$ref"]
                if ref in seen:
                    return node
                seen = seen | {ref}
                if ref.startswith("#/"):
                    parts = ref[2:].split("/")
                    target = root
                    for p in parts:
                        target = target[p]
                    return _resolve(copy.deepcopy(target), root, seen)
                return node
            return {k: _resolve(v, root, seen) for k, v in node.items()}
        if isinstance(node, list):
            return [_resolve(item, root, seen) for item in node]
        return node

    return _resolve(spec, spec, set())


# ---------------------------------------------------------------------------
# OpenAPI: spec loading
# ---------------------------------------------------------------------------


def load_openapi_spec(
    source: str,
    auth_headers: list[tuple[str, str]],
    cache_key: str | None,
    ttl: int,
    refresh: bool,
    oauth_provider: "httpx.Auth | None" = None,
) -> dict:
    is_url = source.startswith("http://") or source.startswith("https://")

    if is_url:
        key = cache_key or cache_key_for({
            'source': source,
            'auth_headers': auth_headers,
        })
        if not refresh:
            cached = load_cached(key, ttl)
            if cached is not None:
                return cached

        headers = dict(auth_headers)
        with httpx.Client(timeout=30, auth=oauth_provider) as client:
            resp = client.get(source, headers=headers)
            resp.raise_for_status()
            raw = resp.text
    else:
        raw = Path(source).read_text()

    # Parse JSON or YAML
    try:
        spec = json.loads(raw)
    except json.JSONDecodeError:
        import yaml

        spec = yaml.safe_load(raw)

    if not isinstance(spec, dict) or "paths" not in spec:
        print("Error: spec must contain 'paths'", file=sys.stderr)
        sys.exit(1)

    spec = resolve_refs(spec)

    if is_url:
        save_cache(key, spec)

    return spec


# ---------------------------------------------------------------------------
# OpenAPI: command extraction
# ---------------------------------------------------------------------------


def extract_openapi_commands(spec: dict) -> list[CommandDef]:
    commands: list[CommandDef] = []
    seen_names: dict[str, int] = {}

    for path, methods in spec.get("paths", {}).items():
        if not isinstance(methods, dict):
            continue
        for method, details in methods.items():
            if method not in ("get", "post", "put", "delete", "patch"):
                continue
            if not isinstance(details, dict):
                continue

            op_id = details.get("operationId")
            if op_id:
                name = to_kebab(op_id)
            else:
                slug = (
                    path.strip("/").replace("/", "-").replace("{", "").replace("}", "")
                )
                name = f"{method}-{slug}" if slug else method

            if name in seen_names:
                seen_names[name] += 1
                name = f"{name}-{method}"
            seen_names[name] = 1

            desc = (
                details.get("summary")
                or details.get("description")
                or f"{method.upper()} {path}"
            )
            params: list[ParamDef] = []

            # Parameters (path, query, header)
            for param in details.get("parameters", []):
                schema = param.get("schema", {})
                py_type, suffix = schema_type_to_python(schema)
                p = ParamDef(
                    name=to_kebab(param["name"]),
                    original_name=param["name"],
                    python_type=py_type,
                    required=param.get("required", False),
                    description=(param.get("description") or param["name"]) + suffix,
                    choices=schema.get("enum"),
                    location=param.get("in", "query"),
                )
                params.append(p)

            # Request body — negotiate content type
            rb_content = details.get("requestBody", {}).get("content", {})
            multipart_schema = rb_content.get("multipart/form-data", {}).get("schema", {})
            json_schema = rb_content.get("application/json", {}).get("schema", {})

            mp_props = multipart_schema.get("properties", {})
            has_binary = any(
                p.get("format") == "binary" for p in mp_props.values()
            )

            if has_binary:
                rb_schema = multipart_schema
                cmd_content_type = "multipart/form-data"
            elif json_schema:
                rb_schema = json_schema
                cmd_content_type = None
            elif mp_props:
                rb_schema = multipart_schema
                cmd_content_type = "multipart/form-data"
            else:
                rb_schema = {}
                cmd_content_type = None

            required_fields = set(rb_schema.get("required", []))
            properties = rb_schema.get("properties", {})
            has_body = bool(properties)

            for prop_name, prop_schema in properties.items():
                is_binary = (
                    cmd_content_type == "multipart/form-data"
                    and prop_schema.get("format") == "binary"
                )
                if is_binary:
                    loc, py_type, suffix = "file", str, " (file path)"
                else:
                    py_type, suffix = schema_type_to_python(prop_schema)
                    loc = "body"
                p = ParamDef(
                    name=to_kebab(prop_name),
                    original_name=prop_name,
                    python_type=py_type,
                    required=prop_name in required_fields,
                    description=(prop_schema.get("description") or prop_name) + suffix,
                    choices=prop_schema.get("enum"),
                    location=loc,
                )
                params.append(p)

            commands.append(
                CommandDef(
                    name=name,
                    description=desc,
                    params=params,
                    has_body=has_body,
                    method=method,
                    path=path,
                    content_type=cmd_content_type,
                )
            )

    return commands


# ---------------------------------------------------------------------------
# MCP: command extraction
# ---------------------------------------------------------------------------


def extract_mcp_commands(tools: list[dict]) -> list[CommandDef]:
    commands: list[CommandDef] = []
    for tool in tools:
        name = to_kebab(tool.get("name", "unknown"))
        desc = tool.get("description", "")
        schema = tool.get("inputSchema", {})
        required_fields = set(schema.get("required", []))
        params: list[ParamDef] = []

        for prop_name, prop_schema in schema.get("properties", {}).items():
            py_type, suffix = schema_type_to_python(prop_schema)
            params.append(
                ParamDef(
                    name=to_kebab(prop_name),
                    original_name=prop_name,
                    python_type=py_type,
                    required=prop_name in required_fields,
                    description=(prop_schema.get("description") or prop_name) + suffix,
                    choices=prop_schema.get("enum"),
                    location="tool_input",
                    schema=prop_schema,
                )
            )

        commands.append(
            CommandDef(
                name=name,
                description=desc,
                params=params,
                has_body=bool(params),
                tool_name=tool.get("name"),
            )
        )
    return commands


# ---------------------------------------------------------------------------
# GraphQL support
# ---------------------------------------------------------------------------

GRAPHQL_INTROSPECTION_QUERY = """
query IntrospectionQuery {
  __schema {
    queryType { name }
    mutationType { name }
    types {
      kind
      name
      fields(includeDeprecated: false) {
        name
        description
        args {
          name
          description
          type {
            ...TypeRef
          }
          defaultValue
        }
        type {
          ...TypeRef
        }
      }
      inputFields {
        name
        description
        type {
          ...TypeRef
        }
        defaultValue
      }
      enumValues(includeDeprecated: false) {
        name
        description
      }
    }
  }
}

fragment TypeRef on __Type {
  kind
  name
  ofType {
    kind
    name
    ofType {
      kind
      name
      ofType {
        kind
        name
        ofType {
          kind
          name
        }
      }
    }
  }
}
"""


def _unwrap_type(type_ref: dict) -> tuple[dict, bool, bool]:
    """Unwrap NON_NULL/LIST wrappers to find the underlying named type.

    Returns (named_type_dict, is_non_null, is_list).
    """
    is_non_null = False
    is_list = False
    t = type_ref
    while t:
        kind = t.get("kind")
        if kind == "NON_NULL":
            is_non_null = True
            t = t.get("ofType", {})
        elif kind == "LIST":
            is_list = True
            t = t.get("ofType", {})
        else:
            return t, is_non_null, is_list
    return type_ref, is_non_null, is_list


def _graphql_type_string(type_ref: dict) -> str:
    """Reconstruct GraphQL type notation from introspection type ref.

    E.g. ``"[String!]!"`` or ``"ID!"`` or ``"Int"``.
    """
    kind = type_ref.get("kind")
    if kind == "NON_NULL":
        inner = _graphql_type_string(type_ref.get("ofType", {}))
        return f"{inner}!"
    if kind == "LIST":
        inner = _graphql_type_string(type_ref.get("ofType", {}))
        return f"[{inner}]"
    return type_ref.get("name", "String")


def graphql_type_to_python(
    type_ref: dict, types_by_name: dict
) -> tuple[type | None, bool, list | None]:
    """Map a GraphQL introspection type to (python_type, required, choices).

    - Scalars → str/int/float/None(bool)
    - Enums → str with choices
    - Input objects → str (JSON)
    - Lists → str (JSON array or comma-delimited)
    """
    named, is_non_null, is_list = _unwrap_type(type_ref)
    type_name = named.get("name", "")
    type_kind = named.get("kind", "")

    if is_list:
        return str, is_non_null, None

    if type_kind == "ENUM":
        enum_type = types_by_name.get(type_name, {})
        choices = [ev["name"] for ev in enum_type.get("enumValues", [])]
        return str, is_non_null, choices or None

    if type_kind == "INPUT_OBJECT":
        return str, is_non_null, None

    # Scalars
    scalar_map = {
        "String": str,
        "ID": str,
        "Int": int,
        "Float": float,
        "Boolean": None,  # store_true
    }
    py_type = scalar_map.get(type_name, str)
    return py_type, is_non_null, None


def _build_selection_set(
    type_ref: dict, types_by_name: dict, depth: int = 2, seen: set | None = None
) -> str:
    """Auto-generate a GraphQL selection set from a return type.

    Depth 2 = scalar fields + one level of nested object scalar fields.
    """
    if seen is None:
        seen = set()

    named, _, is_list = _unwrap_type(type_ref)
    type_name = named.get("name", "")
    type_kind = named.get("kind", "")

    # Scalar / enum — no selection needed
    if type_kind in ("SCALAR", "ENUM"):
        return ""

    if type_name in seen or depth <= 0:
        return ""

    type_def = types_by_name.get(type_name, {})
    fields = type_def.get("fields", [])
    if not fields:
        return ""

    seen = seen | {type_name}
    parts = []
    for f in fields:
        f_named, _, _ = _unwrap_type(f["type"])
        f_kind = f_named.get("kind", "")
        if f_kind in ("SCALAR", "ENUM"):
            parts.append(f["name"])
        elif f_kind == "OBJECT" and depth > 1:
            nested = _build_selection_set(f["type"], types_by_name, depth - 1, seen)
            if nested:
                parts.append(f"{f['name']} {nested}")
    if not parts:
        return ""
    return "{ " + " ".join(parts) + " }"


def load_graphql_schema(
    url: str,
    auth_headers: list[tuple[str, str]],
    cache_key: str | None,
    ttl: int,
    refresh: bool,
    oauth_provider: "httpx.Auth | None" = None,
) -> dict:
    """POST introspection query to a GraphQL endpoint, with caching."""
    key = cache_key or cache_key_for({
        'source': f"graphql:{url}",
        'auth_headers': auth_headers,
    })
    if not refresh:
        cached = load_cached(key, ttl)
        if cached is not None:
            return cached

    headers = dict(auth_headers)
    headers.setdefault("Content-Type", "application/json")
    with httpx.Client(timeout=30, auth=oauth_provider) as client:
        resp = client.post(
            url,
            headers=headers,
            json={"query": GRAPHQL_INTROSPECTION_QUERY},
        )
        resp.raise_for_status()
        result = resp.json()

    if "errors" in result and not result.get("data"):
        msgs = "; ".join(e.get("message", "") for e in result["errors"])
        print(f"Error: GraphQL introspection failed: {msgs}", file=sys.stderr)
        sys.exit(1)

    schema = result.get("data", {}).get("__schema", {})
    if not schema:
        print("Error: introspection returned no schema", file=sys.stderr)
        sys.exit(1)

    save_cache(key, schema)
    return schema


def _detect_field_collisions(
    query_fields: list[dict], mutation_fields: list[dict]
) -> set[str]:
    """Return field names that appear in both query and mutation types."""
    all_names: set[str] = set()
    collisions: set[str] = set()
    for f in query_fields + mutation_fields:
        n = f["name"]
        if n in all_names:
            collisions.add(n)
        all_names.add(n)
    return collisions


def _build_graphql_param(arg: dict, types_by_name: dict) -> ParamDef:
    """Convert a single GraphQL field argument into a ParamDef."""
    py_type, required, choices = graphql_type_to_python(arg["type"], types_by_name)
    gql_type_str = _graphql_type_string(arg["type"])
    named_t, _, is_list = _unwrap_type(arg["type"])

    # Build schema for coerce_value
    param_schema: dict = {"graphql_type": gql_type_str}
    if is_list:
        param_schema["type"] = "array"
        inner_named, _, _ = _unwrap_type(named_t)
        item_type_name = inner_named.get("name", "String")
        item_map = {
            "Int": "integer", "Float": "number", "String": "string",
            "ID": "string", "Boolean": "boolean",
        }
        param_schema["items"] = {"type": item_map.get(item_type_name, "string")}
    elif named_t.get("kind") == "INPUT_OBJECT":
        param_schema["type"] = "object"
    elif named_t.get("kind") == "ENUM":
        param_schema["type"] = "string"

    arg_desc = arg.get("description") or arg["name"]
    if is_list:
        arg_desc += " (JSON array)"
    elif named_t.get("kind") == "INPUT_OBJECT":
        arg_desc += " (JSON object)"

    return ParamDef(
        name=to_kebab(arg["name"]),
        original_name=arg["name"],
        python_type=py_type,
        required=required,
        description=arg_desc,
        choices=choices,
        location="graphql_arg",
        schema=param_schema,
    )


def extract_graphql_commands(schema: dict) -> list[CommandDef]:
    """Convert introspection schema into CommandDef list."""
    types_by_name = {t["name"]: t for t in schema.get("types", []) if t.get("name")}

    query_type_name = (schema.get("queryType") or {}).get("name")
    mutation_type_name = (schema.get("mutationType") or {}).get("name")

    commands: list[CommandDef] = []
    seen_names: set[str] = set()

    query_fields = types_by_name.get(query_type_name, {}).get("fields", []) if query_type_name else []
    mutation_fields = types_by_name.get(mutation_type_name, {}).get("fields", []) if mutation_type_name else []
    collisions = _detect_field_collisions(query_fields, mutation_fields)

    for op_type, type_name, fields in [
        ("query", query_type_name, query_fields),
        ("mutation", mutation_type_name, mutation_fields),
    ]:
        for field_def in fields:
            field_name = field_def["name"]
            if field_name.startswith("__"):
                continue

            cli_name = to_kebab(field_name)
            if field_name in collisions:
                cli_name = f"{op_type}-{cli_name}"

            if cli_name in seen_names:
                cli_name = f"{op_type}-{cli_name}"
            seen_names.add(cli_name)

            desc = field_def.get("description") or f"{op_type} {field_name}"
            params = [
                _build_graphql_param(arg, types_by_name)
                for arg in field_def.get("args", [])
            ]

            commands.append(
                CommandDef(
                    name=cli_name,
                    description=desc,
                    params=params,
                    has_body=bool(params),
                    graphql_operation_type=op_type,
                    graphql_field_name=field_name,
                    graphql_return_type=field_def.get("type"),
                )
            )

    return commands


def _truncate_description(description: str, max_len: int) -> str:
    """Truncate at a word boundary and append '...'."""
    if len(description) <= max_len:
        return description
    return description[:max_len].rsplit(" ", 1)[0].rstrip() + "..."


def _wrap_description(description: str, indent: int, total_width: int = 110) -> str:
    """Wrap long descriptions; indent continuation lines to align with description column."""
    import textwrap
    return textwrap.fill(
        description,
        width=total_width,
        subsequent_indent=" " * indent,
        break_long_words=False,
        break_on_hyphens=False,
    )


def list_graphql_commands(
    commands: list[CommandDef],
    verbose: bool = False,
    compact: bool = False,
    source_hash: str = "",
    sort_mode: str | None = None,
    top: int | None = None,
):
    """Group commands by operation type and print."""
    commands = _apply_list_options(commands, source_hash, sort_mode, top)

    if compact:
        print(" ".join(cmd.name for cmd in commands))
        return

    groups: dict[str, list[CommandDef]] = {}
    for cmd in commands:
        key = cmd.graphql_operation_type or "other"
        groups.setdefault(key, []).append(cmd)

    for group in ["query", "mutation"]:
        cmds = groups.get(group, [])
        if not cmds:
            continue
        label = "queries" if group == "query" else "mutations"
        print(f"\n{label}:")
        for cmd in cmds:
            if cmd.description:
                if verbose:
                    wrapped = _wrap_description(cmd.description, indent=42, total_width=100)
                    desc = f"  {wrapped}"
                else:
                    desc = f"  {_truncate_description(cmd.description, 60)}"
            else:
                desc = ""
            print(f"  {cmd.name:<40}{desc}")


def _build_graphql_document(
    cmd: CommandDef,
    args: argparse.Namespace,
    schema: dict,
    fields_override: str | None = None,
) -> tuple[str, dict, str]:
    """Build a GraphQL document string and variables dict from parsed args.

    Returns (document, variables, field_name).
    """
    types_by_name = {t["name"]: t for t in schema.get("types", []) if t.get("name")}

    # Build variables dict from args
    if getattr(args, "stdin", False):
        variables = read_stdin_json("GraphQL variables")
    else:
        variables = {}
        for p in cmd.params:
            val = getattr(args, p.name.replace("-", "_"), None)
            if val is not None:
                variables[p.original_name] = coerce_value(val, p.schema)

    # Build variable declarations for the document
    var_decls = []
    for p in cmd.params:
        if p.original_name in variables:
            gql_type = p.schema.get("graphql_type", "String")
            var_decls.append(f"${p.original_name}: {gql_type}")

    # Build selection set
    if fields_override:
        selection = f"{{ {fields_override} }}"
    elif cmd.graphql_return_type:
        selection = _build_selection_set(cmd.graphql_return_type, types_by_name)
    else:
        selection = ""

    # Build argument list for the field
    field_args = []
    for p in cmd.params:
        if p.original_name in variables:
            field_args.append(f"{p.original_name}: ${p.original_name}")

    field_name = cmd.graphql_field_name or cmd.name
    args_str = f"({', '.join(field_args)})" if field_args else ""
    op_type = cmd.graphql_operation_type or "query"
    var_decls_str = f"({', '.join(var_decls)})" if var_decls else ""

    document = f"{op_type}{var_decls_str} {{ {field_name}{args_str} {selection} }}"
    return document, variables, field_name


def execute_graphql(
    args: argparse.Namespace,
    cmd: CommandDef,
    url: str,
    schema: dict,
    auth_headers: list[tuple[str, str]],
    pretty: bool,
    raw: bool,
    toon: bool = False,
    fields_override: str | None = None,
    oauth_provider: "httpx.Auth | None" = None,
    jq_expr: str | None = None,
    head: int | None = None,
):
    """Build and execute a GraphQL query/mutation."""
    document, variables, field_name = _build_graphql_document(
        cmd, args, schema, fields_override
    )

    headers = _build_http_headers(auth_headers)

    with httpx.Client(timeout=60, auth=oauth_provider) as client:
        resp = client.post(
            url,
            headers=headers,
            json={"query": document, "variables": variables or None},
        )
        _handle_http_error(resp)

    result = resp.json()
    if "errors" in result:
        if not result.get("data"):
            msgs = "; ".join(e.get("message", "") for e in result["errors"])
            print(f"GraphQL error: {msgs}", file=sys.stderr)
            sys.exit(1)
        # Partial errors — include them in output
        output_result(result, pretty=pretty, raw=raw, toon=toon, jq_expr=jq_expr, head=head)
        return

    data = result.get("data", {})
    # Extract the specific field's data
    field_data = data.get(field_name, data)
    output_result(field_data, pretty=pretty, raw=raw, toon=toon, jq_expr=jq_expr, head=head)


def handle_graphql(
    url: str,
    auth_headers: list[tuple[str, str]],
    remaining: list[str],
    list_mode: bool,
    pretty: bool,
    raw: bool,
    cache_key: str | None,
    ttl: int,
    refresh: bool,
    toon: bool = False,
    fields_override: str | None = None,
    oauth_provider: "httpx.Auth | None" = None,
    jq_expr: str | None = None,
    head: int | None = None,
    verbose: bool = False,
    sort_mode: str | None = None,
    top: int | None = None,
    compact: bool = False,
):
    """Top-level handler for --graphql mode."""
    src_hash = _source_hash_for(url)
    schema = load_graphql_schema(url, auth_headers, cache_key, ttl, refresh, oauth_provider=oauth_provider)
    commands = extract_graphql_commands(schema)

    list_kwargs = dict(
        verbose=verbose, compact=compact,
        source_hash=src_hash, sort_mode=sort_mode, top=top,
    )

    if list_mode:
        list_graphql_commands(commands, **list_kwargs)
        return

    if not remaining:
        if not compact:
            print("Available operations:")
        list_graphql_commands(commands, **list_kwargs)
        if not compact:
            print("\nUse --list for the same output, or provide a subcommand.")
        return

    pre_for_gql = argparse.ArgumentParser(add_help=False)
    parser = build_argparse(commands, pre_for_gql)
    args = parser.parse_args(remaining)

    if not hasattr(args, "_cmd"):
        parser.print_help()
        sys.exit(1)

    cmd: CommandDef = args._cmd
    execute_graphql(
        args, cmd, url, schema, auth_headers, pretty, raw, toon=toon,
        fields_override=fields_override, oauth_provider=oauth_provider,
        jq_expr=jq_expr, head=head,
    )

    # Record usage after successful execution
    record_usage(src_hash, cmd.graphql_field_name or cmd.name)


# ---------------------------------------------------------------------------
# Command filtering (bake mode)
# ---------------------------------------------------------------------------


def filter_commands(
    commands: list[CommandDef],
    include: list[str] | None = None,
    exclude: list[str] | None = None,
    methods: list[str] | None = None,
) -> list[CommandDef]:
    """Filter commands by HTTP method, include whitelist, and exclude blacklist.

    Order: methods filter -> include whitelist -> exclude blacklist.
    MCP commands (method is None) pass the methods filter unchanged.
    """
    result = commands
    if methods:
        upper = [m.upper() for m in methods]
        result = [c for c in result if c.method is None or c.method.upper() in upper]
    if include:
        result = [
            c for c in result
            if any(fnmatch.fnmatch(c.name, pat) for pat in include)
        ]
    if exclude:
        result = [
            c for c in result
            if not any(fnmatch.fnmatch(c.name, pat) for pat in exclude)
        ]
    return result


# ---------------------------------------------------------------------------
# Baked config CRUD
# ---------------------------------------------------------------------------


def _load_baked_all() -> dict:
    """Load all baked configs from disk."""
    if not BAKED_FILE.exists():
        return {}
    try:
        return json.loads(BAKED_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _load_baked(name: str) -> dict | None:
    """Load a single baked config by name."""
    return _load_baked_all().get(name)


def _save_baked_all(data: dict) -> None:
    """Save all baked configs to disk."""
    BAKED_FILE.parent.mkdir(parents=True, exist_ok=True)
    BAKED_FILE.write_text(json.dumps(data, indent=2) + "\n")


def _baked_to_argv(config: dict) -> list[str]:
    """Reconstruct CLI argv from a baked config."""
    argv: list[str] = []
    st = config.get("source_type", "spec")
    source = config["source"]
    if st == "spec":
        argv += ["--spec", source]
    elif st == "mcp":
        argv += ["--mcp", source]
    elif st == "mcp_stdio":
        argv += ["--mcp-stdio", source]

    if config.get("base_url"):
        argv += ["--base-url", config["base_url"]]
    for name, value in config.get("auth_headers", []):
        argv += ["--auth-header", f"{name}:{value}"]
    for k, v in config.get("env_vars", {}).items():
        argv += ["--env", f"{k}={v}"]
    if config.get("cache_ttl") is not None:
        argv += ["--cache-ttl", str(config["cache_ttl"])]
    transport = config.get("transport", "auto")
    if transport != "auto":
        argv += ["--transport", transport]
    if config.get("oauth"):
        argv.append("--oauth")
    if config.get("oauth_client_id"):
        argv += ["--oauth-client-id", config["oauth_client_id"]]
    if config.get("oauth_client_secret"):
        argv += ["--oauth-client-secret", config["oauth_client_secret"]]
    if config.get("oauth_client_name") and config["oauth_client_name"] != "mcp2cli":
        argv += ["--oauth-client-name", config["oauth_client_name"]]
    if config.get("oauth_scope"):
        argv += ["--oauth-scope", config["oauth_scope"]]
    if config.get("oauth_redirect_uri"):
        argv += ["--oauth-redirect-uri", config["oauth_redirect_uri"]]
    if config.get("oauth_flow") and config["oauth_flow"] != "auto":
        argv += ["--oauth-flow", config["oauth_flow"]]
    return argv


# ---------------------------------------------------------------------------
# Bake subcommands
# ---------------------------------------------------------------------------

_BAKE_NAME_RE = re.compile(r"^[a-z][a-z0-9-]*$")


def _handle_bake(argv: list[str]) -> None:
    """Dispatch bake subcommands."""
    if not argv or argv[0] in ("-h", "--help"):
        print("Usage: mcp2cli bake <command> [options]\n")
        print("Commands:")
        print("  create    Save connection settings as a named baked tool")
        print("  list      List all baked tools")
        print("  show      Show config for a baked tool (secrets masked)")
        print("  remove    Delete a baked tool")
        print("  update    Update settings on an existing baked tool")
        print("  install   Create a ~/.local/bin wrapper script")
        print("\nRun 'mcp2cli bake <command> --help' for command-specific help.")
        sys.exit(0 if argv else 1)
    sub = argv[0]
    rest = argv[1:]
    dispatch = {
        "create": _bake_create,
        "list": lambda _: _bake_list(),
        "show": _bake_show,
        "remove": _bake_remove,
        "update": _bake_update,
        "install": _bake_install,
    }
    handler = dispatch.get(sub)
    if handler is None:
        print(f"Unknown bake subcommand: {sub}", file=sys.stderr)
        sys.exit(1)
    handler(rest)


def _bake_create(argv: list[str]) -> None:
    p = argparse.ArgumentParser(prog="mcp2cli bake create")
    p.add_argument("name", help="Name for the baked tool")
    p.add_argument("--spec", default=None)
    p.add_argument("--mcp", default=None)
    p.add_argument("--mcp-stdio", default=None)
    p.add_argument("--base-url", default=None)
    p.add_argument("--auth-header", action="append", default=[])
    p.add_argument("--env", action="append", default=[])
    p.add_argument("--cache-ttl", type=int, default=DEFAULT_CACHE_TTL)
    p.add_argument("--transport", choices=["auto", "sse", "streamable"], default="auto")
    p.add_argument("--oauth", action="store_true")
    p.add_argument("--oauth-client-id", default=None)
    p.add_argument("--oauth-client-secret", default=None)
    p.add_argument("--oauth-client-name", default="mcp2cli")
    p.add_argument("--oauth-scope", default=None)
    p.add_argument("--oauth-redirect-uri", default=None, metavar="URI")
    p.add_argument(
        "--oauth-flow",
        choices=["auto", "authorization_code", "client_credentials"],
        default="auto",
        help=(
            "OAuth flow to use. 'auto' (default) picks client_credentials when both "
            "client-id and client-secret are provided, otherwise authorization_code. "
            "Use 'authorization_code' to force the auth code + PKCE flow even with a "
            "client secret (required for confidential-client servers like Slack)."
        ),
    )
    p.add_argument("--include", default="", help="Comma-separated include globs")
    p.add_argument("--exclude", default="", help="Comma-separated exclude globs")
    p.add_argument("--methods", default="", help="Comma-separated HTTP methods")
    p.add_argument("--description", default="")
    p.add_argument("--force", action="store_true", help="Overwrite existing")
    args = p.parse_args(argv)

    if not _BAKE_NAME_RE.match(args.name):
        print(
            f"Error: invalid name {args.name!r} — must match [a-z][a-z0-9-]*",
            file=sys.stderr,
        )
        sys.exit(1)

    modes = [args.spec, args.mcp, args.mcp_stdio]
    active = sum(1 for m in modes if m is not None)
    if active == 0:
        print("Error: one of --spec, --mcp, or --mcp-stdio is required.", file=sys.stderr)
        sys.exit(1)
    if active > 1:
        print("Error: --spec, --mcp, and --mcp-stdio are mutually exclusive.", file=sys.stderr)
        sys.exit(1)

    all_configs = _load_baked_all()
    if args.name in all_configs and not args.force:
        print(
            f"Error: '{args.name}' already exists. Use --force to overwrite.",
            file=sys.stderr,
        )
        sys.exit(1)

    if args.spec:
        source_type, source = "spec", args.spec
    elif args.mcp:
        source_type, source = "mcp", args.mcp
    else:
        source_type, source = "mcp_stdio", args.mcp_stdio

    auth_headers = [list(t) for t in _parse_kv_list(args.auth_header, ":", "auth header")]
    env_vars = dict(_parse_kv_list(args.env, "=", "env"))

    config = {
        "source_type": source_type,
        "source": source,
        "base_url": args.base_url,
        "auth_headers": auth_headers,
        "env_vars": env_vars,
        "cache_ttl": args.cache_ttl,
        "transport": args.transport,
        "oauth": args.oauth,
        "oauth_client_id": args.oauth_client_id,
        "oauth_client_secret": args.oauth_client_secret,
        "oauth_client_name": args.oauth_client_name,
        "oauth_scope": args.oauth_scope,
        "oauth_redirect_uri": args.oauth_redirect_uri,
        "oauth_flow": args.oauth_flow,
        "include": [x.strip() for x in args.include.split(",") if x.strip()],
        "exclude": [x.strip() for x in args.exclude.split(",") if x.strip()],
        "methods": [x.strip().upper() for x in args.methods.split(",") if x.strip()],
        "description": args.description,
    }

    all_configs[args.name] = config
    _save_baked_all(all_configs)
    print(f"Baked tool '{args.name}' created.")


def _bake_list() -> None:
    configs = _load_baked_all()
    if not configs:
        print("No baked tools.")
        return
    print(f"{'Name':<20} {'Type':<10} {'Source':<50}")
    print("-" * 80)
    for name, cfg in sorted(configs.items()):
        st = cfg.get("source_type", "?")
        src = cfg.get("source", "?")
        if len(src) > 48:
            src = src[:45] + "..."
        print(f"{name:<20} {st:<10} {src:<50}")


def _bake_show(argv: list[str]) -> None:
    p = argparse.ArgumentParser(prog="mcp2cli bake show")
    p.add_argument("name")
    args = p.parse_args(argv)
    cfg = _load_baked(args.name)
    if cfg is None:
        print(f"Error: no baked tool named '{args.name}'", file=sys.stderr)
        sys.exit(1)
    # Mask secrets in auth headers for display
    display = dict(cfg)
    if display.get("auth_headers"):
        masked = []
        for name, val in display["auth_headers"]:
            if val.startswith("env:") or val.startswith("file:"):
                masked.append([name, val])
            else:
                masked.append([name, val[:4] + "****" if len(val) > 4 else "****"])
        display["auth_headers"] = masked
    print(json.dumps(display, indent=2))


def _bake_remove(argv: list[str]) -> None:
    p = argparse.ArgumentParser(prog="mcp2cli bake remove")
    p.add_argument("name")
    args = p.parse_args(argv)
    all_configs = _load_baked_all()
    if args.name not in all_configs:
        print(f"Error: no baked tool named '{args.name}'", file=sys.stderr)
        sys.exit(1)
    del all_configs[args.name]
    _save_baked_all(all_configs)
    # Clean up any installed wrapper
    wrapper = Path.home() / ".local" / "bin" / args.name
    if wrapper.exists():
        wrapper.unlink()
        print(f"Removed installed wrapper: {wrapper}")
    print(f"Baked tool '{args.name}' removed.")


def _bake_update(argv: list[str]) -> None:
    p = argparse.ArgumentParser(prog="mcp2cli bake update")
    p.add_argument("name")
    p.add_argument("--cache-ttl", type=int, default=None)
    p.add_argument("--include", default=None)
    p.add_argument("--exclude", default=None)
    p.add_argument("--methods", default=None)
    p.add_argument("--description", default=None)
    p.add_argument("--base-url", default=None)
    p.add_argument("--transport", choices=["auto", "sse", "streamable"], default=None)
    args = p.parse_args(argv)
    all_configs = _load_baked_all()
    if args.name not in all_configs:
        print(f"Error: no baked tool named '{args.name}'", file=sys.stderr)
        sys.exit(1)
    cfg = all_configs[args.name]
    if args.cache_ttl is not None:
        cfg["cache_ttl"] = args.cache_ttl
    if args.include is not None:
        cfg["include"] = [x.strip() for x in args.include.split(",") if x.strip()]
    if args.exclude is not None:
        cfg["exclude"] = [x.strip() for x in args.exclude.split(",") if x.strip()]
    if args.methods is not None:
        cfg["methods"] = [x.strip().upper() for x in args.methods.split(",") if x.strip()]
    if args.description is not None:
        cfg["description"] = args.description
    if args.base_url is not None:
        cfg["base_url"] = args.base_url
    if args.transport is not None:
        cfg["transport"] = args.transport
    _save_baked_all(all_configs)
    print(f"Baked tool '{args.name}' updated.")


def _bake_install(argv: list[str]) -> None:
    p = argparse.ArgumentParser(prog="mcp2cli bake install")
    p.add_argument("name")
    p.add_argument("--dir", default=None, help="Directory to install wrapper into (default: ~/.local/bin)")
    args = p.parse_args(argv)
    cfg = _load_baked(args.name)
    if cfg is None:
        print(f"Error: no baked tool named '{args.name}'", file=sys.stderr)
        sys.exit(1)
    bin_dir = Path(args.dir) if args.dir else Path.home() / ".local" / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    wrapper = bin_dir / args.name
    # Resolve mcp2cli path
    mcp2cli_bin = shutil.which("mcp2cli") or "mcp2cli"
    wrapper.write_text(
        f"#!/bin/sh\nexec {shlex.quote(mcp2cli_bin)} @{args.name} \"$@\"\n"
    )
    wrapper.chmod(0o755)
    print(f"Installed wrapper: {wrapper}")
    if args.dir is None and str(bin_dir) not in os.environ.get("PATH", ""):
        print(f"  Note: {bin_dir} may not be in your PATH")


# ---------------------------------------------------------------------------
# Run a baked tool
# ---------------------------------------------------------------------------


def _run_baked(name: str, argv: list[str]) -> None:
    """Load a baked config and run it."""
    cfg = _load_baked(name)
    if cfg is None:
        print(f"Error: no baked tool named '{name}'", file=sys.stderr)
        sys.exit(1)
    synthetic_argv = _baked_to_argv(cfg) + list(argv)
    bake_config = BakeConfig(
        include=cfg.get("include", []),
        exclude=cfg.get("exclude", []),
        methods=cfg.get("methods", []),
    )
    _main_impl(synthetic_argv, bake_config=bake_config)


# ---------------------------------------------------------------------------
# CLI builder
# ---------------------------------------------------------------------------


def build_argparse(
    commands: list[CommandDef], pre_parser: argparse.ArgumentParser
) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mcp2cli",
        description="Turn any MCP server or OpenAPI spec into a CLI",
        parents=[pre_parser],
    )
    subparsers = parser.add_subparsers(dest="_command")

    for cmd in commands:
        sub = subparsers.add_parser(
            cmd.name,
            help=escape_argparse_help(cmd.description),
        )
        sub.set_defaults(_cmd=cmd)

        if cmd.has_body:
            sub.add_argument(
                "--stdin",
                action="store_true",
                default=False,
                help="Read JSON body/arguments from stdin",
            )

        seen_flags: set[str] = set()
        for p in cmd.params:
            flag = f"--{p.name}"
            if flag in seen_flags:
                continue  # skip duplicate param names (e.g. path + body both have same name)
            seen_flags.add(flag)
            kwargs: dict = {}
            if p.python_type is not None:
                kwargs["type"] = p.python_type
            else:
                kwargs["action"] = "store_true"
            # Body/tool_input params are never argparse-required (--stdin bypasses them)
            if (
                p.required
                and "action" not in kwargs
                and p.location not in ("body", "tool_input", "graphql_arg")
            ):
                kwargs["required"] = True
            else:
                kwargs.setdefault("default", None)
            kwargs["help"] = escape_argparse_help(p.description)
            if p.choices:
                kwargs["choices"] = p.choices
            sub.add_argument(flag, **kwargs)

    return parser


# ---------------------------------------------------------------------------
# List commands
# ---------------------------------------------------------------------------


def _apply_list_options(
    commands: list[CommandDef],
    source_hash: str = "",
    sort_mode: str | None = None,
    top: int | None = None,
) -> list[CommandDef]:
    """Apply sort and top-N filtering to a command list."""
    effective_sort = _resolve_sort_mode(sort_mode, source_hash)
    commands = sort_commands(commands, effective_sort, source_hash)
    if top is not None:
        commands = commands[:top]
    return commands


def list_openapi_commands(
    commands: list[CommandDef],
    verbose: bool = False,
    compact: bool = False,
    source_hash: str = "",
    sort_mode: str | None = None,
    top: int | None = None,
):
    commands = _apply_list_options(commands, source_hash, sort_mode, top)

    if compact:
        print(" ".join(cmd.name for cmd in commands))
        return

    groups: dict[str, list[CommandDef]] = {}
    for cmd in commands:
        prefix = cmd.name.split("-", 1)[0] if "-" in cmd.name else "other"
        groups.setdefault(prefix, []).append(cmd)

    for group in sorted(groups):
        print(f"\n{group}:")
        for cmd in groups[group]:
            method = (cmd.method or "").upper()
            line = f"  {cmd.name:<45} {method:<6}"
            if cmd.description:
                if verbose:
                    wrapped = _wrap_description(cmd.description, indent=54, total_width=110)
                    line += f" {wrapped}"
                else:
                    line += f" {_truncate_description(cmd.description, 60)}"
            print(line)


def list_mcp_commands(
    commands: list[CommandDef],
    verbose: bool = False,
    compact: bool = False,
    source_hash: str = "",
    sort_mode: str | None = None,
    top: int | None = None,
):
    commands = _apply_list_options(commands, source_hash, sort_mode, top)

    if compact:
        print(" ".join(cmd.name for cmd in commands))
        return

    for cmd in commands:
        if cmd.description:
            if verbose:
                wrapped = _wrap_description(cmd.description, indent=42, total_width=110)
                desc = f"  {wrapped}"
            else:
                desc = f"  {_truncate_description(cmd.description, 70)}"
        else:
            desc = ""
        print(f"  {cmd.name:<40}{desc}")


def _filter_commands(commands: list[CommandDef], pattern: str) -> list[CommandDef]:
    """Filter commands by case-insensitive substring match on name or description."""
    pattern_lower = pattern.lower()
    return [
        cmd
        for cmd in commands
        if pattern_lower in cmd.name.lower()
        or pattern_lower in (cmd.description or "").lower()
    ]


# ---------------------------------------------------------------------------
# OpenAPI: execution
# ---------------------------------------------------------------------------


def _collect_openapi_params(
    cmd: CommandDef,
    args: argparse.Namespace,
) -> tuple[str, dict[str, str], dict[str, str], dict | None, dict | None]:
    """Collect OpenAPI params from parsed args, separated by location.

    Returns (path, query_params, extra_headers, body_or_none, files_or_none)
    where *path* has ``{param}`` placeholders substituted with actual values.
    """
    path = cmd.path or ""
    query_params: dict[str, str] = {}
    extra_headers: dict[str, str] = {}
    body: dict | None = None
    files: dict | None = None

    for p in cmd.params:
        if p.location == "path":
            val = getattr(args, p.name.replace("-", "_"), None)
            if val is not None:
                path = path.replace(f"{{{p.original_name}}}", str(val))

    if cmd.method == "get":
        for p in cmd.params:
            val = getattr(args, p.name.replace("-", "_"), None)
            if val is None:
                continue
            if p.location == "query":
                query_params[p.original_name] = val
            elif p.location == "header":
                extra_headers[p.original_name] = str(val)
    else:
        if getattr(args, "stdin", False):
            body = read_stdin_json("OpenAPI request body")
        else:
            body = {}
            for p in cmd.params:
                val = getattr(args, p.name.replace("-", "_"), None)
                if p.location == "header":
                    if val is not None:
                        extra_headers[p.original_name] = str(val)
                    continue
                if p.location == "path":
                    continue
                if p.location == "file":
                    if val is not None:
                        fp = Path(val)
                        if not fp.is_file():
                            print(f"Error: file not found: {val}", file=sys.stderr)
                            sys.exit(1)
                        mime = mimetypes.guess_type(val)[0] or "application/octet-stream"
                        if files is None:
                            files = {}
                        files[p.original_name] = (fp.name, open(fp, "rb"), mime)
                    continue
                if val is not None:
                    body[p.original_name] = val
            if not body:
                body = None
        # Also collect query params for non-GET
        for p in cmd.params:
            if p.location == "query":
                val = getattr(args, p.name.replace("-", "_"), None)
                if val is not None:
                    query_params[p.original_name] = val

    return path, query_params, extra_headers, body, files


def execute_openapi(
    args: argparse.Namespace,
    cmd: CommandDef,
    base_url: str,
    auth_headers: list[tuple[str, str]],
    pretty: bool,
    raw: bool,
    toon: bool = False,
    oauth_provider: "httpx.Auth | None" = None,
    jq_expr: str | None = None,
    head: int | None = None,
):
    path, query_params, extra_headers, body, files = _collect_openapi_params(cmd, args)
    url = base_url.rstrip("/") + path

    is_multipart = files is not None or cmd.content_type == "multipart/form-data"
    headers = _build_http_headers(auth_headers, multipart=is_multipart)
    headers.update(extra_headers)

    try:
        with httpx.Client(timeout=60, auth=oauth_provider) as client:
            if files is not None:
                resp = client.request(
                    (cmd.method or "get").upper(),
                    url,
                    headers=headers,
                    params=query_params or None,
                    data=body,
                    files=files,
                )
            elif cmd.content_type == "multipart/form-data":
                resp = client.request(
                    (cmd.method or "get").upper(),
                    url,
                    headers=headers,
                    params=query_params or None,
                    data=body,
                )
            else:
                resp = client.request(
                    (cmd.method or "get").upper(),
                    url,
                    headers=headers,
                    params=query_params or None,
                    json=body,
                )
            _handle_http_error(resp)
    finally:
        if files:
            for _, file_tuple in files.items():
                file_tuple[1].close()

    if raw:
        sys.stdout.buffer.write(resp.content)
        return

    try:
        data = resp.json()
    except Exception:
        print(resp.text)
        return

    output_result(data, pretty=pretty, toon=toon, jq_expr=jq_expr, head=head)


# ---------------------------------------------------------------------------
# MCP: execution
# ---------------------------------------------------------------------------


def run_mcp_http(
    url: str,
    auth_headers: list[tuple[str, str]],
    tool_name: str | None,
    arguments: dict | None,
    list_mode: bool,
    pretty: bool,
    raw: bool,
    cache_key: str | None,
    ttl: int,
    refresh: bool,
    toon: bool = False,
    transport: str = "auto",
    oauth_provider: httpx.Auth | None = None,
    resource_action: str | None = None,
    resource_uri: str | None = None,
    prompt_action: str | None = None,
    prompt_name: str | None = None,
    prompt_arguments: dict | None = None,
    search_pattern: str | None = None,
    jq_expr: str | None = None,
    head: int | None = None,
    verbose: bool = False,
    sort_mode: str | None = None,
    top: int | None = None,
    compact: bool = False,
    source_hash: str = "",
):
    extra = dict(
        resource_action=resource_action,
        resource_uri=resource_uri,
        prompt_action=prompt_action,
        prompt_name=prompt_name,
        prompt_arguments=prompt_arguments,
        search_pattern=search_pattern,
        jq_expr=jq_expr,
        head=head,
        verbose=verbose,
        sort_mode=sort_mode,
        top=top,
        compact=compact,
        source_hash=source_hash,
    )

    async def _run():
        from mcp import ClientSession

        headers = dict(auth_headers) if auth_headers else None

        async def _with_streamable():
            from mcp.client.streamable_http import streamablehttp_client

            async with streamablehttp_client(
                url, headers=headers, auth=oauth_provider
            ) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    return await _mcp_session(
                        session,
                        tool_name,
                        arguments,
                        list_mode,
                        pretty,
                        raw,
                        cache_key,
                        ttl,
                        refresh,
                        toon=toon,
                        **extra,
                    )

        async def _with_sse():
            from mcp.client.sse import sse_client

            async with sse_client(url, headers=headers, auth=oauth_provider) as (
                read,
                write,
            ):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    return await _mcp_session(
                        session,
                        tool_name,
                        arguments,
                        list_mode,
                        pretty,
                        raw,
                        cache_key,
                        ttl,
                        refresh,
                        toon=toon,
                        **extra,
                    )

        if transport == "sse":
            return await _with_sse()
        elif transport == "streamable":
            return await _with_streamable()
        else:  # auto
            try:
                return await _with_streamable()
            except Exception:
                return await _with_sse()

    anyio.run(_run)


def run_mcp_stdio(
    command_str: str,
    env_vars: dict[str, str],
    tool_name: str | None,
    arguments: dict | None,
    list_mode: bool,
    pretty: bool,
    raw: bool,
    cache_key: str | None,
    ttl: int,
    refresh: bool,
    toon: bool = False,
    resource_action: str | None = None,
    resource_uri: str | None = None,
    prompt_action: str | None = None,
    prompt_name: str | None = None,
    prompt_arguments: dict | None = None,
    search_pattern: str | None = None,
    jq_expr: str | None = None,
    head: int | None = None,
    verbose: bool = False,
    sort_mode: str | None = None,
    top: int | None = None,
    compact: bool = False,
    source_hash: str = "",
):
    extra = dict(
        resource_action=resource_action,
        resource_uri=resource_uri,
        prompt_action=prompt_action,
        prompt_name=prompt_name,
        prompt_arguments=prompt_arguments,
        search_pattern=search_pattern,
        jq_expr=jq_expr,
        head=head,
        verbose=verbose,
        sort_mode=sort_mode,
        top=top,
        compact=compact,
        source_hash=source_hash,
    )

    import anyio

    async def _run():
        from mcp import ClientSession
        from mcp.client.stdio import StdioServerParameters, stdio_client

        parts = shlex.split(command_str)
        env = {**os.environ, **env_vars}
        params = StdioServerParameters(command=parts[0], args=parts[1:], env=env)

        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                await _mcp_session(
                    session,
                    tool_name,
                    arguments,
                    list_mode,
                    pretty,
                    raw,
                    cache_key,
                    ttl,
                    refresh,
                    toon=toon,
                    **extra,
                )

    anyio.run(_run)


async def _mcp_session(
    session,
    tool_name: str | None,
    arguments: dict | None,
    list_mode: bool,
    pretty: bool,
    raw: bool,
    cache_key: str | None,
    ttl: int,
    refresh: bool,
    toon: bool = False,
    resource_action: str | None = None,
    resource_uri: str | None = None,
    prompt_action: str | None = None,
    prompt_name: str | None = None,
    prompt_arguments: dict | None = None,
    search_pattern: str | None = None,
    jq_expr: str | None = None,
    head: int | None = None,
    verbose: bool = False,
    sort_mode: str | None = None,
    top: int | None = None,
    compact: bool = False,
    source_hash: str = "",
):
    # Handle resource operations
    if resource_action:
        await _handle_resources(
            session, resource_action, resource_uri, pretty, raw, toon,
            jq_expr=jq_expr, head=head,
        )
        return

    # Handle prompt operations
    if prompt_action:
        await _handle_prompts(
            session, prompt_action, prompt_name, prompt_arguments, pretty, raw, toon,
            jq_expr=jq_expr, head=head,
        )
        return

    list_kwargs = dict(
        verbose=verbose, compact=compact,
        source_hash=source_hash, sort_mode=sort_mode, top=top,
    )

    if list_mode:
        result = await session.list_tools()
        tools = [
            {
                "name": t.name,
                "description": t.description or "",
                "inputSchema": t.inputSchema or {},
            }
            for t in result.tools
        ]
        commands = extract_mcp_commands(tools)
        if search_pattern:
            commands = _filter_commands(commands, search_pattern)
            if not commands:
                print(f"\nNo tools matching '{search_pattern}'.")
                return
            if not compact:
                print(f"\nTools matching '{search_pattern}':")
        else:
            if not compact:
                print("\nAvailable tools:")
        list_mcp_commands(commands, **list_kwargs)
        return

    if tool_name is None:
        print(
            "Error: no subcommand specified. Use --list to see available tools.",
            file=sys.stderr,
        )
        sys.exit(1)

    result = await session.call_tool(tool_name, arguments or {})

    text = _extract_content_parts(result.content)
    output_result(text, pretty=pretty, raw=raw, toon=toon, jq_expr=jq_expr, head=head)


# ---------------------------------------------------------------------------
# Resources
# ---------------------------------------------------------------------------


async def _handle_resources(
    session, action: str, uri: str | None, pretty: bool, raw: bool, toon: bool,
    jq_expr: str | None = None, head: int | None = None,
):
    _out = dict(pretty=pretty, raw=raw, toon=toon, jq_expr=jq_expr, head=head)
    if action == "list":
        result = await session.list_resources()
        data = [
            {
                "name": r.name,
                "uri": str(r.uri),
                "description": r.description or "",
                "mimeType": r.mimeType or "",
            }
            for r in result.resources
        ]
        output_result(data, **_out)
    elif action == "templates":
        result = await session.list_resource_templates()
        data = [
            {
                "name": t.name,
                "uriTemplate": str(t.uriTemplate),
                "description": t.description or "",
                "mimeType": t.mimeType or "",
            }
            for t in result.resourceTemplates
        ]
        output_result(data, **_out)
    elif action == "read":
        from pydantic import AnyUrl

        result = await session.read_resource(AnyUrl(uri))
        parts = []
        for content in result.contents:
            if hasattr(content, "text"):
                parts.append(content.text)
            elif hasattr(content, "blob"):
                parts.append(content.blob)
        text = "\n".join(parts) if parts else ""
        output_result(text, **_out)


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------


async def _handle_prompts(
    session,
    action: str,
    name: str | None,
    arguments: dict | None,
    pretty: bool,
    raw: bool,
    toon: bool,
    jq_expr: str | None = None,
    head: int | None = None,
):
    _out = dict(pretty=pretty, raw=raw, toon=toon, jq_expr=jq_expr, head=head)
    if action == "list":
        result = await session.list_prompts()
        data = [
            {
                "name": p.name,
                "description": p.description or "",
                "arguments": [
                    {
                        "name": a.name,
                        "description": a.description or "",
                        "required": a.required or False,
                    }
                    for a in (p.arguments or [])
                ],
            }
            for p in result.prompts
        ]
        output_result(data, **_out)
    elif action == "get":
        result = await session.get_prompt(name, arguments or {})
        messages = []
        for msg in result.messages:
            content = msg.content
            if hasattr(content, "text"):
                messages.append({"role": msg.role, "content": content.text})
            else:
                messages.append(
                    {"role": msg.role, "content": json.dumps(content.model_dump())}
                )
        data = {"description": result.description or "", "messages": messages}
        output_result(data, **_out)


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------

SESSIONS_DIR = CACHE_DIR / "sessions"


def _session_meta_path(name: str) -> Path:
    return SESSIONS_DIR / f"{name}.json"


def _session_sock_path(name: str) -> Path:
    return SESSIONS_DIR / f"{name}.sock"


def _session_is_alive(meta: dict) -> bool:
    pid = meta.get("pid")
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def session_list() -> list[dict]:
    """List active sessions."""
    if not SESSIONS_DIR.exists():
        return []
    sessions = []
    for meta_file in SESSIONS_DIR.glob("*.json"):
        try:
            meta = json.loads(meta_file.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        name = meta_file.stem
        meta["name"] = name
        meta["alive"] = _session_is_alive(meta)
        sessions.append(meta)
    return sessions


def session_stop(name: str):
    """Stop a named session."""
    meta_path = _session_meta_path(name)
    sock_path = _session_sock_path(name)
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text())
            pid = meta.get("pid")
            if pid and _session_is_alive(meta):
                os.kill(pid, signal.SIGTERM)
                # Wait briefly for clean shutdown
                for _ in range(10):
                    try:
                        os.kill(pid, 0)
                        time.sleep(0.1)
                    except OSError:
                        break
        except (json.JSONDecodeError, OSError):
            pass
        meta_path.unlink(missing_ok=True)
    sock_path.unlink(missing_ok=True)


def session_start(
    name: str,
    source: str,
    is_stdio: bool,
    auth_headers: list[tuple[str, str]],
    env_vars: dict[str, str],
    transport: str = "auto",
):
    """Start a persistent session daemon."""
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)

    # Check if already running
    meta_path = _session_meta_path(name)
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text())
            if _session_is_alive(meta):
                print(
                    f"Session '{name}' is already running (PID {meta['pid']})",
                    file=sys.stderr,
                )
                sys.exit(1)
        except (json.JSONDecodeError, OSError):
            pass
        # Stale session — clean up
        meta_path.unlink(missing_ok=True)
        _session_sock_path(name).unlink(missing_ok=True)

    # Spawn daemon
    daemon_script = json.dumps(
        {
            "name": name,
            "source": source,
            "is_stdio": is_stdio,
            "auth_headers": auth_headers,
            "env_vars": env_vars,
            "transport": transport,
        }
    )

    proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            f"import mcp2cli; mcp2cli._run_session_daemon({json.dumps(daemon_script)})",
        ],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
    )

    # Wait for socket to appear
    sock_path = _session_sock_path(name)
    deadline = time.time() + 15
    while time.time() < deadline:
        if sock_path.exists():
            print(f"Session '{name}' started (PID {proc.pid})")
            return
        if proc.poll() is not None:
            print(
                f"Error: session daemon exited with code {proc.returncode}",
                file=sys.stderr,
            )
            sys.exit(1)
        time.sleep(0.1)

    print("Error: session daemon did not start in time", file=sys.stderr)
    proc.kill()
    sys.exit(1)


def _extract_content_parts(content_list, *, attrs=("text", "data")) -> str:
    """Extract text/data/blob from MCP content objects, joined by newline."""
    parts = []
    for c in content_list:
        for attr in attrs:
            if hasattr(c, attr):
                parts.append(getattr(c, attr))
                break
    return "\n".join(parts) if parts else ""


async def _dispatch_list_tools(session, params):
    result = await session.list_tools()
    return [
        {"name": t.name, "description": t.description or "", "inputSchema": t.inputSchema or {}}
        for t in result.tools
    ]


async def _dispatch_call_tool(session, params):
    result = await session.call_tool(params["name"], params.get("arguments", {}))
    return _extract_content_parts(result.content)


async def _dispatch_list_resources(session, params):
    result = await session.list_resources()
    return [
        {"name": r.name, "uri": str(r.uri), "description": r.description or "", "mimeType": r.mimeType or ""}
        for r in result.resources
    ]


async def _dispatch_read_resource(session, params):
    from pydantic import AnyUrl

    result = await session.read_resource(AnyUrl(params["uri"]))
    return _extract_content_parts(result.contents, attrs=("text", "blob"))


async def _dispatch_list_resource_templates(session, params):
    result = await session.list_resource_templates()
    return [
        {"name": t.name, "uriTemplate": str(t.uriTemplate), "description": t.description or "", "mimeType": t.mimeType or ""}
        for t in result.resourceTemplates
    ]


async def _dispatch_list_prompts(session, params):
    result = await session.list_prompts()
    return [
        {
            "name": p.name,
            "description": p.description or "",
            "arguments": [
                {"name": a.name, "description": a.description or "", "required": a.required or False}
                for a in (p.arguments or [])
            ],
        }
        for p in result.prompts
    ]


async def _dispatch_get_prompt(session, params):
    result = await session.get_prompt(params["name"], params.get("arguments", {}))
    messages = []
    for msg in result.messages:
        content = msg.content
        if hasattr(content, "text"):
            messages.append({"role": msg.role, "content": content.text})
        else:
            messages.append({"role": msg.role, "content": json.dumps(content.model_dump())})
    return {"description": result.description or "", "messages": messages}


_SESSION_DISPATCH = {
    "list_tools": _dispatch_list_tools,
    "call_tool": _dispatch_call_tool,
    "list_resources": _dispatch_list_resources,
    "read_resource": _dispatch_read_resource,
    "list_resource_templates": _dispatch_list_resource_templates,
    "list_prompts": _dispatch_list_prompts,
    "get_prompt": _dispatch_get_prompt,
}


def _run_session_daemon(config_json: str):
    """Entry point for the session daemon process."""
    config = json.loads(config_json)
    name = config["name"]
    source = config["source"]
    is_stdio = config["is_stdio"]
    auth_headers = [tuple(h) for h in config["auth_headers"]]
    env_vars = config["env_vars"]
    transport = config["transport"]

    sock_path = _session_sock_path(name)
    meta_path = _session_meta_path(name)

    import anyio

    async def _dispatch(session, method: str, params: dict):
        """Dispatch a method call to the MCP session."""
        handler = _SESSION_DISPATCH.get(method)
        if handler is None:
            raise ValueError(f"Unknown method: {method}")
        return await handler(session, params)

    async def _daemon():
        from mcp import ClientSession

        async def _run_with_session(session):
            await session.initialize()

            # Write metadata
            meta = {
                "pid": os.getpid(),
                "source": source,
                "transport": "stdio" if is_stdio else "http",
                "created_at": time.time(),
            }
            meta_path.write_text(json.dumps(meta))

            # Start Unix domain socket server
            server_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                server_sock.bind(str(sock_path))
                server_sock.listen(5)
                server_sock.settimeout(1.0)

                # Handle SIGTERM
                _shutdown = False

                def _on_sigterm(*_):
                    nonlocal _shutdown
                    _shutdown = True

                signal.signal(signal.SIGTERM, _on_sigterm)

                def _blocking_accept():
                    """Accept a connection in a thread (blocks until connection or timeout)."""
                    while not _shutdown:
                        try:
                            return server_sock.accept()
                        except socket.timeout:
                            continue
                        except OSError:
                            return None, None
                    return None, None

                while not _shutdown:
                    conn, _ = await anyio.to_thread.run_sync(_blocking_accept)
                    if conn is None:
                        break

                    try:
                        conn.settimeout(5)

                        def _recv_request(c):
                            data = b""
                            while True:
                                chunk = c.recv(65536)
                                if not chunk:
                                    break
                                data += chunk
                                if b"\n" in data:
                                    break
                            return data

                        raw = await anyio.to_thread.run_sync(
                            lambda: _recv_request(conn)
                        )
                        line = raw.split(b"\n", 1)[0]
                        if not line:
                            conn.close()
                            continue

                        request = json.loads(line)
                        req_id = request.get("id", 0)
                        method = request.get("method", "")
                        params = request.get("params", {})

                        try:
                            resp_data = await _dispatch(session, method, params)
                            response = (
                                json.dumps({"id": req_id, "result": resp_data}) + "\n"
                            )
                        except Exception as e:
                            response = (
                                json.dumps({"id": req_id, "error": str(e)}) + "\n"
                            )

                        def _send(c, data):
                            c.sendall(data)

                        await anyio.to_thread.run_sync(
                            lambda: _send(conn, response.encode())
                        )
                    except Exception:
                        pass
                    finally:
                        conn.close()

            finally:
                server_sock.close()
                sock_path.unlink(missing_ok=True)
                meta_path.unlink(missing_ok=True)

        if is_stdio:
            from mcp.client.stdio import StdioServerParameters, stdio_client

            parts = shlex.split(source)
            env = {**os.environ, **env_vars}
            params = StdioServerParameters(command=parts[0], args=parts[1:], env=env)
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await _run_with_session(session)
        else:
            headers = dict(auth_headers) if auth_headers else None

            async def _via_streamable():
                from mcp.client.streamable_http import streamablehttp_client

                async with streamablehttp_client(source, headers=headers) as (
                    read,
                    write,
                    _,
                ):
                    async with ClientSession(read, write) as session:
                        await _run_with_session(session)

            async def _via_sse():
                from mcp.client.sse import sse_client

                async with sse_client(source, headers=headers) as (read, write):
                    async with ClientSession(read, write) as session:
                        await _run_with_session(session)

            if transport == "sse":
                await _via_sse()
            elif transport == "streamable":
                await _via_streamable()
            else:
                try:
                    await _via_streamable()
                except Exception:
                    await _via_sse()

    anyio.run(_daemon)


def _session_request(name: str, method: str, params: dict | None = None) -> any:
    """Send a request to a session daemon and return the result."""
    sock_path = _session_sock_path(name)
    if not sock_path.exists():
        print(f"Error: session '{name}' not found", file=sys.stderr)
        sys.exit(1)

    conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        conn.connect(str(sock_path))
        request = json.dumps({"id": 1, "method": method, "params": params or {}}) + "\n"
        conn.sendall(request.encode())
        conn.shutdown(socket.SHUT_WR)

        data = b""
        while True:
            chunk = conn.recv(65536)
            if not chunk:
                break
            data += chunk

        response = json.loads(data.decode())
        if "error" in response:
            print(f"Error: {response['error']}", file=sys.stderr)
            sys.exit(1)
        return response["result"]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# MCP: argparse integration
# ---------------------------------------------------------------------------


def _fetch_or_cache_mcp_tools(
    key: str,
    ttl: int,
    refresh: bool,
    source: str,
    is_stdio: bool,
    auth_headers: list[tuple[str, str]],
    env_vars: dict[str, str],
    transport: str = "auto",
    oauth_provider: httpx.Auth | None = None,
) -> list[dict]:
    """Load MCP tools from cache or fetch from server, caching the result."""
    if not refresh:
        cached = load_cached(f"{key}_tools", ttl)
        if cached is not None:
            return cached
    tools = _fetch_mcp_tools(
        source, is_stdio, auth_headers, env_vars,
        transport=transport, oauth_provider=oauth_provider,
    )
    save_cache(f"{key}_tools", tools)
    return tools


def _dispatch_mcp_call(
    source: str,
    is_stdio: bool,
    auth_headers: list[tuple[str, str]],
    env_vars: dict[str, str],
    tool_name: str | None,
    arguments: dict | None,
    list_mode: bool,
    pretty: bool,
    raw: bool,
    cache_key: str,
    ttl: int,
    refresh: bool,
    *,
    toon: bool = False,
    transport: str = "auto",
    oauth_provider: httpx.Auth | None = None,
    **extra,
) -> None:
    """Route to run_mcp_stdio or run_mcp_http based on is_stdio."""
    if is_stdio:
        run_mcp_stdio(
            source, env_vars, tool_name, arguments, list_mode,
            pretty, raw, cache_key, ttl, refresh, toon=toon, **extra,
        )
    else:
        run_mcp_http(
            source, auth_headers, tool_name, arguments, list_mode,
            pretty, raw, cache_key, ttl, refresh, toon=toon,
            transport=transport, oauth_provider=oauth_provider, **extra,
        )


def handle_mcp(
    source: str,
    is_stdio: bool,
    auth_headers: list[tuple[str, str]],
    env_vars: dict[str, str],
    remaining: list[str],
    list_mode: bool,
    pretty: bool,
    raw: bool,
    cache_key_override: str | None,
    ttl: int,
    refresh: bool,
    toon: bool = False,
    transport: str = "auto",
    oauth_provider: httpx.Auth | None = None,
    resource_action: str | None = None,
    resource_uri: str | None = None,
    prompt_action: str | None = None,
    prompt_name: str | None = None,
    prompt_arguments: dict | None = None,
    search_pattern: str | None = None,
    bake_config: BakeConfig | None = None,
    jq_expr: str | None = None,
    head: int | None = None,
    verbose: bool = False,
    sort_mode: str | None = None,
    top: int | None = None,
    compact: bool = False,
):
    # Build a config dict for cache key generation (future-proof)
    config_for_cache = {
        'source': source,
        'auth_headers': auth_headers,
        'transport': transport,
        'env_vars': env_vars,
        'is_stdio': is_stdio,
    }

    key = cache_key_override or cache_key_for(config_for_cache)
    src_hash = _source_hash_for(source)

    # Resource/prompt operations skip the tool flow entirely
    if resource_action or prompt_action:
        extra = dict(
            resource_action=resource_action,
            resource_uri=resource_uri,
            prompt_action=prompt_action,
            prompt_name=prompt_name,
            prompt_arguments=prompt_arguments,
            jq_expr=jq_expr,
            head=head,
        )
        _dispatch_mcp_call(
            source, is_stdio, auth_headers, env_vars,
            None, None, False, pretty, raw, key, ttl, refresh,
            toon=toon, transport=transport, oauth_provider=oauth_provider,
            **extra,
        )
        return

    list_kwargs = dict(
        verbose=verbose, compact=compact,
        source_hash=src_hash, sort_mode=sort_mode, top=top,
    )

    if list_mode:
        if bake_config and (bake_config.include or bake_config.exclude or bake_config.methods):
            # Fetch tools, filter, then list -- don't delegate to unfiltered path
            tools = _fetch_or_cache_mcp_tools(
                key, ttl, refresh, source, is_stdio, auth_headers, env_vars,
                transport=transport, oauth_provider=oauth_provider,
            )
            commands = extract_mcp_commands(tools)
            commands = filter_commands(
                commands, bake_config.include, bake_config.exclude, bake_config.methods,
            )
            if not compact:
                print("\nAvailable tools:")
            list_mcp_commands(commands, **list_kwargs)
            return
        _dispatch_mcp_call(
            source, is_stdio, auth_headers, env_vars,
            None, None, True, pretty, raw, key, ttl, refresh,
            toon=toon, transport=transport, oauth_provider=oauth_provider,
            search_pattern=search_pattern,
            jq_expr=jq_expr, head=head,
            verbose=verbose,
            sort_mode=sort_mode, top=top, compact=compact,
            source_hash=src_hash,
        )
        return

    # We need tool list to build argparse, try cache first
    tools = _fetch_or_cache_mcp_tools(
        key, ttl, refresh, source, is_stdio, auth_headers, env_vars,
        transport=transport, oauth_provider=oauth_provider,
    )

    commands = extract_mcp_commands(tools)
    if bake_config:
        commands = filter_commands(
            commands, bake_config.include, bake_config.exclude, bake_config.methods,
        )

    if not remaining:
        if not compact:
            print("Available tools:")
        list_mcp_commands(commands, **list_kwargs)
        if not compact:
            print("\nUse --list for the same output, or provide a subcommand.")
        return

    pre = argparse.ArgumentParser(add_help=False)
    parser = build_argparse(commands, pre)
    args = parser.parse_args(remaining)

    if not hasattr(args, "_cmd"):
        parser.print_help()
        sys.exit(1)

    cmd: CommandDef = args._cmd

    if getattr(args, "stdin", False):
        arguments = read_stdin_json("MCP tool arguments")
    else:
        arguments = {}
        for p in cmd.params:
            val = getattr(args, p.name.replace("-", "_"), None)
            if val is not None:
                arguments[p.original_name] = coerce_value(val, p.schema)

    _dispatch_mcp_call(
        source, is_stdio, auth_headers, env_vars,
        cmd.tool_name, arguments, False, pretty, raw, key, ttl, refresh,
        toon=toon, transport=transport, oauth_provider=oauth_provider,
        jq_expr=jq_expr, head=head,
    )

    # Record usage after successful execution
    record_usage(src_hash, cmd.tool_name or cmd.name)


def _fetch_mcp_tools(
    source: str,
    is_stdio: bool,
    auth_headers: list[tuple[str, str]],
    env_vars: dict[str, str],
    transport: str = "auto",
    oauth_provider: httpx.Auth | None = None,
) -> list[dict]:
    tools_result: list[dict] = []

    async def _extract_tools(session):
        result = await session.list_tools()
        tools_result.extend(
            {
                "name": t.name,
                "description": t.description or "",
                "inputSchema": t.inputSchema or {},
            }
            for t in result.tools
        )

    async def _run():
        nonlocal tools_result

        if is_stdio:
            from mcp import ClientSession
            from mcp.client.stdio import StdioServerParameters, stdio_client

            parts = shlex.split(source)
            env = {**os.environ, **env_vars}
            params = StdioServerParameters(command=parts[0], args=parts[1:], env=env)
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    await _extract_tools(session)
        else:
            from mcp import ClientSession

            headers = dict(auth_headers) if auth_headers else None

            async def _via_streamable():
                from mcp.client.streamable_http import streamablehttp_client

                async with streamablehttp_client(
                    source, headers=headers, auth=oauth_provider
                ) as (read, write, _):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        await _extract_tools(session)

            async def _via_sse():
                from mcp.client.sse import sse_client

                async with sse_client(source, headers=headers, auth=oauth_provider) as (
                    read,
                    write,
                ):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        await _extract_tools(session)

            if transport == "sse":
                await _via_sse()
            elif transport == "streamable":
                await _via_streamable()
            else:  # auto
                try:
                    await _via_streamable()
                except Exception:
                    await _via_sse()

    anyio.run(_run)
    return tools_result


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def _split_at_subcommand(
    argv: list[str], pre_parser: argparse.ArgumentParser
) -> tuple[list[str], list[str]]:
    """Split *argv* into ``(global_args, tool_args)`` at the subcommand boundary.

    Walks *argv* consuming only tokens that belong to the global pre-parser
    (options it defines and their values).  The first positional token that is
    **not** a value of a preceding option is treated as the subcommand name;
    everything from that point onward is returned as *tool_args* so that the
    tool sub-parser can handle them — even when a tool parameter shares the
    same name as a global option (e.g. ``--env``).
    """
    value_options: set[str] = set()
    bool_options: set[str] = set()
    for action in pre_parser._actions:
        if not action.option_strings:
            continue
        if action.nargs == 0:
            bool_options.update(action.option_strings)
        else:
            value_options.update(action.option_strings)

    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--":
            # Explicit separator: everything after belongs to the tool.
            return argv[:i], argv[i + 1 :]
        if arg.startswith("-"):
            if arg.startswith("--") and "=" in arg:
                i += 1  # --option=value  (single token)
            elif arg in value_options:
                i += 2  # --option value  (consumes next token)
            elif arg in bool_options:
                i += 1  # --flag
            else:
                i += 1  # unknown option — keep in global portion
        else:
            # First positional token = subcommand boundary
            return argv[:i], argv[i:]
    return argv, []


def main():
    if len(sys.argv) > 1:
        first = sys.argv[1]
        if first == "bake":
            _handle_bake(sys.argv[2:])
            return
        if first.startswith("@"):
            _run_baked(first[1:], sys.argv[2:])
            return
    _main_impl(sys.argv[1:])


def _build_main_parser() -> argparse.ArgumentParser:
    """Build the global ArgumentParser for _main_impl."""
    pre = argparse.ArgumentParser(
        add_help=False,
        allow_abbrev=False,
        epilog=(
            "subcommands:\n"
            "  bake                Manage baked tool configurations\n"
            "                      (create, list, show, remove, update, install)\n"
            "  @<name>             Run a previously baked tool\n"
            "\n"
            "Run 'mcp2cli bake --help' for bake subcommand details."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    pre.add_argument("--spec", default=None, help="OpenAPI spec URL or file path")
    pre.add_argument("--mcp", default=None, help="MCP server URL (HTTP/SSE)")
    pre.add_argument("--mcp-stdio", default=None, help="MCP server command (stdio)")
    pre.add_argument("--graphql", default=None, help="GraphQL endpoint URL")
    pre.add_argument(
        "--auth-header",
        action="append",
        default=[],
        help="HTTP header as Name:Value (repeatable). Value supports env:VAR and file:/path prefixes",
    )
    pre.add_argument("--base-url", default=None, help="Override base URL from spec")
    pre.add_argument("--cache-key", default=None, help="Custom cache key")
    pre.add_argument(
        "--cache-ttl", type=int, default=DEFAULT_CACHE_TTL, help="Cache TTL in seconds"
    )
    pre.add_argument("--refresh", action="store_true", help="Force re-fetch spec")
    pre.add_argument(
        "--list",
        action="store_true",
        dest="list_commands",
        help="List available subcommands",
    )
    pre.add_argument(
        "--search",
        default=None,
        dest="search_pattern",
        metavar="PATTERN",
        help="Search tools by name or description (case-insensitive substring match)",
    )
    pre.add_argument(
        "--verbose",
        action="store_true",
        dest="verbose",
        help="Show full tool descriptions in --list output, wrapped to terminal width (default: truncated with ...)",
    )
    pre.add_argument(
        "--sort",
        choices=["usage", "recent", "alpha", "default"],
        default=None,
        dest="sort_mode",
        help=(
            "Sort order for --list output. 'usage' sorts by call frequency, "
            "'recent' by last-used time, 'alpha' alphabetically, 'default' keeps "
            "insertion order. When omitted, defaults to 'usage' if usage data "
            "exists, otherwise 'default'."
        ),
    )
    pre.add_argument(
        "--top",
        type=int,
        default=None,
        metavar="N",
        help="Show only the top N tools in --list output (useful for LLM agents)",
    )
    pre.add_argument(
        "--compact",
        action="store_true",
        help="Space-separated tool names only, no descriptions (~2 tokens/tool)",
    )
    pre.add_argument("--pretty", action="store_true", help="Pretty-print JSON output")
    pre.add_argument("--raw", action="store_true", help="Print raw response body")
    pre.add_argument(
        "--toon",
        action="store_true",
        help=(
            "Encode output as TOON (Token-Oriented Object Notation) instead of JSON. "
            "TOON is 40-60%% more token-efficient for uniform arrays (e.g. list-tags, "
            "list-users) and 15-20%% for semi-uniform data. Best for LLM consumption "
            "of large result sets. Requires @toon-format/cli (npm install -g @toon-format/cli)."
        ),
    )
    pre.add_argument(
        "--jq",
        default=None,
        metavar="EXPR",
        help="Filter JSON output through jq (e.g. '.[] | .name'). Requires jq installed.",
    )
    pre.add_argument(
        "--head",
        type=int,
        default=None,
        metavar="N",
        help="Limit output to first N records (arrays) or N lines (text)",
    )
    pre.add_argument(
        "--fields",
        default=None,
        help="Override auto-generated GraphQL selection set fields (e.g. 'id name email')",
    )
    pre.add_argument(
        "--transport",
        choices=["auto", "sse", "streamable"],
        default="auto",
        help="MCP HTTP transport: 'auto' tries streamable then SSE, 'sse' skips streamable, 'streamable' skips SSE fallback",
    )
    pre.add_argument(
        "--env",
        action="append",
        default=[],
        help="Environment variable KEY=VALUE for MCP stdio (repeatable)",
    )
    pre.add_argument(
        "--oauth",
        action="store_true",
        help="Enable OAuth authentication (authorization code + PKCE flow)",
    )
    pre.add_argument(
        "--oauth-client-id",
        default=None,
        help="OAuth client ID — supports env:VAR and file:/path prefixes",
    )
    pre.add_argument(
        "--oauth-client-secret",
        default=None,
        help="OAuth client secret — supports env:VAR and file:/path prefixes",
    )
    pre.add_argument(
        "--oauth-client-name",
        default="mcp2cli",
        help="Client name sent during OAuth Dynamic Client Registration (default: mcp2cli). "
             "Some servers require a specific client name for DCR to succeed.",
    )
    pre.add_argument(
        "--oauth-scope",
        default=None,
        help="OAuth scope(s) to request",
    )
    pre.add_argument(
        "--oauth-redirect-uri",
        default=None,
        metavar="URI",
        help="Full redirect URI for the OAuth callback (e.g. http://localhost:3334/oauth/callback). "
             "Overrides the default http://127.0.0.1:<random-port>/callback.",
    )
    pre.add_argument(
        "--oauth-flow",
        choices=["auto", "authorization_code", "client_credentials"],
        default="auto",
        help=(
            "OAuth flow to use. 'auto' (default) picks client_credentials when both "
            "client-id and client-secret are provided, otherwise authorization_code. "
            "Use 'authorization_code' to force the auth code + PKCE flow even with a "
            "client secret (required for confidential-client servers like Slack)."
        ),
    )
    # Resource flags
    pre.add_argument(
        "--list-resources", action="store_true", help="List available resources"
    )
    pre.add_argument(
        "--list-resource-templates", action="store_true", help="List resource templates"
    )
    pre.add_argument(
        "--read-resource", default=None, metavar="URI", help="Read a resource by URI"
    )
    # Prompt flags
    pre.add_argument(
        "--list-prompts", action="store_true", help="List available prompts"
    )
    pre.add_argument(
        "--get-prompt", default=None, metavar="NAME", help="Get a prompt by name"
    )
    pre.add_argument(
        "--prompt-arg",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="Argument for --get-prompt (repeatable)",
    )
    # Session flags
    pre.add_argument(
        "--session-start",
        default=None,
        metavar="NAME",
        help="Start a persistent session daemon",
    )
    pre.add_argument(
        "--session-stop", default=None, metavar="NAME", help="Stop a named session"
    )
    pre.add_argument("--session-list", action="store_true", help="List active sessions")
    pre.add_argument(
        "--session", default=None, metavar="NAME", help="Use an existing session"
    )
    pre.add_argument("--version", action="version", version=f"mcp2cli {__version__}")
    return pre


def _validate_source_modes(pre_args, pre, remaining) -> None:
    """Validate mutual exclusivity of --spec/--mcp/--mcp-stdio/--graphql.

    Exits on validation failure.  Session commands don't require a source.
    """
    needs_source = not (
        pre_args.session_list or pre_args.session_stop or pre_args.session
    )
    modes = [pre_args.spec, pre_args.mcp, pre_args.mcp_stdio, pre_args.graphql]
    active = sum(1 for m in modes if m is not None)
    if needs_source:
        if active == 0:
            pre.print_help()
            if "-h" in remaining or "--help" in remaining:
                sys.exit(0)
            print(
                "\nError: one of --spec, --mcp, --mcp-stdio, or --graphql is required.",
                file=sys.stderr,
            )
            sys.exit(1)
    if active > 1:
        print(
            "Error: --spec, --mcp, --mcp-stdio, and --graphql are mutually exclusive.",
            file=sys.stderr,
        )
        sys.exit(1)


def _setup_oauth(pre_args):
    """Build OAuth provider if --oauth flags are present.

    Returns oauth_provider or None.  Exits on invalid flag combinations.
    """
    use_oauth = (
        pre_args.oauth or pre_args.oauth_client_id or pre_args.oauth_client_secret
    )
    if not use_oauth:
        return None

    if pre_args.oauth_client_secret and not pre_args.oauth_client_id:
        print(
            "Error: --oauth-client-id is required with --oauth-client-secret",
            file=sys.stderr,
        )
        sys.exit(1)
    if pre_args.mcp_stdio:
        print(
            "Error: OAuth is not supported with --mcp-stdio", file=sys.stderr
        )
        sys.exit(1)
    # Determine OAuth server URL for discovery
    server_url = pre_args.mcp or pre_args.graphql
    if not server_url and pre_args.spec:
        if pre_args.spec.startswith("http"):
            server_url = pre_args.spec
        else:
            server_url = pre_args.base_url
    if not server_url:
        print(
            "Error: OAuth requires an HTTP URL (use --base-url with local spec files)",
            file=sys.stderr,
        )
        sys.exit(1)
    client_id = (
        resolve_secret(pre_args.oauth_client_id) if pre_args.oauth_client_id else None
    )
    client_secret = (
        resolve_secret(pre_args.oauth_client_secret)
        if pre_args.oauth_client_secret
        else None
    )
    flow = getattr(pre_args, "oauth_flow", "auto")
    if flow == "client_credentials" and not (client_id and client_secret):
        print(
            "Error: --oauth-flow=client_credentials requires both "
            "--oauth-client-id and --oauth-client-secret",
            file=sys.stderr,
        )
        sys.exit(1)
    return build_oauth_provider(
        server_url,
        client_id=client_id,
        client_secret=client_secret,
        client_name=getattr(pre_args, "oauth_client_name", "mcp2cli"),
        scope=pre_args.oauth_scope,
        redirect_uri=pre_args.oauth_redirect_uri,
        flow=flow,
    )


def _handle_session_operations(
    pre_args,
    auth_headers: list[tuple[str, str]],
    env_vars: dict[str, str],
    remaining: list[str],
    search_pattern: str | None,
) -> bool:
    """Handle --session-list, --session-stop, --session-start, --session.

    Returns True if a session operation was handled (caller should return).
    """
    if pre_args.session_list:
        sessions = session_list()
        if not sessions:
            print("No active sessions.")
        else:
            for s in sessions:
                status = "alive" if s["alive"] else "dead"
                print(
                    f"  {s['name']:<20} {s['transport']:<8} {status}  PID={s.get('pid', '?')}"
                )
        return True

    if pre_args.session_stop:
        session_stop(pre_args.session_stop)
        print(f"Session '{pre_args.session_stop}' stopped.")
        return True

    if pre_args.session_start:
        if not (pre_args.mcp or pre_args.mcp_stdio):
            print(
                "Error: --session-start requires --mcp or --mcp-stdio", file=sys.stderr
            )
            sys.exit(1)
        source = pre_args.mcp or pre_args.mcp_stdio
        is_stdio = pre_args.mcp_stdio is not None
        session_start(
            pre_args.session_start,
            source,
            is_stdio,
            auth_headers,
            env_vars,
            transport=pre_args.transport,
        )
        return True

    if not pre_args.session:
        return False

    # --- Session client mode ---
    sess_name = pre_args.session

    if pre_args.list_resources:
        result = _session_request(sess_name, "list_resources")
        output_result(
            result, pretty=pre_args.pretty, raw=pre_args.raw, toon=pre_args.toon,
            jq_expr=pre_args.jq, head=pre_args.head,
        )
        return True
    if pre_args.list_resource_templates:
        result = _session_request(sess_name, "list_resource_templates")
        output_result(
            result, pretty=pre_args.pretty, raw=pre_args.raw, toon=pre_args.toon,
            jq_expr=pre_args.jq, head=pre_args.head,
        )
        return True
    if pre_args.read_resource:
        result = _session_request(
            sess_name, "read_resource", {"uri": pre_args.read_resource}
        )
        output_result(
            result, pretty=pre_args.pretty, raw=pre_args.raw, toon=pre_args.toon,
            jq_expr=pre_args.jq, head=pre_args.head,
        )
        return True
    if pre_args.list_prompts:
        result = _session_request(sess_name, "list_prompts")
        output_result(
            result, pretty=pre_args.pretty, raw=pre_args.raw, toon=pre_args.toon,
            jq_expr=pre_args.jq, head=pre_args.head,
        )
        return True
    if pre_args.get_prompt:
        p_args = {}
        for pa in pre_args.prompt_arg:
            if "=" in pa:
                k, v = pa.split("=", 1)
                p_args[k] = v
        result = _session_request(
            sess_name,
            "get_prompt",
            {"name": pre_args.get_prompt, "arguments": p_args},
        )
        output_result(
            result, pretty=pre_args.pretty, raw=pre_args.raw, toon=pre_args.toon,
            jq_expr=pre_args.jq, head=pre_args.head,
        )
        return True
    if pre_args.list_commands:
        result = _session_request(sess_name, "list_tools")
        commands = extract_mcp_commands(result)
        if search_pattern:
            commands = _filter_commands(commands, search_pattern)
            if not commands:
                print(f"\nNo tools matching '{search_pattern}'.")
                return True
            print(f"\nTools matching '{search_pattern}':")
        else:
            print("\nAvailable tools:")
        list_mcp_commands(commands, verbose=pre_args.verbose)
        return True

    # Tool call via session
    if not remaining:
        result = _session_request(sess_name, "list_tools")
        commands = extract_mcp_commands(result)
        print("Available tools:")
        list_mcp_commands(commands, verbose=pre_args.verbose)
        print("\nUse --list for the same output, or provide a subcommand.")
        return True

    tools = _session_request(sess_name, "list_tools")
    commands = extract_mcp_commands(tools)
    pre_for_session = argparse.ArgumentParser(add_help=False)
    parser = build_argparse(commands, pre_for_session)
    args = parser.parse_args(remaining)

    if not hasattr(args, "_cmd"):
        parser.print_help()
        sys.exit(1)

    cmd: CommandDef = args._cmd
    if getattr(args, "stdin", False):
        arguments = read_stdin_json(f"session {sess_name} tool arguments")
    else:
        arguments = {}
        for p in cmd.params:
            val = getattr(args, p.name.replace("-", "_"), None)
            if val is not None:
                arguments[p.original_name] = coerce_value(val, p.schema)

    result = _session_request(
        sess_name, "call_tool", {"name": cmd.tool_name, "arguments": arguments}
    )
    output_result(
        result, pretty=pre_args.pretty, raw=pre_args.raw, toon=pre_args.toon
    )
    return True


def _resolve_resource_prompt_actions(pre_args):
    """Determine resource/prompt actions from parsed args.

    Returns (resource_action, resource_uri, prompt_action, prompt_name, prompt_arguments).
    """
    resource_action = None
    resource_uri = None
    prompt_action = None
    prompt_name = None
    prompt_arguments = None

    if pre_args.list_resources:
        resource_action = "list"
    elif pre_args.list_resource_templates:
        resource_action = "templates"
    elif pre_args.read_resource:
        resource_action = "read"
        resource_uri = pre_args.read_resource

    if pre_args.list_prompts:
        prompt_action = "list"
    elif pre_args.get_prompt:
        prompt_action = "get"
        prompt_name = pre_args.get_prompt
        prompt_arguments = {}
        for pa in pre_args.prompt_arg:
            if "=" in pa:
                k, v = pa.split("=", 1)
                prompt_arguments[k] = v

    return resource_action, resource_uri, prompt_action, prompt_name, prompt_arguments


def _handle_openapi_mode(
    pre_args,
    pre: argparse.ArgumentParser,
    remaining: list[str],
    auth_headers: list[tuple[str, str]],
    search_pattern: str | None,
    bake_config: BakeConfig | None,
    oauth_provider: "httpx.Auth | None" = None,
) -> None:
    """Execute OpenAPI mode: load spec, build parser, execute."""
    src_hash = _source_hash_for(pre_args.spec)
    spec = load_openapi_spec(
        pre_args.spec,
        auth_headers,
        pre_args.cache_key,
        pre_args.cache_ttl,
        pre_args.refresh,
        oauth_provider=oauth_provider,
    )
    commands = extract_openapi_commands(spec)
    if bake_config:
        commands = filter_commands(
            commands, bake_config.include, bake_config.exclude, bake_config.methods,
        )

    list_kwargs = dict(
        verbose=pre_args.verbose, compact=pre_args.compact,
        source_hash=src_hash, sort_mode=pre_args.sort_mode, top=pre_args.top,
    )

    if pre_args.list_commands:
        if search_pattern:
            commands = _filter_commands(commands, search_pattern)
            if not commands:
                print(f"\nNo tools matching '{search_pattern}'.")
                return
            if not pre_args.compact:
                print(f"\nTools matching '{search_pattern}':")
        list_openapi_commands(commands, **list_kwargs)
        return

    if not remaining:
        pre.print_help()
        print("\nUse --list to see all available commands.")
        sys.exit(1)

    # Determine base URL
    base_url = pre_args.base_url
    if not base_url:
        servers = spec.get("servers", [])
        if servers and isinstance(servers[0], dict):
            base_url = servers[0].get("url", "")
        # If base_url is relative or empty, derive from spec source
        if not base_url or not base_url.startswith("http"):
            if pre_args.spec and pre_args.spec.startswith("http"):
                from urllib.parse import urlparse

                parsed = urlparse(pre_args.spec)
                origin = f"{parsed.scheme}://{parsed.netloc}"
                if base_url and not base_url.startswith("http"):
                    base_url = origin + base_url
                else:
                    base_url = origin
            elif not base_url:
                print(
                    "Error: cannot determine base URL. Use --base-url.", file=sys.stderr
                )
                sys.exit(1)

    parser = build_argparse(commands, pre)
    args = parser.parse_args(remaining)

    if not hasattr(args, "_cmd"):
        parser.print_help()
        sys.exit(1)

    cmd: CommandDef = args._cmd
    execute_openapi(
        args, cmd, base_url, auth_headers,
        pre_args.pretty, pre_args.raw, toon=pre_args.toon,
        oauth_provider=oauth_provider,
        jq_expr=pre_args.jq, head=pre_args.head,
    )

    # Record usage after successful execution
    record_usage(src_hash, cmd.tool_name or cmd.graphql_field_name or cmd.name)


def _main_impl(argv: list[str], bake_config: BakeConfig | None = None):
    pre = _build_main_parser()

    # Split argv at the subcommand boundary so that tool parameters whose
    # names collide with global options (e.g. --env, --refresh) are not
    # silently consumed by the pre-parser.  See GH #15.
    global_argv, tool_argv = _split_at_subcommand(argv, pre)
    pre_args, leftover = pre.parse_known_args(global_argv)
    remaining = leftover + tool_argv

    # Validate mutually exclusive output flags
    if pre_args.jq and pre_args.toon:
        print("Error: --jq and --toon are mutually exclusive.", file=sys.stderr)
        sys.exit(1)

    # --search implies --list
    search_pattern = pre_args.search_pattern
    if search_pattern:
        pre_args.list_commands = True

    # Parse auth headers (values support env: and file: prefixes)
    auth_headers = _parse_kv_list(
        pre_args.auth_header, ":", "auth header", resolve_values=True
    )
    env_vars = dict(_parse_kv_list(pre_args.env, "=", "env"))

    _validate_source_modes(pre_args, pre, remaining)
    oauth_provider = _setup_oauth(pre_args)

    if _handle_session_operations(
        pre_args, auth_headers, env_vars, remaining, search_pattern
    ):
        return

    resource_action, resource_uri, prompt_action, prompt_name, prompt_arguments = (
        _resolve_resource_prompt_actions(pre_args)
    )

    # --- GraphQL mode ---
    if pre_args.graphql:
        handle_graphql(
            pre_args.graphql,
            auth_headers,
            remaining,
            pre_args.list_commands,
            pre_args.pretty,
            pre_args.raw,
            pre_args.cache_key,
            pre_args.cache_ttl,
            pre_args.refresh,
            toon=pre_args.toon,
            fields_override=pre_args.fields,
            oauth_provider=oauth_provider,
            jq_expr=pre_args.jq,
            head=pre_args.head,
            verbose=pre_args.verbose,
            sort_mode=pre_args.sort_mode,
            top=pre_args.top,
            compact=pre_args.compact,
        )
        return

    # --- MCP modes ---
    if pre_args.mcp or pre_args.mcp_stdio:
        source = pre_args.mcp or pre_args.mcp_stdio
        is_stdio = pre_args.mcp_stdio is not None
        handle_mcp(
            source,
            is_stdio,
            auth_headers,
            env_vars,
            remaining,
            pre_args.list_commands,
            pre_args.pretty,
            pre_args.raw,
            pre_args.cache_key,
            pre_args.cache_ttl,
            pre_args.refresh,
            toon=pre_args.toon,
            transport=pre_args.transport,
            oauth_provider=oauth_provider,
            resource_action=resource_action,
            resource_uri=resource_uri,
            prompt_action=prompt_action,
            prompt_name=prompt_name,
            prompt_arguments=prompt_arguments,
            search_pattern=search_pattern,
            bake_config=bake_config,
            jq_expr=pre_args.jq,
            head=pre_args.head,
            verbose=pre_args.verbose,
            sort_mode=pre_args.sort_mode,
            top=pre_args.top,
            compact=pre_args.compact,
        )
        return

    # --- OpenAPI mode ---
    _handle_openapi_mode(
        pre_args, pre, remaining, auth_headers, search_pattern, bake_config,
        oauth_provider=oauth_provider,
    )


if __name__ == "__main__":
    main()
