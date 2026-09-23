"""Tests for OpenAPI mode — spec loading, argparse building, and execution against a local petstore."""

import argparse
import json
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

import pytest

from mcp2cli import (
    CommandDef,
    ParamDef,
    build_argparse,
    extract_openapi_commands,
    load_openapi_spec,
    _collect_openapi_params,
)


class TestLoadSpec:
    def test_load_json_file(self, petstore_spec_file):
        spec = load_openapi_spec(petstore_spec_file, [], None, 3600, False)
        assert "paths" in spec
        assert "/pets" in spec["paths"]

    def test_load_yaml_file(self, petstore_yaml_file):
        spec = load_openapi_spec(petstore_yaml_file, [], None, 3600, False)
        assert "paths" in spec
        assert "/pets" in spec["paths"]

    def test_load_from_url(self, petstore_server):
        spec = load_openapi_spec(
            f"{petstore_server}/openapi.json", [], None, 3600, False
        )
        assert "paths" in spec

    def test_load_from_url_cached(self, petstore_server):
        url = f"{petstore_server}/openapi.json"
        spec1 = load_openapi_spec(url, [], None, 3600, False)
        spec2 = load_openapi_spec(url, [], None, 3600, False)
        assert spec1 == spec2

    def test_load_from_url_refresh(self, petstore_server):
        url = f"{petstore_server}/openapi.json"
        load_openapi_spec(url, [], None, 3600, False)
        spec = load_openapi_spec(url, [], None, 3600, True)
        assert "paths" in spec

    def test_ref_resolution_from_file(self, tmp_path, petstore_spec_with_refs):
        p = tmp_path / "spec_refs.json"
        p.write_text(json.dumps(petstore_spec_with_refs))
        spec = load_openapi_spec(str(p), [], None, 3600, False)
        params = spec["paths"]["/pets"]["get"]["parameters"]
        assert params[0]["name"] == "limit"


class TestBuildArgparse:
    def test_subcommands_created(self, petstore_spec):
        cmds = extract_openapi_commands(petstore_spec)
        import argparse

        pre = argparse.ArgumentParser(add_help=False)
        parser = build_argparse(cmds, pre)
        # Should parse a known subcommand
        args = parser.parse_args(["list-pets"])
        assert hasattr(args, "_cmd")

    def test_query_params(self, petstore_spec):
        cmds = extract_openapi_commands(petstore_spec)
        import argparse

        pre = argparse.ArgumentParser(add_help=False)
        parser = build_argparse(cmds, pre)
        args = parser.parse_args(
            ["list-pets", "--limit", "10", "--status", "available"]
        )
        assert args.limit == 10
        assert args.status == "available"

    def test_body_params(self, petstore_spec):
        cmds = extract_openapi_commands(petstore_spec)
        import argparse

        pre = argparse.ArgumentParser(add_help=False)
        parser = build_argparse(cmds, pre)
        args = parser.parse_args(["create-pet", "--name", "Rex", "--tag", "dog"])
        assert args.name == "Rex"
        assert args.tag == "dog"

    def test_percent_signs_in_help_text_are_escaped(self):
        cmds = [
            CommandDef(
                name="list-schedule",
                description="显示 80% 容量",
                params=[
                    ParamDef(
                        name="workload",
                        original_name="workload",
                        python_type=int,
                        description="超过 90% 时告警",
                    ),
                ],
            ),
        ]

        pre = argparse.ArgumentParser(add_help=False)
        parser = build_argparse(cmds, pre)

        args = parser.parse_args(["list-schedule", "--workload", "90"])
        assert args.workload == 90


class TestExecuteOpenAPI:
    """Integration tests against the local petstore HTTP server."""

    def _run(self, petstore_server, *args) -> subprocess.CompletedProcess:
        cmd = [
            sys.executable,
            "-m",
            "mcp2cli",
            "--spec",
            f"{petstore_server}/openapi.json",
            "--base-url",
            f"{petstore_server}/api/v1",
            *args,
        ]
        return subprocess.run(cmd, capture_output=True, text=True, timeout=15)

    def test_list_commands(self, petstore_server):
        r = self._run(petstore_server, "--list")
        assert r.returncode == 0
        assert "list-pets" in r.stdout
        assert "create-pet" in r.stdout

    def test_list_pets(self, petstore_server):
        r = self._run(petstore_server, "--pretty", "list-pets")
        assert r.returncode == 0
        data = json.loads(r.stdout)
        assert isinstance(data, list)
        assert len(data) >= 1

    def test_list_pets_with_limit(self, petstore_server):
        r = self._run(petstore_server, "list-pets", "--limit", "1")
        assert r.returncode == 0
        data = json.loads(r.stdout)
        assert len(data) == 1

    def test_list_pets_by_status(self, petstore_server):
        r = self._run(petstore_server, "list-pets", "--status", "sold")
        assert r.returncode == 0
        data = json.loads(r.stdout)
        assert all(p["status"] == "sold" for p in data)

    def test_get_pet(self, petstore_server):
        r = self._run(petstore_server, "get-pet", "--pet-id", "1")
        assert r.returncode == 0
        data = json.loads(r.stdout)
        assert data["name"] == "Fido"

    def test_get_pet_not_found(self, petstore_server):
        r = self._run(petstore_server, "get-pet", "--pet-id", "999")
        assert r.returncode != 0

    def test_create_pet(self, petstore_server):
        r = self._run(petstore_server, "create-pet", "--name", "Buddy", "--tag", "dog")
        assert r.returncode == 0
        data = json.loads(r.stdout)
        assert data["name"] == "Buddy"
        assert "id" in data

    def test_create_pet_stdin(self, petstore_server):
        cmd = [
            sys.executable,
            "-m",
            "mcp2cli",
            "--spec",
            f"{petstore_server}/openapi.json",
            "--base-url",
            f"{petstore_server}/api/v1",
            "create-pet",
            "--stdin",
        ]
        r = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            input='{"name": "Snowball", "tag": "rabbit"}',
            timeout=15,
        )
        assert r.returncode == 0
        data = json.loads(r.stdout)
        assert data["name"] == "Snowball"

    def test_create_pet_stdin_merges_body_flags(self, petstore_server):
        cmd = [
            sys.executable,
            "-m",
            "mcp2cli",
            "--spec",
            f"{petstore_server}/openapi.json",
            "--base-url",
            f"{petstore_server}/api/v1",
            "create-pet",
            "--tag",
            "rabbit",
            "--stdin",
        ]
        r = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            input='{"name": "Snowball"}',
            timeout=15,
        )
        assert r.returncode == 0, r.stderr
        data = json.loads(r.stdout)
        assert data["name"] == "Snowball"
        assert data["tag"] == "rabbit"

    def test_create_pet_stdin_invalid_json(self, petstore_server):
        cmd = [
            sys.executable,
            "-m",
            "mcp2cli",
            "--spec",
            f"{petstore_server}/openapi.json",
            "--base-url",
            f"{petstore_server}/api/v1",
            "create-pet",
            "--stdin",
        ]
        r = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            input='{"name": "Snowball",',
            timeout=15,
        )
        assert r.returncode != 0
        assert "invalid JSON" in r.stderr

    def test_update_pet(self, petstore_server):
        r = self._run(
            petstore_server, "update-pet", "--pet-id", "1", "--name", "FidoUpdated"
        )
        assert r.returncode == 0
        data = json.loads(r.stdout)
        assert data["name"] == "FidoUpdated"

    def test_delete_pet(self, petstore_server):
        # Create one first so we don't affect other tests
        r = self._run(petstore_server, "create-pet", "--name", "ToDelete")
        data = json.loads(r.stdout)
        pid = data["id"]
        r = self._run(petstore_server, "delete-pet", "--pet-id", str(pid))
        assert r.returncode == 0

    def test_raw_output(self, petstore_server):
        r = self._run(petstore_server, "--raw", "list-pets")
        assert r.returncode == 0
        # Raw should still be valid JSON from our server
        json.loads(r.stdout)

    def test_version(self, petstore_server):
        r = subprocess.run(
            [sys.executable, "-m", "mcp2cli", "--version"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert r.returncode == 0
        assert "mcp2cli" in r.stdout

    def test_no_mode_shows_help(self):
        r = subprocess.run(
            [sys.executable, "-m", "mcp2cli"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert r.returncode != 0
        assert "--spec" in r.stdout or "--spec" in r.stderr

    def test_mutual_exclusion(self, petstore_server):
        r = subprocess.run(
            [sys.executable, "-m", "mcp2cli", "--spec", "x", "--mcp", "y", "--list"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert r.returncode != 0
        assert "mutually exclusive" in r.stderr


# ---------------------------------------------------------------------------
# Multipart / file upload tests
# ---------------------------------------------------------------------------

def _multipart_spec(*, include_json=False):
    """Build a minimal OpenAPI spec with a multipart upload endpoint."""
    content = {
        "multipart/form-data": {
            "schema": {
                "type": "object",
                "required": ["file"],
                "properties": {
                    "file": {
                        "type": "string",
                        "format": "binary",
                        "description": "The image to upload",
                    },
                    "caption": {
                        "type": "string",
                        "description": "Image caption",
                    },
                },
            }
        }
    }
    if include_json:
        content["application/json"] = {
            "schema": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "Image URL"},
                },
            }
        }
    return {
        "openapi": "3.0.0",
        "info": {"title": "test", "version": "1"},
        "paths": {
            "/upload": {
                "post": {
                    "operationId": "uploadImage",
                    "summary": "Upload an image",
                    "requestBody": {"content": content},
                }
            }
        },
    }


def _multipart_no_binary_spec():
    """Multipart spec with no binary fields (pure form-data)."""
    return {
        "openapi": "3.0.0",
        "info": {"title": "test", "version": "1"},
        "paths": {
            "/submit": {
                "post": {
                    "operationId": "submitForm",
                    "summary": "Submit a form",
                    "requestBody": {
                        "content": {
                            "multipart/form-data": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "name": {"type": "string"},
                                        "age": {"type": "integer"},
                                    },
                                }
                            }
                        }
                    },
                }
            }
        },
    }


class TestMultipartExtraction:
    def test_binary_field_gets_file_location(self):
        cmds = extract_openapi_commands(_multipart_spec())
        assert len(cmds) == 1
        cmd = cmds[0]
        assert cmd.content_type == "multipart/form-data"
        file_param = next(p for p in cmd.params if p.original_name == "file")
        assert file_param.location == "file"
        assert file_param.python_type is str
        assert "(file path)" in file_param.description

    def test_non_binary_field_stays_body(self):
        cmds = extract_openapi_commands(_multipart_spec())
        cmd = cmds[0]
        caption_param = next(p for p in cmd.params if p.original_name == "caption")
        assert caption_param.location == "body"

    def test_multipart_preferred_over_json_when_binary(self):
        cmds = extract_openapi_commands(_multipart_spec(include_json=True))
        cmd = cmds[0]
        assert cmd.content_type == "multipart/form-data"
        # Should have file + caption from multipart, not url from JSON
        names = {p.original_name for p in cmd.params}
        assert "file" in names
        assert "caption" in names
        assert "url" not in names

    def test_json_preferred_when_no_binary(self):
        """When both JSON and multipart exist but multipart has no binary fields, prefer JSON."""
        spec = {
            "openapi": "3.0.0",
            "info": {"title": "test", "version": "1"},
            "paths": {
                "/data": {
                    "post": {
                        "operationId": "postData",
                        "requestBody": {
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {
                                            "value": {"type": "string"},
                                        },
                                    }
                                },
                                "multipart/form-data": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {
                                            "value": {"type": "string"},
                                        },
                                    }
                                },
                            }
                        },
                    }
                }
            },
        }
        cmds = extract_openapi_commands(spec)
        cmd = cmds[0]
        assert cmd.content_type is None  # JSON chosen

    def test_multipart_no_binary_fallback(self):
        """When only multipart exists with no binary fields, use it as form-data."""
        cmds = extract_openapi_commands(_multipart_no_binary_spec())
        cmd = cmds[0]
        assert cmd.content_type == "multipart/form-data"
        assert all(p.location == "body" for p in cmd.params)

    def test_argparse_file_param(self):
        cmds = extract_openapi_commands(_multipart_spec())
        pre = argparse.ArgumentParser(add_help=False)
        parser = build_argparse(cmds, pre)
        args = parser.parse_args(["upload-image", "--file", "/tmp/test.png", "--caption", "hello"])
        assert args.file == "/tmp/test.png"
        assert args.caption == "hello"


class TestCollectMultipartParams:
    def test_file_params_returned_separately(self, tmp_path):
        # Create a real temp file
        test_file = tmp_path / "photo.png"
        test_file.write_bytes(b"\x89PNG\r\n")

        cmd = CommandDef(
            name="upload",
            method="post",
            path="/upload",
            has_body=True,
            content_type="multipart/form-data",
            params=[
                ParamDef(name="file", original_name="file", python_type=str, location="file"),
                ParamDef(name="caption", original_name="caption", python_type=str, location="body"),
            ],
        )
        args = argparse.Namespace(file=str(test_file), caption="my photo", stdin=False)
        path, query, headers, body, files = _collect_openapi_params(cmd, args)

        assert body == {"caption": "my photo"}
        assert files is not None
        assert "file" in files
        name, fh, mime = files["file"]
        assert name == "photo.png"
        assert mime == "image/png"
        fh.close()

    def test_file_not_found_exits(self):
        cmd = CommandDef(
            name="upload",
            method="post",
            path="/upload",
            has_body=True,
            content_type="multipart/form-data",
            params=[
                ParamDef(name="file", original_name="file", python_type=str, location="file"),
            ],
        )
        args = argparse.Namespace(file="/nonexistent/file.png", stdin=False)
        with pytest.raises(SystemExit):
            _collect_openapi_params(cmd, args)

    def test_no_files_when_param_not_provided(self):
        cmd = CommandDef(
            name="upload",
            method="post",
            path="/upload",
            has_body=True,
            content_type="multipart/form-data",
            params=[
                ParamDef(name="file", original_name="file", python_type=str, location="file"),
                ParamDef(name="caption", original_name="caption", python_type=str, location="body"),
            ],
        )
        args = argparse.Namespace(file=None, caption="hello", stdin=False)
        _, _, _, body, files = _collect_openapi_params(cmd, args)
        assert files is None
        assert body == {"caption": "hello"}


def _path_level_param_spec():
    """A path item that declares its parameters once, for every operation."""
    return {
        "openapi": "3.0.0",
        "paths": {
            "/users/{userId}": {
                "parameters": [
                    {
                        "name": "userId",
                        "in": "path",
                        "required": True,
                        "schema": {"type": "string"},
                    },
                    {
                        "name": "verbose",
                        "in": "query",
                        "schema": {"type": "string"},
                    },
                ],
                "get": {"operationId": "getUser", "responses": {}},
                "delete": {"operationId": "deleteUser", "responses": {}},
            }
        },
    }


def _mixed_location_spec(method="post"):
    """An operation carrying query, header, path and body parameters at once."""
    return {
        "openapi": "3.0.0",
        "paths": {
            "/tenants/{tenantId}/items": {
                method: {
                    "operationId": "createItem",
                    "parameters": [
                        {"name": "tenantId", "in": "path", "required": True,
                         "schema": {"type": "string"}},
                        {"name": "dryRun", "in": "query",
                         "schema": {"type": "string"}},
                        {"name": "region", "in": "query",
                         "schema": {"type": "string"}},
                        {"name": "X-Trace", "in": "header",
                         "schema": {"type": "string"}},
                    ],
                    "requestBody": {"content": {"application/json": {"schema": {
                        "type": "object",
                        "required": ["name"],
                        "properties": {
                            "name": {"type": "string"},
                            "qty": {"type": "integer"},
                        },
                    }}}},
                    "responses": {},
                }
            }
        },
    }


class TestPathLevelParameters:
    """OpenAPI 3.x: `parameters` on a path item apply to every operation."""

    def test_inherited_by_every_operation(self):
        cmds = extract_openapi_commands(_path_level_param_spec())
        by_name = {c.name: c for c in cmds}
        assert set(by_name) == {"get-user", "delete-user"}
        for cmd in by_name.values():
            locations = {p.original_name: p.location for p in cmd.params}
            assert locations == {"userId": "path", "verbose": "query"}

    def test_path_placeholder_is_substituted(self):
        cmd = extract_openapi_commands(_path_level_param_spec())[0]
        args = argparse.Namespace(user_id="u-42", verbose=None, stdin=False)
        path, query, headers, body, files = _collect_openapi_params(cmd, args)
        assert path == "/users/u-42"

    def test_operation_level_overrides_inherited(self):
        spec = _path_level_param_spec()
        spec["paths"]["/users/{userId}"]["get"]["parameters"] = [
            {
                "name": "userId",
                "in": "path",
                "required": True,
                "schema": {"type": "integer"},
            }
        ]
        cmds = {c.name: c for c in extract_openapi_commands(spec)}
        get_param = next(
            p for p in cmds["get-user"].params if p.original_name == "userId"
        )
        # The operation's narrower declaration wins...
        assert get_param.python_type is int
        # ...while the sibling operation keeps the inherited one.
        del_param = next(
            p for p in cmds["delete-user"].params if p.original_name == "userId"
        )
        assert del_param.python_type is str
        # An override must not drop the other inherited parameters.
        assert {p.original_name for p in cmds["get-user"].params} == {
            "userId",
            "verbose",
        }

    def test_operation_level_only_still_works(self):
        spec = {
            "openapi": "3.0.0",
            "paths": {
                "/items": {
                    "get": {
                        "operationId": "listItems",
                        "parameters": [
                            {"name": "limit", "in": "query",
                             "schema": {"type": "integer"}}
                        ],
                        "responses": {},
                    }
                }
            },
        }
        cmd = extract_openapi_commands(spec)[0]
        assert [p.original_name for p in cmd.params] == ["limit"]

    def test_malformed_parameter_entries_are_skipped(self):
        spec = {
            "openapi": "3.0.0",
            "paths": {
                "/x": {
                    "parameters": ["not-a-dict", {"no_name": True}],
                    "get": {"operationId": "getX", "responses": {}},
                }
            },
        }
        cmd = extract_openapi_commands(spec)[0]
        assert cmd.params == []
def _collision_spec(*entries):
    """Build a spec from (path, method, operationId) triples."""
    paths: dict = {}
    for path, method, op_id in entries:
        op: dict = {"responses": {}}
        if op_id is not None:
            op["operationId"] = op_id
        paths.setdefault(path, {})[method] = op
    return {"openapi": "3.0.0", "paths": paths}


class TestCommandNameCollisions:
    """Colliding OpenAPI command names must stay unique and addressable."""

    def test_three_way_collision_stays_unique(self):
        spec = _collision_spec(
            ("/a", "post", "doThing"),
            ("/b", "post", "doThing"),
            ("/c", "post", "doThing"),
        )
        names = [c.name for c in extract_openapi_commands(spec)]
        assert len(names) == len(set(names))
        assert names == ["do-thing", "do-thing-post", "do-thing-post-2"]

    def test_three_way_collision_builds_a_parser(self):
        spec = _collision_spec(
            ("/a", "post", "doThing"),
            ("/b", "post", "doThing"),
            ("/c", "post", "doThing"),
        )
        cmds = extract_openapi_commands(spec)
        parser = build_argparse(cmds, argparse.ArgumentParser(add_help=False))
        for cmd in cmds:
            args = parser.parse_args([cmd.name])
            assert args._cmd is cmd

    def test_method_suffix_used_when_free(self):
        spec = _collision_spec(
            ("/a", "get", "doThing"),
            ("/b", "post", "doThing"),
        )
        assert [c.name for c in extract_openapi_commands(spec)] == [
            "do-thing",
            "do-thing-post",
        ]

    def test_alias_never_shadows_another_natural_name(self):
        spec = _collision_spec(
            ("/a", "post", "doThing"),
            ("/b", "post", "doThing"),
            ("/c", "post", "doThingPost"),
        )
        cmds = extract_openapi_commands(spec)
        names = [c.name for c in cmds]
        assert len(names) == len(set(names))
        by_op = {c.path: c.name for c in cmds}
        assert by_op["/c"] == "do-thing-post"
        assert by_op["/b"] == "do-thing-post-2"

    def test_pathless_slug_names_still_unique(self):
        spec = _collision_spec(
            ("/items", "get", None),
            ("/items", "post", None),
        )
        names = [c.name for c in extract_openapi_commands(spec)]
        assert names == ["get-items", "post-items"]

    def test_no_collision_leaves_names_untouched(self):
        spec = _collision_spec(
            ("/a", "get", "listThings"),
            ("/b", "post", "createThing"),
        )
        assert [c.name for c in extract_openapi_commands(spec)] == [
            "list-things",
            "create-thing",
        ]

    def test_distinct_operation_ids_that_kebab_alike_stay_addressable(self):
        # A perfectly valid spec: four different operationIds that all
        # normalize to the same CLI name. Every operation must keep its own
        # command, and that command must carry its own path and method.
        spec = _collision_spec(
            ("/pets", "get", "listPets"),
            ("/pets", "post", "list-pets"),
            ("/pets/all", "get", "list_pets"),
            ("/pets/legacy", "get", "ListPets"),
        )
        cmds = extract_openapi_commands(spec)
        wire = {c.name: (c.method, c.path) for c in cmds}
        assert wire == {
            "list-pets": ("get", "/pets"),
            "list-pets-post": ("post", "/pets"),
            "list-pets-get": ("get", "/pets/all"),
            "list-pets-get-2": ("get", "/pets/legacy"),
        }
        parser = build_argparse(cmds, argparse.ArgumentParser(add_help=False))
        for cmd in cmds:
            assert parser.parse_args([cmd.name])._cmd is cmd


class TestQueryParamsStayOutOfBody:
    """Query, header and path values travel outside the JSON body."""

    @pytest.mark.parametrize("method", ["post", "put", "patch", "delete"])
    def test_query_param_not_copied_into_json_body(self, method):
        cmd = extract_openapi_commands(_mixed_location_spec(method))[0]
        args = argparse.Namespace(
            tenant_id="acme", dry_run=None, region="eu", x_trace=None,
            name="widget", qty=3, stdin=False,
        )
        path, query, headers, body, files = _collect_openapi_params(cmd, args)
        assert body == {"name": "widget", "qty": 3}
        assert query == {"region": "eu"}
        assert path == "/tenants/acme/items"

    def test_header_and_path_still_excluded_from_body(self):
        cmd = extract_openapi_commands(_mixed_location_spec())[0]
        args = argparse.Namespace(
            tenant_id="acme", dry_run="yes", region=None, x_trace="abc123",
            name="widget", qty=None, stdin=False,
        )
        path, query, headers, body, files = _collect_openapi_params(cmd, args)
        assert body == {"name": "widget"}
        assert headers == {"X-Trace": "abc123"}
        assert query == {"dryRun": "yes"}

    def test_body_becomes_none_when_only_query_supplied(self):
        cmd = extract_openapi_commands(_mixed_location_spec())[0]
        args = argparse.Namespace(
            tenant_id="acme", dry_run=None, region="eu", x_trace=None,
            name=None, qty=None, stdin=False,
        )
        path, query, headers, body, files = _collect_openapi_params(cmd, args)
        assert body is None
        assert query == {"region": "eu"}

    def test_query_and_header_still_collected_with_stdin_body(self, monkeypatch):
        import io
        cmd = extract_openapi_commands(_mixed_location_spec())[0]
        monkeypatch.setattr(sys, "stdin", io.StringIO('{"name": "from-stdin"}'))
        args = argparse.Namespace(
            tenant_id="acme", dry_run=None, region="eu", x_trace="abc123",
            name=None, qty=None, stdin=True,
        )
        path, query, headers, body, files = _collect_openapi_params(cmd, args)
        assert body == {"name": "from-stdin"}
        assert query == {"region": "eu"}
        assert headers == {"X-Trace": "abc123"}

    def test_get_verb_unaffected(self):
        cmd = extract_openapi_commands(_mixed_location_spec("get"))[0]
        args = argparse.Namespace(
            tenant_id="acme", dry_run=None, region="eu", x_trace="abc123",
            name=None, qty=None, stdin=False,
        )
        path, query, headers, body, files = _collect_openapi_params(cmd, args)
        assert body is None
        assert query == {"region": "eu"}
        assert headers == {"X-Trace": "abc123"}
        assert path == "/tenants/acme/items"


# ---------------------------------------------------------------------------
# Wire-level checks against a server with a strict body schema
# ---------------------------------------------------------------------------

_STRICT_SPEC = {
    "openapi": "3.0.0",
    "info": {"title": "Strict", "version": "1.0.0"},
    "paths": {
        "/tenants/{tenantId}/items": {
            "post": {
                "operationId": "createItem",
                "parameters": [
                    {"name": "tenantId", "in": "path", "required": True,
                     "schema": {"type": "string"}},
                    {"name": "region", "in": "query", "schema": {"type": "string"}},
                    {"name": "X-Trace", "in": "header", "schema": {"type": "string"}},
                ],
                "requestBody": {"content": {"application/json": {"schema": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["name"],
                    "properties": {
                        "name": {"type": "string"},
                        "qty": {"type": "integer"},
                    },
                }}}},
                "responses": {},
            },
        }
    },
}


class _StrictItemsHandler(BaseHTTPRequestHandler):
    """Echoes the request it received, rejecting unknown body fields with 400."""

    BODY_FIELDS = {"name", "qty"}

    def log_message(self, fmt, *args):
        pass

    def _send_json(self, data, status=200):
        payload = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _echo(self):
        parsed = urlparse(self.path)
        if parsed.path == "/openapi.json":
            self._send_json(_STRICT_SPEC)
            return
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b""
        body = json.loads(raw) if raw else {}
        unexpected = sorted(set(body) - self.BODY_FIELDS)
        if unexpected:
            self._send_json(
                {"error": "unexpected body fields", "fields": unexpected}, 400
            )
            return
        self._send_json({
            "method": self.command,
            "path": parsed.path,
            "query": {k: v[0] for k, v in parse_qs(parsed.query).items()},
            "body": body,
            "trace": self.headers.get("X-Trace"),
        })

    do_GET = _echo
    do_POST = _echo


@pytest.fixture(scope="module")
def strict_items_server():
    server = HTTPServer(("127.0.0.1", 0), _StrictItemsHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


class TestStrictBodySchemaOnTheWire:
    """The request that actually leaves the client separates query from body."""

    def _cmd(self, server, *args):
        return [
            sys.executable, "-m", "mcp2cli",
            "--spec", f"{server}/openapi.json",
            "--base-url", server,
            *args,
        ]

    def test_post_keeps_query_out_of_the_json_body(self, strict_items_server):
        r = subprocess.run(
            self._cmd(
                strict_items_server, "create-item",
                "--tenant-id", "acme", "--region", "eu",
                "--x-trace", "t-1", "--name", "widget", "--qty", "3",
            ),
            capture_output=True, text=True, timeout=15,
        )
        assert r.returncode == 0, r.stderr
        echo = json.loads(r.stdout)
        assert echo["path"] == "/tenants/acme/items"
        assert echo["query"] == {"region": "eu"}
        assert echo["body"] == {"name": "widget", "qty": 3}
        assert echo["trace"] == "t-1"

    def test_stdin_body_still_carries_query_and_header(self, strict_items_server):
        r = subprocess.run(
            self._cmd(
                strict_items_server, "create-item",
                "--tenant-id", "acme", "--region", "eu",
                "--x-trace", "t-2", "--stdin",
            ),
            capture_output=True, text=True,
            input='{"name": "from-stdin"}', timeout=15,
        )
        assert r.returncode == 0, r.stderr
        echo = json.loads(r.stdout)
        assert echo["query"] == {"region": "eu"}
        assert echo["body"] == {"name": "from-stdin"}
        assert echo["trace"] == "t-2"
