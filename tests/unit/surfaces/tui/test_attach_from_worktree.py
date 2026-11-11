"""A console started in a linked worktree attaches to its repository's tree and daemon."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

import eawf.surfaces.tui.launch as launch
from eawf.runtime.daemon.runtime_dir import runtime_dir, socket_path
from eawf.surfaces.tui.console.attach import AttachRequest, AttachResult, resolve_attach
from tests._linked_worktree import repo_with_linked_worktree


def _set_isatty(monkeypatch: pytest.MonkeyPatch, *, value: bool) -> None:
    class _Stdout:
        @staticmethod
        def isatty() -> bool:
            return value

        @staticmethod
        def write(text: str) -> int:
            return len(text)

        @staticmethod
        def flush() -> None:
            return None

    monkeypatch.setattr("sys.stdout", _Stdout())


#: Where each platform keeps the per-tree runtime directories, relative to the
#: test's root: Linux under XDG_RUNTIME_DIR, macOS under the home whatever that says.
_TREES = {"linux": ("run", "eawfd", "trees"), "darwin": ("home", ".eawfd", "trees")}


@pytest.mark.parametrize("platform", sorted(_TREES))
@pytest.mark.parametrize("tty", [True, False], ids=["tty", "scripted"])
def test_console_attach_from_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tty: bool, platform: str
) -> None:
    main, linked = repo_with_linked_worktree(tmp_path / "repo")
    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.delenv("EA_STATE", raising=False)
    monkeypatch.delenv("EAWF_RUNTIME_DIR", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))

    monkeypatch.chdir(main)
    main_socket = socket_path()

    requests: list[AttachRequest] = []

    def recording(chrome: object, request: AttachRequest) -> AttachResult:
        requests.append(request)
        return resolve_attach(chrome, request)  # type: ignore[arg-type]

    monkeypatch.setattr("eawf.surfaces.tui.launch.resolve_attach", recording)
    monkeypatch.setattr(launch, "_launch_entry", lambda **_kwargs: 0)
    monkeypatch.setattr(launch, "_launch_native", lambda **_kwargs: 0)
    monkeypatch.setattr(launch, "_emit_plain", lambda **_kwargs: 0)
    _set_isatty(monkeypatch, value=tty)

    monkeypatch.chdir(linked)
    assert launch.launch_tui(workspace=None, no_input=False, plain=False) == 0

    [request] = requests
    assert request.state_path == main / ".ea" / "state.json"
    assert request.repo_root == main
    assert request.authority.root == main / ".ea"
    # The live link dials the runtime directory of the tree it resolves from here.
    assert socket_path() == main_socket
    assert runtime_dir().parent == tmp_path.joinpath(*_TREES[platform])
