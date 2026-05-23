"""Path-traversal guard on ``BackupStore.get_snapshot`` (P27-I02-W09).

``get_snapshot`` joins ``self.root / ts`` with an operator-supplied ``--ts``
value. Without a guard, ``--ts ../../..`` or an absolute path escapes the
per-repo backup tree (a path-traversal read). These tests pin the guard:
*ts* is validated against the snapshot-id timestamp pattern *before* the
path-join, so traversal / absolute / malformed input is rejected (raising the
typed :class:`BackupError` the CLI renders as ``USER_ERROR``) and only the
exact filesystem-safe ISO-8601 dir-name the store writes is accepted.

The guard MUST fire before any filesystem access, so the rejection tests run
against a store whose backup root does not exist on disk: a raise there proves
validation precedes the ``root / ts`` join (no path outside ``root`` is ever
stat-ed).
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from eawf.backup import BackupError
from eawf.backup.store import BackupStore, format_timestamp
from eawf.cli import exit_codes
from eawf.cli.app import app

runner = CliRunner()


def _store(tmp_path: Path) -> BackupStore:
    """Bind a store whose per-repo backup root is absent on disk."""
    repo = tmp_path / "repo"
    home = tmp_path / "home"
    return BackupStore(repo, home=home)


# --- error path: traversal / absolute / malformed are rejected --------------


def test_get_snapshot_parent_traversal_raises(tmp_path: Path) -> None:
    store = _store(tmp_path)

    with pytest.raises(BackupError, match=r"invalid snapshot timestamp"):
        store.get_snapshot("../x")


def test_get_snapshot_dotdot_segment_raises(tmp_path: Path) -> None:
    store = _store(tmp_path)

    with pytest.raises(BackupError, match=r"invalid snapshot timestamp"):
        store.get_snapshot("..")


def test_get_snapshot_nested_relative_raises(tmp_path: Path) -> None:
    store = _store(tmp_path)

    with pytest.raises(BackupError, match=r"invalid snapshot timestamp"):
        store.get_snapshot("a/b")


def test_get_snapshot_absolute_posix_path_raises(tmp_path: Path) -> None:
    store = _store(tmp_path)

    with pytest.raises(BackupError, match=r"invalid snapshot timestamp"):
        store.get_snapshot("/etc/passwd")


def test_get_snapshot_absolute_tmp_path_raises(tmp_path: Path) -> None:
    store = _store(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("secret\n", encoding="utf-8")

    with pytest.raises(BackupError, match=r"invalid snapshot timestamp"):
        store.get_snapshot(str(outside))


def test_get_snapshot_empty_raises(tmp_path: Path) -> None:
    store = _store(tmp_path)

    with pytest.raises(BackupError, match=r"invalid snapshot timestamp"):
        store.get_snapshot("")


def test_get_snapshot_trailing_newline_raises(tmp_path: Path) -> None:
    """An otherwise-valid timestamp with a trailing newline is rejected.

    The pattern anchors on ``\\Z`` (strict end-of-string), not ``$`` (which
    matches before a trailing ``\\n``), so a ``--ts`` value that smuggles a
    newline past the timestamp does not pass the guard.
    """
    store = _store(tmp_path)
    ts = format_timestamp(datetime(2026, 5, 17, 12, 0, 0, tzinfo=UTC))

    with pytest.raises(BackupError, match=r"invalid snapshot timestamp"):
        store.get_snapshot(f"{ts}\n")


def test_get_snapshot_error_message_quotes_bad_ts(tmp_path: Path) -> None:
    """The bad value is shown ``!r`` so the rejected input is visible."""
    store = _store(tmp_path)

    with pytest.raises(BackupError, match=r"invalid snapshot timestamp: '\.\./x'"):
        store.get_snapshot("../x")


def test_get_snapshot_rejects_before_filesystem_access(tmp_path: Path) -> None:
    """The guard fires before the ``root / ts`` join touches the filesystem.

    The backup root does not exist on disk, and a traversal target outside it
    is staged. A raise (rather than ``None``) proves validation precedes any
    path access — no path outside ``root`` is ever stat-ed.
    """
    store = _store(tmp_path)
    assert not store.root.exists()
    # Stage a real directory the traversal would otherwise resolve into.
    escape_target = tmp_path / "escape"
    escape_target.mkdir()

    with pytest.raises(BackupError, match=r"invalid snapshot timestamp"):
        store.get_snapshot(f"../{escape_target.name}")


# --- boundary: a valid timestamp id still works -----------------------------


def test_get_snapshot_valid_ts_returns_snapshot(tmp_path: Path) -> None:
    store = _store(tmp_path)
    ts = format_timestamp(datetime(2026, 5, 17, 12, 0, 0, tzinfo=UTC))
    snap_dir = store.root / ts
    snap_dir.mkdir(parents=True)
    (snap_dir / "state.json").write_bytes(b"{}\n")

    snapshot = store.get_snapshot(ts)

    assert snapshot is not None
    assert snapshot.ts == ts
    assert snapshot.path == snap_dir
    assert snapshot.artifacts == ("state.json",)


def test_get_snapshot_valid_ts_absent_returns_none(tmp_path: Path) -> None:
    """A well-formed but nonexistent timestamp passes the guard and returns None."""
    store = _store(tmp_path)
    store.root.mkdir(parents=True)

    assert store.get_snapshot("1999-01-01T00-00-00Z") is None


# --- CLI: a traversal --ts renders a clean USER_ERROR, not a traceback ------


def test_cli_restore_traversal_ts_exits_user_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``backup restore --ts ../x`` exits USER_ERROR with the canonical envelope.

    The guard is defense against path-traversal, but the operator-facing
    failure must still be a clean envelope (the guard raises ``BackupError``,
    which the CLI catches) — never an internal traceback.
    """
    repo = tmp_path / "repo"
    home = tmp_path / "home"
    ea = repo / ".ea"
    ea.mkdir(parents=True)
    (ea / "state.json").write_bytes(b"{}\n")
    monkeypatch.setenv("EAWF_HOME", str(home))

    res = runner.invoke(app, ["-w", str(repo), "--json", "backup", "restore", "--ts", "../x"])

    assert res.exit_code == exit_codes.USER_ERROR
    assert '"error": "UserError"' in res.output
    assert "invalid snapshot timestamp" in res.output
