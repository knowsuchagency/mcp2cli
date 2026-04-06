"""Tests for helper functions and data structures."""

import argparse
import json
import shutil

from mcp2cli import (
    ParamDef,
    CommandDef,
    _apply_head,
    _collect_openapi_params,
    _find_toon_cli,
    _split_at_subcommand,
    _toon_encode,
    cache_key_for,
    coerce_value,
    extract_mcp_commands,
    extract_openapi_commands,
    load_cached,
    output_result,
    resolve_refs,
    save_cache,
    schema_type_to_python,
    to_kebab,
)


class TestSchemaTypeToPython:
    def test_integer(self):
        assert schema_type_to_python({"type": "integer"}) == (int, "")

    def test_number(self):
        assert schema_type_to_python({"type": "number"}) == (float, "")

    def test_boolean(self):
        assert schema_type_to_python({"type": "boolean"}) == (None, "")

    def test_string(self):
        assert schema_type_to_python({"type": "string"}) == (str, "")

    def test_array(self):
        py_type, suffix = schema_type_to_python({"type": "array"})
        assert py_type is str
        assert "JSON array" in suffix

    def test_object(self):
        py_type, suffix = schema_type_to_python({"type": "object"})
        assert py_type is str
        assert "JSON object" in suffix

    def test_missing_type(self):
        assert schema_type_to_python({}) == (str, "")


class TestCoerceValue:
    def test_none(self):
        assert coerce_value(None, {"type": "string"}) is None

    def test_integer(self):
        assert coerce_value("42", {"type": "integer"}) == 42

    def test_number(self):
        assert coerce_value("3.14", {"type": "number"}) == 3.14

    def test_boolean(self):
        assert coerce_value(True, {"type": "boolean"}) is True

    def test_array_json(self):
        assert coerce_value('[1, 2, 3]', {"type": "array"}) == [1, 2, 3]

    def test_object_json(self):
        assert coerce_value('{"a": 1}', {"type": "object"}) == {"a": 1}

    def test_invalid_json_passthrough(self):
        # Non-JSON strings are now wrapped as single-element arrays for array type
        assert coerce_value("not json", {"type": "array"}) == ["not json"]

    def test_string(self):
        assert coerce_value("hello", {"type": "string"}) == "hello"

    def test_array_comma_separated(self):
        assert coerce_value("TO_DO,IN_PROGRESS", {"type": "array"}) == [
            "TO_DO",
            "IN_PROGRESS",
        ]

    def test_array_single_value(self):
        assert coerce_value("TO_DO", {"type": "array"}) == ["TO_DO"]

    def test_array_json_passthrough(self):
        assert coerce_value('["TO_DO","IN_PROGRESS"]', {"type": "array"}) == [
            "TO_DO",
            "IN_PROGRESS",
        ]

    def test_array_already_list(self):
        assert coerce_value(["a", "b"], {"type": "array"}) == ["a", "b"]

    def test_array_number_items(self):
        assert coerce_value(
            "1,2,3", {"type": "array", "items": {"type": "number"}}
        ) == [1.0, 2.0, 3.0]

    def test_array_integer_items(self):
        assert coerce_value(
            "1,2,3", {"type": "array", "items": {"type": "integer"}}
        ) == [1, 2, 3]

    def test_array_boolean_items(self):
        assert coerce_value(
            "true,false", {"type": "array", "items": {"type": "boolean"}}
        ) == [True, False]

    def test_non_array_unaffected(self):
        assert coerce_value("hello", {"type": "string"}) == "hello"

    def test_schemaless_json_object(self):
        """JSON object string is parsed when schema has no type."""
        assert coerce_value('{"key": "val"}', {}) == {"key": "val"}

    def test_schemaless_json_array(self):
        """JSON array string is parsed when schema has no type."""
        assert coerce_value('[1, 2, 3]', {}) == [1, 2, 3]

    def test_schemaless_plain_string(self):
        """Plain string stays a string when schema has no type."""
        assert coerce_value("hello", {}) == "hello"

    def test_schemaless_invalid_json(self):
        """String that looks like JSON but isn't stays as string."""
        assert coerce_value("{not valid json", {}) == "{not valid json"


class TestToKebab:
    def test_camel_case(self):
        assert to_kebab("findPetsByStatus") == "find-pets-by-status"

    def test_underscores(self):
        assert to_kebab("list_items") == "list-items"

    def test_already_kebab(self):
        assert to_kebab("list-items") == "list-items"

    def test_mixed(self):
        assert to_kebab("getHTTPResponse") == "get-httpresponse"


class TestOutputResult:
    def test_raw_string(self, capsys):
        output_result("hello", raw=True)
        assert capsys.readouterr().out.strip() == "hello"

    def test_raw_dict(self, capsys):
        output_result({"a": 1}, raw=True)
        assert json.loads(capsys.readouterr().out) == {"a": 1}

    def test_pretty_dict(self, capsys):
        output_result({"a": 1}, pretty=True)
        out = capsys.readouterr().out
        assert '"a": 1' in out
        assert "\n" in out  # indented

    def test_json_string_parsed(self, capsys):
        output_result('{"x": 2}', pretty=True)
        out = capsys.readouterr().out
        assert '"x": 2' in out

    def test_non_json_string(self, capsys):
        output_result("plain text", pretty=True)
        assert capsys.readouterr().out.strip() == "plain text"

    def test_toon_flag_with_cli(self, capsys, monkeypatch):
        """--toon encodes output as TOON when the CLI is available."""
        def fake_toon_encode(json_str):
            data = json.loads(json_str)
            # Simulate TOON output for a simple uniform array
            return "name: Alice\nage: 30\n"

        monkeypatch.setattr("mcp2cli._toon_encode", fake_toon_encode)
        output_result({"name": "Alice", "age": 30}, toon=True)
        out = capsys.readouterr().out
        assert "Alice" in out
        assert "{" not in out  # should NOT be JSON

    def test_toon_flag_fallback_when_unavailable(self, capsys, monkeypatch):
        """--toon falls back to JSON with a warning when the CLI is missing."""
        monkeypatch.setattr("mcp2cli._toon_encode", lambda s: None)
        output_result({"a": 1}, toon=True, pretty=True)
        captured = capsys.readouterr()
        assert '"a": 1' in captured.out  # fell back to JSON
        assert "TOON CLI" in captured.err  # printed warning

    def test_head_truncates_list(self, capsys):
        output_result([{"a": 1}, {"a": 2}, {"a": 3}], head=2, pretty=True)
        out = json.loads(capsys.readouterr().out)
        assert len(out) == 2

    def test_head_dict_passthrough(self, capsys):
        """--head on a dict passes through unchanged."""
        output_result({"a": 1, "b": 2}, head=1, pretty=True)
        out = json.loads(capsys.readouterr().out)
        assert out == {"a": 1, "b": 2}

class TestApplyHead:
    def test_list_truncation(self):
        assert _apply_head([1, 2, 3, 4, 5], 3) == [1, 2, 3]

    def test_dict_passthrough(self):
        d = {"a": 1}
        assert _apply_head(d, 2) is d

    def test_empty_list(self):
        assert _apply_head([], 5) == []

    def test_head_larger_than_list(self):
        assert _apply_head([1, 2], 10) == [1, 2]


class TestToonEncode:
    def test_find_toon_cli_npx(self, monkeypatch):
        """Falls back to npx when toon binary isn't in PATH."""
        original_which = shutil.which
        def mock_which(cmd):
            if cmd == "toon":
                return None
            return original_which(cmd)
        monkeypatch.setattr(shutil, "which", mock_which)
        result = _find_toon_cli()
        # Either npx is available or None
        if shutil.which("npx") is not None:
            assert result == "npx @toon-format/cli"
        else:
            assert result is None

    def test_toon_encode_uniform_array(self):
        """TOON CLI encodes a uniform array into tabular format."""
        cli = _find_toon_cli()
        if cli is None:
            import pytest
            pytest.skip("TOON CLI not available")
        data = json.dumps([
            {"name": "Alice", "age": 30},
            {"name": "Bob", "age": 25},
        ])
        result = _toon_encode(data)
        assert result is not None
        assert "Alice" in result
        assert "Bob" in result
        # Should NOT look like JSON (TOON uses {fields} in headers, so check for JSON-specific patterns)
        assert '"name"' not in result

    def test_toon_encode_single_object(self):
        """TOON CLI encodes a single object in YAML-like format."""
        cli = _find_toon_cli()
        if cli is None:
            import pytest
            pytest.skip("TOON CLI not available")
        data = json.dumps({"status": "ok", "count": 42})
        result = _toon_encode(data)
        assert result is not None
        assert "ok" in result
        assert "42" in result


class TestCaching:
    def test_roundtrip(self, tmp_path, monkeypatch):
        import mcp2cli

        monkeypatch.setattr(mcp2cli, "CACHE_DIR", tmp_path)
        data = {"foo": "bar"}
        save_cache("testkey", data)
        assert load_cached("testkey", 3600) == data

    def test_expired(self, tmp_path, monkeypatch):
        import mcp2cli
        import time

        monkeypatch.setattr(mcp2cli, "CACHE_DIR", tmp_path)
        save_cache("testkey", {"a": 1})
        # Set mtime in the past
        cache_file = tmp_path / "testkey.json"
        import os

        old_time = time.time() - 7200
        os.utime(cache_file, (old_time, old_time))
        assert load_cached("testkey", 3600) is None

    def test_missing(self, tmp_path, monkeypatch):
        import mcp2cli

        monkeypatch.setattr(mcp2cli, "CACHE_DIR", tmp_path)
        assert load_cached("nonexistent", 3600) is None

    def test_cache_key_deterministic(self):
        config = {"url": "https://example.com/spec.json"}
        k1 = cache_key_for(config)
        k2 = cache_key_for(config)
        assert k1 == k2

    def test_cache_key_different(self):
        k1 = cache_key_for({"url": "https://example.com/a.json"})
        k2 = cache_key_for({"url": "https://example.com/b.json"})
        assert k1 != k2

    def test_cache_key_ignores_excluded_fields(self):
        k1 = cache_key_for({"url": "https://example.com/spec.json", "cache_ttl": 60})
        k2 = cache_key_for({"url": "https://example.com/spec.json", "cache_ttl": 3600})
        assert k1 == k2


class TestResolveRefs:
    def test_simple_ref(self, petstore_spec_with_refs):
        resolved = resolve_refs(petstore_spec_with_refs)
        params = resolved["paths"]["/pets"]["get"]["parameters"]
        assert len(params) == 1
        assert params[0]["name"] == "limit"
        assert "$ref" not in params[0]

    def test_no_refs_unchanged(self, petstore_spec):
        resolved = resolve_refs(petstore_spec)
        assert resolved["paths"]["/pets"]["get"]["operationId"] == "listPets"

    def test_circular_ref_safe(self):
        spec = {
            "a": {"$ref": "#/b"},
            "b": {"$ref": "#/a"},
        }
        resolved = resolve_refs(spec)
        # Should not infinite loop — returns node with $ref intact
        assert "$ref" in resolved["a"] or "$ref" in resolved["b"]


class TestExtractOpenAPICommands:
    def test_command_count(self, petstore_spec):
        cmds = extract_openapi_commands(petstore_spec)
        # listPets, createPet, getPet, deletePet, updatePet
        assert len(cmds) == 5

    def test_command_names(self, petstore_spec):
        cmds = extract_openapi_commands(petstore_spec)
        names = {c.name for c in cmds}
        assert "list-pets" in names
        assert "create-pet" in names
        assert "get-pet" in names
        assert "delete-pet" in names
        assert "update-pet" in names

    def test_list_pets_params(self, petstore_spec):
        cmds = extract_openapi_commands(petstore_spec)
        list_pets = next(c for c in cmds if c.name == "list-pets")
        assert list_pets.method == "get"
        assert list_pets.path == "/pets"
        param_names = {p.name for p in list_pets.params}
        assert "limit" in param_names
        assert "status" in param_names

    def test_create_pet_body(self, petstore_spec):
        cmds = extract_openapi_commands(petstore_spec)
        create = next(c for c in cmds if c.name == "create-pet")
        assert create.has_body
        assert create.method == "post"
        body_params = [p for p in create.params if p.location == "body"]
        assert len(body_params) == 3  # name, tag, age

    def test_path_param(self, petstore_spec):
        cmds = extract_openapi_commands(petstore_spec)
        get_pet = next(c for c in cmds if c.name == "get-pet")
        path_params = [p for p in get_pet.params if p.location == "path"]
        assert len(path_params) == 1
        assert path_params[0].original_name == "petId"

    def test_enum_choices(self, petstore_spec):
        cmds = extract_openapi_commands(petstore_spec)
        list_pets = next(c for c in cmds if c.name == "list-pets")
        status_param = next(p for p in list_pets.params if p.name == "status")
        assert status_param.choices == ["available", "pending", "sold"]

    def test_schema_preserved_on_params(self, petstore_spec):
        """Extracted OpenAPI commands preserve schema on ParamDef."""
        cmds = extract_openapi_commands(petstore_spec)
        create = next(c for c in cmds if c.name == "create-pet")
        name_param = next(p for p in create.params if p.original_name == "name")
        assert name_param.schema.get("type") == "string"
        age_param = next(p for p in create.params if p.original_name == "age")
        assert age_param.schema.get("type") == "integer"
        # Query param schema
        list_pets = next(c for c in cmds if c.name == "list-pets")
        limit_param = next(p for p in list_pets.params if p.original_name == "limit")
        assert limit_param.schema.get("type") == "integer"


class TestExtractMCPCommands:
    def test_basic(self):
        tools = [
            {
                "name": "echo",
                "description": "Echo back",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "message": {"type": "string", "description": "Msg"},
                    },
                    "required": ["message"],
                },
            }
        ]
        cmds = extract_mcp_commands(tools)
        assert len(cmds) == 1
        assert cmds[0].name == "echo"
        assert cmds[0].tool_name == "echo"
        assert len(cmds[0].params) == 1
        assert cmds[0].params[0].required

    def test_underscore_to_kebab(self):
        tools = [
            {
                "name": "list_items",
                "description": "List",
                "inputSchema": {"type": "object", "properties": {}},
            }
        ]
        cmds = extract_mcp_commands(tools)
        assert cmds[0].name == "list-items"
        assert cmds[0].tool_name == "list_items"


class TestSplitAtSubcommand:
    """Tests for _split_at_subcommand() — GH #15."""

    @staticmethod
    def _pre():
        """Build a pre-parser with representative global options."""
        pre = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
        pre.add_argument("--spec", default=None)
        pre.add_argument("--mcp", default=None)
        pre.add_argument("--env", action="append", default=[])
        pre.add_argument("--pretty", action="store_true")
        pre.add_argument("--refresh", action="store_true")
        pre.add_argument("--cache-ttl", type=int, default=3600)
        pre.add_argument("--auth-header", action="append", default=[])
        pre.add_argument("--version", action="version", version="test")
        return pre

    def test_basic_split(self):
        g, t = _split_at_subcommand(
            ["--mcp", "http://server", "my-tool", "--env", "prod"], self._pre()
        )
        assert g == ["--mcp", "http://server"]
        assert t == ["my-tool", "--env", "prod"]

    def test_no_subcommand(self):
        g, t = _split_at_subcommand(
            ["--mcp", "http://server", "--pretty"], self._pre()
        )
        assert g == ["--mcp", "http://server", "--pretty"]
        assert t == []

    def test_boolean_flag_before_subcommand(self):
        g, t = _split_at_subcommand(
            ["--pretty", "--mcp", "http://server", "tool"], self._pre()
        )
        assert g == ["--pretty", "--mcp", "http://server"]
        assert t == ["tool"]

    def test_append_option_repeated(self):
        g, t = _split_at_subcommand(
            ["--env", "FOO=BAR", "--env", "BAZ=QUX", "tool"], self._pre()
        )
        assert g == ["--env", "FOO=BAR", "--env", "BAZ=QUX"]
        assert t == ["tool"]

    def test_double_dash_separator(self):
        g, t = _split_at_subcommand(
            ["--mcp", "http://server", "--", "tool", "--env", "x"], self._pre()
        )
        assert g == ["--mcp", "http://server"]
        assert t == ["tool", "--env", "x"]

    def test_equals_form(self):
        g, t = _split_at_subcommand(["--mcp=http://server", "tool"], self._pre())
        assert g == ["--mcp=http://server"]
        assert t == ["tool"]

    def test_unknown_option_before_subcommand(self):
        g, t = _split_at_subcommand(["--unknown", "tool"], self._pre())
        assert g == ["--unknown"]
        assert t == ["tool"]

    def test_empty_argv(self):
        g, t = _split_at_subcommand([], self._pre())
        assert g == []
        assert t == []

    def test_value_not_treated_as_subcommand(self):
        g, t = _split_at_subcommand(["--mcp", "http://server"], self._pre())
        assert g == ["--mcp", "http://server"]
        assert t == []

    def test_tool_env_preserved(self):
        """Tool --env must not end up in the global portion."""
        g, t = _split_at_subcommand(
            ["--mcp", "http://s", "deploy", "--env", "production"], self._pre()
        )
        assert g == ["--mcp", "http://s"]
        assert t == ["deploy", "--env", "production"]

    def test_head_value_not_treated_as_subcommand(self):
        """--head's numeric value should not be mistaken for a subcommand."""
        pre = self._pre()
        pre.add_argument("--head", type=int, default=None)
        g, t = _split_at_subcommand(
            ["--mcp", "http://s", "--head", "5", "my-tool"], pre
        )
        assert g == ["--mcp", "http://s", "--head", "5"]
        assert t == ["my-tool"]


class TestCollectOpenAPIParams:
    def test_body_params_coerced_object(self):
        """Body params with object schema get JSON-parsed."""
        cmd = CommandDef(
            name="create-item",
            method="post",
            path="/items",
            has_body=True,
            params=[
                ParamDef(
                    name="metadata",
                    original_name="metadata",
                    python_type=str,
                    location="body",
                    schema={"type": "object"},
                ),
                ParamDef(
                    name="count",
                    original_name="count",
                    python_type=str,
                    location="body",
                    schema={"type": "integer"},
                ),
            ],
        )
        args = argparse.Namespace(
            metadata='{"key": "val"}',
            count="42",
            stdin=False,
        )
        _, _, _, body, _ = _collect_openapi_params(cmd, args)
        assert body == {"metadata": {"key": "val"}, "count": 42}

    def test_body_params_coerced_array(self):
        """Body params with array schema get JSON-parsed."""
        cmd = CommandDef(
            name="create-item",
            method="post",
            path="/items",
            has_body=True,
            params=[
                ParamDef(
                    name="tags",
                    original_name="tags",
                    python_type=str,
                    location="body",
                    schema={"type": "array"},
                ),
            ],
        )
        args = argparse.Namespace(tags='["a","b"]', stdin=False)
        _, _, _, body, _ = _collect_openapi_params(cmd, args)
        assert body == {"tags": ["a", "b"]}

    def test_query_params_coerced(self):
        """Query params get coerced (e.g., integer)."""
        cmd = CommandDef(
            name="list-items",
            method="get",
            path="/items",
            params=[
                ParamDef(
                    name="limit",
                    original_name="limit",
                    python_type=int,
                    location="query",
                    schema={"type": "integer"},
                ),
            ],
        )
        args = argparse.Namespace(limit="10")
        _, query, _, _, _ = _collect_openapi_params(cmd, args)
        assert query == {"limit": 10}

    def test_body_schemaless_json_object(self):
        """Body params without schema type still parse JSON objects."""
        cmd = CommandDef(
            name="run-script",
            method="post",
            path="/run",
            has_body=True,
            params=[
                ParamDef(
                    name="args",
                    original_name="args",
                    python_type=str,
                    location="body",
                    schema={},
                ),
            ],
        )
        args = argparse.Namespace(args='{"key": "value"}', stdin=False)
        _, _, _, body, _ = _collect_openapi_params(cmd, args)
        assert body == {"args": {"key": "value"}}
