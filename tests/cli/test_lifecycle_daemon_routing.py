"""Verb→daemon routing + WAL-backed in-process fallback for lifecycle verbs.

P27-I02-W18 routed the eager-scope mutating lifecycle verbs through the
generic :func:`eawf.surfaces.cli._dispatch._mutate_via_daemon` shim (rule 4: the
daemon is the canonical mutator) and flipped the in-process fallback in
:func:`eawf.surfaces.cli.commands.lifecycle._commit_mutation` to a **state-first,
WAL-backed** ordering that mirrors the daemon's outcome-WAL algorithm.

The suite has three planes:

1. **Routing** — with ``daemon.proxy_enabled=True`` + a reachable daemon,
   each routed verb marshals one typed
   :class:`~eawf.kernel.state.mutations.Mutation` of the correct
   :class:`~eawf.kernel.state.mutations.MutationKind` across ``state.mutate``;
   the in-process fallback does NOT run.
2. **Registry** — the verb→kind table the routing test parametrises is
   pinned against the daemon's apply registry so a kind can never be
   routed that the daemon cannot dispatch.
3. **Fallback + crash safety** — with the daemon down (transport error)
   the in-process WAL-backed writer carries the mutation; an injected
   crash between the state write and the event append leaves NO phantom
   event (no event row whose state change did not commit) and the next
   mutation's ``replay_wal`` reconciles the ``.pending`` record.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import orjson
import pytest
from typer.testing import CliRunner

from eawf.kernel.state.mutations import MutationKind
from eawf.surfaces.cli import _dispatch
from eawf.surfaces.cli.app import app

pytestmark = pytest.mark.unit

runner = CliRunner()


# ---- fixtures ---------------------------------------------------------------


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Yield a temp workspace with ``EA_STATE`` pointing inside it.

    Bootstrap mutations run daemonless (``EAWF_DAEMONLESS=1``) so the
    in-process WAL-backed writer brings the state up; the per-test
    proxy-up scenario clears the env and enables proxying just before
    the verb under test.
    """
    state_path = tmp_path / ".ea" / "state.json"
    monkeypatch.setenv("EA_STATE", str(state_path))
    monkeypatch.setenv("EAWF_DAEMONLESS", "1")
    yield tmp_path


def _state_path(workspace: Path) -> Path:
    return workspace / ".ea" / "state.json"


def _event_path(workspace: Path) -> Path:
    return workspace / ".ea" / "store" / "event.jsonl"


def _wal_dir(workspace: Path) -> Path:
    return workspace / ".ea" / "locks" / "wal"


def _read_state(workspace: Path) -> dict[str, Any]:
    return orjson.loads(_state_path(workspace).read_bytes())  # type: ignore[no-any-return]


def _event_commands(workspace: Path) -> list[str]:
    path = _event_path(workspace)
    if not path.exists():
        return []
    out: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            out.append(orjson.loads(line)["payload"]["command"])
    return out


def _bootstrap_to_pending_wave(workspace: Path, wave_id: str = "P01-I01-W01") -> None:
    """Bring the state up to one PENDING wave under an ACTIVE P01-I01."""
    assert (
        runner.invoke(
            app, ["project", "init", "QR", "--title", "Quant", "--domains", "quant"]
        ).exit_code
        == 0
    )
    assert runner.invoke(app, ["phase", "open", "--auto", "--title", "P1"]).exit_code == 0
    assert runner.invoke(app, ["iter", "open", "--phase", "P01", "--title", "I1"]).exit_code == 0
    assert (
        runner.invoke(
            app,
            [
                "wave",
                "plan",
                "P01-I01",
                "--id",
                wave_id,
                "--title",
                "w",
                "--files",
                "src/",
                "--effort-bucket",
                "M",
            ],
        ).exit_code
        == 0
    )


# ---- fake daemon client -----------------------------------------------------


class _CapturingClient:
    """DaemonClient stand-in that records the marshalled mutation."""

    last_kind: MutationKind | None = None
    last_params: dict[str, Any] | None = None
    last_scope_id: str | None = None
    call_count: int = 0

    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    def __enter__(self) -> _CapturingClient:
        return self

    def __exit__(self, *_args: Any) -> None:
        return None

    def state_mutate(
        self,
        mutation: Any,
        *,
        idempotency_key: str | None = None,
        repo_root: str | None = None,
    ) -> dict[str, Any]:
        _CapturingClient.last_kind = mutation.kind
        _CapturingClient.last_params = dict(mutation.params)
        _CapturingClient.last_scope_id = mutation.scope_id
        _CapturingClient.call_count += 1
        return {
            "event": {"id": "EV-routed-1", "kind": "event", "scope_id": mutation.scope_id},
            "before_version": "before-x",
            "after_version": "after-x",
            "idempotent_replay": False,
        }


class _CapturingWorktreeClient:
    """DaemonClient stand-in that records daemon-owned worktree calls."""

    last_method: str | None = None
    last_params: dict[str, Any] | None = None
    call_count: int = 0

    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    def __enter__(self) -> _CapturingWorktreeClient:
        return self

    def __exit__(self, *_args: Any) -> None:
        return None

    def call(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        _CapturingWorktreeClient.last_method = method
        _CapturingWorktreeClient.last_params = dict(params or {})
        _CapturingWorktreeClient.call_count += 1
        if method == "state.wave_land_batch":
            return {
                "landed": [],
                "failed_wave": None,
                "error": None,
                "skipped": [],
            }
        if method == "state.wave_autoland":
            return {
                "order": [],
                "landed": [],
                "failed_wave": None,
                "error": None,
                "remaining": [],
                "dry_run": False,
            }
        return {
            "wave": params["wave_id"] if params else "P01-I01-W01",
            "commits": ["abc123"],
            "outcome": "landed 1 commit(s) via wave land",
            "closed": True,
            "worktree_cleaned": False,
            "merged_commit": "abc123",
        }


def _enable_proxy(monkeypatch: pytest.MonkeyPatch, *, client: type) -> None:
    """Switch from the daemonless bootstrap to a proxy-up scenario."""
    monkeypatch.delenv("EAWF_DAEMONLESS", raising=False)
    # The proxy gate in ``_run_mutation`` consults ``_proxy_enabled``.
    monkeypatch.setattr("eawf.surfaces.cli._mutation._proxy_enabled", lambda _ws: True)
    # ``escalate_mutation`` auto-spawns when no daemon is up; stub the spawn.
    monkeypatch.setattr(_dispatch, "ensure_daemon", lambda _runtime=None: 4242)
    monkeypatch.setattr("eawf.surfaces.cli._daemon_client.DaemonClient", client)


# ---- plane 1: routing -------------------------------------------------------

#: Verb argv (after the bootstrap to a PENDING wave) → expected MutationKind.
#: Each tuple is ``(test_id, argv, expected_kind, expected_scope_id)``.
_ROUTED_VERBS: list[tuple[str, list[str], MutationKind, str]] = [
    (
        "wave_plan",
        [
            "wave",
            "plan",
            "P01-I01",
            "--id",
            "P01-I01-W02",
            "--title",
            "w2",
            "--files",
            "src/",
            "--effort-bucket",
            "M",
        ],
        MutationKind.ROADMAP_REVISE,
        "P01-I01-W02",
    ),
    (
        "wave_claim",
        ["wave", "claim", "P01-I01-W01", "--session", "S-1"],
        MutationKind.WAVE_CLAIM,
        "P01-I01-W01",
    ),
    (
        "wave_fail",
        ["wave", "fail", "P01-I01-W01", "--reason", "boom"],
        MutationKind.WAVE_FAIL,
        "P01-I01-W01",
    ),
    (
        "phase_activate",
        ["phase", "activate", "P01"],
        MutationKind.PHASE_ACTIVATE,
        "P01",
    ),
]


@pytest.mark.parametrize(("test_id", "argv", "expected_kind", "expected_scope"), _ROUTED_VERBS)
def test_lifecycle_verb_proxies_to_daemon_when_up(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
    test_id: str,
    argv: list[str],
    expected_kind: MutationKind,
    expected_scope: str,
) -> None:
    """Each routed verb marshals the correct typed mutation across the daemon."""
    _bootstrap_to_pending_wave(workspace)
    _enable_proxy(monkeypatch, client=_CapturingClient)
    _CapturingClient.last_kind = None
    _CapturingClient.last_params = None
    _CapturingClient.last_scope_id = None
    _CapturingClient.call_count = 0

    state_before = _state_path(workspace).read_bytes()

    res = runner.invoke(app, argv)
    assert res.exit_code == 0, res.stdout

    # The daemon path was taken exactly once with the right discriminator.
    assert _CapturingClient.call_count == 1
    assert _CapturingClient.last_kind is expected_kind
    assert _CapturingClient.last_scope_id == expected_scope
    # The (fake) daemon owns the write; the in-process fallback did NOT run,
    # so the local state.json is byte-for-byte unchanged.
    assert _state_path(workspace).read_bytes() == state_before


def test_iter_close_proxies_to_daemon_when_up(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``iter close`` routes ITER_CLOSE through the daemon (separate setup)."""
    _bootstrap_to_pending_wave(workspace)
    # Close the only wave so the iter has a closed wave but no open ones.
    assert runner.invoke(app, ["wave", "claim", "P01-I01-W01", "--session", "S-1"]).exit_code == 0
    assert runner.invoke(app, ["wave", "close", "P01-I01-W01", "--outcome", "done"]).exit_code == 0
    _enable_proxy(monkeypatch, client=_CapturingClient)
    _CapturingClient.last_kind = None
    _CapturingClient.call_count = 0

    res = runner.invoke(app, ["iter", "close", "P01-I01", "--audit", "AUD-1"])
    assert res.exit_code == 0, res.stdout
    assert _CapturingClient.call_count == 1
    assert _CapturingClient.last_kind is MutationKind.ITER_CLOSE
    assert _CapturingClient.last_params == {"iter_id": "P01-I01", "audit_id": "AUD-1"}


def test_track_add_proxies_to_daemon_when_up(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``track add`` routes TRACK_ADD through the daemon."""
    _bootstrap_to_pending_wave(workspace)
    _enable_proxy(monkeypatch, client=_CapturingClient)
    _CapturingClient.last_kind = None
    _CapturingClient.last_params = None
    _CapturingClient.last_scope_id = None
    _CapturingClient.call_count = 0
    state_before = _state_path(workspace).read_bytes()

    res = runner.invoke(
        app,
        [
            "track",
            "add",
            "COLLAR",
            "--kind",
            "strategy",
            "--title",
            "Collar",
            "--domains",
            "quant,research",
        ],
    )

    assert res.exit_code == 0, res.stdout
    assert _CapturingClient.call_count == 1
    assert _CapturingClient.last_kind is MutationKind.TRACK_ADD
    assert _CapturingClient.last_scope_id == "COLLAR"
    assert _CapturingClient.last_params == {
        "code": "COLLAR",
        "kind": "strategy",
        "title": "Collar",
        "domains": ["quant", "research"],
    }
    assert _state_path(workspace).read_bytes() == state_before


def test_track_switch_proxies_to_daemon_when_up(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``track switch`` routes TRACK_SWITCH through the daemon."""
    _bootstrap_to_pending_wave(workspace)
    assert (
        runner.invoke(
            app,
            ["track", "add", "COLLAR", "--kind", "strategy", "--title", "Collar"],
        ).exit_code
        == 0
    )
    _enable_proxy(monkeypatch, client=_CapturingClient)
    _CapturingClient.last_kind = None
    _CapturingClient.last_params = None
    _CapturingClient.last_scope_id = None
    _CapturingClient.call_count = 0
    state_before = _state_path(workspace).read_bytes()

    res = runner.invoke(app, ["track", "switch", "COLLAR"])

    assert res.exit_code == 0, res.stdout
    assert _CapturingClient.call_count == 1
    assert _CapturingClient.last_kind is MutationKind.TRACK_SWITCH
    assert _CapturingClient.last_scope_id == "COLLAR"
    assert _CapturingClient.last_params == {"code": "COLLAR"}
    assert _state_path(workspace).read_bytes() == state_before


def test_wave_land_proxies_to_daemon_owned_method(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``wave land`` routes the whole state write through daemon-owned RPC."""
    from eawf.surfaces.cli.commands import worktree as worktree_cmd

    _bootstrap_to_pending_wave(workspace)
    monkeypatch.setattr(worktree_cmd, "_resolve_repo_root", lambda _state_path: workspace)
    _enable_proxy(monkeypatch, client=_CapturingWorktreeClient)
    _CapturingWorktreeClient.last_method = None
    _CapturingWorktreeClient.last_params = None
    _CapturingWorktreeClient.call_count = 0
    state_before = _state_path(workspace).read_bytes()

    res = runner.invoke(app, ["wave", "land", "P01-I01-W01", "--keep-worktree"])

    assert res.exit_code == 0, res.stdout
    assert _CapturingWorktreeClient.call_count == 1
    assert _CapturingWorktreeClient.last_method == "state.wave_land"
    assert _CapturingWorktreeClient.last_params == {
        "repo_root": str(workspace),
        "wave_id": "P01-I01-W01",
        "outcome": None,
        "keep_worktree": True,
    }
    assert _state_path(workspace).read_bytes() == state_before


def test_wave_land_batch_proxies_to_daemon_owned_method(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``wave land-batch`` also routes through daemon-owned RPC."""
    from eawf.surfaces.cli.commands import worktree as worktree_cmd

    _bootstrap_to_pending_wave(workspace)
    monkeypatch.setattr(worktree_cmd, "_resolve_repo_root", lambda _state_path: workspace)
    _enable_proxy(monkeypatch, client=_CapturingWorktreeClient)
    _CapturingWorktreeClient.last_method = None
    _CapturingWorktreeClient.last_params = None
    _CapturingWorktreeClient.call_count = 0
    state_before = _state_path(workspace).read_bytes()

    res = runner.invoke(app, ["wave", "land-batch", "--iter", "P01-I01", "--ready-only"])

    assert res.exit_code == 0, res.stdout
    assert _CapturingWorktreeClient.call_count == 1
    assert _CapturingWorktreeClient.last_method == "state.wave_land_batch"
    assert _CapturingWorktreeClient.last_params == {
        "repo_root": str(workspace),
        "iter_id": "P01-I01",
        "ready_only": True,
        "keep_worktree": False,
    }
    assert _state_path(workspace).read_bytes() == state_before


def test_wave_autoland_proxies_to_daemon_owned_method(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``wave autoland`` routes the whole state write through daemon-owned RPC."""
    from eawf.surfaces.cli.commands import worktree as worktree_cmd

    _bootstrap_to_pending_wave(workspace)
    monkeypatch.setattr(worktree_cmd, "_resolve_repo_root", lambda _state_path: workspace)
    _enable_proxy(monkeypatch, client=_CapturingWorktreeClient)
    _CapturingWorktreeClient.last_method = None
    _CapturingWorktreeClient.last_params = None
    _CapturingWorktreeClient.call_count = 0
    state_before = _state_path(workspace).read_bytes()

    res = runner.invoke(app, ["wave", "autoland", "--iter", "P01-I01", "--dry-run"])

    assert res.exit_code == 0, res.stdout
    assert _CapturingWorktreeClient.call_count == 1
    assert _CapturingWorktreeClient.last_method == "state.wave_autoland"
    assert _CapturingWorktreeClient.last_params == {
        "repo_root": str(workspace),
        "iter_id": "P01-I01",
        "keep_worktree": False,
        "dry_run": True,
    }
    assert _state_path(workspace).read_bytes() == state_before


# ---- plane 2: registry ------------------------------------------------------


def test_routed_kinds_are_all_in_daemon_apply_registry() -> None:
    """Every routed kind resolves to a real daemon apply function.

    A verb can only proxy a kind the daemon can dispatch; pinning the
    routed kinds against the apply registry stops a verb from routing a
    kind the daemon would reject with ``-32601``.
    """
    from eawf.runtime.daemon.methods.state import _APPLY_REGISTRY

    routed_kinds = {kind for _id, _argv, kind, _scope in _ROUTED_VERBS}
    routed_kinds.add(MutationKind.ITER_CLOSE)
    routed_kinds.add(MutationKind.PHASE_CLOSE)
    routed_kinds.add(MutationKind.TRACK_ADD)
    routed_kinds.add(MutationKind.TRACK_SWITCH)
    assert routed_kinds <= set(_APPLY_REGISTRY)


# ---- plane 3: fallback + crash safety ---------------------------------------


class _DownClient:
    """DaemonClient stand-in whose connect fails like a down daemon."""

    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    def __enter__(self) -> _DownClient:
        raise ConnectionRefusedError("daemon socket not listening")

    def __exit__(self, *_args: Any) -> None:
        return None


def test_fallback_when_daemon_down_writes_in_process(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Proxy on but daemon down → the in-process WAL-backed writer carries it."""
    _bootstrap_to_pending_wave(workspace)
    _enable_proxy(monkeypatch, client=_DownClient)

    res = runner.invoke(app, ["wave", "claim", "P01-I01-W01", "--session", "S-fallback"])
    assert res.exit_code == 0, res.stdout

    # The in-process fallback committed the claim to the local state.json.
    state = _read_state(workspace)
    assert state["waves"]["P01-I01-W01"]["status"] == "claimed"
    # And the WAL retired a record for the fallback write.
    fsynced = list(_wal_dir(workspace).glob("*.fsynced.json"))
    assert fsynced, "the in-process fallback retires a WAL record on success"
    # The event row landed.
    assert "wave claim" in _event_commands(workspace)


def test_injected_crash_leaves_no_phantom_event(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A crash between the state write and the event append yields no phantom event.

    The in-process WAL-backed commit writes state first, marks the WAL
    record APPLIED, then appends the event. Injecting an ``OSError`` at
    the event append leaves the state mutated but NO event row for the
    mutation — there is never an event whose state change did not commit.
    Because ``mark_applied`` ran before the append (W32), an ``.applied``
    record survives for roll-forward (not a ``.pending`` one, which replay
    would poison and silently drop the row).
    """
    _bootstrap_to_pending_wave(workspace)
    events_before = _event_commands(workspace)

    def _fail_append(*_args: object, **_kwargs: object) -> None:
        raise OSError("simulated event store failure")

    monkeypatch.setattr("eawf.kernel.store.append.append_envelope", _fail_append)

    # Daemonless (bootstrap env still set) → the in-process path runs.
    res = runner.invoke(app, ["wave", "claim", "P01-I01-W01", "--session", "S-crash"])
    assert res.exit_code != 0, res.stdout

    # No phantom event: the event log gained nothing for the failed claim.
    assert _event_commands(workspace) == events_before, "no event row without its state change"
    # An .applied WAL record survives for the next mutation's replay to
    # re-issue (not a .pending one — mark_applied ran before the append).
    assert list(_wal_dir(workspace).glob("*.pending.json")) == []
    assert list(_wal_dir(workspace).glob("*.applied.json")), "an .applied WAL record survives"


def test_next_mutation_reissues_applied_event_after_crash(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The next successful mutation's ``replay_wal`` re-issues the crash record.

    After a crashed event append leaves an ``.applied`` record
    (``mark_applied`` ran before the append, W32), the next in-process
    commit runs ``replay_wal`` first; replay re-issues the ``.applied``
    record's captured envelope so the event row the crash dropped lands
    exactly once. Before W32 this row was lost — the ``.pending`` record
    it used to leave got poisoned, diverging state from the event log.
    """
    from eawf.kernel.store import append as append_module

    _bootstrap_to_pending_wave(workspace)

    call_count = {"n": 0}
    real_append: Any = append_module.append_envelope

    def _flaky_append(*args: Any, **kwargs: Any) -> None:
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise OSError("simulated event store failure (first append only)")
        real_append(*args, **kwargs)

    monkeypatch.setattr("eawf.kernel.store.append.append_envelope", _flaky_append)

    # First claim: state lands, mark_applied runs, event append crashes.
    res1 = runner.invoke(app, ["wave", "claim", "P01-I01-W01", "--session", "S-1"])
    assert res1.exit_code != 0, res1.stdout
    assert list(_wal_dir(workspace).glob("*.applied.json")), "crash leaves an .applied record"
    assert list(_wal_dir(workspace).glob("*.pending.json")) == []

    # A second mutation succeeds; its commit replays the WAL first, which
    # re-issues the prior .applied record's envelope.
    res2 = runner.invoke(
        app,
        [
            "wave",
            "plan",
            "P01-I01",
            "--id",
            "P01-I01-W02",
            "--title",
            "w2",
            "--files",
            "src/",
            "--effort-bucket",
            "M",
        ],
    )
    assert res2.exit_code == 0, res2.stdout

    # Nothing poisoned — an APPLIED record is recovered, not poisoned.
    assert list(_wal_dir(workspace).glob("*.applied.json")) == []
    assert list(_wal_dir(workspace).glob("*.pending.json")) == []
    assert list((_wal_dir(workspace) / "poisoned").glob("*.poisoned.json")) == []
    # The dropped claim row was re-issued by replay (recovered, not lost),
    # and the wave plan rows are present — state and event log stay in sync.
    commands = _event_commands(workspace)
    assert "wave claim" in commands, "the crashed claim's event row was re-issued by replay"
    assert commands.count("wave plan") == 2  # the bootstrap W01 + the W02 above


def test_clean_round_trip_via_in_process_path(workspace: Path) -> None:
    """Boundary: a normal daemonless mutation round-trips cleanly + WAL retires."""
    _bootstrap_to_pending_wave(workspace)
    res = runner.invoke(app, ["wave", "claim", "P01-I01-W01", "--session", "S-clean"])
    assert res.exit_code == 0, res.stdout

    state = _read_state(workspace)
    assert state["waves"]["P01-I01-W01"]["status"] == "claimed"
    # No half-applied WAL leftovers from a clean commit.
    assert list(_wal_dir(workspace).glob("*.pending.json")) == []
    assert list(_wal_dir(workspace).glob("*.applied.json")) == []
    assert list(_wal_dir(workspace).glob("*.fsynced.json")), "clean commit retires to .fsynced"
