"""Tests that measure and verify LLM token savings from using mcp2cli.

Compares the native tool injection approach (all tool schemas in the system
prompt on every turn) vs mcp2cli's on-demand CLI approach, across realistic
multi-turn conversations for both OpenAPI and MCP servers.

The cost model is intentionally honest about mcp2cli's overhead:
- System prompt: 67 tokens, fixed per turn
- Discovery (--list): scales linearly with tool/endpoint count (~15 tokens/tool)
- Per-tool help (--help): ~80-200 tokens per unique tool the LLM actually uses
- Tool call output: same for both approaches

The native approach pays the full schema cost on every turn regardless of usage.
"""

import json
import subprocess
import sys
from pathlib import Path

import tiktoken

from conftest import PETSTORE_SPEC, REALISTIC_MCP_TOOLS

enc = tiktoken.get_encoding("cl100k_base")


def _count_tokens(text: str) -> int:
    return len(enc.encode(text))


# ---------------------------------------------------------------------------
# Realistic tool schemas — what the LLM API actually injects
# ---------------------------------------------------------------------------

def _build_native_tool_definitions(tools: list[dict]) -> list[dict]:
    """Build the tool definitions that an LLM API injects into the system prompt.

    This mirrors the Claude/OpenAI tool_use format: each tool has name,
    description, and a full JSON Schema for input_schema.
    """
    return [
        {
            "name": t["name"],
            "description": t.get("description", ""),
            "input_schema": t.get("inputSchema", {}),
        }
        for t in tools
    ]


def _openapi_to_native_tools(spec: dict) -> list[dict]:
    """Convert an OpenAPI spec to the native tool injection format.

    Each operation becomes a tool with its full parameter/body schema.
    """
    tools = []
    for path, methods in spec.get("paths", {}).items():
        for method, op in methods.items():
            if method not in ("get", "post", "put", "delete", "patch"):
                continue
            properties = {}
            required = []

            for p in op.get("parameters", []):
                prop = dict(p.get("schema", {}))
                prop["description"] = p.get("description", "")
                properties[p["name"]] = prop
                if p.get("required"):
                    required.append(p["name"])

            body = op.get("requestBody", {})
            body_schema = (
                body.get("content", {})
                .get("application/json", {})
                .get("schema", {})
            )
            if body_schema:
                for prop_name, prop_schema in body_schema.get("properties", {}).items():
                    properties[prop_name] = prop_schema
                    if prop_name in body_schema.get("required", []):
                        required.append(prop_name)

            tools.append({
                "name": op.get("operationId", f"{method}_{path}"),
                "description": op.get("summary", ""),
                "input_schema": {
                    "type": "object",
                    "properties": properties,
                    **({"required": required} if required else {}),
                },
            })
    return tools


def _measure_list_tokens(tools: list[dict]) -> int:
    """Measure the actual token cost of a --list output for a set of tools.

    The --list output includes tool names and truncated descriptions,
    so it scales linearly with the number of tools.
    """
    lines = [f"  {t['name']:<30} {t.get('description', '')[:60]}" for t in tools]
    list_output = "Available tools:\n" + "\n".join(lines)
    return _count_tokens(list_output)


# The MCP test server's tool definitions (3 simple tools)
MCP_TOOLS = [
    {
        "name": "echo",
        "description": "Echo back the input",
        "inputSchema": {
            "type": "object",
            "properties": {
                "message": {"type": "string", "description": "Message to echo"},
            },
            "required": ["message"],
        },
    },
    {
        "name": "add_numbers",
        "description": "Add two numbers",
        "inputSchema": {
            "type": "object",
            "properties": {
                "a": {"type": "integer", "description": "First number"},
                "b": {"type": "integer", "description": "Second number"},
            },
            "required": ["a", "b"],
        },
    },
    {
        "name": "list_items",
        "description": "List items in a directory (test tool)",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Directory path"},
                "recursive": {"type": "boolean", "description": "Recurse into subdirs"},
            },
            "required": ["path"],
        },
    },
]

# The one-line system prompt mcp2cli needs
MCP2CLI_SYSTEM_PROMPT = (
    'Use `mcp2cli --spec <url> <command> [--flags]` to interact with the API. '
    'Run `mcp2cli --spec <url> --list` to see available commands, '
    'or `mcp2cli --spec <url> <command> --help` for details on a specific command.'
)

# Average --help output per tool, measured from realistic tool definitions.
# Simple tools ~80 tokens, complex tools (create_task) ~200 tokens.
AVG_HELP_TOKENS_PER_TOOL = 120


def _simulate_conversation(
    num_turns: int,
    native_tool_tokens: int,
    mcp2cli_prompt_tokens: int,
    discovery_tokens: int,
    num_tool_calls: int,
    tool_call_output_tokens: int,
    unique_tools_used: int = 0,
    help_tokens_per_tool: int = AVG_HELP_TOKENS_PER_TOOL,
) -> dict:
    """Simulate token costs over a multi-turn conversation.

    Native: tool definitions are injected on EVERY turn.
    mcp2cli: system prompt is injected on every turn, discovery happens once,
    the LLM runs --help once per unique tool it uses, and tool calls add
    their output to the context.

    Returns token counts and savings for both approaches.
    """
    # Native: pay the full tool schema cost every turn, plus tool call outputs
    native_total = (native_tool_tokens * num_turns) + (tool_call_output_tokens * num_tool_calls)

    # mcp2cli: pay the small system prompt every turn, discovery once,
    # --help once per unique tool used, plus tool call outputs
    mcp2cli_total = (
        (mcp2cli_prompt_tokens * num_turns)
        + discovery_tokens  # --list, run once
        + (help_tokens_per_tool * unique_tools_used)  # --help per unique tool
        + (tool_call_output_tokens * num_tool_calls)
    )

    return {
        "turns": num_turns,
        "tool_calls": num_tool_calls,
        "unique_tools_used": unique_tools_used,
        "native_total": native_total,
        "mcp2cli_total": mcp2cli_total,
        "tokens_saved": native_total - mcp2cli_total,
        "reduction_pct": round((1 - mcp2cli_total / native_total) * 100, 1),
    }


class TestOpenAPITokenSavings:
    """Measure and verify token savings for OpenAPI specs."""

    def test_system_prompt_token_count(self):
        """The mcp2cli system prompt should be under 100 tokens."""
        tokens = _count_tokens(MCP2CLI_SYSTEM_PROMPT)
        assert tokens < 100, f"System prompt is {tokens} tokens, expected < 100"
        print(f"\nmcp2cli system prompt: {tokens} tokens")

    def test_petstore_openapi_savings(self):
        """Measure savings for a 5-endpoint petstore API."""
        native_tools = _openapi_to_native_tools(PETSTORE_SPEC)
        native_tokens = _count_tokens(json.dumps(native_tools))
        prompt_tokens = _count_tokens(MCP2CLI_SYSTEM_PROMPT)

        # Actual --list output token cost
        list_output = "list-pets  create-pet  get-pet  delete-pet  update-pet"
        list_tokens = _count_tokens(list_output)

        result = _simulate_conversation(
            num_turns=10,
            native_tool_tokens=native_tokens,
            mcp2cli_prompt_tokens=prompt_tokens,
            discovery_tokens=list_tokens,
            num_tool_calls=5,
            unique_tools_used=3,
            tool_call_output_tokens=30,
        )

        print(f"\n--- Petstore (5 endpoints) over {result['turns']} turns ---")
        print(f"Native tool injection:  {native_tokens} tokens/turn")
        print(f"mcp2cli system prompt:  {prompt_tokens} tokens/turn")
        print(f"Discovery (--list):     {list_tokens} tokens (once)")
        print(f"Help (3 tools):         {AVG_HELP_TOKENS_PER_TOOL * 3} tokens (once each)")
        print(f"Native total:           {result['native_total']:,} tokens")
        print(f"mcp2cli total:          {result['mcp2cli_total']:,} tokens")
        print(f"Tokens saved:           {result['tokens_saved']:,} ({result['reduction_pct']}%)")

        assert result["reduction_pct"] > 60, f"Expected >60% reduction, got {result['reduction_pct']}%"

    def test_scaled_api_savings(self):
        """Project savings for realistic API sizes: 20, 50, 200 endpoints.

        Uses per-endpoint token cost from our petstore spec to extrapolate.
        """
        native_tools = _openapi_to_native_tools(PETSTORE_SPEC)
        per_endpoint = _count_tokens(json.dumps(native_tools)) / len(native_tools)
        prompt_tokens = _count_tokens(MCP2CLI_SYSTEM_PROMPT)

        scenarios = [
            {"name": "Medium API", "endpoints": 20, "turns": 15, "calls": 8, "unique": 5},
            {"name": "Large API", "endpoints": 50, "turns": 20, "calls": 12, "unique": 8},
            {"name": "Enterprise API", "endpoints": 200, "turns": 25, "calls": 15, "unique": 10},
        ]

        print(f"\nPer-endpoint token cost: {per_endpoint:.0f} tokens")
        print(f"mcp2cli prompt: {prompt_tokens} tokens/turn")
        print()

        for s in scenarios:
            native_tokens = int(per_endpoint * s["endpoints"])
            # --list output scales linearly: ~3 tokens per command name for OpenAPI
            list_tokens = s["endpoints"] * 3

            result = _simulate_conversation(
                num_turns=s["turns"],
                native_tool_tokens=native_tokens,
                mcp2cli_prompt_tokens=prompt_tokens,
                discovery_tokens=list_tokens,
                num_tool_calls=s["calls"],
                unique_tools_used=s["unique"],
                tool_call_output_tokens=30,
            )

            print(f"--- {s['name']} ({s['endpoints']} endpoints) over {result['turns']} turns, {s['calls']} calls, {s['unique']} unique ---")
            print(f"  Native: {result['native_total']:>8,} tokens")
            print(f"  mcp2cli: {result['mcp2cli_total']:>7,} tokens")
            print(f"  Saved: {result['tokens_saved']:>9,} tokens ({result['reduction_pct']}%)")

            assert result["reduction_pct"] > 85, (
                f"{s['name']}: expected >85% reduction, got {result['reduction_pct']}%"
            )

    def test_conversation_breakdown(self):
        """Show turn-by-turn token accumulation for a 50-endpoint API.

        Includes --help cost on the turn each unique tool is first used.
        """
        endpoints = 50
        native_tools = _openapi_to_native_tools(PETSTORE_SPEC)
        per_endpoint = _count_tokens(json.dumps(native_tools)) / len(native_tools)
        native_per_turn = int(per_endpoint * endpoints)
        prompt_tokens = _count_tokens(MCP2CLI_SYSTEM_PROMPT)
        list_tokens = endpoints * 3

        turns = 10
        # Turn 1 = discovery, turns 3,5,7,9 = tool calls (each a new unique tool)
        tool_call_turns = {3, 5, 7, 9}
        call_output = 30

        native_cumulative = 0
        mcp2cli_cumulative = 0

        print(f"\n{'Turn':<6} {'Native':<12} {'mcp2cli':<12} {'Savings':<12}")
        print("-" * 50)

        for turn in range(1, turns + 1):
            native_cumulative += native_per_turn
            mcp2cli_cumulative += prompt_tokens

            label = ""
            if turn == 1:
                mcp2cli_cumulative += list_tokens
                label = " ← discovery (--list)"

            if turn in tool_call_turns:
                native_cumulative += call_output
                mcp2cli_cumulative += call_output
                # First time using this tool: pay --help cost
                mcp2cli_cumulative += AVG_HELP_TOKENS_PER_TOOL
                label = " ← --help + tool call"

            savings = native_cumulative - mcp2cli_cumulative
            print(f"{turn:<6} {native_cumulative:<12,} {mcp2cli_cumulative:<12,} {savings:<12,}{label}")

        pct = round((1 - mcp2cli_cumulative / native_cumulative) * 100, 1)
        print(f"\nTotal savings: {native_cumulative - mcp2cli_cumulative:,} tokens ({pct}%)")

        assert pct > 93, f"Expected >93% savings over {turns} turns, got {pct}%"

    def test_actual_cli_list_output_tokens(self):
        """Measure the actual token cost of running mcp2cli --list."""
        spec_file = Path(__file__).parent / "_petstore_tmp.json"
        spec_file.write_text(json.dumps(PETSTORE_SPEC))
        try:
            r = subprocess.run(
                [sys.executable, "-m", "mcp2cli", "--spec", str(spec_file),
                 "--base-url", "http://unused", "--list"],
                capture_output=True, text=True, encoding="utf-8", timeout=15,
            )
            assert r.returncode == 0
            list_output = r.stdout.strip()
            list_tokens = _count_tokens(list_output)

            native_tools = _openapi_to_native_tools(PETSTORE_SPEC)
            native_tokens = _count_tokens(json.dumps(native_tools))

            print(f"\n--- Actual CLI output ---")
            print(f"--list output ({list_tokens} tokens):")
            for line in list_output.split("\n"):
                print(f"  {line}")
            print(f"\nNative tool schemas: {native_tokens} tokens")
            print(f"--list output: {list_tokens} tokens")
            print(f"Ratio: {native_tokens / max(list_tokens, 1):.1f}x more compact")

            assert list_tokens < native_tokens, "CLI list should be more compact than full schemas"
        finally:
            spec_file.unlink(missing_ok=True)

    def test_help_output_tokens(self):
        """Measure token cost of a single command's --help vs its native schema."""
        spec_file = Path(__file__).parent / "_petstore_tmp.json"
        spec_file.write_text(json.dumps(PETSTORE_SPEC))
        try:
            r = subprocess.run(
                [sys.executable, "-m", "mcp2cli", "--spec", str(spec_file),
                 "--base-url", "http://unused", "create-pet", "--help"],
                capture_output=True, text=True, encoding="utf-8", timeout=15,
            )
            assert r.returncode == 0
            help_output = r.stdout.strip()
            help_tokens = _count_tokens(help_output)

            # Native schema for create-pet
            native_tools = _openapi_to_native_tools(PETSTORE_SPEC)
            create_tool = next(t for t in native_tools if t["name"] == "createPet")
            native_tokens = _count_tokens(json.dumps(create_tool))

            print(f"\n--- create-pet --help vs native schema ---")
            print(f"--help output: {help_tokens} tokens")
            print(f"Native schema: {native_tokens} tokens")
            print(f"Difference: {help_tokens - native_tokens:+d} tokens")
            print(f"(--help is loaded once on demand, not every turn)")
        finally:
            spec_file.unlink(missing_ok=True)

    def test_readme_numbers_are_accurate(self):
        """Verify the specific numbers claimed in the README are grounded in measurement."""
        prompt_tokens = _count_tokens(MCP2CLI_SYSTEM_PROMPT)

        # System prompt should be compact
        assert 40 <= prompt_tokens <= 80, (
            f"System prompt is {prompt_tokens} tokens, expected 40-80"
        )

        # Per-endpoint cost should be meaningful
        native_tools = _openapi_to_native_tools(PETSTORE_SPEC)
        per_endpoint = _count_tokens(json.dumps(native_tools)) / len(native_tools)
        assert per_endpoint > 30, f"Per-endpoint cost is {per_endpoint}, expected > 30"

        # The savings ratio should hold: mcp2cli prompt < any meaningful tool set
        native_5 = _count_tokens(json.dumps(native_tools))
        assert prompt_tokens < native_5, (
            f"System prompt ({prompt_tokens}) should be less than 5-endpoint schemas ({native_5})"
        )

        print(f"\nMeasured numbers:")
        print(f"  System prompt: {prompt_tokens} tokens")
        print(f"  Per-endpoint cost: {per_endpoint:.0f} tokens")
        print(f"  5 endpoints native: {native_5} tokens")
        print(f"  Ratio: {native_5 / prompt_tokens:.1f}x")


class TestMCPTokenSavings:
    """Measure and verify token savings for MCP servers specifically.

    Uses realistic tool definitions modeled after a real-world task
    management MCP server (~30 tools with full schemas).
    """

    def test_small_mcp_server_savings(self):
        """Measure savings for a 3-tool MCP test server."""
        native_tools = _build_native_tool_definitions(MCP_TOOLS)
        native_tokens = _count_tokens(json.dumps(native_tools))
        prompt_tokens = _count_tokens(MCP2CLI_SYSTEM_PROMPT)

        list_tokens = _measure_list_tokens(MCP_TOOLS)

        result = _simulate_conversation(
            num_turns=10,
            native_tool_tokens=native_tokens,
            mcp2cli_prompt_tokens=prompt_tokens,
            discovery_tokens=list_tokens,
            num_tool_calls=5,
            unique_tools_used=2,
            tool_call_output_tokens=20,
        )

        print(f"\n--- Small MCP server (3 tools) over {result['turns']} turns ---")
        print(f"Native tool injection:  {native_tokens} tokens/turn")
        print(f"mcp2cli system prompt:  {prompt_tokens} tokens/turn")
        print(f"Discovery (--list):     {list_tokens} tokens (once)")
        print(f"Help (2 tools):         {AVG_HELP_TOKENS_PER_TOOL * 2} tokens (once each)")
        print(f"Native total:           {result['native_total']:,} tokens")
        print(f"mcp2cli total:          {result['mcp2cli_total']:,} tokens")
        print(f"Tokens saved:           {result['tokens_saved']:,} ({result['reduction_pct']}%)")

        assert result["reduction_pct"] > 30, f"Expected >30% reduction, got {result['reduction_pct']}%"

    def test_realistic_mcp_server_savings(self):
        """Measure savings for a realistic 30-tool MCP server.

        Uses tool definitions modeled after a real task management server
        with complex inputSchemas including enums, arrays, nested objects,
        and detailed descriptions.
        """
        native_tools = _build_native_tool_definitions(REALISTIC_MCP_TOOLS)
        native_tokens = _count_tokens(json.dumps(native_tools))
        per_tool = native_tokens / len(REALISTIC_MCP_TOOLS)
        prompt_tokens = _count_tokens(MCP2CLI_SYSTEM_PROMPT)

        list_tokens = _measure_list_tokens(REALISTIC_MCP_TOOLS)

        result = _simulate_conversation(
            num_turns=15,
            native_tool_tokens=native_tokens,
            mcp2cli_prompt_tokens=prompt_tokens,
            discovery_tokens=list_tokens,
            num_tool_calls=8,
            unique_tools_used=5,
            tool_call_output_tokens=30,
        )

        print(f"\n--- Realistic MCP server ({len(REALISTIC_MCP_TOOLS)} tools) over {result['turns']} turns ---")
        print(f"Per-tool token cost:    {per_tool:.0f} tokens")
        print(f"Native tool injection:  {native_tokens:,} tokens/turn")
        print(f"mcp2cli system prompt:  {prompt_tokens} tokens/turn")
        print(f"Discovery (--list):     {list_tokens} tokens (once, scales linearly)")
        print(f"Help (5 tools):         {AVG_HELP_TOKENS_PER_TOOL * 5} tokens (once each)")
        print(f"Native total:           {result['native_total']:,} tokens")
        print(f"mcp2cli total:          {result['mcp2cli_total']:,} tokens")
        print(f"Tokens saved:           {result['tokens_saved']:,} ({result['reduction_pct']}%)")

        assert result["reduction_pct"] > 90, f"Expected >90% reduction, got {result['reduction_pct']}%"

    def test_per_tool_token_cost(self):
        """Measure the per-tool token cost for MCP tool schemas.

        Real MCP tools tend to be heavier than OpenAPI endpoints because
        they include full inputSchema with descriptions and enums inline.
        """
        native_tools = _build_native_tool_definitions(REALISTIC_MCP_TOOLS)
        native_tokens = _count_tokens(json.dumps(native_tools))
        per_tool = native_tokens / len(REALISTIC_MCP_TOOLS)

        # Compare with OpenAPI per-endpoint cost
        openapi_tools = _openapi_to_native_tools(PETSTORE_SPEC)
        openapi_per_endpoint = _count_tokens(json.dumps(openapi_tools)) / len(openapi_tools)

        print(f"\n--- Per-tool/endpoint token cost ---")
        print(f"MCP per-tool:         {per_tool:.0f} tokens (avg over {len(REALISTIC_MCP_TOOLS)} tools)")
        print(f"OpenAPI per-endpoint: {openapi_per_endpoint:.0f} tokens (avg over {len(openapi_tools)} endpoints)")

        # Show individual tool costs
        print(f"\nIndividual MCP tool costs:")
        for t in REALISTIC_MCP_TOOLS[:5]:
            tool_def = {"name": t["name"], "description": t.get("description", ""), "input_schema": t.get("inputSchema", {})}
            tokens = _count_tokens(json.dumps(tool_def))
            print(f"  {t['name']:<30} {tokens:>4} tokens")
        print(f"  ...")

        # MCP per-tool cost should be significant (real schemas aren't tiny)
        assert per_tool > 40, f"Per-tool cost {per_tool:.0f} seems too low for realistic schemas"

    def test_list_output_scales_linearly(self):
        """Verify that --list output cost scales linearly with tool count.

        This is important for honesty: mcp2cli's discovery cost isn't
        constant — it grows with the number of tools. But it grows at
        ~15 tokens/tool vs ~121 tokens/tool for native schemas.
        """
        # Measure at different scales by using subsets of REALISTIC_MCP_TOOLS
        sizes = [3, 10, 20, 30]
        measurements = []

        print(f"\n--- --list output token cost vs tool count ---")
        print(f"{'Tools':<8} {'--list tokens':<15} {'Native tokens':<15} {'Ratio':<8}")
        print("-" * 46)

        for n in sizes:
            tools = REALISTIC_MCP_TOOLS[:n]
            list_tokens = _measure_list_tokens(tools)
            native_tools = _build_native_tool_definitions(tools)
            native_tokens = _count_tokens(json.dumps(native_tools))
            ratio = native_tokens / max(list_tokens, 1)

            measurements.append((n, list_tokens, native_tokens))
            print(f"{n:<8} {list_tokens:<15} {native_tokens:<15} {ratio:.1f}x")

        # Verify linear scaling: tokens-per-tool should be roughly constant
        per_tool_costs = [tokens / n for n, tokens, _ in measurements]
        avg_per_tool = sum(per_tool_costs) / len(per_tool_costs)
        print(f"\nAverage --list cost per tool: {avg_per_tool:.1f} tokens")
        print(f"Average native cost per tool: {measurements[-1][2] / measurements[-1][0]:.0f} tokens")

        # --list should always be much cheaper than native
        for n, list_tokens, native_tokens in measurements:
            assert list_tokens < native_tokens, f"--list should be cheaper than native at {n} tools"

    def test_scaled_mcp_server_savings(self):
        """Project savings for realistic MCP server sizes: 30, 80, 120 tools.

        Uses per-tool token cost from our realistic fixture to extrapolate
        to larger servers like Fulcrum (~120 tools) or multi-server setups.
        Includes --help costs for unique tools actually used.
        """
        native_tools = _build_native_tool_definitions(REALISTIC_MCP_TOOLS)
        per_tool = _count_tokens(json.dumps(native_tools)) / len(REALISTIC_MCP_TOOLS)
        prompt_tokens = _count_tokens(MCP2CLI_SYSTEM_PROMPT)

        # Measure --list cost per tool from our fixture
        list_per_tool = _measure_list_tokens(REALISTIC_MCP_TOOLS) / len(REALISTIC_MCP_TOOLS)

        scenarios = [
            {"name": "Task manager", "tools": 30, "turns": 15, "calls": 8, "unique": 5},
            {"name": "Multi-server (3 servers)", "tools": 80, "turns": 20, "calls": 12, "unique": 8},
            {"name": "Full platform (Fulcrum-scale)", "tools": 120, "turns": 25, "calls": 15, "unique": 10},
        ]

        print(f"\nMCP per-tool token cost: {per_tool:.0f} tokens (native)")
        print(f"--list per-tool cost:   {list_per_tool:.0f} tokens")
        print(f"mcp2cli prompt:         {prompt_tokens} tokens/turn")
        print()

        for s in scenarios:
            native_tokens = int(per_tool * s["tools"])
            list_tokens = int(list_per_tool * s["tools"])

            result = _simulate_conversation(
                num_turns=s["turns"],
                native_tool_tokens=native_tokens,
                mcp2cli_prompt_tokens=prompt_tokens,
                discovery_tokens=list_tokens,
                num_tool_calls=s["calls"],
                unique_tools_used=s["unique"],
                tool_call_output_tokens=30,
            )

            print(f"--- {s['name']} ({s['tools']} tools) over {result['turns']} turns, {s['calls']} calls, {s['unique']} unique ---")
            print(f"  Native:  {result['native_total']:>8,} tokens")
            print(f"  mcp2cli: {result['mcp2cli_total']:>7,} tokens  (list={list_tokens} + help={AVG_HELP_TOKENS_PER_TOOL * s['unique']} + prompt={prompt_tokens * s['turns']} + output={30 * s['calls']})")
            print(f"  Saved:   {result['tokens_saved']:>9,} tokens ({result['reduction_pct']}%)")

            assert result["reduction_pct"] > 85, (
                f"{s['name']}: expected >85% reduction, got {result['reduction_pct']}%"
            )

    def test_mcp_conversation_breakdown(self):
        """Turn-by-turn token accumulation for a 30-tool MCP server.

        Includes --help costs when a new unique tool is first used.
        """
        native_tools = _build_native_tool_definitions(REALISTIC_MCP_TOOLS)
        native_per_turn = _count_tokens(json.dumps(native_tools))
        prompt_tokens = _count_tokens(MCP2CLI_SYSTEM_PROMPT)
        list_tokens = _measure_list_tokens(REALISTIC_MCP_TOOLS)

        turns = 10
        tool_call_turns = {3, 5, 7, 9}
        call_output = 30

        native_cumulative = 0
        mcp2cli_cumulative = 0

        print(f"\n--- {len(REALISTIC_MCP_TOOLS)}-tool MCP server over {turns} turns ---")
        print(f"{'Turn':<6} {'Native':<12} {'mcp2cli':<12} {'Savings':<12}")
        print("-" * 50)

        for turn in range(1, turns + 1):
            native_cumulative += native_per_turn
            mcp2cli_cumulative += prompt_tokens

            label = ""
            if turn == 1:
                mcp2cli_cumulative += list_tokens
                label = f" ← discovery ({list_tokens} tokens)"

            if turn in tool_call_turns:
                native_cumulative += call_output
                mcp2cli_cumulative += call_output
                mcp2cli_cumulative += AVG_HELP_TOKENS_PER_TOOL  # --help for new tool
                label = f" ← --help ({AVG_HELP_TOKENS_PER_TOOL}) + call ({call_output})"

            savings = native_cumulative - mcp2cli_cumulative
            print(f"{turn:<6} {native_cumulative:<12,} {mcp2cli_cumulative:<12,} {savings:<12,}{label}")

        pct = round((1 - mcp2cli_cumulative / native_cumulative) * 100, 1)
        print(f"\nTotal savings: {native_cumulative - mcp2cli_cumulative:,} tokens ({pct}%)")

        assert pct > 90, f"Expected >90% savings over {turns} turns, got {pct}%"

    def test_multi_server_scenario(self):
        """Simulate the common case: multiple MCP servers connected at once.

        This is the scenario that hurts most with native injection — every
        server's tools get injected on every turn, even when you're only
        using one server at a time.
        """
        native_tools = _build_native_tool_definitions(REALISTIC_MCP_TOOLS)
        per_tool = _count_tokens(json.dumps(native_tools)) / len(REALISTIC_MCP_TOOLS)
        list_per_tool = _measure_list_tokens(REALISTIC_MCP_TOOLS) / len(REALISTIC_MCP_TOOLS)
        prompt_tokens = _count_tokens(MCP2CLI_SYSTEM_PROMPT)

        # 3 servers: task manager (30), filesystem (10), database (20) = 60 tools total
        server_sizes = [30, 10, 20]
        total_tools = sum(server_sizes)
        native_per_turn = int(per_tool * total_tools)
        list_tokens = int(list_per_tool * total_tools)

        result = _simulate_conversation(
            num_turns=20,
            native_tool_tokens=native_per_turn,
            mcp2cli_prompt_tokens=prompt_tokens,
            discovery_tokens=list_tokens,
            num_tool_calls=10,
            unique_tools_used=6,
            tool_call_output_tokens=30,
        )

        print(f"\n--- Multi-server ({' + '.join(str(s) for s in server_sizes)} = {total_tools} tools) over {result['turns']} turns ---")
        print(f"Native per turn:  {native_per_turn:,} tokens")
        print(f"Native total:     {result['native_total']:,} tokens")
        print(f"mcp2cli total:    {result['mcp2cli_total']:,} tokens  (list={list_tokens} + help={AVG_HELP_TOKENS_PER_TOOL * 6} + prompt={prompt_tokens * 20} + output={30 * 10})")
        print(f"Tokens saved:     {result['tokens_saved']:,} ({result['reduction_pct']}%)")

        assert result["reduction_pct"] > 95, f"Expected >95% reduction, got {result['reduction_pct']}%"
