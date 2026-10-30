"""The session daemon must spawn where /dev/null cannot be opened.

`--session-start` used to hand `subprocess.DEVNULL` to Popen for the daemon's
stdout and stdin. Popen resolves DEVNULL by opening the device in the *parent*
with `os.open` (not `builtins.open`), so sandboxes that deny /dev/null failed
the spawn with PermissionError before the daemon existed:

    PermissionError: [Errno 13] Permission denied: '/dev/null'

The sandbox tests deny exactly that `os.open` and start a real daemon, so they fail
on the old spawn and pass on the current one.
"""

import json
import os
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import mcp2cli

_PROC_FD = Path("/proc/self/fd")


def _deny_dev_null(monkeypatch):
    """Refuse every /dev/null open, the way a hardened sandbox does."""
    real_open = os.open

    def guarded(path, *args, **kwargs):
        if str(path) == os.devnull:
            raise PermissionError(13, "Permission denied", str(path))
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", guarded)


def _text_content(result: dict) -> list[str]:
    return [item["text"] for item in result["content"] if item.get("type") == "text"]


def _parent_holds(log_path: Path) -> bool:
    """Whether this process still has the session log open."""
    if not _PROC_FD.exists():
        pytest.skip("descriptor introspection needs /proc")
    for entry in _PROC_FD.iterdir():
        try:
            if os.readlink(entry) == str(log_path):
                return True
        except OSError:  # descriptor closed while we walked the directory
            continue
    return False


def test_session_serves_calls_where_dev_null_is_denied(
    session_home, started, monkeypatch, mcp_test_server_cmd, capsys
):
    """A sandbox that denies /dev/null must not break `--session-start`."""
    _deny_dev_null(monkeypatch)
    started.append("sandboxed")

    mcp2cli.session_start("sandboxed", mcp_test_server_cmd, True, [], {})
    assert "sandboxed" in capsys.readouterr().out
    meta = mcp2cli._session_read_meta("sandboxed")
    assert mcp2cli._session_endpoint(meta) is not None
    if meta["endpoint"]["kind"] == "unix":
        assert mcp2cli._session_sock_path("sandboxed").exists()

    names = [
        tool["name"] for tool in mcp2cli._session_request("sandboxed", "list_tools", {})
    ]
    assert "echo" in names

    echoed = mcp2cli._session_request(
        "sandboxed",
        "call_tool",
        {"name": "echo", "arguments": {"message": "via session"}},
    )
    assert _text_content(echoed) == ["via session"]
    assert echoed["isError"] is False

    # A second call: the daemon's EOF stdin must not knock it over between
    # requests.
    summed = mcp2cli._session_request(
        "sandboxed",
        "call_tool",
        {"name": "add_numbers", "arguments": {"a": 2, "b": 3}},
    )
    assert _text_content(summed) == ["5"]
    assert summed["isError"] is False

    # The spawn handed its log descriptor to the daemon and kept none.
    assert not _parent_holds(mcp2cli._session_log_path("sandboxed"))


def test_failed_daemon_reports_through_the_session_log(
    session_home, monkeypatch, capsys
):
    """A daemon that dies must leave its diagnostics in the log, not nowhere."""
    _deny_dev_null(monkeypatch)
    # No such binary: the daemon raises on spawn and exits with a traceback.
    dead_server = "mcp2cli-nonexistent-test-server"

    with pytest.raises(SystemExit) as exc:
        mcp2cli.session_start("broken", dead_server, True, [], {})
    assert exc.value.code == 1
    assert "session daemon" in capsys.readouterr().err

    log_path = mcp2cli._session_log_path("broken")
    # The daemon's uncaught traceback reached the log instead of /dev/null.
    assert "Traceback" in log_path.read_text()
    assert not _parent_holds(log_path)
    assert not mcp2cli._session_sock_path("broken").exists()


def test_session_bootstrap_keeps_configuration_out_of_process_arguments(
    session_home, monkeypatch
):
    """Pass secrets over the private pipe, not the process-visible command line."""
    source = "https://example.test/mcp?token=fixture-url-secret"
    headers = [("Authorization", "Bearer fixture-header-secret")]
    environment = {"API_KEY": "fixture-env-secret", "LABEL": "café"}
    roots = [session_home.as_uri()]
    process = MagicMock()
    spawned = []

    def spawn(argv, **kwargs):
        spawned.extend(argv)
        assert kwargs["stdin"] == mcp2cli.subprocess.PIPE
        # Readiness is the daemon publishing an endpoint, not a socket file.
        mcp2cli._session_write_meta(
            mcp2cli._session_meta_path("private"),
            {"pid": 4242, "endpoint": {"kind": "unix", "path": "unused"}},
        )
        return process

    monkeypatch.setattr(mcp2cli.subprocess, "Popen", spawn)
    mcp2cli.session_start(
        "private",
        source,
        False,
        headers,
        environment,
        transport="streamable",
        roots=roots,
    )

    for secret in ("fixture-url-secret", "fixture-header-secret", "fixture-env-secret"):
        assert all(secret not in argument for argument in spawned)
    payload = process.stdin.write.call_args.args[0]
    assert json.loads(payload) == {
        "name": "private",
        "source": source,
        "is_stdio": False,
        "auth_headers": [list(header) for header in headers],
        "env_vars": environment,
        "transport": "streamable",
        "roots": roots,
    }


def test_running_daemon_does_not_expose_credentials_in_argv(
    session_home, started, mcp_test_server_cmd
):
    if not Path("/proc/self/cmdline").exists():
        pytest.skip("process argument introspection needs /proc")
    started.append("private")
    mcp2cli.session_start(
        "private",
        mcp_test_server_cmd,
        True,
        [("Authorization", "Bearer fixture-header-secret")],
        {"API_KEY": "fixture-env-secret"},
    )

    metadata = json.loads(mcp2cli._session_meta_path("private").read_text())
    argv = Path(f"/proc/{metadata['pid']}/cmdline").read_bytes()
    assert b"fixture-header-secret" not in argv
    assert b"fixture-env-secret" not in argv

    echoed = mcp2cli._session_request(
        "private",
        "call_tool",
        {"name": "echo", "arguments": {"message": "private session"}},
    )
    assert _text_content(echoed) == ["private session"]


@pytest.mark.parametrize("failure_stage", ["write", "close"])
def test_daemon_exit_before_reading_config_reports_startup_failure(
    session_home, monkeypatch, capsys, failure_stage
):
    process = MagicMock(returncode=23)
    process.poll.return_value = 23
    if failure_stage == "write":
        process.stdin.write.side_effect = BrokenPipeError
    else:
        process.stdin.__exit__.side_effect = BrokenPipeError
    monkeypatch.setattr(mcp2cli.subprocess, "Popen", lambda *args, **kwargs: process)

    with pytest.raises(SystemExit) as exc:
        mcp2cli.session_start("early-exit", "https://example.test/mcp", False, [], {})

    assert exc.value.code == 1
    assert "session daemon exited with code 23" in capsys.readouterr().err


def test_large_bootstrap_can_time_out_while_daemon_import_is_slow(
    session_home, tmp_path, monkeypatch, capsys
):
    """A full config pipe must not block the parent's startup deadline."""
    # Only the child sees this shadow module; the real package is loaded above.
    slow_modules = tmp_path / "slow-modules"
    slow_modules.mkdir()
    (slow_modules / "mcp2cli.py").write_text("import time; time.sleep(5)\n")
    monkeypatch.setenv("PYTHONPATH", str(slow_modules))

    real_popen = mcp2cli.subprocess.Popen
    children = []

    def spawn(*args, **kwargs):
        child = real_popen(*args, **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(mcp2cli.subprocess, "Popen", spawn)
    # Accelerate only the parent deadline, keeping the real pipe and imports.
    monkeypatch.setattr(
        mcp2cli,
        "time",
        SimpleNamespace(time=lambda: time.monotonic() * 100, sleep=time.sleep),
    )
    started_at = time.monotonic()
    try:
        with pytest.raises(SystemExit) as exc:
            mcp2cli.session_start(
                "slow-import",
                "https://example.test/mcp",
                False,
                [("X-Config-Padding", "x" * (1024 * 1024))],
                {},
            )
        assert exc.value.code == 1
        assert "session daemon did not start in time" in capsys.readouterr().err
        assert time.monotonic() - started_at < 2
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=5)
