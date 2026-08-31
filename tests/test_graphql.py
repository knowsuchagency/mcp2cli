"""Tests for GraphQL support."""

import argparse
import json
import subprocess
import sys

import pytest

import mcp2cli


# ---------------------------------------------------------------------------
# Unit tests: type mapping
# ---------------------------------------------------------------------------


class TestGraphQLTypeMapping:
    def _types(self):
        return {
            "String": {"kind": "SCALAR", "name": "String"},
            "Int": {"kind": "SCALAR", "name": "Int"},
            "Status": {
                "kind": "ENUM",
                "name": "Status",
                "enumValues": [
                    {"name": "ACTIVE"},
                    {"name": "INACTIVE"},
                    {"name": "BANNED"},
                ],
            },
        }

    def test_string(self):
        ref = {"kind": "SCALAR", "name": "String", "ofType": None}
        py, req, choices = mcp2cli.graphql_type_to_python(ref, self._types())
        assert py is str
        assert req is False
        assert choices is None

    def test_int(self):
        ref = {"kind": "SCALAR", "name": "Int", "ofType": None}
        py, req, choices = mcp2cli.graphql_type_to_python(ref, self._types())
        assert py is int
        assert req is False

    def test_float(self):
        ref = {"kind": "SCALAR", "name": "Float", "ofType": None}
        py, req, choices = mcp2cli.graphql_type_to_python(ref, self._types())
        assert py is float

    def test_boolean(self):
        ref = {"kind": "SCALAR", "name": "Boolean", "ofType": None}
        py, req, choices = mcp2cli.graphql_type_to_python(ref, self._types())
        assert py is None  # store_true

    def test_id(self):
        ref = {"kind": "SCALAR", "name": "ID", "ofType": None}
        py, req, choices = mcp2cli.graphql_type_to_python(ref, self._types())
        assert py is str

    def test_non_null_string(self):
        ref = {
            "kind": "NON_NULL",
            "name": None,
            "ofType": {"kind": "SCALAR", "name": "String", "ofType": None},
        }
        py, req, choices = mcp2cli.graphql_type_to_python(ref, self._types())
        assert py is str
        assert req is True

    def test_enum(self):
        ref = {"kind": "ENUM", "name": "Status", "ofType": None}
        py, req, choices = mcp2cli.graphql_type_to_python(ref, self._types())
        assert py is str
        assert choices == ["ACTIVE", "INACTIVE", "BANNED"]

    def test_list(self):
        ref = {
            "kind": "LIST",
            "name": None,
            "ofType": {"kind": "SCALAR", "name": "Int", "ofType": None},
        }
        py, req, choices = mcp2cli.graphql_type_to_python(ref, self._types())
        assert py is str  # list → str (JSON/comma)
        assert req is False

    def test_non_null_list(self):
        ref = {
            "kind": "NON_NULL",
            "name": None,
            "ofType": {
                "kind": "LIST",
                "name": None,
                "ofType": {
                    "kind": "NON_NULL",
                    "name": None,
                    "ofType": {"kind": "SCALAR", "name": "String", "ofType": None},
                },
            },
        }
        py, req, choices = mcp2cli.graphql_type_to_python(ref, self._types())
        assert py is str
        assert req is True

    def test_input_object(self):
        ref = {"kind": "INPUT_OBJECT", "name": "UserInput", "ofType": None}
        py, req, choices = mcp2cli.graphql_type_to_python(ref, self._types())
        assert py is str


# ---------------------------------------------------------------------------
# Unit tests: type string reconstruction
# ---------------------------------------------------------------------------


class TestGraphQLTypeString:
    def test_simple_string(self):
        ref = {"kind": "SCALAR", "name": "String", "ofType": None}
        assert mcp2cli._graphql_type_string(ref) == "String"

    def test_non_null(self):
        ref = {
            "kind": "NON_NULL",
            "name": None,
            "ofType": {"kind": "SCALAR", "name": "String", "ofType": None},
        }
        assert mcp2cli._graphql_type_string(ref) == "String!"

    def test_list_int(self):
        ref = {
            "kind": "LIST",
            "name": None,
            "ofType": {"kind": "SCALAR", "name": "Int", "ofType": None},
        }
        assert mcp2cli._graphql_type_string(ref) == "[Int]"

    def test_non_null_list_non_null_string(self):
        ref = {
            "kind": "NON_NULL",
            "name": None,
            "ofType": {
                "kind": "LIST",
                "name": None,
                "ofType": {
                    "kind": "NON_NULL",
                    "name": None,
                    "ofType": {"kind": "SCALAR", "name": "String", "ofType": None},
                },
            },
        }
        assert mcp2cli._graphql_type_string(ref) == "[String!]!"


# ---------------------------------------------------------------------------
# Unit tests: selection set builder
# ---------------------------------------------------------------------------


class TestBuildSelectionSet:
    def _types_by_name(self):
        return {
            "User": {
                "kind": "OBJECT",
                "name": "User",
                "fields": [
                    {"name": "id", "type": {"kind": "SCALAR", "name": "ID", "ofType": None}},
                    {"name": "name", "type": {"kind": "SCALAR", "name": "String", "ofType": None}},
                    {"name": "email", "type": {"kind": "SCALAR", "name": "String", "ofType": None}},
                    {"name": "status", "type": {"kind": "ENUM", "name": "Status", "ofType": None}},
                    {"name": "address", "type": {"kind": "OBJECT", "name": "Address", "ofType": None}},
                ],
            },
            "Address": {
                "kind": "OBJECT",
                "name": "Address",
                "fields": [
                    {"name": "city", "type": {"kind": "SCALAR", "name": "String", "ofType": None}},
                    {"name": "country", "type": {"kind": "SCALAR", "name": "String", "ofType": None}},
                ],
            },
            "Status": {
                "kind": "ENUM",
                "name": "Status",
                "enumValues": [{"name": "ACTIVE"}],
            },
        }

    def test_scalar_returns_empty(self):
        ref = {"kind": "SCALAR", "name": "Boolean", "ofType": None}
        assert mcp2cli._build_selection_set(ref, self._types_by_name()) == ""

    def test_object_depth_2(self):
        ref = {"kind": "OBJECT", "name": "User", "ofType": None}
        result = mcp2cli._build_selection_set(ref, self._types_by_name(), depth=2)
        assert "id" in result
        assert "name" in result
        assert "address" in result
        assert "city" in result  # nested

    def test_object_depth_1(self):
        ref = {"kind": "OBJECT", "name": "User", "ofType": None}
        result = mcp2cli._build_selection_set(ref, self._types_by_name(), depth=1)
        assert "id" in result
        assert "city" not in result  # no nested at depth 1

    def test_list_wrapper(self):
        ref = {
            "kind": "NON_NULL",
            "name": None,
            "ofType": {
                "kind": "LIST",
                "name": None,
                "ofType": {"kind": "OBJECT", "name": "User", "ofType": None},
            },
        }
        result = mcp2cli._build_selection_set(ref, self._types_by_name())
        assert "id" in result

    def test_circular_ref(self):
        types = {
            "Node": {
                "kind": "OBJECT",
                "name": "Node",
                "fields": [
                    {"name": "id", "type": {"kind": "SCALAR", "name": "ID", "ofType": None}},
                    {"name": "parent", "type": {"kind": "OBJECT", "name": "Node", "ofType": None}},
                ],
            },
        }
        ref = {"kind": "OBJECT", "name": "Node", "ofType": None}
        result = mcp2cli._build_selection_set(ref, types, depth=3)
        assert "id" in result
        # Should not infinitely recurse


# ---------------------------------------------------------------------------
# Unit tests: command extraction
# ---------------------------------------------------------------------------


class TestExtractGraphQLCommands:
    def test_extract_commands(self, graphql_schema):
        commands = mcp2cli.extract_graphql_commands(graphql_schema)
        names = [c.name for c in commands]
        assert "users" in names
        assert "user" in names
        assert "create-user" in names
        assert "delete-user" in names

    def test_query_params(self, graphql_schema):
        commands = mcp2cli.extract_graphql_commands(graphql_schema)
        user_cmd = next(c for c in commands if c.name == "user")
        assert len(user_cmd.params) == 1
        assert user_cmd.params[0].original_name == "id"
        assert user_cmd.params[0].required is True

    def test_mutation_params(self, graphql_schema):
        commands = mcp2cli.extract_graphql_commands(graphql_schema)
        create_cmd = next(c for c in commands if c.name == "create-user")
        param_names = {p.original_name for p in create_cmd.params}
        assert "name" in param_names
        assert "email" in param_names
        assert "age" in param_names
        name_param = next(p for p in create_cmd.params if p.original_name == "name")
        assert name_param.required is True
        age_param = next(p for p in create_cmd.params if p.original_name == "age")
        assert age_param.required is False

    def test_operation_types(self, graphql_schema):
        commands = mcp2cli.extract_graphql_commands(graphql_schema)
        users_cmd = next(c for c in commands if c.name == "users")
        assert users_cmd.graphql_operation_type == "query"
        create_cmd = next(c for c in commands if c.name == "create-user")
        assert create_cmd.graphql_operation_type == "mutation"

    def test_list_arg_param(self, graphql_schema):
        commands = mcp2cli.extract_graphql_commands(graphql_schema)
        cmd = next(c for c in commands if c.name == "users-by-ids")
        ids_param = next(p for p in cmd.params if p.original_name == "ids")
        assert ids_param.schema.get("type") == "array"


# ---------------------------------------------------------------------------
# Integration tests (subprocess)
# ---------------------------------------------------------------------------


def _run(args, input_data=None, env=None):
    """Run mcp2cli as a subprocess."""
    cmd = [sys.executable, "-m", "mcp2cli"] + args
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=30,
        input=input_data,
        env=env,
    )
    return result


class TestGraphQLIntegration:
    def test_list_commands(self, graphql_server):
        r = _run(["--graphql", graphql_server, "--list"])
        assert r.returncode == 0
        assert "users" in r.stdout
        assert "create-user" in r.stdout

    def test_query_users(self, graphql_server):
        r = _run(["--graphql", graphql_server, "users"])
        assert r.returncode == 0
        data = json.loads(r.stdout)
        assert isinstance(data, list)
        assert len(data) >= 2
        assert data[0]["name"] == "Alice"

    def test_query_user_by_id(self, graphql_server):
        r = _run(["--graphql", graphql_server, "user", "--id", "1"])
        assert r.returncode == 0
        data = json.loads(r.stdout)
        assert data["name"] == "Alice"

    def test_mutation_create_user(self, graphql_server):
        r = _run([
            "--graphql", graphql_server,
            "create-user", "--name", "Charlie", "--email", "charlie@example.com", "--age", "35",
        ])
        assert r.returncode == 0
        data = json.loads(r.stdout)
        assert data["name"] == "Charlie"
        assert data["email"] == "charlie@example.com"

    def test_mutation_delete_user(self, graphql_server):
        r = _run(["--graphql", graphql_server, "delete-user", "--id", "2"])
        assert r.returncode == 0
        data = json.loads(r.stdout)
        assert data is True

    def test_pretty_output(self, graphql_server):
        r = _run(["--graphql", graphql_server, "--pretty", "users"])
        assert r.returncode == 0
        # Pretty output has indentation
        assert "  " in r.stdout

    def test_raw_output(self, graphql_server):
        r = _run(["--graphql", graphql_server, "--raw", "users"])
        assert r.returncode == 0
        # Should be valid JSON
        json.loads(r.stdout)

    def test_fields_override(self, graphql_server):
        r = _run(["--graphql", graphql_server, "--fields", "id name", "users"])
        assert r.returncode == 0
        data = json.loads(r.stdout)
        assert isinstance(data, list)
        # With field override, we should get data
        assert "name" in data[0]

    def test_mutual_exclusion(self, graphql_server, petstore_server):
        r = _run([
            "--graphql", graphql_server,
            "--spec", f"{petstore_server}/openapi.json",
            "--list",
        ])
        assert r.returncode != 0
        assert "mutually exclusive" in r.stderr

    def test_caching(self, graphql_server):
        # First call populates cache
        r1 = _run(["--graphql", graphql_server, "--list"])
        assert r1.returncode == 0
        # Second call should use cache (still succeeds)
        r2 = _run(["--graphql", graphql_server, "--list"])
        assert r2.returncode == 0

    def test_stdin_input(self, graphql_server):
        input_json = json.dumps({"name": "StdinUser", "email": "stdin@example.com"})
        r = _run(
            ["--graphql", graphql_server, "create-user", "--stdin"],
            input_data=input_json,
        )
        assert r.returncode == 0
        data = json.loads(r.stdout)
        assert data["name"] == "StdinUser"

    def test_list_arg_comma_delimited(self, graphql_server):
        r = _run(["--graphql", graphql_server, "users-by-ids", "--ids", "1,2"])
        assert r.returncode == 0
        data = json.loads(r.stdout)
        assert isinstance(data, list)

    def test_list_arg_json(self, graphql_server):
        r = _run(["--graphql", graphql_server, "users-by-ids", "--ids", '["1","2"]'])
        assert r.returncode == 0
        data = json.loads(r.stdout)
        assert isinstance(data, list)


def _name_collision_schema(query_fields=(), mutation_fields=()):
    """Minimal introspection schema with the given Query/Mutation field names."""
    def field(name):
        return {
            "name": name,
            "description": None,
            "args": [],
            "type": {"kind": "SCALAR", "name": "String", "ofType": None},
        }

    return {
        "queryType": {"name": "Query"},
        "mutationType": {"name": "Mutation"} if mutation_fields else None,
        "types": [
            {
                "name": "Query",
                "kind": "OBJECT",
                "fields": [field(n) for n in query_fields],
            },
            {
                "name": "Mutation",
                "kind": "OBJECT",
                "fields": [field(n) for n in mutation_fields],
            },
        ],
    }


class TestGraphQLNameCollisions:
    """Fields that kebab-case onto the same name must stay addressable."""

    def test_three_way_kebab_collision_stays_unique(self):
        schema = _name_collision_schema(
            query_fields=["getUser", "get_user", "GetUser"]
        )
        names = [c.name for c in mcp2cli.extract_graphql_commands(schema)]
        assert len(names) == len(set(names))
        assert names == ["get-user", "query-get-user", "query-get-user-2"]

    def test_three_way_collision_builds_a_parser(self):
        # Duplicate subparser names raise argparse.ArgumentError, which took
        # down every command rather than just the colliding ones.
        schema = _name_collision_schema(
            query_fields=["getUser", "get_user", "GetUser"]
        )
        cmds = mcp2cli.extract_graphql_commands(schema)
        parser = mcp2cli.build_argparse(cmds, argparse.ArgumentParser(add_help=False))
        for cmd in cmds:
            assert parser.parse_args([cmd.name])._cmd is cmd

    def test_alias_never_shadows_another_fields_natural_name(self):
        schema = _name_collision_schema(
            query_fields=["getUser", "get_user", "queryGetUser"]
        )
        cmds = mcp2cli.extract_graphql_commands(schema)
        by_field = {c.graphql_field_name: c.name for c in cmds}
        # queryGetUser owns `query-get-user`; the alias for get_user must
        # not take it.
        assert by_field["queryGetUser"] == "query-get-user"
        assert by_field["getUser"] == "get-user"
        assert len(set(by_field.values())) == 3

    def test_query_mutation_overlap_still_prefixed_symmetrically(self):
        schema = _name_collision_schema(
            query_fields=["user"], mutation_fields=["user"]
        )
        cmds = mcp2cli.extract_graphql_commands(schema)
        assert [c.name for c in cmds] == ["query-user", "mutation-user"]

    def test_cross_type_kebab_collision_is_disambiguated(self):
        schema = _name_collision_schema(
            query_fields=["getUser"], mutation_fields=["get_user"]
        )
        names = [c.name for c in mcp2cli.extract_graphql_commands(schema)]
        assert len(names) == len(set(names))
        assert names == ["get-user", "mutation-get-user"]

    def test_introspection_fields_still_skipped(self):
        schema = _name_collision_schema(
            query_fields=["__schema", "__type", "user"]
        )
        assert [c.name for c in mcp2cli.extract_graphql_commands(schema)] == ["user"]

    def test_non_colliding_names_are_untouched(self):
        schema = _name_collision_schema(
            query_fields=["listUsers"], mutation_fields=["createUser"]
        )
        assert [c.name for c in mcp2cli.extract_graphql_commands(schema)] == [
            "list-users",
            "create-user",
        ]

    def test_field_metadata_survives_renaming(self):
        schema = _name_collision_schema(
            query_fields=["getUser", "get_user"]
        )
        cmds = mcp2cli.extract_graphql_commands(schema)
        # The wire name is what actually goes into the document, so it must
        # be preserved even when the CLI name is aliased.
        assert [c.graphql_field_name for c in cmds] == ["getUser", "get_user"]
        assert all(c.graphql_operation_type == "query" for c in cmds)
