"""A minimal MCP server double, implemented against the wire protocol.

Deliberately free of any `mcp` SDK import. The SDK's *server* API changed
shape between 1.x and 2.x (2.0 dropped the `Server.list_tools()` decorators in
favour of `add_request_handler`), so a fixture built on it stops working the
moment we test another SDK major -- which is exactly when we most need the
coverage. The JSON-RPC wire format did *not* change, so speaking it directly
lets one fixture serve every SDK version.

Field names here are the camelCase wire names. SDK 2.0 renamed the Python
attributes to snake_case but kept camelCase as serialization aliases, so the
same payload validates on both majors.

Shared by `mcp_test_server.py` (stdio) and `_mcp_http_server.py` (HTTP).
"""

from __future__ import annotations

import base64
import json

SERVER_NAME = "test-server"

TOOLS = [
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
                "recursive": {
                    "type": "boolean",
                    "description": "Recurse into subdirs",
                },
            },
            "required": ["path"],
        },
    },
    {
        "name": "deploy",
        "description": "Deploy to an environment (shadows global --env and --refresh)",
        "inputSchema": {
            "type": "object",
            "properties": {
                "env": {"type": "string", "description": "Target environment"},
                "refresh": {"type": "boolean", "description": "Force refresh"},
            },
            "required": ["env"],
        },
    },
    {
        "name": "fail",
        "description": "Always fails with isError (for exit-code tests)",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "struct_only",
        "description": "Returns only structuredContent (empty content list)",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "reserved_args",
        "description": "Accepts properties that collide with CLI flags",
        "inputSchema": {
            "type": "object",
            "properties": {
                "help": {"type": "string"},
                "stdin": {"type": "boolean"},
                "arg_stdin": {"type": "string"},
            },
        },
    },
    {
        # Optional[...] params as pydantic (FastMCP, MCPServer) emits them.
        "name": "echo_optional",
        "description": "Echo back optional arguments",
        "inputSchema": {
            "type": "object",
            "properties": {
                "tags": {
                    "anyOf": [
                        {"type": "array", "items": {"type": "string"}},
                        {"type": "null"},
                    ],
                    "default": None,
                    "title": "Tags",
                },
                "count": {
                    "anyOf": [{"type": "integer"}, {"type": "null"}],
                    "default": None,
                    "title": "Count",
                },
                "verbose": {
                    "anyOf": [{"type": "boolean"}, {"type": "null"}],
                    "default": None,
                    "title": "Verbose",
                },
            },
        },
    },
    {
        "name": "client_roots",
        "description": "Return roots exposed by the connected client",
        "inputSchema": {"type": "object", "properties": {}},
    },
]

RESOURCES = [
    {
        "uri": "file:///test/doc.txt",
        "name": "Test Document",
        "description": "A test text document",
        "mimeType": "text/plain",
    },
    {
        "uri": "file:///test/data.bin",
        "name": "Binary Data",
        "description": "A test binary resource",
        "mimeType": "application/octet-stream",
    },
]

RESOURCE_TEMPLATES = [
    {
        "uriTemplate": "file:///test/{name}.txt",
        "name": "Text File",
        "description": "A text file by name",
        "mimeType": "text/plain",
    },
]

PROMPTS = [
    {
        "name": "greeting",
        "description": "Generate a greeting message",
        "arguments": [
            {"name": "name", "description": "Name to greet", "required": True},
            {"name": "style", "description": "Greeting style", "required": False},
        ],
    },
    {
        "name": "summary",
        "description": "Summarize content",
        "arguments": [
            {"name": "topic", "description": "Topic to summarize", "required": True},
        ],
    },
]

# JSON-RPC / MCP error codes
INVALID_PARAMS = -32602
METHOD_NOT_FOUND = -32601


class _Error(Exception):
    def __init__(self, message: str, code: int = INVALID_PARAMS):
        super().__init__(message)
        self.code = code
        self.message = message


def _text(text: str) -> dict:
    return {"type": "text", "text": text}


def _call_tool(name: str, arguments: dict) -> dict:
    if name == "echo":
        return {"content": [_text(arguments.get("message", ""))], "isError": False}
    if name == "add_numbers":
        total = arguments.get("a", 0) + arguments.get("b", 0)
        return {"content": [_text(str(total))], "isError": False}
    if name == "list_items":
        payload = {
            "path": arguments.get("path", "/"),
            "recursive": bool(arguments.get("recursive", False)),
            "items": ["file1.txt", "file2.txt"],
        }
        return {"content": [_text(json.dumps(payload))], "isError": False}
    if name == "deploy":
        payload = {
            "env": arguments.get("env", ""),
            "refresh": arguments.get("refresh", False),
        }
        return {"content": [_text(json.dumps(payload))], "isError": False}
    if name == "fail":
        return {"content": [_text("boom: deliberate failure")], "isError": True}
    if name == "struct_only":
        return {
            "content": [],
            "structuredContent": {"answer": 42},
            "isError": False,
        }
    if name == "echo_optional":
        return {
            "content": [_text(json.dumps(arguments, sort_keys=True))],
            "isError": False,
        }
    if name == "reserved_args":
        return {
            "content": [_text(json.dumps(arguments, sort_keys=True))],
            "isError": False,
        }
    return {"content": [_text(f"Unknown tool: {name}")], "isError": True}


def _read_resource(uri: str) -> dict:
    if uri == "file:///test/doc.txt":
        return {
            "contents": [
                {
                    "uri": uri,
                    "mimeType": "text/plain",
                    "text": "Hello from test document!",
                }
            ]
        }
    if uri == "file:///test/data.bin":
        return {
            "contents": [
                {
                    "uri": uri,
                    "mimeType": "application/octet-stream",
                    "blob": base64.b64encode(b"\x00\x01\x02\x03").decode(),
                }
            ]
        }
    raise _Error(f"Resource not found: {uri}")


def _get_prompt(name: str, arguments: dict) -> dict:
    if name == "greeting":
        who = arguments.get("name", "World")
        style = arguments.get("style", "friendly")
        return {
            "description": f"A {style} greeting",
            "messages": [
                {
                    "role": "user",
                    "content": _text(f"Please greet {who} in a {style} way."),
                }
            ],
        }
    if name == "summary":
        topic = arguments.get("topic", "general")
        return {
            "description": f"Summary of {topic}",
            "messages": [
                {
                    "role": "user",
                    "content": _text(f"Please summarize the topic: {topic}"),
                }
            ],
        }
    raise _Error(f"Unknown prompt: {name}")


def _complete(params: dict) -> dict:
    prefix = (params.get("argument") or {}).get("value", "")
    candidates = ["San Diego", "San Francisco", "San Jose"]
    matches = [value for value in candidates if value.startswith(prefix)]
    return {
        "completion": {
            "values": matches[:2],
            "total": len(matches),
            "hasMore": len(matches) > 2,
        }
    }


def _result(method: str, params: dict) -> dict:
    if method == "initialize":
        # Echo the client's protocol version back rather than pinning one, so
        # this double stays valid as the SDK advances its LATEST_PROTOCOL_VERSION.
        return {
            "protocolVersion": params.get("protocolVersion", "2025-06-18"),
            "capabilities": {
                "tools": {},
                "resources": {},
                "prompts": {},
                "completions": {},
            },
            "serverInfo": {"name": SERVER_NAME, "version": "1.0.0"},
        }
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": TOOLS}
    if method == "tools/call":
        return _call_tool(params.get("name", ""), params.get("arguments") or {})
    if method == "resources/list":
        return {"resources": RESOURCES}
    if method == "resources/templates/list":
        return {"resourceTemplates": RESOURCE_TEMPLATES}
    if method == "resources/read":
        return _read_resource(params.get("uri", ""))
    if method == "prompts/list":
        return {"prompts": PROMPTS}
    if method == "prompts/get":
        return _get_prompt(params.get("name", ""), params.get("arguments") or {})
    if method == "completion/complete":
        return _complete(params)
    raise _Error(f"Method not found: {method}", METHOD_NOT_FOUND)


def handle(message: dict) -> dict | None:
    """Answer one JSON-RPC message. Returns None for notifications."""
    method = message.get("method", "")
    if "id" not in message:
        return None  # notification, e.g. notifications/initialized
    try:
        result = _result(method, message.get("params") or {})
    except _Error as exc:
        return {
            "jsonrpc": "2.0",
            "id": message["id"],
            "error": {"code": exc.code, "message": exc.message},
        }
    return {"jsonrpc": "2.0", "id": message["id"], "result": result}
