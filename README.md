<p align="center">
  <img src="https://raw.githubusercontent.com/knowsuchagency/mcp2cli/main/assets/hero.png" alt="mcp2cli — one CLI for every API" width="700">
</p>

<h1 align="center">mcp2cli</h1>

<p align="center">
  Turn any MCP server, OpenAPI spec, or GraphQL endpoint into a CLI — at runtime, with zero codegen.<br>
  <strong>Save 96–99% of the tokens wasted on tool schemas every turn.</strong><br><br>
  <a href="https://www.orangecountyai.com/blog/mcp2cli-one-cli-for-every-api-zero-wasted-tokens"><strong>Read the full writeup →</strong></a>
</p>

## Install

```bash
# Run directly without installing
uvx mcp2cli --help

# Or install globally
uv tool install mcp2cli
```

## AI Agent Skill

mcp2cli ships with an installable [skill](https://skills.sh) that teaches AI coding agents (Claude Code, Cursor, Codex) how to use it. Once installed, your agent can discover and call any MCP server or OpenAPI endpoint — and even generate new skills from APIs.

```bash
npx skills add knowsuchagency/mcp2cli --skill mcp2cli
```

After installing, try prompts like:
- `mcp2cli --mcp https://mcp.example.com/sse` — interact with an MCP server
- `mcp2cli create a skill for https://api.example.com/openapi.json` — generate a skill from an API

## Usage

### MCP HTTP/SSE mode

```bash
# Connect to an MCP server over HTTP
mcp2cli --mcp https://mcp.example.com/sse --list

# Call a tool
mcp2cli --mcp https://mcp.example.com/sse search --query "test"

# With auth header
mcp2cli --mcp https://mcp.example.com/sse --auth-header "x-api-key:sk-..." \
  query --sql "SELECT 1"

# Force a specific transport (skip streamable HTTP fallback dance)
mcp2cli --mcp https://mcp.example.com/sse --transport sse --list

# Search tools by name or description (case-insensitive substring match)
mcp2cli --mcp https://mcp.example.com/sse --search "task"
```

`--search` implies `--list` and works across all modes (`--mcp`, `--spec`, `--graphql`, `--mcp-stdio`).

### OAuth authentication

APIs that require OAuth are supported out of the box — across MCP, OpenAPI, and GraphQL modes.
mcp2cli handles token acquisition, caching, and refresh automatically.

```bash
# Authorization code + PKCE flow (opens browser for login)
mcp2cli --mcp https://mcp.example.com/sse --oauth --list
mcp2cli --spec https://api.example.com/openapi.json --oauth --list
mcp2cli --graphql https://api.example.com/graphql --oauth --list

# Client credentials flow (machine-to-machine, no browser)
mcp2cli --spec https://api.example.com/openapi.json \
  --oauth-client-id "my-client-id" \
  --oauth-client-secret "my-secret" \
  list-pets

# With specific scopes
mcp2cli --graphql https://api.example.com/graphql --oauth --oauth-scope "read write" users

# Local spec file — use --base-url for OAuth discovery
mcp2cli --spec ./openapi.json --base-url https://api.example.com --oauth --list
```

Tokens are persisted in `~/.cache/mcp2cli/oauth/` so subsequent calls reuse existing tokens
and refresh automatically when they expire.

#### Headless hosts — no browser on the machine running mcp2cli

The default authorization-code flow starts a callback server on `127.0.0.1`, which only
works when the browser runs on the same machine. On a VPS over SSH or in a container,
add `--oauth-manual-callback`: mcp2cli prints the authorization URL instead of opening a
browser, and reads the redirect back from stdin.

```bash
mcp2cli --mcp https://mcp.linear.app/mcp --oauth --oauth-manual-callback --list
```

Open the printed URL in a browser on any machine, authorize, then paste the URL you land
on. That page will fail to load — nothing is listening on the loopback port — which is
expected; only its address matters, because it carries the `code` and `state` parameters.
PKCE and state verification are unchanged, so paste the URL unmodified.

### Secrets from environment or files

Sensitive values (`--auth-header` values, `--oauth-client-id`, `--oauth-client-secret`) support
`env:` and `file:` prefixes to avoid passing secrets as CLI arguments (which are visible in
process listings):

```bash
# Read from environment variable
mcp2cli --mcp https://mcp.example.com/sse \
  --auth-header "Authorization:env:MY_API_TOKEN" \
  --list

# Read from file
mcp2cli --mcp https://mcp.example.com/sse \
  --oauth-client-secret "file:/run/secrets/client_secret" \
  --oauth-client-id "my-client-id" \
  --list

# Works with secret managers that inject env vars
fnox exec -- mcp2cli --mcp https://mcp.example.com/sse \
  --oauth-client-id "env:OAUTH_CLIENT_ID" \
  --oauth-client-secret "env:OAUTH_CLIENT_SECRET" \
  --list
```

### MCP stdio mode

```bash
# List tools from an MCP server
mcp2cli --mcp-stdio "npx @modelcontextprotocol/server-filesystem /tmp" --list

# Call a tool
mcp2cli --mcp-stdio "npx @modelcontextprotocol/server-filesystem /tmp" \
  read-file --path /tmp/hello.txt

# Pass environment variables to the server process
mcp2cli --mcp-stdio "node server.js" --env API_KEY=sk-... --env DEBUG=1 \
  search --query "test"
```

### MCP roots and completion

Expose one or more filesystem roots when a server scopes operations to a
workspace. Paths are converted to `file://` URIs; explicit roots must also use
the `file://` scheme.

```bash
mcp2cli --mcp-stdio "npx @modelcontextprotocol/server-filesystem /tmp" \
  --root "$PWD" --root file:///var/shared --list
```

Request prompt-argument or resource-template completions with
`REF:ARG=PREFIX`:

```bash
mcp2cli --mcp https://example.com/mcp \
  --complete "greeting:name=San"
mcp2cli --mcp https://example.com/mcp \
  --complete "file:///docs/{topic}:topic=api"
```

Both options work when starting a persistent session; roots are retained by
the session daemon and completion requests can be sent through `--session`.

### OpenAPI mode

```bash
# List all commands from a remote spec
mcp2cli --spec https://petstore3.swagger.io/api/v3/openapi.json --list

# Call an endpoint
mcp2cli --spec ./openapi.json --base-url https://api.example.com list-pets --status available

# With auth
mcp2cli --spec ./spec.json --auth-header "Authorization:Bearer tok_..." create-item --name "Test"

# POST with JSON body from stdin
echo '{"name": "Fido", "tag": "dog"}' | mcp2cli --spec ./spec.json create-pet --stdin

# Local YAML spec
mcp2cli --spec ./api.yaml --base-url http://localhost:8000 --list
```

### GraphQL mode

```bash
# List all queries and mutations from a GraphQL endpoint
mcp2cli --graphql https://api.example.com/graphql --list

# Call a query
mcp2cli --graphql https://api.example.com/graphql users --limit 10

# Call a mutation
mcp2cli --graphql https://api.example.com/graphql create-user --name "Alice" --email "alice@example.com"

# Override auto-generated selection set fields
mcp2cli --graphql https://api.example.com/graphql users --fields "id name email"

# With auth
mcp2cli --graphql https://api.example.com/graphql --auth-header "Authorization:Bearer tok_..." users
```

mcp2cli introspects the endpoint, discovers queries and mutations, auto-generates selection sets, and constructs parameterized queries with proper variable declarations. No SDL parsing, no code generation — just point and run.

### Bake mode — save connection settings

Tired of repeating `--spec`/`--mcp`/`--mcp-stdio` plus auth flags on every invocation? Bake them into a named configuration:

```bash
# Create a baked tool from an OpenAPI spec
mcp2cli bake create petstore --spec https://api.example.com/spec.json \
  --exclude "delete-*,update-*" --methods GET,POST --cache-ttl 7200

# Create a baked tool from an MCP stdio server
mcp2cli bake create myfs --mcp-stdio "npx -y @modelcontextprotocol/server-filesystem /tmp" \
  --include "search-*,list-*" --exclude "list-allowed-*"

# Use a baked tool with @ prefix — no connection flags needed
mcp2cli @petstore --list
mcp2cli @petstore list-pets --limit 10
mcp2cli @myfs search-files --path /tmp --pattern "*.md"

# Manage baked tools
mcp2cli bake list                         # show all baked tools
mcp2cli bake show petstore                # show config (secrets masked)
mcp2cli bake update petstore --cache-ttl 3600
mcp2cli bake remove petstore
mcp2cli bake install petstore             # creates ~/.local/bin/petstore wrapper
mcp2cli bake install petstore --dir ./scripts/  # install wrapper to custom directory
```

Filtering options:
- `--include` — comma-separated glob patterns to whitelist tools (e.g. `"list-*,get-*"`)
- `--exclude` — comma-separated glob patterns to blacklist tools (e.g. `"delete-*"`)
- `--methods` — comma-separated HTTP methods to allow (e.g. `"GET,POST"`, OpenAPI only)

Configs are stored in `~/.config/mcp2cli/baked.json`. Override with `MCP2CLI_CONFIG_DIR`.

### Usage-aware tool ranking

mcp2cli tracks tool invocations locally and uses that data to rank `--list` output, reducing token costs for LLM agents working with large servers.

```bash
# Default --list: ~1,400 tokens for 96 tools
mcp2cli @myapi --list

# Top 10 most-used tools, names only: ~20 tokens
mcp2cli @myapi --list --top 10 --compact

# Sort by most recently used
mcp2cli @myapi --list --sort recent

# Alphabetical sort
mcp2cli @myapi --list --sort alpha
```

When usage data exists for a source, `--list` defaults to sorting by call frequency. Otherwise insertion order is preserved. Usage data is stored in `~/.cache/mcp2cli/usage.json`.

### JSON output

`--json` forces **valid JSON on stdout for every command**, in every mode. It is the
machine-readable counterpart to the human-formatted default output, designed for LLM
agents and scripts that need to parse results reliably.

```bash
# --list emits a JSON array of command objects (name, description, parameters, ...)
mcp2cli --mcp https://mcp.example.com/sse --list --json
mcp2cli --spec ./openapi.json --list --json
mcp2cli --graphql https://api.example.com/graphql --list --json

# --list --json --compact emits a JSON array of names only
mcp2cli --mcp https://mcp.example.com/sse --list --json --compact

# MCP tool calls emit the FULL CallToolResult envelope — including
# structuredContent and isError, not just the flattened text. This surfaces the
# machine-readable result that modern MCP tools put in structuredContent.
mcp2cli --mcp https://mcp.example.com/sse --json search --query "test"
# { "content": [...], "structuredContent": {...}, "isError": false }

# OpenAPI / GraphQL calls emit the response as JSON (non-JSON bodies become a JSON string)
mcp2cli --spec ./openapi.json --json list-pets
mcp2cli --graphql https://api.example.com/graphql --json users
```

`--json` takes precedence over `--raw` and `--toon` (both of which can produce
non-JSON), so it always wins — that is what makes it a reliable "force JSON" switch.
Indentation follows the usual rule: pretty on a TTY or with `--pretty`, compact when piped.

### Output control

```bash
# Pretty-print JSON (also auto-enabled for TTY)
mcp2cli --spec ./spec.json --pretty list-pets

# Raw response body (no JSON parsing)
mcp2cli --spec ./spec.json --raw get-data

# Truncate large responses to first N records
mcp2cli --spec ./spec.json list-records --head 5

# Pipe-friendly (compact JSON when not a TTY)
mcp2cli --spec ./spec.json list-pets | jq '.[] | .name'

# TOON output — token-efficient encoding for LLM consumption
# Best for large uniform arrays (40-60% fewer tokens than JSON)
mcp2cli --mcp https://mcp.example.com/sse --toon list-tags
```

### Caching

Specs and MCP tool lists are cached in `~/.cache/mcp2cli/` with a 1-hour TTL by default.

```bash
# Force refresh
mcp2cli --spec https://api.example.com/spec.json --refresh --list

# Custom TTL (seconds)
mcp2cli --spec https://api.example.com/spec.json --cache-ttl 86400 --list

# Custom cache key
mcp2cli --spec https://api.example.com/spec.json --cache-key my-api --list

# Override cache directory
MCP2CLI_CACHE_DIR=/tmp/my-cache mcp2cli --spec ./spec.json --list
```

Local file specs are never cached.

## CLI reference

```
mcp2cli [global options] <subcommand> [command options]

Source (mutually exclusive, one required):
  --spec URL|FILE       OpenAPI spec (JSON or YAML, local or remote)
  --mcp URL             MCP server URL (HTTP/SSE)
  --mcp-stdio CMD       MCP server command (stdio transport)
  --graphql URL         GraphQL endpoint URL

Options:
  --auth-header K:V       HTTP header (repeatable, value supports env:/file: prefixes)
  --base-url URL          Override base URL from spec
  --transport TYPE        MCP HTTP transport: auto|sse|streamable (default: auto)
  --env KEY=VALUE         Env var for MCP stdio server (repeatable)
  --root PATH|FILE_URI    Expose a filesystem root to an MCP server (repeatable)
  --complete SPEC         Complete an MCP prompt or resource-template argument
  --oauth                 Enable OAuth (authorization code + PKCE flow)
  --oauth-client-id ID    OAuth client ID (supports env:/file: prefixes)
  --oauth-client-secret S OAuth client secret (supports env:/file: prefixes)
  --oauth-scope SCOPE     OAuth scope(s) to request
  --oauth-manual-callback Print the auth URL and read the redirect from stdin
                          (for hosts with no reachable browser)
  --cache-key KEY         Custom cache key
  --cache-ttl SECONDS     Cache TTL (default: 3600)
  --refresh               Bypass cache
  --list                  List available subcommands
  --search PATTERN        Search tools by name or description (implies --list)
  --sort MODE             Sort --list output: usage|recent|alpha|default
  --top N                 Show only the top N tools in --list output
  --compact               Space-separated tool names only, no descriptions
  --verbose               Show full tool descriptions (unwrapped)
  --fields FIELDS         Override GraphQL selection set (e.g. "id name email")
  --pretty                Pretty-print JSON output
  --raw                   Print raw response body
  --json                  Force valid JSON output for every command (--list and tool
                          calls). MCP calls emit the full result envelope including
                          structuredContent. Takes precedence over --raw and --toon.
  --toon                  Encode output as TOON (token-efficient for LLMs)
  --head N                Limit output to first N records (arrays)
  --version               Show version

Bake mode:
  bake create NAME [opts]   Save connection settings as a named tool
  bake list                 List all baked tools
  bake show NAME            Show config (secrets masked)
  bake update NAME [opts]   Update a baked tool
  bake remove NAME          Delete a baked tool
  bake install NAME         Create ~/.local/bin wrapper script
  @NAME [args]              Run a baked tool (e.g. mcp2cli @petstore --list)
```

Subcommands and their flags are generated dynamically from the spec or MCP server tool definitions. Run `<subcommand> --help` for details.

> For token savings analysis, architecture details, and comparison to Anthropic's Tool Search, see the **[full writeup on the OCAI blog](https://www.orangecountyai.com/blog/mcp2cli-one-cli-for-every-api-zero-wasted-tokens)**.

## Development

```bash
# Install with test + MCP deps
uv sync --extra test

# Run tests
uv run pytest tests/ -v

# Run just the token savings tests
uv run pytest tests/test_token_savings.py -v -s
```

### MCP SDK compatibility

mcp2cli works with **both major versions** of the MCP Python SDK (`mcp>=1.26,<3`),
so it never forces a resolver conflict with other tools in the same environment.
CI runs the suite against the declared floor, the latest 1.x, and the latest 2.x.

The two majors differ in ways that matter to a client — v2 renamed model fields
to snake_case, replaced `streamablehttp_client`, moved to `httpx2`, and dropped
the session-id element from the transport tuple. Those differences are confined
to a handful of helpers (`_mcp_attr`, `_mcp_dump`, `_streamable_streams`,
`_list_tools_page`, `_resource_uri`, `_authorization_code_result`); the rest of
the codebase is version-agnostic. The test fixtures speak the JSON-RPC wire
protocol directly and import no SDK, so they hold across majors.

---

## License

[MIT](LICENSE)
