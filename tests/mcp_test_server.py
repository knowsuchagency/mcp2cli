"""Minimal MCP server for testing — runs over stdio."""

import asyncio
import sys

from mcp.server import Server
from mcp.server.lowlevel.helper_types import ReadResourceContents
from mcp.server.stdio import stdio_server
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

app = Server("test-server")


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
        Tool(
            name="list_items",
            description="List items in a directory (test tool)",
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Directory path"},
                    "recursive": {"type": "boolean", "description": "Recurse into subdirs"},
                },
                "required": ["path"],
            },
        ),
    ]


@app.list_resources()
async def list_resources():
    return [
        Resource(
            uri="file:///test/doc.txt",
            name="Test Document",
            description="A test text document",
            mimeType="text/plain",
        ),
        Resource(
            uri="file:///test/data.bin",
            name="Binary Data",
            description="A test binary resource",
            mimeType="application/octet-stream",
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
    if uri_str == "file:///test/data.bin":
        return [ReadResourceContents(content=b"\x00\x01\x02\x03", mime_type="application/octet-stream")]
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
        Prompt(
            name="summary",
            description="Summarize content",
            arguments=[
                PromptArgument(name="topic", description="Topic to summarize", required=True),
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
    if name == "summary":
        topic = arguments.get("topic", "general")
        return GetPromptResult(
            description=f"Summary of {topic}",
            messages=[
                PromptMessage(role="user", content=TextContent(type="text", text=f"Please summarize the topic: {topic}")),
            ],
        )
    raise ValueError(f"Unknown prompt: {name}")


@app.call_tool()
async def call_tool(name: str, arguments: dict):
    if name == "echo":
        return [TextContent(type="text", text=arguments.get("message", ""))]
    if name == "add_numbers":
        result = arguments.get("a", 0) + arguments.get("b", 0)
        return [TextContent(type="text", text=str(result))]
    if name == "list_items":
        path = arguments.get("path", "/")
        recursive = arguments.get("recursive", False)
        return [TextContent(type="text", text=f'{{"path": "{path}", "recursive": {str(recursive).lower()}, "items": ["file1.txt", "file2.txt"]}}')]
    return [TextContent(type="text", text=f"Unknown tool: {name}")]


async def main():
    async with stdio_server() as (read, write):
        await app.run(read, write, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
