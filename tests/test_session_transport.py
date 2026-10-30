"""Session transport: loopback TCP where AF_UNIX is missing, Unix sockets elsewhere.

`--session-start` used to bind a Unix domain socket unconditionally, so on
Windows — where CPython exposes no ``socket.AF_UNIX`` — the daemon died with
``AttributeError: module 'socket' has no attribute 'AF_UNIX'`` and every
``--session*`` command was unusable. Both transports now go through one code
path, and ``MCP2CLI_SESSION_TRANSPORT`` pins a transport so the TCP branch is
exercised even on hosts that also have AF_UNIX.
"""

import json
import os
import socket

import pytest

import mcp2cli


def _text_content(result: dict) -> list[str]:
    return [item["text"] for item in result["content"] if item.get("type") == "text"]


@pytest.fixture(params=["tcp", "unix"])
def transport(request, monkeypatch):
    """Run each case on every transport this host supports."""
    if request.param == "unix" and not hasattr(socket, "AF_UNIX"):
        pytest.skip("this host has no socket.AF_UNIX")
    # The daemon is a fresh interpreter, so the pin has to travel in the env.
    monkeypatch.setenv("MCP2CLI_SESSION_TRANSPORT", request.param)
    return request.param


def _dead_endpoint(kind: str, home) -> dict:
    """An endpoint that is valid in shape but answers nothing."""
    if kind == "tcp":
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()  # nothing listens on it any more
        return {"kind": "tcp", "host": "127.0.0.1", "port": port}
    return {"kind": "unix", "path": str(home / "gone.sock")}


def test_session_serves_calls_over_either_transport(
    transport, session_home, started, mcp_test_server_cmd
):
    started.append("over-transport")
    mcp2cli.session_start("over-transport", mcp_test_server_cmd, True, [], {})

    meta = mcp2cli._session_read_meta("over-transport")
    assert meta["v"] == mcp2cli._SESSION_PROTOCOL_VERSION
    assert meta["endpoint"]["kind"] == transport
    if transport == "unix":
        assert mcp2cli._session_sock_path("over-transport").exists()
    else:
        # No socket file at all: clients reach the daemon through the port.
        assert not mcp2cli._session_sock_path("over-transport").exists()
        assert 0 < meta["endpoint"]["port"] < 65536

    echoed = mcp2cli._session_request(
        "over-transport",
        "call_tool",
        {"name": "echo", "arguments": {"message": "over transport"}},
    )
    assert _text_content(echoed) == ["over transport"]


def test_daemon_rejects_requests_without_the_session_token(
    transport, session_home, started, mcp_test_server_cmd
):
    started.append("guarded")
    mcp2cli.session_start("guarded", mcp_test_server_cmd, True, [], {})

    meta = mcp2cli._session_read_meta("guarded")
    assert len(meta["token"]) == 32
    conn = mcp2cli._session_connect(meta["endpoint"])
    try:
        conn.sendall(
            json.dumps(
                {"id": 1, "method": "list_tools", "params": {}, "token": "wrong"}
            ).encode()
            + b"\n"
        )
        conn.shutdown(socket.SHUT_WR)
        response = json.loads(conn.recv(65536).decode())
    finally:
        conn.close()

    assert response == {"id": 1, "error": "unauthorized"}
    # A rejected caller must not take the daemon down with it.
    assert mcp2cli._session_request("guarded", "list_tools")


def test_stop_shuts_the_daemon_down_and_removes_its_state(
    transport, session_home, started, mcp_test_server_cmd
):
    started.append("stoppable")
    mcp2cli.session_start("stoppable", mcp_test_server_cmd, True, [], {})
    endpoint = mcp2cli._session_read_meta("stoppable")["endpoint"]

    mcp2cli.session_stop("stoppable")

    assert not mcp2cli._session_meta_path("stoppable").exists()
    assert not mcp2cli._session_sock_path("stoppable").exists()
    assert mcp2cli.session_list() == []
    if endpoint["kind"] == "tcp":
        with pytest.raises(OSError):
            socket.create_connection(
                (endpoint["host"], endpoint["port"]), timeout=1
            ).close()


def test_stale_metadata_is_reported_dead_and_replaced(
    transport, session_home, started, mcp_test_server_cmd
):
    session_home.mkdir(parents=True, exist_ok=True)
    mcp2cli._session_write_meta(
        mcp2cli._session_meta_path("stale"),
        {
            # Our own PID: the PID probe would call this alive, so only the
            # endpoint round trip can spot the dead session.
            "pid": os.getpid(),
            "source": "unused",
            "transport": "stdio",
            "endpoint": _dead_endpoint(transport, session_home),
            "token": "unused",
            "v": mcp2cli._SESSION_PROTOCOL_VERSION,
        },
    )

    sessions = mcp2cli.session_list()
    assert [s["alive"] for s in sessions] == [False]

    started.append("stale")
    mcp2cli.session_start("stale", mcp_test_server_cmd, True, [], {})
    recovered = mcp2cli._session_request(
        "stale", "call_tool", {"name": "echo", "arguments": {"message": "recovered"}}
    )
    assert _text_content(recovered) == ["recovered"]


def test_request_reports_an_unknown_session(session_home, capsys):
    with pytest.raises(SystemExit) as exc:
        mcp2cli._session_request("missing", "list_tools")

    assert exc.value.code == 1
    assert "session 'missing' not found" in capsys.readouterr().err
