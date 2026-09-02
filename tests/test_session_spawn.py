"""The session daemon must spawn where /dev/null cannot be opened."""

import builtins
import subprocess

import pytest

import mcp2cli


@pytest.fixture
def session_dirs(tmp_path, monkeypatch):
    sessions = tmp_path / "sessions"
    sessions.mkdir(parents=True)
    monkeypatch.setattr(mcp2cli, "SESSIONS_DIR", sessions)
    return sessions


def _spawn_kwargs(monkeypatch):
    """Capture the kwargs session_start hands to Popen, without spawning."""
    captured = {}

    class _FakeProc:
        """Exits immediately, so session_start reports failure and returns."""

        pid = 4321
        stdin = None
        returncode = 1

        def poll(self):
            return 1

    def fake_popen(_argv, **kwargs):
        captured.update(kwargs)
        return _FakeProc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    return captured


def test_daemon_streams_avoid_dev_null(session_dirs, monkeypatch, capsys):
    """A sandbox that denies /dev/null must not break `--session-start`."""
    captured = _spawn_kwargs(monkeypatch)
    real_open = builtins.open

    def guarded_open(path, *args, **kwargs):
        if str(path) == "/dev/null":
            raise PermissionError(13, "Permission denied", "/dev/null")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", guarded_open)
    monkeypatch.setattr(mcp2cli.time, "sleep", lambda _seconds: None)

    # The stub daemon exits at once, so session_start reports and exits; the
    # spawn arguments are already captured by then.
    with pytest.raises(SystemExit):
        mcp2cli.session_start("sandboxed", "cmd", True, [], {})
    capsys.readouterr()

    assert captured["stdout"] is not subprocess.DEVNULL
    assert captured["stderr"] is not subprocess.DEVNULL
    assert captured["stdin"] is subprocess.PIPE
    assert captured["start_new_session"] is True


def test_daemon_output_lands_in_the_session_log(session_dirs, monkeypatch, capsys):
    captured = _spawn_kwargs(monkeypatch)
    monkeypatch.setattr(mcp2cli.time, "sleep", lambda _seconds: None)

    with pytest.raises(SystemExit):
        mcp2cli.session_start("logged", "cmd", True, [], {})
    capsys.readouterr()

    assert captured["stdout"] is captured["stderr"]
    assert captured["stdout"].name == str(mcp2cli._session_log_path("logged"))
