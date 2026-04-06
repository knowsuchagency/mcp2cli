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
mcp2cli bake create mygit --mcp-stdio "npx @mcp/github" \
  --include "search-*,list-*" --exclude "delete-*"

# Use a baked tool with @ prefix — no connection flags needed
mcp2cli @petstore --list
mcp2cli @petstore list-pets --limit 10
mcp2cli @mygit search-repos --query "rust"

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

### Output control

```bash
# Pretty-print JSON (also auto-enabled for TTY)
mcp2cli --spec ./spec.json --pretty list-pets

# Raw response body (no JSON parsing)
mcp2cli --spec ./spec.json --raw get-data

# Filter JSON with jq (preferred over Python for JSON processing)
mcp2cli --spec ./spec.json list-pets --jq '.[].name'
mcp2cli --spec ./spec.json list-pets --jq '[.[] | select(.status == "available")] | length'

# Truncate large responses to first N records
mcp2cli --spec ./spec.json list-records --head 5
mcp2cli --spec ./spec.json list-records --head 3 --jq '.'  # preview then filter

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
  --oauth                 Enable OAuth (authorization code + PKCE flow)
  --oauth-client-id ID    OAuth client ID (supports env:/file: prefixes)
  --oauth-client-secret S OAuth client secret (supports env:/file: prefixes)
  --oauth-scope SCOPE     OAuth scope(s) to request
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
  --toon                  Encode output as TOON (token-efficient for LLMs)
  --jq EXPR               Filter JSON output through jq expression
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

# Run tests (96 tests covering OpenAPI, MCP stdio, MCP HTTP, caching, and token savings)
uv run pytest tests/ -v

# Run just the token savings tests
uv run pytest tests/test_token_savings.py -v -s
```

---

## License

[MIT](LICENSE)

---

<sub>mcp2cli builds on ideas from [CLIHub](https://kanyilmaz.me/2026/02/23/cli-vs-mcp.html) by Kagan Yilmaz (CLI-based tool access for token efficiency)</sub>
