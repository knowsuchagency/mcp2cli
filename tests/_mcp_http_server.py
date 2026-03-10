
"""Minimal MCP HTTP server for testing."""
import asyncio
import base64
import socket
import sys

from mcp.server import Server
from mcp.server.lowlevel.helper_types import ReadResourceContents
from mcp.types import (
    GetPromptResult,
    Prompt,
    PromptArgument,
    PromptMessage,
    Resource,
    ResourceTemplate,
    TextContent,
    Tool,
)

app = Server("test-http-server")


@app.list_tools()
async def list_tools():
    return [
        Tool(
            name="echo",
            description="Echo back the input",
            inputSchema={
                "type": "object",
                "properties": {
                    "message": {"type": "string", "description": "Message to echo"},
                },
                "required": ["message"],
            },
        ),
        Tool(
            name="add_numbers",
            description="Add two numbers",
            inputSchema={
                "type": "object",
                "properties": {
                    "a": {"type": "integer", "description": "First number"},
                    "b": {"type": "integer", "description": "Second number"},
                },
                "required": ["a", "b"],
            },
        ),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict):
    if name == "echo":
        return [TextContent(type="text", text=arguments.get("message", ""))]
    if name == "add_numbers":
        result = arguments.get("a", 0) + arguments.get("b", 0)
        return [TextContent(type="text", text=str(result))]
    return [TextContent(type="text", text=f"Unknown tool: {name}")]


@app.list_resources()
async def list_resources():
    return [
        Resource(
            uri="file:///test/doc.txt",
            name="Test Document",
            description="A test text document",
            mimeType="text/plain",
        ),
    ]


@app.list_resource_templates()
async def list_resource_templates():
    return [
        ResourceTemplate(
            uriTemplate="file:///test/{name}.txt",
            name="Text File",
            description="A text file by name",
            mimeType="text/plain",
        ),
    ]


@app.read_resource()
async def read_resource(uri):
    uri_str = str(uri)
    if uri_str == "file:///test/doc.txt":
        return [ReadResourceContents(content="Hello from test document!", mime_type="text/plain")]
    raise ValueError(f"Resource not found: {uri_str}")


@app.list_prompts()
async def list_prompts():
    return [
        Prompt(
            name="greeting",
            description="Generate a greeting message",
            arguments=[
                PromptArgument(name="name", description="Name to greet", required=True),
                PromptArgument(name="style", description="Greeting style", required=False),
            ],
        ),
    ]


@app.get_prompt()
async def get_prompt(name: str, arguments: dict | None = None):
    arguments = arguments or {}
    if name == "greeting":
        who = arguments.get("name", "World")
        style = arguments.get("style", "friendly")
        return GetPromptResult(
            description=f"A {style} greeting",
            messages=[
                PromptMessage(role="user", content=TextContent(type="text", text=f"Please greet {who} in a {style} way.")),
            ],
        )
    raise ValueError(f"Unknown prompt: {name}")


def find_free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def main():
    from mcp.server.sse import SseServerTransport
    from starlette.applications import Starlette
    from starlette.routing import Mount, Route
    import uvicorn

    sse = SseServerTransport("/messages/")

    async def handle_sse(request):
        async with sse.connect_sse(request.scope, request.receive, request._send) as (read, write):
            await app.run(read, write, app.create_initialization_options())

    starlette_app = Starlette(
        routes=[
            Route("/sse", endpoint=handle_sse),
            Mount("/messages/", app=sse.handle_post_message),
        ],
    )

    port = find_free_port()
    print(f"PORT={port}", flush=True)

    config = uvicorn.Config(starlette_app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    await server.serve()


if __name__ == "__main__":
    asyncio.run(main())
