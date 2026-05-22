"""Unit tests for ``eawf backup`` create/list/restore/prune (P27-I01-W23).

Covers the four load-bearing guarantees of the manual snapshot surface:

- **create + list** — ``eawf backup create`` snapshots ``state.json`` +
  ``config.yaml`` + ``profile.yaml`` into a user-scope timestamped dir, and
  ``eawf backup list`` surfaces it.
- **restore round-trip** — ``create -> mutate state -> restore --ts`` lands
  the state.json bytes byte-identical to the snapshot.
- **prune --keep N** — keeps the N most-recent snapshots, deletes older ones.
- **unknown --ts** — exits ``USER_ERROR`` (1) with the canonical envelope.

The user-scope home is redirected to a tmp dir via the ``EAWF_HOME`` env seam
and the in-process ``home=`` kwarg, so no test ever touches the operator's real
``~/.eawf`` tree. Workspaces use ``-w`` so the resolver finds the tmp state.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from eawf.backup import (
    BackupError,
    UnknownSnapshotError,
    create_backup,
    list_backups,
    prune_backups,
    restore_backup,
)
from eawf.backup.store import BackupStore, repo_sha
from eawf.cli import exit_codes
from eawf.cli.app import app

runner = CliRunner()


def _seed_workspace(repo_root: Path, *, state: bytes = b'{"schema_version": "1.0"}\n') -> Path:
    """Create a minimal ``.ea/`` tree under *repo_root* and return state path."""
    ea = repo_root / ".ea"
    ea.mkdir(parents=True, exist_ok=True)
    state_path = ea / "state.json"
    state_path.write_bytes(state)
    (ea / "config.yaml").write_text("ui:\n  theme: dark\n", encoding="utf-8")
    (ea / "profile.yaml").write_text("ids:\n  - core\n", encoding="utf-8")
    return state_path


# --- create + list ----------------------------------------------------------


def test_create_backup_writes_all_three_artifacts(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    home = tmp_path / "home"
    state_path = _seed_workspace(repo)

    snapshot = create_backup(state_path, home=home)

    assert set(snapshot.artifacts) == {"state.json", "config.yaml", "profile.yaml"}
    assert snapshot.path.is_dir()
    assert (snapshot.path / "state.json").read_bytes() == state_path.read_bytes()


def test_create_backup_keys_under_repo_sha(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    home = tmp_path / "home"
    state_path = _seed_workspace(repo)

    snapshot = create_backup(state_path, home=home)

    expected_root = home / ".eawf" / "backups" / repo_sha(repo)
    assert snapshot.path.parent == expected_root


def test_create_backup_persists_note(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    home = tmp_path / "home"
    state_path = _seed_workspace(repo)

    snapshot = create_backup(state_path, note="pre-migration", home=home)

    assert snapshot.note == "pre-migration"
    assert (snapshot.path / "note.txt").read_text(encoding="utf-8") == "pre-migration"


def test_create_backup_missing_state_raises(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / ".ea").mkdir(parents=True)
    state_path = repo / ".ea" / "state.json"
    home = tmp_path / "home"

    with pytest.raises(BackupError, match="no state to back up"):
        create_backup(state_path, home=home)


def test_create_backup_without_config_or_profile(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    home = tmp_path / "home"
    ea = repo / ".ea"
    ea.mkdir(parents=True)
    state_path = ea / "state.json"
    state_path.write_bytes(b"{}\n")

    snapshot = create_backup(state_path, home=home)

    assert snapshot.artifacts == ("state.json",)


def test_list_backups_empty(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    home = tmp_path / "home"
    state_path = _seed_workspace(repo)

    assert list_backups(state_path, home=home) == []


def test_list_backups_most_recent_first(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    home = tmp_path / "home"
    state_path = _seed_workspace(repo)

    create_backup(state_path, home=home, when=datetime(2026, 5, 15, 9, 0, 0, tzinfo=UTC))
    create_backup(state_path, home=home, when=datetime(2026, 5, 17, 12, 0, 0, tzinfo=UTC))

    snapshots = list_backups(state_path, home=home)
    assert [s.ts for s in snapshots] == ["2026-05-17T12-00-00Z", "2026-05-15T09-00-00Z"]


# --- restore round-trip -----------------------------------------------------


def test_restore_round_trips_state_byte_identical(tmp_path: Path) -> None:
    """create -> mutate state -> restore lands byte-identical state.json."""
    repo = tmp_path / "repo"
    home = tmp_path / "home"
    original = b'{"schema_version": "1.0", "phase": "P27"}\n'
    state_path = _seed_workspace(repo, state=original)

    snapshot = create_backup(state_path, home=home, when=datetime(2026, 5, 17, 12, tzinfo=UTC))
    snapshot_bytes = (snapshot.path / "state.json").read_bytes()

    # Mutate the live state away from the snapshot.
    state_path.write_bytes(b'{"schema_version": "1.0", "phase": "P99-DRIFT"}\n')
    assert state_path.read_bytes() != snapshot_bytes

    result = restore_backup(state_path, ts=snapshot.ts, home=home)

    assert state_path.read_bytes() == snapshot_bytes == original
    assert "state.json" in result.restored


def test_restore_writes_pre_restore_safety_copy(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    home = tmp_path / "home"
    state_path = _seed_workspace(repo)

    snapshot = create_backup(state_path, home=home, when=datetime(2026, 5, 17, 12, tzinfo=UTC))
    pre_state = b'{"schema_version": "1.0", "live": true}\n'
    state_path.write_bytes(pre_state)

    result = restore_backup(
        state_path,
        ts=snapshot.ts,
        home=home,
        when=datetime(2026, 5, 17, 12, 30, 0, tzinfo=UTC),
    )

    assert result.pre_restore is not None
    assert result.pre_restore.read_bytes() == pre_state
    assert "pre-restore-state.json." in result.pre_restore.name


def test_restore_unknown_ts_raises(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    home = tmp_path / "home"
    state_path = _seed_workspace(repo)

    with pytest.raises(UnknownSnapshotError, match="unknown snapshot timestamp"):
        restore_backup(state_path, ts="1999-01-01T00-00-00Z", home=home)


# --- prune ------------------------------------------------------------------


def test_prune_keeps_n_most_recent(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    home = tmp_path / "home"
    state_path = _seed_workspace(repo)

    stamps = [
        datetime(2026, 5, 10, 9, tzinfo=UTC),
        datetime(2026, 5, 12, 9, tzinfo=UTC),
        datetime(2026, 5, 14, 9, tzinfo=UTC),
        datetime(2026, 5, 16, 9, tzinfo=UTC),
    ]
    for when in stamps:
        create_backup(state_path, home=home, when=when)

    removed = prune_backups(state_path, keep=2, home=home)

    survivors = [s.ts for s in list_backups(state_path, home=home)]
    assert survivors == ["2026-05-16T09-00-00Z", "2026-05-14T09-00-00Z"]
    assert set(removed) == {"2026-05-12T09-00-00Z", "2026-05-10T09-00-00Z"}


def test_prune_keep_zero_removes_all(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    home = tmp_path / "home"
    state_path = _seed_workspace(repo)
    create_backup(state_path, home=home, when=datetime(2026, 5, 16, 9, tzinfo=UTC))

    removed = prune_backups(state_path, keep=0, home=home)

    assert removed == ["2026-05-16T09-00-00Z"]
    assert list_backups(state_path, home=home) == []


def test_prune_keep_more_than_present_removes_none(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    home = tmp_path / "home"
    state_path = _seed_workspace(repo)
    create_backup(state_path, home=home, when=datetime(2026, 5, 16, 9, tzinfo=UTC))

    assert prune_backups(state_path, keep=5, home=home) == []


def test_prune_negative_keep_raises(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    home = tmp_path / "home"
    state_path = _seed_workspace(repo)

    with pytest.raises(BackupError, match="invalid keep count"):
        prune_backups(state_path, keep=-1, home=home)


def test_prune_preserves_pre_restore_copies(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    home = tmp_path / "home"
    state_path = _seed_workspace(repo)

    snap = create_backup(state_path, home=home, when=datetime(2026, 5, 16, 9, tzinfo=UTC))
    restore_backup(
        state_path,
        ts=snap.ts,
        home=home,
        when=datetime(2026, 5, 16, 10, tzinfo=UTC),
    )

    prune_backups(state_path, keep=0, home=home)

    store = BackupStore(repo, home=home)
    pre_restore_copies = list(store.root.glob("pre-restore-state.json.*"))
    assert len(pre_restore_copies) == 1


# --- CLI dispatch -----------------------------------------------------------


def test_cli_create_then_list(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = tmp_path / "repo"
    home = tmp_path / "home"
    _seed_workspace(repo)
    monkeypatch.setenv("EAWF_HOME", str(home))

    create_res = runner.invoke(app, ["-w", str(repo), "--json", "backup", "create"])
    assert create_res.exit_code == 0, create_res.output

    list_res = runner.invoke(app, ["-w", str(repo), "backup", "list"])
    assert list_res.exit_code == 0
    assert "state.json" in list_res.output


def test_cli_restore_round_trips(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = tmp_path / "repo"
    home = tmp_path / "home"
    original = b'{"schema_version": "1.0", "phase": "P27"}\n'
    state_path = _seed_workspace(repo, state=original)
    monkeypatch.setenv("EAWF_HOME", str(home))

    create_backup(state_path, home=home, when=datetime(2026, 5, 17, 12, tzinfo=UTC))
    state_path.write_bytes(b'{"drift": true}\n')

    res = runner.invoke(app, ["-w", str(repo), "backup", "restore", "--ts", "2026-05-17T12-00-00Z"])
    assert res.exit_code == 0, res.output
    assert state_path.read_bytes() == original


def test_cli_restore_unknown_ts_exits_user_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    home = tmp_path / "home"
    _seed_workspace(repo)
    monkeypatch.setenv("EAWF_HOME", str(home))

    res = runner.invoke(app, ["-w", str(repo), "backup", "restore", "--ts", "1999-01-01T00-00-00Z"])
    assert res.exit_code == exit_codes.USER_ERROR
    assert "unknown snapshot timestamp" in res.output


def test_cli_restore_unknown_ts_json_envelope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    home = tmp_path / "home"
    _seed_workspace(repo)
    monkeypatch.setenv("EAWF_HOME", str(home))

    res = runner.invoke(
        app,
        ["-w", str(repo), "--json", "backup", "restore", "--ts", "nope"],
    )
    assert res.exit_code == exit_codes.USER_ERROR
    assert '"error": "UserError"' in res.output
    assert '"exit_name": "USER_ERROR"' in res.output


def test_cli_prune_keeps_n(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = tmp_path / "repo"
    home = tmp_path / "home"
    state_path = _seed_workspace(repo)
    monkeypatch.setenv("EAWF_HOME", str(home))
    for day in (10, 12, 14):
        create_backup(state_path, home=home, when=datetime(2026, 5, day, 9, tzinfo=UTC))

    res = runner.invoke(app, ["-w", str(repo), "--json", "backup", "prune", "--keep", "1"])
    assert res.exit_code == 0, res.output

    survivors = [s.ts for s in list_backups(state_path, home=home)]
    assert survivors == ["2026-05-14T09-00-00Z"]
