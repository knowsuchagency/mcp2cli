"""Main entry point and argument parsing."""

from __future__ import annotations

import argparse
import json
import sys

from mcp2cli._types import CommandDef, BakeConfig, build_argparse, filter_commands
from mcp2cli._helpers import (
    resolve_secret,
    _parse_kv_list,
    read_stdin_json,
    coerce_value,
    output_result,
    list_openapi_commands,
    list_mcp_commands,
    _filter_commands,
)
from mcp2cli._cache import DEFAULT_CACHE_TTL, cache_key_for
from mcp2cli._oauth import build_oauth_provider
from mcp2cli._openapi import load_openapi_spec, extract_openapi_commands, execute_openapi
from mcp2cli._graphql import handle_graphql
from mcp2cli._mcp import extract_mcp_commands, handle_mcp
from mcp2cli._sessions import session_list, session_stop, session_start, _session_request
from mcp2cli._bake import _handle_bake, _run_baked


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
    # Lazy import to avoid circular dependency at module load time
    from mcp2cli import __version__

    pre = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
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
        "--oauth-scope",
        default=None,
        help="OAuth scope(s) to request",
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

    if pre_args.oauth_client_id and not pre_args.oauth_client_secret:
        print(
            "Error: --oauth-client-secret is required with --oauth-client-id",
            file=sys.stderr,
        )
        sys.exit(1)
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
    return build_oauth_provider(
        server_url,
        client_id=client_id,
        client_secret=client_secret,
        scope=pre_args.oauth_scope,
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
            result, pretty=pre_args.pretty, raw=pre_args.raw, toon=pre_args.toon
        )
        return True
    if pre_args.list_resource_templates:
        result = _session_request(sess_name, "list_resource_templates")
        output_result(
            result, pretty=pre_args.pretty, raw=pre_args.raw, toon=pre_args.toon
        )
        return True
    if pre_args.read_resource:
        result = _session_request(
            sess_name, "read_resource", {"uri": pre_args.read_resource}
        )
        output_result(
            result, pretty=pre_args.pretty, raw=pre_args.raw, toon=pre_args.toon
        )
        return True
    if pre_args.list_prompts:
        result = _session_request(sess_name, "list_prompts")
        output_result(
            result, pretty=pre_args.pretty, raw=pre_args.raw, toon=pre_args.toon
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
            result, pretty=pre_args.pretty, raw=pre_args.raw, toon=pre_args.toon
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
        list_mcp_commands(commands)
        return True

    # Tool call via session
    if not remaining:
        result = _session_request(sess_name, "list_tools")
        commands = extract_mcp_commands(result)
        print("Available tools:")
        list_mcp_commands(commands)
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

    if pre_args.list_commands:
        if search_pattern:
            commands = _filter_commands(commands, search_pattern)
            if not commands:
                print(f"\nNo tools matching '{search_pattern}'.")
                return
            print(f"\nTools matching '{search_pattern}':")
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
    )


def _main_impl(argv: list[str], bake_config: BakeConfig | None = None):
    pre = _build_main_parser()

    # Split argv at the subcommand boundary so that tool parameters whose
    # names collide with global options (e.g. --env, --refresh) are not
    # silently consumed by the pre-parser.  See GH #15.
    global_argv, tool_argv = _split_at_subcommand(argv, pre)
    pre_args, leftover = pre.parse_known_args(global_argv)
    remaining = leftover + tool_argv

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

    use_oauth = (
        pre_args.oauth or pre_args.oauth_client_id or pre_args.oauth_client_secret
    )
    if pre_args.session_start and pre_args.mcp and use_oauth:
        print(
            "Error: OAuth is not yet supported with --session-start over HTTP transports",
            file=sys.stderr,
        )
        sys.exit(1)

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
        )
        return

    # --- OpenAPI mode ---
    _handle_openapi_mode(
        pre_args, pre, remaining, auth_headers, search_pattern, bake_config,
        oauth_provider=oauth_provider,
    )
