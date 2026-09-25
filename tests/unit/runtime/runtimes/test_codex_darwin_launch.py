"""A seatbelt-jailed codex child on darwin leaves sandboxing to the eawf jail.

Codex wraps each model-issued command in its own ``sandbox-exec`` profile,
and macOS refuses to apply a seatbelt profile inside a process already
running under one. The eawf jail is itself a seatbelt profile, so a jailed
codex that keeps its own sandbox dies before it reaches the model. The fix
starts a seatbelt-jailed codex with ``--sandbox danger-full-access``.

The argv tests stub the jail seam so they run on every host. The launch
tests start the real eawf seatbelt jail around a fake ``codex`` that does
what the real one does first -- apply a nested seatbelt profile unless told
not to sandbox -- so the refusal is the kernel's, not a simulation. No model
is ever reached.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import stat
import sys
from pathlib import Path

import pytest

from eawf.runtime.runtimes.adapter import RuntimeSpawnError
from eawf.runtime.runtimes.codex import adapter as codex_adapter
from eawf.runtime.runtimes.codex.adapter import CodexAdapter

_SANDBOX_OFF = ["--sandbox", "danger-full-access"]

_INNER_ARGV = ["codex", "exec", "--json", "--skip-git-repo-check", "-m", "m", "prompt"]

_EVENTS: list[dict[str, object]] = [
    {"type": "thread.started", "thread_id": "thread-child"},
    {"type": "turn.started"},
    {"type": "item.completed", "item": {"id": "item_0", "type": "agent_message", "text": "ok"}},
    {"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}},
]

_darwin_seatbelt = pytest.mark.skipif(
    sys.platform != "darwin" or shutil.which("sandbox-exec") is None,
    reason="the nested-seatbelt refusal exists only under darwin sandbox-exec",
)


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
    """Under the seatbelt wrapper the flag lands after ``exec``, prompt last."""
    wrapped = _stub_jail(monkeypatch, platform="darwin")
    repo = _pin_repo_root(monkeypatch, tmp_path)

    jailed = codex_adapter._maybe_jail_argv(list(_INNER_ARGV), runtime="codex", cwd=str(repo))

    assert wrapped == [["codex", "exec", *_SANDBOX_OFF, *_INNER_ARGV[2:]]]
    assert jailed[0] == "wrapper"
    assert jailed[-1] == "prompt"


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


def _write_fake_codex(directory: Path) -> Path:
    """Write a fake ``codex`` that applies a nested seatbelt unless told not to.

    Given ``--sandbox danger-full-access`` it skips the nested profile, the
    way the real CLI does; otherwise it applies one through ``sandbox-exec``
    and exits with that refusal. On success it prints a well-formed
    ``codex exec --json`` event stream.
    """
    events = "\\n".join(json.dumps(event) for event in _EVENTS)
    script = directory / "codex"
    script.write_text(
        "#!/bin/sh\n"
        'case " $* " in\n'
        '  *" --sandbox danger-full-access "*) ;;\n'
        "  *) /usr/bin/sandbox-exec -p '(version 1)(allow default)' /usr/bin/true"
        " || exit 71 ;;\n"
        "esac\n"
        f"printf '{events}\\n'\n",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script


def _jailed_launch(repo: Path) -> str:
    """Spawn the fake codex through the real seatbelt jail; return its session."""
    adapter = CodexAdapter()
    adapter.cli_binary = str(_write_fake_codex(repo))
    result = asyncio.run(
        adapter.spawn_session("prompt", model="placeholder-model", cwd=str(repo), timeout=30.0)
    )
    return result.session_id


@_darwin_seatbelt
def test_spawn_session_darwin_jailed_codex_starts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The shipped darwin launch command starts a codex that sandboxes itself."""
    repo = _pin_repo_root(monkeypatch, tmp_path.resolve())

    assert _jailed_launch(repo) == "thread-child"


@_darwin_seatbelt
def test_spawn_session_darwin_nested_seatbelt_is_refused(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Gate-fire proof: without the flag the jailed child dies at its sandbox.

    Reverting the launch to the pre-fix command line reproduces the refusal,
    so the green launch above rests on the flag and not on a lenient fake.
    """
    monkeypatch.setattr(codex_adapter, "_defer_to_outer_seatbelt", lambda argv: argv)
    repo = _pin_repo_root(monkeypatch, tmp_path.resolve())

    with pytest.raises(RuntimeSpawnError):
        _jailed_launch(repo)
