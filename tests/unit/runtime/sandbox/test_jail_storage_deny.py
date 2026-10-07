"""The native-Run jail denies reads of the daemon's storage, not of the workspace."""

from __future__ import annotations

from pathlib import Path

import pytest

from eawf.runtime.sandbox.jail import build_jail_argv, build_seatbelt_profile, jail_command

_CLAUDE = "claude-code"
_CODEX = "codex"


def _tree(tmp_path: Path, *, leased_under_storage: bool) -> tuple[Path, Path, Path]:
    """Return ``(storage, workspace, private home)`` with the workspace placed as asked."""
    storage = tmp_path / "repo" / ".ea"
    storage.mkdir(parents=True)
    parent = storage / "worktrees" if leased_under_storage else tmp_path / "leases"
    workspace = parent / "ws"
    private = workspace / ".eawf-mcp" / "codex-home"
    private.mkdir(parents=True)
    return storage.resolve(), workspace.resolve(), private.resolve()


@pytest.mark.parametrize("runtime", [_CLAUDE, _CODEX])
def test_the_storage_deny_precedes_every_workspace_write_allow(
    tmp_path: Path, runtime: str
) -> None:
    storage, workspace, private = _tree(tmp_path, leased_under_storage=False)

    lines = build_seatbelt_profile(
        cwd=workspace,
        runtime=runtime,
        home=tmp_path,
        private_home=private,
        storage_dirs=(storage,),
    ).splitlines()

    deny = lines.index(f'(deny file-read* (subpath "{storage}"))')
    assert deny < lines.index(f'(allow file-write* (subpath "{workspace}"))')
    assert deny < lines.index(f'(allow file-write* (subpath "{private}"))')
    # A workspace outside the storage needs no read carve-out.
    assert not [line for line in lines if line.startswith("(allow file-read-metadata")]


def test_a_workspace_leased_under_the_storage_is_read_back_after_the_deny(
    tmp_path: Path,
) -> None:
    storage, workspace, private = _tree(tmp_path, leased_under_storage=True)

    lines = build_seatbelt_profile(
        cwd=workspace,
        runtime=_CODEX,
        home=tmp_path,
        private_home=private,
        storage_dirs=(storage,),
    ).splitlines()

    deny = lines.index(f'(deny file-read* (subpath "{storage}"))')
    assert deny < lines.index(f'(allow file-read* (subpath "{workspace}"))')
    assert deny < lines.index(f'(allow file-read* (subpath "{private}"))')
    metadata = [line for line in lines if line.startswith("(allow file-read-metadata")]
    assert metadata.count(f'(allow file-read-metadata (literal "{storage}"))') == 1
    assert f'(allow file-read-metadata (literal "{storage / "worktrees"}"))' in metadata
    # The deny closes the sibling leases: only the Run's own subtree comes back.
    assert not any(f'"{storage / "worktrees"}")' in line for line in lines if "subpath" in line)


def test_no_storage_dirs_emit_no_storage_rules(tmp_path: Path) -> None:
    _storage, workspace, private = _tree(tmp_path, leased_under_storage=True)

    profile = build_seatbelt_profile(
        cwd=workspace, runtime=_CODEX, home=tmp_path, private_home=private
    )

    assert "file-read-metadata" not in profile
    assert f'(allow file-read* (subpath "{workspace}"))' not in profile


def test_every_storage_dir_is_denied(tmp_path: Path) -> None:
    storage, workspace, private = _tree(tmp_path, leased_under_storage=False)
    wal = tmp_path / "runtime" / "wal"

    profile = build_seatbelt_profile(
        cwd=workspace,
        runtime=_CODEX,
        home=tmp_path,
        private_home=private,
        storage_dirs=(storage, wal),
    )

    assert f'(deny file-read* (subpath "{storage}"))' in profile
    assert f'(deny file-read* (subpath "{wal.resolve()}"))' in profile
    # The runtime dir holding the socket stays readable.
    assert f'(subpath "{wal.parent.resolve()}")' not in profile


def test_jail_command_threads_the_storage_into_the_darwin_profile(tmp_path: Path) -> None:
    storage, workspace, private = _tree(tmp_path, leased_under_storage=True)

    argv = jail_command(
        ["claude"],
        runtime=_CLAUDE,
        cwd=workspace,
        root=workspace,
        platform="darwin",
        home=tmp_path,
        private_home=private,
        storage_dirs=(storage,),
    )

    assert argv[:2] == ["sandbox-exec", "-p"]
    assert argv[-1] == "claude"
    assert f'(deny file-read* (subpath "{storage}"))' in argv[2]


def test_the_linux_mask_lands_before_the_workspace_bind(tmp_path: Path) -> None:
    storage, workspace, private = _tree(tmp_path, leased_under_storage=True)
    absent = tmp_path / "runtime" / "wal"

    argv = build_jail_argv(
        _CODEX,
        cwd=workspace,
        root=workspace,
        platform="linux",
        home=tmp_path,
        private_home=private,
        storage_dirs=(storage, absent),
    )

    mask = argv.index(str(storage))
    assert argv[mask - 1] == "--tmpfs"
    assert mask < argv.index(str(workspace)) < argv.index(str(private))
    # An absent directory has no mount point in the read-only root, so it is skipped.
    assert str(absent) not in argv


def test_an_unknown_runtime_is_refused_with_storage_dirs(tmp_path: Path) -> None:
    storage, workspace, private = _tree(tmp_path, leased_under_storage=False)

    with pytest.raises(ValueError, match="unknown runtime lane"):
        build_seatbelt_profile(
            cwd=workspace,
            runtime="nope",
            home=tmp_path,
            private_home=private,
            storage_dirs=(storage,),
        )
