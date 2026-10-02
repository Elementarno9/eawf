"""Parsing and classification of the stray-daemon scan."""

from __future__ import annotations

from pathlib import Path

import pytest

from eawf.observability.doctor import daemon_strays
from eawf.observability.doctor.daemon_strays import (
    STARTUP_GRACE_SECONDS,
    StrayDaemon,
    check_stray_daemons,
    describe_stray,
)


@pytest.mark.parametrize(
    ("etime", "seconds"),
    [("00:07", 7.0), ("12:34", 754.0), ("01:00:00", 3600.0), ("3-00:00:01", 259201.0)],
)
def test_etime_seconds(etime: str, seconds: float) -> None:
    assert daemon_strays._etime_seconds(etime) == pytest.approx(seconds)


def test_etime_seconds_rejects_garbage() -> None:
    with pytest.raises(ValueError):
        daemon_strays._etime_seconds("soon")


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("/venv/bin/python -m eawf.runtime.daemon.main", True),
        ("/usr/bin/python3.14 -m eawf.runtime.daemon.main", True),
        ("/venv/bin/python /venv/bin/eawfd --foreground", True),
        ("/venv/bin/eawfd --foreground", True),
        ("/venv/bin/eawf doctor --fix", False),
        ("tail -f .eawfd/eawfd.log", False),
        ("tail -f eawfd", False),
        ("pgrep eawfd", False),
        ("vim /venv/bin/eawfd", False),
        ("grep eawf.runtime.daemon.main notes.txt", False),
        ("/venv/bin/python", False),
        ("", False),
    ],
)
def test_is_daemon_command(command: str, expected: bool) -> None:
    assert daemon_strays._is_daemon_command(command) is expected


def _process(age: float) -> daemon_strays._DaemonProcess:
    return daemon_strays._DaemonProcess(
        pid=4242, age_seconds=age, started="Wed Sep 30 08:47:05 2026"
    )


def test_unbound_daemon_in_startup_grace_is_not_a_stray() -> None:
    assert daemon_strays._stray_reason(_process(STARTUP_GRACE_SECONDS - 1), None) is None


def test_unbound_daemon_past_grace_is_a_stray() -> None:
    assert daemon_strays._stray_reason(_process(STARTUP_GRACE_SECONDS), None) == "unbound"


def test_daemon_whose_socket_path_is_gone_is_a_stray(tmp_path: Path) -> None:
    socket = str(tmp_path / "gone" / "eawfd.sock")

    assert daemon_strays._stray_reason(_process(0.0), socket) == "address_gone"


def test_daemon_whose_socket_answers_as_another_is_a_stray(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    socket = tmp_path / "eawfd.sock"
    socket.write_text("", encoding="utf-8")
    monkeypatch.setattr(daemon_strays, "daemon_pid_if_ready", lambda _dir: 1)

    assert daemon_strays._stray_reason(_process(0.0), str(socket)) == "address_lost"


def _at_base(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pinned: list[bytes] | None) -> str:
    """Bind a live daemon at the per-user base dir while clients dial a per-tree dir."""
    base = tmp_path / "base"
    base.mkdir()
    socket = base / "eawfd.sock"
    socket.write_text("", encoding="utf-8")
    monkeypatch.setattr(daemon_strays, "daemon_pid_if_ready", lambda _dir: 4242)
    monkeypatch.setattr(daemon_strays, "runtime_base_dir", lambda: base)
    monkeypatch.setattr(daemon_strays, "runtime_dir", lambda: base / "trees" / "abc")
    monkeypatch.setattr(daemon_strays, "_process_environment", lambda _pid: pinned)
    return str(socket)


def test_daemon_on_the_per_user_address_is_superseded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    socket = _at_base(tmp_path, monkeypatch, [b"HOME=/h"])

    assert daemon_strays._stray_reason(_process(0.0), socket) == "superseded_address"


def test_daemon_with_an_unreadable_environment_keeps_the_superseded_reading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    socket = _at_base(tmp_path, monkeypatch, None)

    assert daemon_strays._stray_reason(_process(0.0), socket) == "superseded_address"


def test_daemon_strays_repro_pinned_runtime_dir_flagged_superseded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pin = f"EAWF_RUNTIME_DIR={tmp_path / 'base'}".encode()
    socket = _at_base(tmp_path, monkeypatch, [b"HOME=/h", pin])

    assert daemon_strays._stray_reason(_process(0.0), socket) is None


def test_daemon_pinned_elsewhere_on_the_per_user_address_is_superseded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    socket = _at_base(tmp_path, monkeypatch, [b"EAWF_RUNTIME_DIR=/somewhere/else"])

    assert daemon_strays._stray_reason(_process(0.0), socket) == "superseded_address"


def test_check_is_ok_without_a_readable_process_table(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(daemon_strays, "find_stray_daemons", lambda: None)

    result = check_stray_daemons()

    assert result.status == "ok"
    assert "unavailable" in (result.detail or "")


def test_check_warns_naming_each_stray(monkeypatch: pytest.MonkeyPatch) -> None:
    stray = StrayDaemon(pid=77, started="Sat Sep 26 20:43:50 2026", reason="address_gone")
    monkeypatch.setattr(daemon_strays, "find_stray_daemons", lambda: [stray])

    result = check_stray_daemons()

    assert result.status == "warn"
    assert describe_stray(stray) in (result.detail or "")
    assert "eawf doctor --fix" in (result.detail or "")


def test_stray_model_refuses_unknown_fields() -> None:
    with pytest.raises(ValueError):
        StrayDaemon.model_validate(
            {"pid": 1, "started": "x", "reason": "unbound", "tree": "/somewhere"}
        )
