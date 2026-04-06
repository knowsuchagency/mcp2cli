"""Tests for usage-aware tool ranking (--sort, --top, --compact)."""

import json
import os
from pathlib import Path
from unittest import mock

import pytest

from mcp2cli import (
    USAGE_FILE,
    CommandDef,
    _load_usage,
    _resolve_sort_mode,
    _save_usage,
    _source_hash_for,
    list_mcp_commands,
    list_openapi_commands,
    list_graphql_commands,
    record_usage,
    sort_commands,
)


@pytest.fixture(autouse=True)
def tmp_usage_file(tmp_path, monkeypatch):
    """Redirect USAGE_FILE to a temp location for every test."""
    usage_path = tmp_path / "usage.json"
    monkeypatch.setattr("mcp2cli.USAGE_FILE", usage_path)
    monkeypatch.setattr("mcp2cli.CACHE_DIR", tmp_path)
    return usage_path


# ---------------------------------------------------------------------------
# _load_usage / _save_usage
# ---------------------------------------------------------------------------


class TestLoadSaveUsage:
    def test_load_empty(self, tmp_usage_file):
        assert _load_usage() == {}

    def test_roundtrip(self, tmp_usage_file):
        data = {"abc123": {"my-tool": {"count": 5, "last_used": "2026-01-01T00:00:00+00:00"}}}
        _save_usage(data)
        assert _load_usage() == data

    def test_load_corrupt_json(self, tmp_usage_file):
        tmp_usage_file.write_text("{bad json")
        assert _load_usage() == {}


# ---------------------------------------------------------------------------
# record_usage
# ---------------------------------------------------------------------------


class TestRecordUsage:
    def test_first_call(self, tmp_usage_file):
        record_usage("src1", "tool-a")
        usage = _load_usage()
        assert usage["src1"]["tool-a"]["count"] == 1
        assert usage["src1"]["tool-a"]["last_used"] != ""

    def test_increment(self, tmp_usage_file):
        record_usage("src1", "tool-a")
        record_usage("src1", "tool-a")
        record_usage("src1", "tool-a")
        assert _load_usage()["src1"]["tool-a"]["count"] == 3

    def test_multiple_tools(self, tmp_usage_file):
        record_usage("src1", "tool-a")
        record_usage("src1", "tool-b")
        record_usage("src1", "tool-a")
        usage = _load_usage()
        assert usage["src1"]["tool-a"]["count"] == 2
        assert usage["src1"]["tool-b"]["count"] == 1

    def test_multiple_sources(self, tmp_usage_file):
        record_usage("src1", "tool-a")
        record_usage("src2", "tool-a")
        usage = _load_usage()
        assert usage["src1"]["tool-a"]["count"] == 1
        assert usage["src2"]["tool-a"]["count"] == 1


# ---------------------------------------------------------------------------
# _source_hash_for
# ---------------------------------------------------------------------------


class TestSourceHash:
    def test_deterministic(self):
        h1 = _source_hash_for("http://example.com")
        h2 = _source_hash_for("http://example.com")
        assert h1 == h2

    def test_different_sources(self):
        h1 = _source_hash_for("http://example.com")
        h2 = _source_hash_for("http://other.com")
        assert h1 != h2

    def test_length(self):
        assert len(_source_hash_for("anything")) == 16


# ---------------------------------------------------------------------------
# sort_commands
# ---------------------------------------------------------------------------


def _make_commands(names):
    """Helper to create a list of CommandDef with given names."""
    return [CommandDef(name=n, tool_name=n, description=f"desc-{n}") for n in names]


class TestSortCommands:
    def test_default_preserves_order(self, tmp_usage_file):
        cmds = _make_commands(["c", "a", "b"])
        result = sort_commands(cmds, "default", "src1")
        assert [c.name for c in result] == ["c", "a", "b"]

    def test_alpha(self, tmp_usage_file):
        cmds = _make_commands(["c", "a", "b"])
        result = sort_commands(cmds, "alpha", "src1")
        assert [c.name for c in result] == ["a", "b", "c"]

    def test_usage_sort(self, tmp_usage_file):
        record_usage("src1", "b")
        record_usage("src1", "b")
        record_usage("src1", "b")
        record_usage("src1", "a")
        cmds = _make_commands(["a", "b", "c"])
        result = sort_commands(cmds, "usage", "src1")
        assert [c.name for c in result] == ["b", "a", "c"]

    def test_recent_sort(self, tmp_usage_file):
        record_usage("src1", "a")
        record_usage("src1", "c")
        record_usage("src1", "b")  # b is most recent
        cmds = _make_commands(["a", "b", "c"])
        result = sort_commands(cmds, "recent", "src1")
        # b was recorded last, so it should come first
        assert result[0].name == "b"

    def test_usage_no_data_preserves_order(self, tmp_usage_file):
        cmds = _make_commands(["c", "a", "b"])
        result = sort_commands(cmds, "usage", "src1")
        assert [c.name for c in result] == ["c", "a", "b"]


# ---------------------------------------------------------------------------
# _resolve_sort_mode
# ---------------------------------------------------------------------------


class TestResolveSortMode:
    def test_explicit_overrides(self, tmp_usage_file):
        assert _resolve_sort_mode("alpha", "src1") == "alpha"

    def test_default_no_data(self, tmp_usage_file):
        assert _resolve_sort_mode(None, "src1") == "default"

    def test_default_with_data(self, tmp_usage_file):
        record_usage("src1", "tool-a")
        assert _resolve_sort_mode(None, "src1") == "usage"


# ---------------------------------------------------------------------------
# list_mcp_commands with new params
# ---------------------------------------------------------------------------


class TestListMcpCommandsCompact:
    def test_compact_output(self, capsys, tmp_usage_file):
        cmds = _make_commands(["tool-a", "tool-b", "tool-c"])
        list_mcp_commands(cmds, compact=True)
        out = capsys.readouterr().out.strip()
        assert out == "tool-a tool-b tool-c"

    def test_top_n(self, capsys, tmp_usage_file):
        cmds = _make_commands(["a", "b", "c", "d", "e"])
        list_mcp_commands(cmds, top=3)
        out = capsys.readouterr().out
        # Should only show 3 tools
        lines = [l for l in out.strip().split("\n") if l.strip()]
        assert len(lines) == 3

    def test_compact_with_top(self, capsys, tmp_usage_file):
        cmds = _make_commands(["a", "b", "c", "d", "e"])
        list_mcp_commands(cmds, compact=True, top=2)
        out = capsys.readouterr().out.strip()
        assert out == "a b"

    def test_sort_and_top(self, capsys, tmp_usage_file):
        record_usage("src1", "c")
        record_usage("src1", "c")
        record_usage("src1", "c")
        record_usage("src1", "b")
        record_usage("src1", "b")
        record_usage("src1", "a")
        cmds = _make_commands(["a", "b", "c"])
        list_mcp_commands(
            cmds, compact=True, source_hash="src1", sort_mode="usage", top=2
        )
        out = capsys.readouterr().out.strip()
        assert out == "c b"


class TestListOpenApiCommandsCompact:
    def test_compact_output(self, capsys, tmp_usage_file):
        cmds = [
            CommandDef(name="get-users", method="get", description="Get users"),
            CommandDef(name="get-items", method="get", description="Get items"),
        ]
        list_openapi_commands(cmds, compact=True)
        out = capsys.readouterr().out.strip()
        assert out == "get-users get-items"


class TestListGraphqlCommandsCompact:
    def test_compact_output(self, capsys, tmp_usage_file):
        cmds = [
            CommandDef(
                name="get-user",
                graphql_operation_type="query",
                graphql_field_name="getUser",
                description="Get a user",
            ),
            CommandDef(
                name="create-user",
                graphql_operation_type="mutation",
                graphql_field_name="createUser",
                description="Create a user",
            ),
        ]
        list_graphql_commands(cmds, compact=True)
        out = capsys.readouterr().out.strip()
        assert out == "get-user create-user"
