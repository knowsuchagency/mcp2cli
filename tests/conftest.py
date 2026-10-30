"""Shared fixtures for mcp2cli tests."""

import json
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

PETSTORE_SPEC = {
    "openapi": "3.0.3",
    "info": {"title": "Petstore", "version": "1.0.0"},
    "servers": [{"url": "/api/v1"}],
    "paths": {
        "/pets": {
            "get": {
                "operationId": "listPets",
                "summary": "List all pets",
                "parameters": [
                    {
                        "name": "limit",
                        "in": "query",
                        "schema": {"type": "integer"},
                        "description": "Max items to return",
                    },
                    {
                        "name": "status",
                        "in": "query",
                        "schema": {
                            "type": "string",
                            "enum": ["available", "pending", "sold"],
                        },
                        "description": "Filter by status",
                    },
                ],
            },
            "post": {
                "operationId": "createPet",
                "summary": "Create a pet",
                "requestBody": {
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "required": ["name"],
                                "properties": {
                                    "name": {
                                        "type": "string",
                                        "description": "Pet name",
                                    },
                                    "tag": {
                                        "type": "string",
                                        "description": "Pet tag",
                                    },
                                    "age": {
                                        "type": "integer",
                                        "description": "Pet age",
                                    },
                                },
                            }
                        }
                    }
                },
            },
        },
        "/pets/{petId}": {
            "get": {
                "operationId": "getPet",
                "summary": "Get a pet by ID",
                "parameters": [
                    {
                        "name": "petId",
                        "in": "path",
                        "required": True,
                        "schema": {"type": "integer"},
                        "description": "Pet ID",
                    }
                ],
            },
            "delete": {
                "operationId": "deletePet",
                "summary": "Delete a pet",
                "parameters": [
                    {
                        "name": "petId",
                        "in": "path",
                        "required": True,
                        "schema": {"type": "integer"},
                        "description": "Pet ID",
                    }
                ],
            },
            "put": {
                "operationId": "updatePet",
                "summary": "Update a pet",
                "parameters": [
                    {
                        "name": "petId",
                        "in": "path",
                        "required": True,
                        "schema": {"type": "integer"},
                        "description": "Pet ID",
                    }
                ],
                "requestBody": {
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "name": {
                                        "type": "string",
                                        "description": "Pet name",
                                    },
                                    "tag": {
                                        "type": "string",
                                        "description": "Pet tag",
                                    },
                                },
                            }
                        }
                    }
                },
            },
        },
    },
    "components": {
        "schemas": {
            "Pet": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "name": {"type": "string"},
                    "tag": {"type": "string"},
                },
            }
        }
    },
}

# Spec using $ref to test ref resolution
PETSTORE_SPEC_WITH_REFS = {
    "openapi": "3.0.3",
    "info": {"title": "Petstore (refs)", "version": "1.0.0"},
    "servers": [{"url": "/api/v1"}],
    "paths": {
        "/pets": {
            "get": {
                "operationId": "listPets",
                "summary": "List all pets",
                "parameters": [
                    {"$ref": "#/components/parameters/LimitParam"},
                ],
            },
        },
    },
    "components": {
        "parameters": {
            "LimitParam": {
                "name": "limit",
                "in": "query",
                "schema": {"type": "integer"},
                "description": "Max items to return",
            },
        },
    },
}


# In-memory pet store
_PETS = {
    1: {"id": 1, "name": "Fido", "tag": "dog", "status": "available", "age": 3},
    2: {"id": 2, "name": "Whiskers", "tag": "cat", "status": "available", "age": 5},
    3: {"id": 3, "name": "Goldie", "tag": "fish", "status": "sold", "age": 1},
}
_NEXT_ID = 4


class PetstoreHandler(BaseHTTPRequestHandler):
    """HTTP handler that serves the petstore spec and API."""

    def log_message(self, format, *args):
        pass  # silence logs during tests

    def _send_json(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if length:
            return json.loads(self.rfile.read(length))
        return {}

    def _parse_path(self):
        path = self.path.split("?")[0]
        query_str = self.path.split("?")[1] if "?" in self.path else ""
        params = {}
        if query_str:
            for pair in query_str.split("&"):
                if "=" in pair:
                    k, v = pair.split("=", 1)
                    params[k] = v
        return path, params

    def do_GET(self):
        path, params = self._parse_path()

        if path == "/openapi.json":
            self._send_json(PETSTORE_SPEC)
            return

        if path == "/api/v1/pets":
            pets = list(_PETS.values())
            if "status" in params:
                pets = [p for p in pets if p.get("status") == params["status"]]
            if "limit" in params:
                pets = pets[: int(params["limit"])]
            self._send_json(pets)
            return

        if path.startswith("/api/v1/pets/"):
            pet_id = int(path.split("/")[-1])
            if pet_id in _PETS:
                self._send_json(_PETS[pet_id])
            else:
                self._send_json({"error": "not found"}, 404)
            return

        self._send_json({"error": "not found"}, 404)

    def do_POST(self):
        global _NEXT_ID
        path, _ = self._parse_path()

        if path == "/api/v1/pets":
            body = self._read_body()
            pet = {"id": _NEXT_ID, **body}
            _PETS[_NEXT_ID] = pet
            _NEXT_ID += 1
            self._send_json(pet, 201)
            return

        self._send_json({"error": "not found"}, 404)

    def do_PUT(self):
        path, _ = self._parse_path()

        if path.startswith("/api/v1/pets/"):
            pet_id = int(path.split("/")[-1])
            if pet_id in _PETS:
                body = self._read_body()
                _PETS[pet_id].update(body)
                self._send_json(_PETS[pet_id])
            else:
                self._send_json({"error": "not found"}, 404)
            return

        self._send_json({"error": "not found"}, 404)

    def do_DELETE(self):
        path, _ = self._parse_path()

        if path.startswith("/api/v1/pets/"):
            pet_id = int(path.split("/")[-1])
            if pet_id in _PETS:
                del _PETS[pet_id]
                self._send_json({"deleted": True})
            else:
                self._send_json({"error": "not found"}, 404)
            return

        self._send_json({"error": "not found"}, 404)


@pytest.fixture(scope="session")
def petstore_server():
    """Start a local petstore HTTP server for the test session."""
    # Reset store
    global _NEXT_ID
    _PETS.clear()
    _PETS.update({
        1: {"id": 1, "name": "Fido", "tag": "dog", "status": "available", "age": 3},
        2: {"id": 2, "name": "Whiskers", "tag": "cat", "status": "available", "age": 5},
        3: {"id": 3, "name": "Goldie", "tag": "fish", "status": "sold", "age": 1},
    })
    _NEXT_ID = 4

    server = HTTPServer(("127.0.0.1", 0), PetstoreHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{port}"
    server.shutdown()


@pytest.fixture
def petstore_spec():
    """Return the petstore spec dict."""
    return PETSTORE_SPEC


@pytest.fixture
def petstore_spec_with_refs():
    """Return the petstore spec with $ref."""
    return PETSTORE_SPEC_WITH_REFS


@pytest.fixture
def petstore_spec_file(tmp_path):
    """Write petstore spec to a temp file and return the path."""
    p = tmp_path / "petstore.json"
    p.write_text(json.dumps(PETSTORE_SPEC))
    return str(p)


@pytest.fixture
def petstore_yaml_file(tmp_path):
    """Write petstore spec as YAML to a temp file and return the path."""
    import yaml

    p = tmp_path / "petstore.yaml"
    p.write_text(yaml.dump(PETSTORE_SPEC))
    return str(p)


@pytest.fixture
def mcp_test_server_cmd():
    """Return the command to run the test MCP stdio server."""
    server_path = Path(__file__).parent / "mcp_test_server.py"
    return f"{sys.executable} {server_path}"


@pytest.fixture(autouse=True)
def _clean_cache(tmp_path, monkeypatch):
    """Redirect cache dir to tmp for all tests."""
    import mcp2cli

    monkeypatch.setattr(mcp2cli, "CACHE_DIR", tmp_path / "cache")


@pytest.fixture
def session_home(tmp_path, monkeypatch):
    """Point this process *and* spawned session daemons at a throwaway cache.

    The daemon is a fresh interpreter, so only the environment reaches it.
    """
    import mcp2cli

    cache = tmp_path / "session-cache"
    sessions = cache / "sessions"
    monkeypatch.setenv("MCP2CLI_CACHE_DIR", str(cache))
    monkeypatch.setattr(mcp2cli, "CACHE_DIR", cache)
    monkeypatch.setattr(mcp2cli, "SESSIONS_DIR", sessions)
    return sessions


@pytest.fixture
def started(session_home):
    """Names to tear down; set up after `session_home`, so torn down first."""
    import mcp2cli

    names: list[str] = []
    yield names
    for name in names:
        mcp2cli.session_stop(name)


# ---------------------------------------------------------------------------
# Realistic MCP tool definitions — modeled after a real task management
# server (Fulcrum, ~120 tools). These represent the JSON schemas that get
# injected into the system prompt when using native MCP tool injection.
# ---------------------------------------------------------------------------

REALISTIC_MCP_TOOLS = [
    {
        "name": "list_tasks",
        "description": "List all tasks with flexible filtering. Supports text search across title and description, status filtering, project filtering, and tag filtering.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "search": {"type": "string", "description": "Search text to filter tasks by title or description"},
                "status": {"type": "string", "enum": ["TO_DO", "IN_PROGRESS", "IN_REVIEW", "DONE", "CANCELED"], "description": "Filter by task status"},
                "project_id": {"type": "integer", "description": "Filter by project ID"},
                "tag": {"type": "string", "description": "Filter by tag name"},
                "limit": {"type": "integer", "description": "Maximum number of tasks to return"},
                "offset": {"type": "integer", "description": "Number of tasks to skip for pagination"},
                "sort_by": {"type": "string", "enum": ["created", "updated", "title", "status", "due_date"], "description": "Field to sort results by"},
                "sort_order": {"type": "string", "enum": ["asc", "desc"], "description": "Sort direction"},
            },
        },
    },
    {
        "name": "create_task",
        "description": "Create a new task. For worktree tasks, provide repoPath to create a git worktree. Supports setting priority, due dates, tags, and recurrence rules.",
        "inputSchema": {
            "type": "object",
            "required": ["title"],
            "properties": {
                "title": {"type": "string", "description": "Task title"},
                "type": {"type": "string", "enum": ["worktree", "scratch"], "description": "Task type"},
                "repo_path": {"type": "string", "description": "Path to git repository for worktree tasks"},
                "base_branch": {"type": "string", "description": "Base branch for worktree"},
                "branch": {"type": "string", "description": "Branch name override"},
                "prefix": {"type": "string", "description": "Branch name prefix"},
                "description": {"type": "string", "description": "Task description in markdown"},
                "status": {"type": "string", "enum": ["TO_DO", "IN_PROGRESS", "IN_REVIEW", "DONE", "CANCELED"], "description": "Initial task status"},
                "project_id": {"type": "integer", "description": "Project to assign task to"},
                "repository_id": {"type": "integer", "description": "Repository to link task to"},
                "tags": {"type": "array", "items": {"type": "string"}, "description": "Tags to apply to the task"},
                "due_date": {"type": "string", "description": "Due date in ISO 8601 format"},
                "time_estimate": {"type": "string", "description": "Time estimate (e.g. '2h', '30m')"},
                "priority": {"type": "string", "enum": ["high", "medium", "low"], "description": "Task priority level"},
                "recurrence_rule": {"type": "string", "enum": ["daily", "weekly", "biweekly", "monthly", "quarterly", "yearly"], "description": "Recurrence schedule"},
                "recurrence_end_date": {"type": "string", "description": "End date for recurrence"},
            },
        },
    },
    {
        "name": "update_task",
        "description": "Update task metadata including title, description, and recurrence settings.",
        "inputSchema": {
            "type": "object",
            "required": ["id"],
            "properties": {
                "id": {"type": "integer", "description": "Task ID"},
                "title": {"type": "string", "description": "New task title"},
                "description": {"type": "string", "description": "New task description"},
                "recurrence_rule": {"type": "string", "enum": ["daily", "weekly", "biweekly", "monthly", "quarterly", "yearly"], "description": "Recurrence schedule"},
                "recurrence_end_date": {"type": "string", "description": "End date for recurrence"},
            },
        },
    },
    {
        "name": "get_task",
        "description": "Get details of a specific task by ID, including dependencies and attachments.",
        "inputSchema": {
            "type": "object",
            "required": ["id"],
            "properties": {
                "id": {"type": "integer", "description": "Task ID"},
            },
        },
    },
    {
        "name": "delete_task",
        "description": "Delete a task and optionally its linked git worktree.",
        "inputSchema": {
            "type": "object",
            "required": ["id"],
            "properties": {
                "id": {"type": "integer", "description": "Task ID"},
                "delete_worktree": {"type": "boolean", "description": "Also delete the git worktree if one exists"},
            },
        },
    },
    {
        "name": "move_task",
        "description": "Move a task to a different status column.",
        "inputSchema": {
            "type": "object",
            "required": ["id", "status"],
            "properties": {
                "id": {"type": "integer", "description": "Task ID"},
                "status": {"type": "string", "enum": ["TO_DO", "IN_PROGRESS", "IN_REVIEW", "DONE", "CANCELED"], "description": "New status"},
            },
        },
    },
    {
        "name": "add_task_tag",
        "description": "Add a tag to a task for categorization. Returns similar existing tags if available.",
        "inputSchema": {
            "type": "object",
            "required": ["id", "tag"],
            "properties": {
                "id": {"type": "integer", "description": "Task ID"},
                "tag": {"type": "string", "description": "Tag name to add"},
            },
        },
    },
    {
        "name": "remove_task_tag",
        "description": "Remove a tag from a task.",
        "inputSchema": {
            "type": "object",
            "required": ["id", "tag"],
            "properties": {
                "id": {"type": "integer", "description": "Task ID"},
                "tag": {"type": "string", "description": "Tag name to remove"},
            },
        },
    },
    {
        "name": "set_task_due_date",
        "description": "Set or clear the due date for a task.",
        "inputSchema": {
            "type": "object",
            "required": ["id"],
            "properties": {
                "id": {"type": "integer", "description": "Task ID"},
                "due_date": {"type": "string", "description": "Due date in ISO 8601 format, or null to clear"},
            },
        },
    },
    {
        "name": "add_task_link",
        "description": "Add a URL link to a task for documentation, related PRs, or design files.",
        "inputSchema": {
            "type": "object",
            "required": ["id", "url"],
            "properties": {
                "id": {"type": "integer", "description": "Task ID"},
                "url": {"type": "string", "description": "URL to link"},
                "title": {"type": "string", "description": "Display title for the link"},
            },
        },
    },
    {
        "name": "add_task_dependency",
        "description": "Add a dependency between tasks. The task cannot start until the dependency is completed.",
        "inputSchema": {
            "type": "object",
            "required": ["task_id", "depends_on_id"],
            "properties": {
                "task_id": {"type": "integer", "description": "Task that depends on another"},
                "depends_on_id": {"type": "integer", "description": "Task that must be completed first"},
            },
        },
    },
    {
        "name": "get_task_dependency_graph",
        "description": "Get all tasks and their dependencies as a graph structure for visualization.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "integer", "description": "Filter graph to a specific project"},
            },
        },
    },
    {
        "name": "upload_task_attachment",
        "description": "Upload a file to a task from a local path. Supported types: PDF, images, text, and code files.",
        "inputSchema": {
            "type": "object",
            "required": ["task_id", "file_path"],
            "properties": {
                "task_id": {"type": "integer", "description": "Task ID"},
                "file_path": {"type": "string", "description": "Local file path to upload"},
                "description": {"type": "string", "description": "Description of the attachment"},
            },
        },
    },
    {
        "name": "list_projects",
        "description": "List all projects with optional filtering by status and search.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "search": {"type": "string", "description": "Search text to filter projects"},
                "status": {"type": "string", "enum": ["active", "archived"], "description": "Filter by project status"},
            },
        },
    },
    {
        "name": "create_project",
        "description": "Create a new project for organizing tasks and repositories.",
        "inputSchema": {
            "type": "object",
            "required": ["name"],
            "properties": {
                "name": {"type": "string", "description": "Project name"},
                "description": {"type": "string", "description": "Project description"},
                "color": {"type": "string", "description": "Project color (hex code)"},
                "repository_id": {"type": "integer", "description": "Link a repository to the project"},
            },
        },
    },
    {
        "name": "update_project",
        "description": "Update project metadata.",
        "inputSchema": {
            "type": "object",
            "required": ["id"],
            "properties": {
                "id": {"type": "integer", "description": "Project ID"},
                "name": {"type": "string", "description": "New project name"},
                "description": {"type": "string", "description": "New description"},
                "color": {"type": "string", "description": "New color"},
                "status": {"type": "string", "enum": ["active", "archived"], "description": "Project status"},
            },
        },
    },
    {
        "name": "list_repositories",
        "description": "List all configured repositories. Supports filtering for orphans not linked to any project.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "orphans_only": {"type": "boolean", "description": "Only show repositories not linked to a project"},
                "project_id": {"type": "integer", "description": "Filter by linked project"},
            },
        },
    },
    {
        "name": "add_repository",
        "description": "Add a repository from a local path. The path must be an existing git repository.",
        "inputSchema": {
            "type": "object",
            "required": ["path"],
            "properties": {
                "path": {"type": "string", "description": "Local path to git repository"},
                "name": {"type": "string", "description": "Display name for the repository"},
                "is_template": {"type": "boolean", "description": "Mark as a Copier template repository"},
            },
        },
    },
    {
        "name": "execute_command",
        "description": "Execute a CLI command on the remote server. Supports persistent sessions for stateful operations.",
        "inputSchema": {
            "type": "object",
            "required": ["command"],
            "properties": {
                "command": {"type": "string", "description": "Command to execute"},
                "session_id": {"type": "string", "description": "Session ID for persistent state"},
                "working_directory": {"type": "string", "description": "Working directory for the command"},
                "timeout": {"type": "integer", "description": "Command timeout in seconds"},
                "env": {"type": "object", "description": "Additional environment variables"},
            },
        },
    },
    {
        "name": "list_calendar_events",
        "description": "List calendar events within a date range across all calendars.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "start_date": {"type": "string", "description": "Start of date range (ISO 8601)"},
                "end_date": {"type": "string", "description": "End of date range (ISO 8601)"},
                "calendar_id": {"type": "string", "description": "Filter to specific calendar"},
                "search": {"type": "string", "description": "Search text to filter events"},
            },
        },
    },
    {
        "name": "create_calendar_event",
        "description": "Create a new calendar event.",
        "inputSchema": {
            "type": "object",
            "required": ["title", "start", "end"],
            "properties": {
                "title": {"type": "string", "description": "Event title"},
                "start": {"type": "string", "description": "Start time (ISO 8601)"},
                "end": {"type": "string", "description": "End time (ISO 8601)"},
                "description": {"type": "string", "description": "Event description"},
                "location": {"type": "string", "description": "Event location"},
                "calendar_id": {"type": "string", "description": "Calendar to create event in"},
                "all_day": {"type": "boolean", "description": "Whether this is an all-day event"},
            },
        },
    },
    {
        "name": "deploy_app",
        "description": "Deploy an application to the configured platform.",
        "inputSchema": {
            "type": "object",
            "required": ["app_id"],
            "properties": {
                "app_id": {"type": "string", "description": "Application ID"},
                "branch": {"type": "string", "description": "Git branch to deploy"},
                "force": {"type": "boolean", "description": "Force deployment even if checks fail"},
                "env_overrides": {"type": "object", "description": "Environment variable overrides for this deployment"},
            },
        },
    },
    {
        "name": "search",
        "description": "Full-text search across tasks, projects, repositories, and notes.",
        "inputSchema": {
            "type": "object",
            "required": ["query"],
            "properties": {
                "query": {"type": "string", "description": "Search query"},
                "types": {"type": "array", "items": {"type": "string", "enum": ["task", "project", "repository", "note"]}, "description": "Entity types to search"},
                "limit": {"type": "integer", "description": "Maximum results to return"},
            },
        },
    },
    {
        "name": "memory_store",
        "description": "Store a memory/note for later retrieval by the AI assistant.",
        "inputSchema": {
            "type": "object",
            "required": ["content"],
            "properties": {
                "content": {"type": "string", "description": "Memory content to store"},
                "tags": {"type": "array", "items": {"type": "string"}, "description": "Tags for categorization"},
                "category": {"type": "string", "description": "Memory category"},
            },
        },
    },
    {
        "name": "memory_search",
        "description": "Search stored memories using semantic or keyword search.",
        "inputSchema": {
            "type": "object",
            "required": ["query"],
            "properties": {
                "query": {"type": "string", "description": "Search query"},
                "limit": {"type": "integer", "description": "Maximum results"},
                "category": {"type": "string", "description": "Filter by category"},
            },
        },
    },
    {
        "name": "send_notification",
        "description": "Send a notification to all enabled notification channels.",
        "inputSchema": {
            "type": "object",
            "required": ["title", "message"],
            "properties": {
                "title": {"type": "string", "description": "Notification title"},
                "message": {"type": "string", "description": "Notification body"},
                "priority": {"type": "string", "enum": ["low", "normal", "high", "urgent"], "description": "Notification priority"},
            },
        },
    },
    {
        "name": "create_backup",
        "description": "Create a backup of the database and configuration.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Backup name"},
                "include_attachments": {"type": "boolean", "description": "Include file attachments in backup"},
            },
        },
    },
    {
        "name": "list_jobs",
        "description": "List all scheduled jobs and their status.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "status": {"type": "string", "enum": ["active", "disabled", "all"], "description": "Filter by job status"},
            },
        },
    },
    {
        "name": "create_job",
        "description": "Create a new scheduled job with a cron expression.",
        "inputSchema": {
            "type": "object",
            "required": ["name", "command", "schedule"],
            "properties": {
                "name": {"type": "string", "description": "Job name"},
                "command": {"type": "string", "description": "Command to execute"},
                "schedule": {"type": "string", "description": "Cron expression (e.g. '0 * * * *')"},
                "description": {"type": "string", "description": "Job description"},
                "enabled": {"type": "boolean", "description": "Whether job is enabled"},
                "timeout": {"type": "integer", "description": "Job timeout in seconds"},
                "notify_on_failure": {"type": "boolean", "description": "Send notification on failure"},
            },
        },
    },
    {
        "name": "search_tools",
        "description": "Search for available MCP tools by keyword or category. Use this to discover tools you don't know about.",
        "inputSchema": {
            "type": "object",
            "required": ["query"],
            "properties": {
                "query": {"type": "string", "description": "Search keyword or category"},
            },
        },
    },
]


# ---------------------------------------------------------------------------
# GraphQL test server
# ---------------------------------------------------------------------------

GRAPHQL_SCHEMA = {
    "queryType": {"name": "Query"},
    "mutationType": {"name": "Mutation"},
    "types": [
        {
            "kind": "OBJECT",
            "name": "Query",
            "fields": [
                {
                    "name": "users",
                    "description": "List all users",
                    "args": [],
                    "type": {
                        "kind": "NON_NULL",
                        "name": None,
                        "ofType": {
                            "kind": "LIST",
                            "name": None,
                            "ofType": {
                                "kind": "NON_NULL",
                                "name": None,
                                "ofType": {
                                    "kind": "OBJECT",
                                    "name": "User",
                                    "ofType": None,
                                },
                            },
                        },
                    },
                },
                {
                    "name": "user",
                    "description": "Get a user by ID",
                    "args": [
                        {
                            "name": "id",
                            "description": "User ID",
                            "type": {
                                "kind": "NON_NULL",
                                "name": None,
                                "ofType": {
                                    "kind": "SCALAR",
                                    "name": "ID",
                                    "ofType": None,
                                },
                            },
                            "defaultValue": None,
                        }
                    ],
                    "type": {
                        "kind": "OBJECT",
                        "name": "User",
                        "ofType": None,
                    },
                },
                {
                    "name": "usersByIds",
                    "description": "Get users by a list of IDs",
                    "args": [
                        {
                            "name": "ids",
                            "description": "User IDs",
                            "type": {
                                "kind": "NON_NULL",
                                "name": None,
                                "ofType": {
                                    "kind": "LIST",
                                    "name": None,
                                    "ofType": {
                                        "kind": "NON_NULL",
                                        "name": None,
                                        "ofType": {
                                            "kind": "SCALAR",
                                            "name": "ID",
                                            "ofType": None,
                                        },
                                    },
                                },
                            },
                            "defaultValue": None,
                        }
                    ],
                    "type": {
                        "kind": "NON_NULL",
                        "name": None,
                        "ofType": {
                            "kind": "LIST",
                            "name": None,
                            "ofType": {
                                "kind": "NON_NULL",
                                "name": None,
                                "ofType": {
                                    "kind": "OBJECT",
                                    "name": "User",
                                    "ofType": None,
                                },
                            },
                        },
                    },
                },
            ],
            "inputFields": None,
            "enumValues": None,
        },
        {
            "kind": "OBJECT",
            "name": "Mutation",
            "fields": [
                {
                    "name": "createUser",
                    "description": "Create a new user",
                    "args": [
                        {
                            "name": "name",
                            "description": "User name",
                            "type": {
                                "kind": "NON_NULL",
                                "name": None,
                                "ofType": {
                                    "kind": "SCALAR",
                                    "name": "String",
                                    "ofType": None,
                                },
                            },
                            "defaultValue": None,
                        },
                        {
                            "name": "email",
                            "description": "User email",
                            "type": {
                                "kind": "NON_NULL",
                                "name": None,
                                "ofType": {
                                    "kind": "SCALAR",
                                    "name": "String",
                                    "ofType": None,
                                },
                            },
                            "defaultValue": None,
                        },
                        {
                            "name": "age",
                            "description": "User age",
                            "type": {
                                "kind": "SCALAR",
                                "name": "Int",
                                "ofType": None,
                            },
                            "defaultValue": None,
                        },
                    ],
                    "type": {
                        "kind": "OBJECT",
                        "name": "User",
                        "ofType": None,
                    },
                },
                {
                    "name": "deleteUser",
                    "description": "Delete a user by ID",
                    "args": [
                        {
                            "name": "id",
                            "description": "User ID",
                            "type": {
                                "kind": "NON_NULL",
                                "name": None,
                                "ofType": {
                                    "kind": "SCALAR",
                                    "name": "ID",
                                    "ofType": None,
                                },
                            },
                            "defaultValue": None,
                        }
                    ],
                    "type": {
                        "kind": "SCALAR",
                        "name": "Boolean",
                        "ofType": None,
                    },
                },
            ],
            "inputFields": None,
            "enumValues": None,
        },
        {
            "kind": "OBJECT",
            "name": "User",
            "fields": [
                {
                    "name": "id",
                    "description": None,
                    "args": [],
                    "type": {
                        "kind": "NON_NULL",
                        "name": None,
                        "ofType": {
                            "kind": "SCALAR",
                            "name": "ID",
                            "ofType": None,
                        },
                    },
                },
                {
                    "name": "name",
                    "description": None,
                    "args": [],
                    "type": {
                        "kind": "NON_NULL",
                        "name": None,
                        "ofType": {
                            "kind": "SCALAR",
                            "name": "String",
                            "ofType": None,
                        },
                    },
                },
                {
                    "name": "email",
                    "description": None,
                    "args": [],
                    "type": {
                        "kind": "SCALAR",
                        "name": "String",
                        "ofType": None,
                    },
                },
                {
                    "name": "age",
                    "description": None,
                    "args": [],
                    "type": {
                        "kind": "SCALAR",
                        "name": "Int",
                        "ofType": None,
                    },
                },
                {
                    "name": "status",
                    "description": None,
                    "args": [],
                    "type": {
                        "kind": "ENUM",
                        "name": "Status",
                        "ofType": None,
                    },
                },
                {
                    "name": "address",
                    "description": None,
                    "args": [],
                    "type": {
                        "kind": "OBJECT",
                        "name": "Address",
                        "ofType": None,
                    },
                },
            ],
            "inputFields": None,
            "enumValues": None,
        },
        {
            "kind": "OBJECT",
            "name": "Address",
            "fields": [
                {
                    "name": "city",
                    "description": None,
                    "args": [],
                    "type": {
                        "kind": "SCALAR",
                        "name": "String",
                        "ofType": None,
                    },
                },
                {
                    "name": "country",
                    "description": None,
                    "args": [],
                    "type": {
                        "kind": "SCALAR",
                        "name": "String",
                        "ofType": None,
                    },
                },
            ],
            "inputFields": None,
            "enumValues": None,
        },
        {
            "kind": "ENUM",
            "name": "Status",
            "fields": None,
            "inputFields": None,
            "enumValues": [
                {"name": "ACTIVE", "description": None},
                {"name": "INACTIVE", "description": None},
                {"name": "BANNED", "description": None},
            ],
        },
        {
            "kind": "SCALAR",
            "name": "ID",
            "fields": None,
            "inputFields": None,
            "enumValues": None,
        },
        {
            "kind": "SCALAR",
            "name": "String",
            "fields": None,
            "inputFields": None,
            "enumValues": None,
        },
        {
            "kind": "SCALAR",
            "name": "Int",
            "fields": None,
            "inputFields": None,
            "enumValues": None,
        },
        {
            "kind": "SCALAR",
            "name": "Float",
            "fields": None,
            "inputFields": None,
            "enumValues": None,
        },
        {
            "kind": "SCALAR",
            "name": "Boolean",
            "fields": None,
            "inputFields": None,
            "enumValues": None,
        },
    ],
}

_USERS = {
    "1": {"id": "1", "name": "Alice", "email": "alice@example.com", "age": 30, "status": "ACTIVE", "address": {"city": "Portland", "country": "US"}},
    "2": {"id": "2", "name": "Bob", "email": "bob@example.com", "age": 25, "status": "INACTIVE", "address": {"city": "London", "country": "UK"}},
}
_NEXT_USER_ID = 3


def _extract_field_name(query: str) -> str | None:
    """Extract the top-level field name from a GraphQL query/mutation body."""
    # Match the first word after the operation-level opening brace
    m = re.search(r'\{\s*(\w+)', query)
    return m.group(1) if m else None


def _select_fields(obj, query: str):
    """Rough field selection based on query string."""
    if not isinstance(obj, dict):
        return obj
    # Extract field names from the selection set around our field
    # This is a simplified approach — find the innermost braces content
    # for the field we're returning
    field_name = _extract_field_name(query)
    if not field_name:
        return obj

    # Find the selection set for this field
    pattern = re.escape(field_name) + r'[^{]*\{([^}]+)\}'
    m = re.search(pattern, query)
    if not m:
        # Try nested braces (depth 2)
        pattern2 = re.escape(field_name) + r'[^{]*\{([^}]*\{[^}]*\}[^}]*)\}'
        m = re.search(pattern2, query)
    if not m:
        return obj

    selection_text = m.group(1)
    # Extract simple field names (not nested objects)
    selected = re.findall(r'\b(\w+)\b', selection_text)
    # Also find nested selections like "address { city country }"
    nested = re.findall(r'(\w+)\s*\{([^}]+)\}', selection_text)
    nested_names = {n[0] for n in nested}

    result = {}
    for key in selected:
        if key in obj and key not in nested_names:
            result[key] = obj[key]
    for name, inner in nested:
        if name in obj:
            if isinstance(obj[name], dict):
                inner_fields = re.findall(r'\b(\w+)\b', inner)
                result[name] = {k: v for k, v in obj[name].items() if k in inner_fields}
            else:
                result[name] = obj[name]
    return result


class GraphQLHandler(BaseHTTPRequestHandler):
    """HTTP handler that serves a mock GraphQL API."""

    def log_message(self, format, *args):
        pass

    def _send_json(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if length:
            return json.loads(self.rfile.read(length))
        return {}

    def do_POST(self):
        global _NEXT_USER_ID
        body = self._read_body()
        query = body.get("query", "")
        variables = body.get("variables") or {}

        # Introspection
        if "__schema" in query:
            self._send_json({"data": {"__schema": GRAPHQL_SCHEMA}})
            return

        field_name = _extract_field_name(query)

        if field_name == "users":
            users = list(_USERS.values())
            users = [_select_fields(u, query) for u in users]
            self._send_json({"data": {"users": users}})
            return

        if field_name == "user":
            uid = variables.get("id", "")
            user = _USERS.get(str(uid))
            if user:
                self._send_json({"data": {"user": _select_fields(user, query)}})
            else:
                self._send_json({"data": {"user": None}})
            return

        if field_name == "usersByIds":
            ids = variables.get("ids", [])
            users = [_USERS[str(i)] for i in ids if str(i) in _USERS]
            users = [_select_fields(u, query) for u in users]
            self._send_json({"data": {"usersByIds": users}})
            return

        if field_name == "createUser":
            user = {
                "id": str(_NEXT_USER_ID),
                "name": variables.get("name", ""),
                "email": variables.get("email", ""),
                "age": variables.get("age"),
                "status": "ACTIVE",
                "address": None,
            }
            _USERS[str(_NEXT_USER_ID)] = user
            _NEXT_USER_ID += 1
            self._send_json({"data": {"createUser": _select_fields(user, query)}})
            return

        if field_name == "deleteUser":
            uid = variables.get("id", "")
            deleted = str(uid) in _USERS
            if deleted:
                del _USERS[str(uid)]
            self._send_json({"data": {"deleteUser": deleted}})
            return

        self._send_json({"errors": [{"message": f"Unknown field: {field_name}"}]})


@pytest.fixture(scope="session")
def graphql_server():
    """Start a local GraphQL HTTP server for the test session."""
    global _NEXT_USER_ID
    _USERS.clear()
    _USERS.update({
        "1": {"id": "1", "name": "Alice", "email": "alice@example.com", "age": 30, "status": "ACTIVE", "address": {"city": "Portland", "country": "US"}},
        "2": {"id": "2", "name": "Bob", "email": "bob@example.com", "age": 25, "status": "INACTIVE", "address": {"city": "London", "country": "UK"}},
    })
    _NEXT_USER_ID = 3

    server = HTTPServer(("127.0.0.1", 0), GraphQLHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{port}"
    server.shutdown()


@pytest.fixture
def graphql_schema():
    """Return the GraphQL introspection schema dict."""
    return GRAPHQL_SCHEMA


@pytest.fixture(scope="session")
def mcp_http_server():
    """Start the Streamable HTTP MCP test server for the whole session.

    Yields the endpoint URL. A startup failure is a hard error, not a skip:
    silently skipping is how streamable-http coverage disappeared exactly when
    an SDK upgrade broke that transport (issues #68, #74).
    """
    import subprocess
    import time

    script = Path(__file__).parent / "_mcp_http_server.py"
    proc = subprocess.Popen(
        [sys.executable, str(script)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    port = None
    deadline = time.time() + 10
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"MCP HTTP server exited: {proc.stderr.read()}")
        line = proc.stdout.readline().strip()
        if line.startswith("PORT="):
            port = int(line.split("=")[1])
            break
    if port is None:
        proc.kill()
        raise RuntimeError("MCP HTTP server did not report a port within 10s")
    try:
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        proc.terminate()
        proc.wait(timeout=5)
