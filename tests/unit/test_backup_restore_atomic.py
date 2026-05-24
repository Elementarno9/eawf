"""Atomicity + locking guarantees for ``BackupStore.restore_snapshot`` (P27-I02-W07).

The restore overwrites ``state.json`` — the daemon's sole canonical mutable
surface — so the swap MUST be all-or-nothing and MUST respect the sibling
portalock a concurrent daemon write would hold. These tests cover the three
load-bearing guarantees the bare-``shutil.copyfile`` restore lacked:

- **interrupt-safety** — an interruption mid-restore (``os.replace`` raising
  after the temp write) leaves ``state.json`` the fully-old valid document,
  never a truncated/partial file.
- **happy path** — a normal restore lands the snapshot content, re-loadable as
  JSON (and the pre-restore live file is captured first via the service).
- **lock respect** — a restore competing with a held portalock on
  ``state.json`` raises :exc:`portalock.LockTimeout` rather than clobbering.

The user-scope home is redirected to a tmp dir via the ``home=`` kwarg so no
test ever touches the operator's real ``~/.eawf`` tree.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

import orjson
import pytest

from eawf.platform.backup.service import restore_backup
from eawf.platform.backup.store import BackupStore, format_timestamp
from eawf.runtime.lock import portalock

# A valid, daemon-canonical (orjson-shaped) state document used as the snapshot
# content so the round-trip is unambiguous JSON.
_SNAPSHOT_STATE: bytes = (
    orjson.dumps(
        {"schema_version": "1.0", "phase": "P27"},
        option=orjson.OPT_INDENT_2 | orjson.OPT_SORT_KEYS,
    )
    + b"\n"
)
_LIVE_STATE: bytes = (
    orjson.dumps(
        {"schema_version": "1.0", "phase": "P99-DRIFT"},
        option=orjson.OPT_INDENT_2 | orjson.OPT_SORT_KEYS,
    )
    + b"\n"
)


def _seed(repo_root: Path, *, live: bytes, snapshot: bytes) -> tuple[BackupStore, str, Path]:
    """Seed a repo with a live ``state.json`` and one snapshot.

    Returns the bound store, the snapshot ``ts``, and the live state path.
    """
    ea = repo_root / ".ea"
    ea.mkdir(parents=True, exist_ok=True)
    state_path = ea / "state.json"
    state_path.write_bytes(live)

    home = repo_root.parent / "home"
    store = BackupStore(repo_root, home=home)
    ts = format_timestamp(datetime(2026, 5, 17, 12, 0, 0, tzinfo=UTC))
    snap_dir = store.root / ts
    snap_dir.mkdir(parents=True, exist_ok=True)
    (snap_dir / "state.json").write_bytes(snapshot)
    return store, ts, state_path


def test_restore_snapshot_interrupt_leaves_state_fully_old(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """os.replace raising mid-swap leaves state.json the fully-old document."""
    repo = tmp_path / "repo"
    store, ts, state_path = _seed(repo, live=_LIVE_STATE, snapshot=_SNAPSHOT_STATE)
    snapshot = store.get_snapshot(ts)
    assert snapshot is not None

    real_replace = os.replace

    def _boom(src: object, dst: object, *args: object, **kwargs: object) -> None:
        # Fail only on the state.json swap; leave any other rename untouched so
        # the failure models a crash at the atomic-commit point precisely.
        if str(dst) == str(state_path):
            raise OSError("injected interrupt at os.replace")
        real_replace(src, dst, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "replace", _boom)

    with pytest.raises(OSError, match="injected interrupt"):
        store.restore_snapshot(snapshot)

    # The live file must be the fully-old document: parses, byte-equal to the
    # pre-restore content, never a truncated/partial write.
    raw = state_path.read_bytes()
    assert raw == _LIVE_STATE
    assert orjson.loads(raw) == {"schema_version": "1.0", "phase": "P99-DRIFT"}

    # No orphaned temp file is left behind in the .ea dir.
    leftovers = list(state_path.parent.glob("state.json.tmp.*"))
    assert leftovers == []


def test_restore_snapshot_happy_path_lands_snapshot_content(tmp_path: Path) -> None:
    """A normal restore replaces state.json with the snapshot bytes."""
    repo = tmp_path / "repo"
    store, ts, state_path = _seed(repo, live=_LIVE_STATE, snapshot=_SNAPSHOT_STATE)
    snapshot = store.get_snapshot(ts)
    assert snapshot is not None

    restored = store.restore_snapshot(snapshot)

    assert restored == ["state.json"]
    raw = state_path.read_bytes()
    assert raw == _SNAPSHOT_STATE
    # Re-loadable as a JSON document (proxy for State.model_validate on a real
    # full state payload — this fixture is a minimal valid document).
    assert orjson.loads(raw) == {"schema_version": "1.0", "phase": "P27"}


def test_restore_backup_captures_pre_restore_before_overwrite(tmp_path: Path) -> None:
    """The service backs up the live file first, so restore is itself reversible."""
    repo = tmp_path / "repo"
    home = tmp_path / "home"
    ea = repo / ".ea"
    ea.mkdir(parents=True)
    state_path = ea / "state.json"
    state_path.write_bytes(_LIVE_STATE)

    store = BackupStore(repo, home=home)
    ts = format_timestamp(datetime(2026, 5, 17, 12, 0, 0, tzinfo=UTC))
    snap_dir = store.root / ts
    snap_dir.mkdir(parents=True, exist_ok=True)
    (snap_dir / "state.json").write_bytes(_SNAPSHOT_STATE)

    result = restore_backup(
        state_path,
        ts=ts,
        home=home,
        when=datetime(2026, 5, 17, 12, 30, 0, tzinfo=UTC),
    )

    # Pre-restore copy holds the OLD live bytes, captured before the swap.
    assert result.pre_restore is not None
    assert result.pre_restore.read_bytes() == _LIVE_STATE
    # And the live file now holds the snapshot content.
    assert state_path.read_bytes() == _SNAPSHOT_STATE


def test_restore_snapshot_respects_held_portalock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A concurrent holder of the state.json portalock blocks the restore swap.

    Models an in-flight daemon write: the restore acquires the same sibling
    lock and so fails to acquire it (rather than clobbering a half-written
    ``state.json``) while a competing writer holds it.
    """
    repo = tmp_path / "repo"
    store, ts, state_path = _seed(repo, live=_LIVE_STATE, snapshot=_SNAPSHOT_STATE)
    snapshot = store.get_snapshot(ts)
    assert snapshot is not None

    # Shorten the restore's lock wait so the contended path fails fast instead
    # of blocking for the source's full 5 s timeout.
    real_acquire = portalock.acquire
    acquired_targets: list[Path] = []

    def _short_acquire(target: Path, **kwargs: object) -> object:
        acquired_targets.append(Path(target))
        return real_acquire(target, timeout=0.2)

    monkeypatch.setattr("eawf.platform.backup.store.portalock.acquire", _short_acquire)

    with real_acquire(state_path, timeout=2.0), pytest.raises(portalock.LockTimeout):
        store.restore_snapshot(snapshot)

    # The restore reached for the lock on the live state.json (proving it
    # serialises through the lock, not a bare copy).
    assert state_path in acquired_targets
    # The live file is untouched while the competing writer held the lock.
    assert state_path.read_bytes() == _LIVE_STATE
