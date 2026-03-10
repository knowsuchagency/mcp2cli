"""Tests for OpenAPI mode — spec loading, argparse building, and execution against a local petstore."""

import json
import subprocess
import sys

import pytest

from mcp2cli import (
    build_argparse,
    extract_openapi_commands,
    load_openapi_spec,
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
        r = self._run(petstore_server, "list-pets", "--pretty")
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
