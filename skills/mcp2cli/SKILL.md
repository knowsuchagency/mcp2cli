---
name: mcp2cli
description: Turn any MCP server or OpenAPI spec into a CLI. Use this skill when the user wants to interact with an MCP server or OpenAPI/REST API via command line, discover available tools/endpoints, call API operations, or generate a new skill from an API. Triggers include "mcp2cli", "call this MCP server", "use this API", "list tools from", "create a skill for this API", or any task involving MCP tool invocation or OpenAPI endpoint calls without writing code.
---

# mcp2cli

Turn any MCP server or OpenAPI spec into a CLI at runtime. No codegen.

## Install

```bash
# Run directly (no install needed)
uvx mcp2cli --help

# Or install
pip install mcp2cli
```

## Core Workflow

1. **Connect** to a source (MCP server or OpenAPI spec)
2. **Discover** available commands with `--list`
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
```

## CLI Reference

```
mcp2cli [global options] <subcommand> [command options]

Source (mutually exclusive, one required):
  --spec URL|FILE       OpenAPI spec (JSON or YAML, local or remote)
  --mcp URL             MCP server URL (HTTP/SSE)
  --mcp-stdio CMD       MCP server command (stdio transport)

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
  --pretty                Pretty-print JSON output
  --raw                   Print raw response body
  --toon                  Encode output as TOON (token-efficient for LLMs)
  --version               Show version
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

### POST with JSON body from stdin

```bash
echo '{"name": "Fido", "tag": "dog"}' | mcp2cli --spec ./spec.json create-pet --stdin
```

### Env vars for stdio servers

```bash
mcp2cli --mcp-stdio "node server.js" --env API_KEY=sk-... --env DEBUG=1 search --query "test"
```

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

When the user asks to create a skill from an MCP server or OpenAPI spec, follow this workflow:

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
   - The source flag (`--mcp`, `--mcp-stdio`, or `--spec`) and URL
   - Any required auth headers
   - Common workflows with example commands
   - The `--list` and `--help` discovery pattern for commands not covered

The generated skill should use mcp2cli as its execution layer — the agent runs `uvx mcp2cli ...` commands rather than making raw HTTP/MCP calls.
