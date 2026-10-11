"""A seatbelt-jailed codex child on darwin leaves sandboxing to the eawf jail.

Codex wraps each model-issued command in its own ``sandbox-exec`` profile,
and macOS refuses to apply a seatbelt profile inside a process already
running under one. The eawf jail is itself a seatbelt profile, so a jailed
codex that keeps its own sandbox dies before it reaches the model. The fix
starts a seatbelt-jailed ``codex app-server`` with a root
``-c sandbox_mode="danger-full-access"`` override.

The argv tests stub the jail seam so they run on every host.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from eawf.runtime.runtimes.codex import adapter as codex_adapter

_INNER_ARGV = ["codex", "app-server", "--listen", "stdio://"]


def _pin_repo_root(monkeypatch: pytest.MonkeyPatch, path: Path) -> Path:
    """Make *path* the repo root the jail confines the child to.

    Stands in for the ``git rev-parse`` lookup so the test needs no working
    tree; the jail itself is unchanged.
    """
    monkeypatch.setattr(codex_adapter, "_repo_root_for", lambda _cwd: path)
    return path


def _stub_jail(
    monkeypatch: pytest.MonkeyPatch,
    *,
    platform: str,
    wrapper_on_path: bool = True,
) -> list[list[str]]:
    """Make the jail seam decide as it would on *platform*, recording its input.

    Returns:
        The inner argv of every ``jail_command`` call, so a test reads the
        codex command line the jail was asked to wrap.
    """
    wrapped: list[list[str]] = []

    def _record(argv: list[str], **_kwargs: object) -> list[str]:
        wrapped.append(list(argv))
        return ["wrapper", *argv]

    monkeypatch.setattr(codex_adapter.sys, "platform", platform)
    monkeypatch.setattr(codex_adapter, "jail_supported", lambda: True)
    monkeypatch.setattr(
        codex_adapter.shutil,
        "which",
        lambda name: f"/usr/bin/{name}" if wrapper_on_path else None,
    )
    monkeypatch.setattr(codex_adapter, "jail_command", _record)
    return wrapped


def test_maybe_jail_argv_darwin_turns_codex_sandbox_off(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Under the seatbelt wrapper the override lands as a root ``-c``."""
    wrapped = _stub_jail(monkeypatch, platform="darwin")
    repo = _pin_repo_root(monkeypatch, tmp_path)

    jailed = codex_adapter._maybe_jail_argv(list(_INNER_ARGV), runtime="codex", cwd=str(repo))

    assert wrapped == [["codex", "-c", codex_adapter.OUTER_SEATBELT_OVERRIDE, *_INNER_ARGV[1:]]]
    assert jailed[0] == "wrapper"
    assert jailed[-3:] == _INNER_ARGV[1:]


def test_maybe_jail_argv_linux_keeps_codex_sandbox(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """bubblewrap does not collide with codex's sandbox, so linux is unchanged."""
    wrapped = _stub_jail(monkeypatch, platform="linux")
    repo = _pin_repo_root(monkeypatch, tmp_path)

    codex_adapter._maybe_jail_argv(list(_INNER_ARGV), runtime="codex", cwd=str(repo))

    assert wrapped == [_INNER_ARGV]


def test_maybe_jail_argv_unjailed_darwin_keeps_codex_sandbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no wrapper on PATH the child runs unjailed and keeps its own sandbox."""
    wrapped = _stub_jail(monkeypatch, platform="darwin", wrapper_on_path=False)

    argv = codex_adapter._maybe_jail_argv(list(_INNER_ARGV), runtime="codex", cwd=None)

    assert wrapped == []
    assert argv == _INNER_ARGV


def test_maybe_jail_argv_cwd_outside_repo_keeps_codex_sandbox(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A cwd the jail cannot confine runs unjailed, so codex keeps its sandbox."""
    wrapped = _stub_jail(monkeypatch, platform="darwin")
    monkeypatch.setattr(codex_adapter, "_repo_root_for", lambda _path: None)

    argv = codex_adapter._maybe_jail_argv(list(_INNER_ARGV), runtime="codex", cwd=str(tmp_path))

    assert wrapped == []
    assert argv == _INNER_ARGV
