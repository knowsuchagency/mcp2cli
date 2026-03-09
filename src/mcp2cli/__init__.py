"""mcp2cli — Turn any MCP server or OpenAPI spec into a CLI."""

from __future__ import annotations

__version__ = "1.3.0"

import argparse
import copy
import hashlib
import json
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from dataclasses import dataclass, field
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import os

import anyio
import httpx

CACHE_DIR = Path(os.environ.get("MCP2CLI_CACHE_DIR", Path.home() / ".cache" / "mcp2cli"))
DEFAULT_CACHE_TTL = 3600


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


@dataclass
class CommandDef:
    name: str
    description: str = ""
    params: list[ParamDef] = field(default_factory=list)
    has_body: bool = False
    # OpenAPI
    method: str | None = None
    path: str | None = None
    # MCP
    tool_name: str | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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


def coerce_value(value, schema: dict):
    if value is None:
        return None
    t = schema.get("type")
    if t in ("array", "object"):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
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


def output_result(data, *, pretty: bool = False, raw: bool = False, toon: bool = False):
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


# ---------------------------------------------------------------------------
# Caching
# ---------------------------------------------------------------------------


def cache_key_for(source: str) -> str:
    return hashlib.sha256(source.encode()).hexdigest()[:16]


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
            self.wfile.write(b"<h1>Authorization failed</h1><p>You can close this tab.</p>")
        else:
            self.wfile.write(b"<h1>Authorization successful</h1><p>You can close this tab.</p>")
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
    scope: str | None = None,
) -> "httpx.Auth":
    """Build an OAuth provider for MCP HTTP connections.

    If client_id and client_secret are provided, uses client credentials flow.
    Otherwise, uses authorization code + PKCE with a local callback server.
    """
    storage = FileTokenStorage(server_url)

    if client_id and client_secret:
        from mcp.client.auth.extensions.client_credentials import ClientCredentialsOAuthProvider

        return ClientCredentialsOAuthProvider(
            server_url=server_url,
            storage=storage,
            client_id=client_id,
            client_secret=client_secret,
            scopes=scope,
        )

    from mcp.client.auth.oauth2 import OAuthClientProvider
    from mcp.shared.auth import OAuthClientMetadata

    port = _find_free_port()
    redirect_uri = f"http://127.0.0.1:{port}/callback"

    client_metadata = OAuthClientMetadata(
        redirect_uris=[redirect_uri],
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        scope=scope,
    )

    # Reset callback handler state
    _CallbackHandler.auth_code = None
    _CallbackHandler.state = None
    _CallbackHandler.error = None
    _CallbackHandler.done = threading.Event()

    server = HTTPServer(("127.0.0.1", port), _CallbackHandler)

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
) -> dict:
    is_url = source.startswith("http://") or source.startswith("https://")

    if is_url:
        key = cache_key or cache_key_for(source)
        if not refresh:
            cached = load_cached(key, ttl)
            if cached is not None:
                return cached

        headers = dict(auth_headers)
        with httpx.Client(timeout=30) as client:
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
                slug = path.strip("/").replace("/", "-").replace("{", "").replace("}", "")
                name = f"{method}-{slug}" if slug else method

            if name in seen_names:
                seen_names[name] += 1
                name = f"{name}-{method}"
            seen_names[name] = 1

            desc = details.get("summary") or details.get("description") or f"{method.upper()} {path}"
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

            # Request body
            rb_schema = (
                details.get("requestBody", {})
                .get("content", {})
                .get("application/json", {})
                .get("schema", {})
            )
            required_fields = set(rb_schema.get("required", []))
            properties = rb_schema.get("properties", {})
            has_body = bool(properties)

            for prop_name, prop_schema in properties.items():
                py_type, suffix = schema_type_to_python(prop_schema)
                p = ParamDef(
                    name=to_kebab(prop_name),
                    original_name=prop_name,
                    python_type=py_type,
                    required=prop_name in required_fields,
                    description=(prop_schema.get("description") or prop_name) + suffix,
                    choices=prop_schema.get("enum"),
                    location="body",
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
        sub = subparsers.add_parser(cmd.name, help=cmd.description)
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
            if p.required and "action" not in kwargs and p.location not in ("body", "tool_input"):
                kwargs["required"] = True
            else:
                kwargs.setdefault("default", None)
            kwargs["help"] = p.description
            if p.choices:
                kwargs["choices"] = p.choices
            sub.add_argument(flag, **kwargs)

    return parser


# ---------------------------------------------------------------------------
# List commands
# ---------------------------------------------------------------------------


def list_openapi_commands(commands: list[CommandDef]):
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
                line += f" {cmd.description[:60]}"
            print(line)


def list_mcp_commands(commands: list[CommandDef]):
    for cmd in commands:
        desc = f"  {cmd.description[:70]}" if cmd.description else ""
        print(f"  {cmd.name:<40}{desc}")


# ---------------------------------------------------------------------------
# OpenAPI: execution
# ---------------------------------------------------------------------------


def execute_openapi(
    args: argparse.Namespace,
    cmd: CommandDef,
    base_url: str,
    auth_headers: list[tuple[str, str]],
    pretty: bool,
    raw: bool,
    toon: bool = False,
):
    path = cmd.path or ""
    # Substitute path parameters
    for p in cmd.params:
        if p.location == "path":
            val = getattr(args, p.name.replace("-", "_"), None)
            if val is not None:
                path = path.replace(f"{{{p.original_name}}}", str(val))

    url = base_url.rstrip("/") + path

    headers = dict(auth_headers)
    headers.setdefault("Content-Type", "application/json")
    query_params = {}
    body = None

    if cmd.method == "get":
        for p in cmd.params:
            if p.location == "query":
                val = getattr(args, p.name.replace("-", "_"), None)
                if val is not None:
                    query_params[p.original_name] = val
            elif p.location == "header":
                val = getattr(args, p.name.replace("-", "_"), None)
                if val is not None:
                    headers[p.original_name] = str(val)
    else:
        if getattr(args, "stdin", False):
            body = json.loads(sys.stdin.read())
        else:
            body = {}
            for p in cmd.params:
                if p.location == "header":
                    val = getattr(args, p.name.replace("-", "_"), None)
                    if val is not None:
                        headers[p.original_name] = str(val)
                    continue
                if p.location == "path":
                    continue
                val = getattr(args, p.name.replace("-", "_"), None)
                if val is not None:
                    body[p.original_name] = val
            # Also collect query params for non-GET
            for p in cmd.params:
                if p.location == "query":
                    val = getattr(args, p.name.replace("-", "_"), None)
                    if val is not None:
                        query_params[p.original_name] = val
            if not body:
                body = None

    with httpx.Client(timeout=60) as client:
        resp = client.request(
            (cmd.method or "get").upper(),
            url,
            headers=headers,
            params=query_params or None,
            json=body,
        )
        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError:
            print(f"Error {resp.status_code}: {resp.text}", file=sys.stderr)
            sys.exit(1)

    if raw:
        print(resp.text)
        return

    try:
        data = resp.json()
    except Exception:
        print(resp.text)
        return

    output_result(data, pretty=pretty, toon=toon)


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
):

    async def _run():
        from mcp import ClientSession

        headers = dict(auth_headers) if auth_headers else None

        async def _with_streamable():
            from mcp.client.streamable_http import streamablehttp_client
            async with streamablehttp_client(url, headers=headers, auth=oauth_provider) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    return await _mcp_session(
                        session, tool_name, arguments, list_mode, pretty, raw,
                        cache_key, ttl, refresh, toon=toon,
                    )

        async def _with_sse():
            from mcp.client.sse import sse_client
            async with sse_client(url, headers=headers, auth=oauth_provider) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    return await _mcp_session(
                        session, tool_name, arguments, list_mode, pretty, raw,
                        cache_key, ttl, refresh, toon=toon,
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
):

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
                    session, tool_name, arguments, list_mode, pretty, raw,
                    cache_key, ttl, refresh, toon=toon,
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
):
    if list_mode:
        result = await session.list_tools()
        tools = [
            {"name": t.name, "description": t.description or "", "inputSchema": t.inputSchema or {}}
            for t in result.tools
        ]
        commands = extract_mcp_commands(tools)
        print("\nAvailable tools:")
        list_mcp_commands(commands)
        return

    if tool_name is None:
        print("Error: no subcommand specified. Use --list to see available tools.", file=sys.stderr)
        sys.exit(1)

    result = await session.call_tool(tool_name, arguments or {})

    # Extract text content
    output_parts = []
    for content in result.content:
        if hasattr(content, "text"):
            output_parts.append(content.text)
        elif hasattr(content, "data"):
            output_parts.append(content.data)

    text = "\n".join(output_parts) if output_parts else ""
    output_result(text, pretty=pretty, raw=raw, toon=toon)


# ---------------------------------------------------------------------------
# MCP: argparse integration
# ---------------------------------------------------------------------------


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
):


    key = cache_key_override or cache_key_for(source)

    if list_mode:
        if is_stdio:
            run_mcp_stdio(source, env_vars, None, None, True, pretty, raw, key, ttl, refresh, toon=toon)
        else:
            run_mcp_http(source, auth_headers, None, None, True, pretty, raw, key, ttl, refresh, toon=toon,
                         transport=transport, oauth_provider=oauth_provider)
        return

    # We need tool list to build argparse, try cache first
    cached_tools = None
    if not refresh:
        cached_tools = load_cached(f"{key}_tools", ttl)

    if cached_tools is not None:
        tools = cached_tools
    else:
        # Must connect to get tool list
        tools = _fetch_mcp_tools(source, is_stdio, auth_headers, env_vars,
                                 transport=transport, oauth_provider=oauth_provider)
        save_cache(f"{key}_tools", tools)

    commands = extract_mcp_commands(tools)

    if not remaining:
        print("Available tools:")
        list_mcp_commands(commands)
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
        arguments = json.loads(sys.stdin.read())
    else:
        arguments = {}
        for p in cmd.params:
            val = getattr(args, p.name.replace("-", "_"), None)
            if val is not None:
                arguments[p.original_name] = val

    if is_stdio:
        run_mcp_stdio(
            source, env_vars, cmd.tool_name, arguments, False,
            pretty, raw, key, ttl, refresh, toon=toon,
        )
    else:
        run_mcp_http(
            source, auth_headers, cmd.tool_name, arguments, False,
            pretty, raw, key, ttl, refresh, toon=toon,
            transport=transport, oauth_provider=oauth_provider,
        )


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
            {"name": t.name, "description": t.description or "", "inputSchema": t.inputSchema or {}}
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
                async with streamablehttp_client(source, headers=headers, auth=oauth_provider) as (read, write, _):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        await _extract_tools(session)

            async def _via_sse():
                from mcp.client.sse import sse_client
                async with sse_client(source, headers=headers, auth=oauth_provider) as (read, write):
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


def main():
    pre = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    pre.add_argument("--spec", default=None, help="OpenAPI spec URL or file path")
    pre.add_argument("--mcp", default=None, help="MCP server URL (HTTP/SSE)")
    pre.add_argument("--mcp-stdio", default=None, help="MCP server command (stdio)")
    pre.add_argument(
        "--auth-header",
        action="append",
        default=[],
        help="HTTP header as Name:Value (repeatable)",
    )
    pre.add_argument("--base-url", default=None, help="Override base URL from spec")
    pre.add_argument("--cache-key", default=None, help="Custom cache key")
    pre.add_argument("--cache-ttl", type=int, default=DEFAULT_CACHE_TTL, help="Cache TTL in seconds")
    pre.add_argument("--refresh", action="store_true", help="Force re-fetch spec")
    pre.add_argument("--list", action="store_true", dest="list_commands", help="List available subcommands")
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
        help="OAuth client ID (enables client credentials flow when used with --oauth-client-secret)",
    )
    pre.add_argument(
        "--oauth-client-secret",
        default=None,
        help="OAuth client secret (enables client credentials flow when used with --oauth-client-id)",
    )
    pre.add_argument(
        "--oauth-scope",
        default=None,
        help="OAuth scope(s) to request",
    )
    pre.add_argument("--version", action="version", version=f"mcp2cli {__version__}")

    pre_args, remaining = pre.parse_known_args()

    # Parse auth headers
    auth_headers: list[tuple[str, str]] = []
    for h in pre_args.auth_header:
        if ":" not in h:
            print(f"Error: invalid auth header format: {h!r} (expected Name:Value)", file=sys.stderr)
            sys.exit(1)
        name, value = h.split(":", 1)
        auth_headers.append((name.strip(), value.strip()))

    # Parse env vars
    env_vars: dict[str, str] = {}
    for e in pre_args.env:
        if "=" not in e:
            print(f"Error: invalid env format: {e!r} (expected KEY=VALUE)", file=sys.stderr)
            sys.exit(1)
        k, v = e.split("=", 1)
        env_vars[k] = v

    # Validate mutual exclusivity
    modes = [pre_args.spec, pre_args.mcp, pre_args.mcp_stdio]
    active = sum(1 for m in modes if m is not None)
    if active == 0:
        pre.print_help()
        if "-h" in remaining or "--help" in remaining:
            sys.exit(0)
        print("\nError: one of --spec, --mcp, or --mcp-stdio is required.", file=sys.stderr)
        sys.exit(1)
    if active > 1:
        print("Error: --spec, --mcp, and --mcp-stdio are mutually exclusive.", file=sys.stderr)
        sys.exit(1)

    # --- Build OAuth provider if requested ---
    oauth_provider = None
    use_oauth = pre_args.oauth or pre_args.oauth_client_id or pre_args.oauth_client_secret
    if use_oauth:
        if pre_args.oauth_client_id and not pre_args.oauth_client_secret:
            print("Error: --oauth-client-secret is required with --oauth-client-id", file=sys.stderr)
            sys.exit(1)
        if pre_args.oauth_client_secret and not pre_args.oauth_client_id:
            print("Error: --oauth-client-id is required with --oauth-client-secret", file=sys.stderr)
            sys.exit(1)
        if not pre_args.mcp:
            print("Error: OAuth is only supported with --mcp (HTTP/SSE)", file=sys.stderr)
            sys.exit(1)
        oauth_provider = build_oauth_provider(
            pre_args.mcp,
            client_id=pre_args.oauth_client_id,
            client_secret=pre_args.oauth_client_secret,
            scope=pre_args.oauth_scope,
        )

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
        )
        return

    # --- OpenAPI mode ---
    spec = load_openapi_spec(
        pre_args.spec,
        auth_headers,
        pre_args.cache_key,
        pre_args.cache_ttl,
        pre_args.refresh,
    )
    commands = extract_openapi_commands(spec)

    if pre_args.list_commands:
        list_openapi_commands(commands)
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
                print("Error: cannot determine base URL. Use --base-url.", file=sys.stderr)
                sys.exit(1)

    parser = build_argparse(commands, pre)
    args = parser.parse_args(remaining)

    if not hasattr(args, "_cmd"):
        parser.print_help()
        sys.exit(1)

    cmd: CommandDef = args._cmd
    execute_openapi(args, cmd, base_url, auth_headers, pre_args.pretty, pre_args.raw, toon=pre_args.toon)


if __name__ == "__main__":
    main()
