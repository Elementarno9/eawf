"""A live smoke's daemon reads the operator's codex login, not the suite's empty one.

The suite points ``CODEX_HOME`` at an empty history directory, so a daemon
that inherited it gave the codex canary no credential and the turn failed
401. Only the spawned processes get the operator's home back.
"""

from __future__ import annotations

from tests.e2e.test_smoke_milestone import smoke_daemon_env

_SUITE = {"CODEX_HOME": "/suite/history-home", "PATH": "/bin"}


def test_a_live_smoke_daemon_gets_the_operator_codex_home() -> None:
    env = smoke_daemon_env(_SUITE, provider="codex", operator_codex_home="/operator/codex")

    assert env["CODEX_HOME"] == "/operator/codex"
    assert env["PATH"] == "/bin"


def test_a_live_smoke_daemon_without_an_operator_home_uses_the_default() -> None:
    env = smoke_daemon_env(_SUITE, provider="claude", operator_codex_home=None)

    assert "CODEX_HOME" not in env


def test_the_stub_smoke_keeps_the_suite_isolation() -> None:
    env = smoke_daemon_env(_SUITE, provider="stub", operator_codex_home="/operator/codex")

    assert env == _SUITE


def test_the_test_environment_itself_is_not_changed() -> None:
    environ = dict(_SUITE)

    smoke_daemon_env(environ, provider="codex", operator_codex_home=None)

    assert environ == _SUITE
