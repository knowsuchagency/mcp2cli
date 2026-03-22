"""Tests for OpenAPI mode — spec loading, argparse building, and execution against a local petstore."""

import argparse
import json
import subprocess
import sys

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
