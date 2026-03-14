---
name: mcp2cli
description: Turn any MCP server, OpenAPI spec, or GraphQL endpoint into a CLI. Use this skill when the user wants to interact with an MCP server, OpenAPI/REST API, or GraphQL API via command line, discover available tools/endpoints, call API operations, or generate a new skill from an API. Triggers include "mcp2cli", "call this MCP server", "use this API", "list tools from", "create a skill for this API", "graphql", or any task involving MCP tool invocation, OpenAPI endpoint calls, or GraphQL queries without writing code.
---

# mcp2cli

Turn any MCP server, OpenAPI spec, or GraphQL endpoint into a CLI at runtime. No codegen.

## Install

```bash
# Run directly (no install needed)
uvx mcp2cli --help

# Or install
pip install mcp2cli
```

## Core Workflow

1. **Connect** to a source (MCP server, OpenAPI spec, or GraphQL endpoint)
2. **Discover** available commands with `--list` (or filter with `--search`)
3. **Inspect** a specific command with `<command> --help`
4. **Execute** the command with flags

```bash
# MCP over HTTP
mcp2cli --mcp https://mcp.example.com/sse --list
mcp2cli --mcp https://mcp.example.com/sse create-task --help
mcp2cli --mcp https://mcp.example.com/sse create-task --title "Fix bug"

# MCP over stdio
mcp2cli --mcp-stdio "npx @modelcontextprotocol/server-filesystem /tmp" --list
mcp2cli --mcp-stdio "npx @modelcontextprotocol/server-filesystem /tmp" read-file --path /tmp/hello.txt

# OpenAPI spec (remote or local, JSON or YAML)
mcp2cli --spec https://petstore3.swagger.io/api/v3/openapi.json --list
mcp2cli --spec ./openapi.json --base-url https://api.example.com list-pets --status available

# GraphQL endpoint
mcp2cli --graphql https://api.example.com/graphql --list
mcp2cli --graphql https://api.example.com/graphql users --limit 10
mcp2cli --graphql https://api.example.com/graphql create-user --name "Alice"
```

## CLI Reference

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
  --env KEY=VALUE         Env var for stdio server process (repeatable)
  --oauth                 Enable OAuth (authorization code + PKCE flow)
  --oauth-client-id ID    OAuth client ID (supports env:/file: prefixes)
  --oauth-client-secret S OAuth client secret (supports env:/file: prefixes)
  --oauth-scope SCOPE     OAuth scope(s) to request
  --cache-key KEY         Custom cache key
  --cache-ttl SECONDS     Cache TTL (default: 3600)
  --refresh               Bypass cache
  --list                  List available subcommands
  --search PATTERN        Search tools by name or description (implies --list)
  --fields FIELDS         Override GraphQL selection set (e.g. "id name email")
  --pretty                Pretty-print JSON output
  --raw                   Print raw response body
  --toon                  Encode output as TOON (token-efficient for LLMs)
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

Subcommands and flags are generated dynamically from the source.

## Patterns

### Authentication

```bash
# API key header (literal value)
mcp2cli --spec ./spec.json --auth-header "Authorization:Bearer tok_..." list-items

# Secret from environment variable (avoids exposing in process list)
mcp2cli --mcp https://mcp.example.com/sse \
  --auth-header "Authorization:env:API_TOKEN" \
  search --query "test"

# Secret from file
mcp2cli --mcp https://mcp.example.com/sse \
  --auth-header "x-api-key:file:/run/secrets/api_key" \
  search --query "test"
```

### OAuth authentication (MCP HTTP only)

```bash
# Authorization code + PKCE (opens browser)
mcp2cli --mcp https://mcp.example.com/sse --oauth --list

# Client credentials (machine-to-machine)
mcp2cli --mcp https://mcp.example.com/sse \
  --oauth-client-id "my-id" --oauth-client-secret "my-secret" \
  search --query "test"

# With scopes
mcp2cli --mcp https://mcp.example.com/sse --oauth --oauth-scope "read write" --list
```

Tokens are cached in `~/.cache/mcp2cli/oauth/` and refreshed automatically.

### Transport selection (MCP HTTP only)

```bash
# Default: tries streamable HTTP, falls back to SSE
mcp2cli --mcp https://mcp.example.com/sse --list

# Force SSE transport (skip streamable HTTP attempt)
mcp2cli --mcp https://mcp.example.com/sse --transport sse --list

# Force streamable HTTP (no SSE fallback)
mcp2cli --mcp https://mcp.example.com/sse --transport streamable --list
```

### GraphQL

```bash
# Discover queries and mutations
mcp2cli --graphql https://api.example.com/graphql --list

# Run a query
mcp2cli --graphql https://api.example.com/graphql users --limit 10

# Run a mutation
mcp2cli --graphql https://api.example.com/graphql create-user --name "Alice" --email "alice@example.com"

# Override auto-generated selection set
mcp2cli --graphql https://api.example.com/graphql users --fields "id name email"

# With auth
mcp2cli --graphql https://api.example.com/graphql --auth-header "Authorization:Bearer tok_..." users
```

### Tool search

```bash
# Filter tools by name or description (case-insensitive)
mcp2cli --mcp https://mcp.example.com/sse --search "task"
mcp2cli --spec ./openapi.json --search "create"
mcp2cli --graphql https://api.example.com/graphql --search "user"
```

`--search` implies `--list` — shows only matching tools.

### POST with JSON body from stdin

```bash
echo '{"name": "Fido", "tag": "dog"}' | mcp2cli --spec ./spec.json create-pet --stdin
```

### Env vars for stdio servers

```bash
mcp2cli --mcp-stdio "node server.js" --env API_KEY=sk-... --env DEBUG=1 search --query "test"
```

### Bake mode — saved configurations

Save connection settings as named configurations to avoid repeating flags:

```bash
# Create a baked tool
mcp2cli bake create petstore --spec https://api.example.com/spec.json \
  --exclude "delete-*,update-*" --methods GET,POST --cache-ttl 7200

mcp2cli bake create mygit --mcp-stdio "npx @mcp/github" \
  --include "search-*,list-*" --exclude "delete-*"

# Use with @ prefix
mcp2cli @petstore --list
mcp2cli @petstore list-pets --limit 10

# Manage
mcp2cli bake list
mcp2cli bake show petstore
mcp2cli bake update petstore --cache-ttl 3600
mcp2cli bake remove petstore
mcp2cli bake install petstore    # creates ~/.local/bin/petstore wrapper
```

Filter options: `--include` (glob whitelist), `--exclude` (glob blacklist), `--methods` (HTTP methods, OpenAPI only).

Configs stored in `~/.config/mcp2cli/baked.json` (override with `MCP2CLI_CONFIG_DIR`).

### Caching

Specs and MCP tool lists are cached in `~/.cache/mcp2cli/` (1h TTL). Local files are never cached.

```bash
mcp2cli --spec https://api.example.com/spec.json --refresh --list    # Force refresh
mcp2cli --spec https://api.example.com/spec.json --cache-ttl 86400 --list  # 24h TTL
```

### TOON output (token-efficient for LLMs)

```bash
mcp2cli --mcp https://mcp.example.com/sse --toon list-tags
```

Best for large uniform arrays — 40-60% fewer tokens than JSON.

## Generating a Skill from an API

When the user asks to create a skill from an MCP server, OpenAPI spec, or GraphQL endpoint, follow this workflow:

1. **Discover** all available commands:
   ```bash
   uvx mcp2cli --mcp https://target.example.com/sse --list
   ```

2. **Inspect** each command to understand parameters:
   ```bash
   uvx mcp2cli --mcp https://target.example.com/sse <command> --help
   ```

3. **Test** key commands to verify they work:
   ```bash
   uvx mcp2cli --mcp https://target.example.com/sse <command> --param value
   ```

4. **Create a SKILL.md** that teaches another AI agent how to use this API via mcp2cli. Include:
   - The source flag (`--mcp`, `--mcp-stdio`, `--spec`, or `--graphql`) and URL
   - Any required auth headers
   - Common workflows with example commands
   - The `--list` and `--help` discovery pattern for commands not covered

The generated skill should use mcp2cli as its execution layer — the agent runs `uvx mcp2cli ...` commands rather than making raw HTTP/MCP calls.
