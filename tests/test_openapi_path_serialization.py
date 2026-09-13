"""Path serialization must preserve values without changing the URL structure."""

import argparse

import httpx
import pytest

from mcp2cli import _collect_openapi_params, build_argparse, extract_openapi_commands


def collect(value, schema, **serialization):
    spec = {
        "openapi": "3.0.4",
        "paths": {
            "/items/{color}/details": {
                "parameters": [
                    {
                        "name": "color",
                        "in": "path",
                        "required": True,
                        "schema": schema,
                        **serialization,
                    }
                ],
                "get": {"operationId": "getItem", "responses": {}},
            },
        },
    }
    commands = extract_openapi_commands(spec)
    parser = build_argparse(commands, argparse.ArgumentParser(add_help=False))
    args = parser.parse_args(["get-item", "--color", value])
    return _collect_openapi_params(args._cmd, args)[0]


@pytest.mark.parametrize(
    "value,encoded",
    [
        ("normal-ID_~.txt", "normal-ID_~.txt"),
        ("team/item", "team%2Fitem"),
        ("a?b#c", "a%3Fb%23c"),
        ("100% / ready", "100%25%20%2F%20ready"),
        ("already%2Fencoded", "already%252Fencoded"),
        ("a,b;c=d", "a%2Cb%3Bc%3Dd"),
        ("café", "caf%C3%A9"),
        ("{color}", "%7Bcolor%7D"),
        (".", "%2E"),
        ("..", "%2E%2E"),
    ],
)
def test_scalar_encoding(value, encoded):
    path = collect(value, {"type": "string"})
    assert path == f"/items/{encoded}/details"
    request = httpx.Request(
        "GET", "https://example.test/api" + path, params={"limit": "2"}
    )
    assert request.url.raw_path == f"/api/items/{encoded}/details?limit=2".encode()
    assert not request.url.fragment


@pytest.mark.parametrize(
    "style,explode,array,object_value",
    [
        ("simple", False, "blue,black,brown", "R,100,G,200,B,150"),
        ("simple", True, "blue,black,brown", "R=100,G=200,B=150"),
        ("label", False, ".blue,black,brown", ".R,100,G,200,B,150"),
        ("label", True, ".blue.black.brown", ".R=100.G=200.B=150"),
        ("matrix", False, ";color=blue,black,brown", ";color=R,100,G,200,B,150"),
        ("matrix", True, ";color=blue;color=black;color=brown", ";R=100;G=200;B=150"),
    ],
)
def test_openapi_style_examples(style, explode, array, object_value):
    serialization = {"style": style, "explode": explode}
    assert (
        collect(
            '["blue","black","brown"]',
            {"type": "array", "items": {"type": "string"}},
            **serialization,
        )
        == f"/items/{array}/details"
    )
    assert (
        collect('{"R":100,"G":200,"B":150}', {"type": "object"}, **serialization)
        == f"/items/{object_value}/details"
    )


@pytest.mark.parametrize(
    "style,prefix",
    [
        ("simple", ""),
        ("label", "."),
        ("matrix", ";color="),
    ],
)
@pytest.mark.parametrize("explode", [False, True])
def test_primitive_style(style, prefix, explode):
    assert (
        collect("a/b", {"type": "string"}, style=style, explode=explode)
        == f"/items/{prefix}a%2Fb/details"
    )


def test_default_simple_array_and_object():
    assert collect('["a,b","c/d"]', {"type": "array"}) == "/items/a%2Cb,c%2Fd/details"
    assert collect('{"a=b":"c;d"}', {"type": "object"}) == "/items/a%3Db,c%3Bd/details"


@pytest.mark.parametrize("style", ["simple", "label", "matrix"])
@pytest.mark.parametrize("explode", [False, True])
@pytest.mark.parametrize(
    "value,schema",
    [
        ("[]", {"type": "array"}),
        ("{}", {"type": "object"}),
        ("[null]", {"type": "array"}),
        ('{"a":null}', {"type": "object"}),
    ],
)
def test_undefined_collections_do_not_add_style_delimiters(
    value, schema, style, explode
):
    assert collect(value, schema, style=style, explode=explode) == "/items//details"


@pytest.mark.parametrize(
    "style,expected",
    [
        ("simple", "a%3Db=c%3Bd,x%2Fy=z%3Fq"),
        ("label", ".a%3Db=c%3Bd.x%2Fy=z%3Fq"),
        ("matrix", ";a%3Db=c%3Bd;x%2Fy=z%3Fq"),
    ],
)
def test_exploded_object_encodes_data_not_delimiters(style, expected):
    assert (
        collect(
            '{"a=b":"c;d","x/y":"z?q"}', {"type": "object"}, style=style, explode=True
        )
        == f"/items/{expected}/details"
    )


@pytest.mark.parametrize(
    "value,schema,expected",
    [
        ("42", {"type": "integer"}, "42"),
        ("1.25", {"type": "number"}, "1.25"),
        ("[true,false]", {"type": "array"}, "true,false"),
        ('{"enabled":true}', {"type": "object"}, "enabled,true"),
        ('["a?b#c", "100%"]', {"type": ["array", "null"]}, "a%3Fb%23c,100%25"),
    ],
)
def test_json_types(value, schema, expected):
    assert collect(value, schema) == f"/items/{expected}/details"


def test_values_cannot_inject_a_second_placeholder():
    from mcp2cli import CommandDef, ParamDef

    cmd = CommandDef(
        "get",
        method="get",
        path="/items/{first}/{second}",
        params=[
            ParamDef("first", "first", str, location="path", schema={"type": "string"}),
            ParamDef(
                "second", "second", str, location="path", schema={"type": "string"}
            ),
        ],
    )
    args = argparse.Namespace(first="{second}", second="real", stdin=False)
    assert _collect_openapi_params(cmd, args)[0] == "/items/%7Bsecond%7D/real"
