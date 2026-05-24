"""Unit tests for the opaque-handle registry + TTL sweep (P24-W07).

Covers :mod:`eawf.daemon.session` (register / resolve / prune) and
:mod:`eawf.daemon.session_ttl` (plan_evictions + sweep_once +
build_pruned_envelope). The registry is process-local in-memory; each
test resets it via :func:`eawf.daemon.session.reset_registry` so the
order of execution does not leak.

Per AGENTS rule 16 (secrets / PII hygiene): the handle is the only
identifier serialised into ``state.json`` / ``event.jsonl``. Tests
assert the handle format follows the URN-shaped convention so the
opaque-handle invariant has a regression seat.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import orjson
import pytest

from eawf.daemon import session_ttl
from eawf.daemon.session import (
    HANDLE_PREFIX,
    known_handles,
    prune_handles_for_wave,
    register_session_log,
    reset_registry,
    resolve_session_log,
)
from eawf.daemon.session_ttl import (
    DEFAULT_TTL_SECONDS,
    PrunePlan,
    build_pruned_envelope,
    plan_evictions,
    sweep_once,
)
from eawf.kernel.state.enums import StoreKind
from eawf.kernel.state.models import SessionAttempt, Wave

pytestmark = pytest.mark.unit


_HANDLE_RE = re.compile(r"^urn:eawf:v1:session-log:[a-z0-9_\-]+:[0-9a-f]{32}$")


@pytest.fixture(autouse=True)
def _isolate_registry() -> Iterator[None]:
    """Reset the process-local registry between tests."""
    reset_registry()
    yield
    reset_registry()


def _run(body: Callable[[], Awaitable[None]]) -> None:
    """Run an async test body without ``pytest-asyncio``."""
    asyncio.run(body())


def test_register_resolves_round_trip(tmp_path: Path) -> None:
    raw_path = tmp_path / "session-log.json"
    handle = register_session_log("claude-code", raw_path)
    assert resolve_session_log(handle) == raw_path


def test_register_returns_urn_shaped_handle(tmp_path: Path) -> None:
    handle = register_session_log("codex", tmp_path / "log.json")
    assert handle.startswith(HANDLE_PREFIX)
    assert _HANDLE_RE.fullmatch(handle), f"unexpected handle shape: {handle!r}"


def test_handle_prefix_is_urn_eawf_v1_session_log() -> None:
    assert HANDLE_PREFIX == "urn:eawf:v1:session-log:"


def test_resolve_unknown_handle_raises_keyerror() -> None:
    with pytest.raises(KeyError, match="unknown session-log handle"):
        resolve_session_log("urn:eawf:v1:session-log:claude-code:dead")


def test_register_rejects_empty_runtime(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="runtime must be non-empty"):
        register_session_log("", tmp_path / "log.json")


def test_distinct_runtimes_get_distinct_handles(tmp_path: Path) -> None:
    h1 = register_session_log("claude-code", tmp_path / "a.json")
    h2 = register_session_log("codex", tmp_path / "b.json")
    assert h1 != h2
    assert resolve_session_log(h1) != resolve_session_log(h2)


def test_two_handles_for_same_runtime_differ(tmp_path: Path) -> None:
    h1 = register_session_log("claude-code", tmp_path / "a.json")
    h2 = register_session_log("claude-code", tmp_path / "b.json")
    assert h1 != h2


def test_prune_drops_only_matching_wave(tmp_path: Path) -> None:
    keep = register_session_log("claude-code", tmp_path / "keep.json", wave_id="P24-I01-W01")
    drop_1 = register_session_log("claude-code", tmp_path / "d1.json", wave_id="P24-I01-W07")
    drop_2 = register_session_log("codex", tmp_path / "d2.json", wave_id="P24-I01-W07")
    dropped = prune_handles_for_wave("P24-I01-W07")
    assert dropped == 2
    assert resolve_session_log(keep) == tmp_path / "keep.json"
    with pytest.raises(KeyError):
        resolve_session_log(drop_1)
    with pytest.raises(KeyError):
        resolve_session_log(drop_2)


def test_prune_unknown_wave_is_noop(tmp_path: Path) -> None:
    register_session_log("claude-code", tmp_path / "a.json", wave_id="P24-I01-W01")
    dropped = prune_handles_for_wave("P99-I99-W99")
    assert dropped == 0
    assert len(tuple(known_handles())) == 1


def test_prune_leaves_unscoped_handles_alone(tmp_path: Path) -> None:
    """Handles registered without a wave_id (synthetic / test) survive prune."""
    h = register_session_log("claude-code", tmp_path / "a.json")
    prune_handles_for_wave("P24-I01-W07")
    assert resolve_session_log(h) == tmp_path / "a.json"


def test_session_ttl_shim_delegates_to_session_module(tmp_path: Path) -> None:
    register_session_log("claude-code", tmp_path / "a.json", wave_id="P24-I01-W07")
    dropped = session_ttl.prune_handles_for_wave("P24-I01-W07")
    assert dropped == 1


def _now() -> datetime:
    return datetime(2026, 5, 19, 12, 0, 0, tzinfo=UTC)


def _make_attempt(
    *,
    attempt: int = 1,
    runtime: str = "claude-code",
    started_at: datetime | None = None,
    ended_at: datetime | None = None,
    session_id: str | None = None,
) -> SessionAttempt:
    return SessionAttempt(
        attempt=attempt,
        runtime=runtime,
        session_id=session_id or f"sess-{attempt}",
        session_log_handle=f"urn:eawf:v1:session-log:{runtime}:{'a' * 32}",
        started_at=started_at or _now(),
        ended_at=ended_at,
    )


def _build_state_with_wave_session(
    *,
    wave_id: str,
    sessions: dict[int, SessionAttempt],
) -> dict[str, object]:
    """Build a minimal state payload with one wave + its sessions."""
    wave = Wave.model_validate(
        {
            "id": wave_id,
            "iter_id": "P24-I01",
            "title": "session-ttl-test",
            "status": "in_progress",
            "opened_at": _now().isoformat(),
            "sessions": {str(k): v.model_dump(mode="json") for k, v in sessions.items()},
        }
    )
    return {
        "schema_version": "1.0",
        "scope_kind": "repo",
        "urn": "urn:eawf:v1:state:QR",
        "updated_at": _now().isoformat(),
        "project": {
            "code": "QR",
            "slug": "qr",
            "title": "QR",
            "description": None,
            "domains": ["x"],
            "default_branch": "main",
            "status": "active",
            "repo_urn": "urn:eawf:v1:repo:QR",
        },
        "current": {"project_code": "QR"},
        "workspace": None,
        "phases": {},
        "iters": {},
        "waves": {wave_id: wave.model_dump(mode="json")},
        "artifacts": {},
        "agent_sessions": {},
        "plugins": {},
        "indexes": {},
    }


def _write_state(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(orjson.dumps(payload))


def test_plan_evictions_skips_running_attempts() -> None:
    """Attempts without ended_at stay regardless of TTL."""
    from eawf.evidence._io import load_state

    payload = _build_state_with_wave_session(
        wave_id="P24-I01-W01",
        sessions={1: _make_attempt(ended_at=None)},
    )
    # round-trip through the validator to get a typed State
    state_path = Path("/tmp/eawf-test-state-running.json")
    try:
        _write_state(state_path, payload)
        state = load_state(state_path)
        plans = plan_evictions(state, now=_now() + timedelta(days=10))
        assert plans == []
    finally:
        state_path.unlink(missing_ok=True)


def test_plan_evictions_picks_expired_attempts(tmp_path: Path) -> None:
    from eawf.evidence._io import load_state

    ended = _now() - timedelta(seconds=DEFAULT_TTL_SECONDS + 1)
    payload = _build_state_with_wave_session(
        wave_id="P24-I01-W01",
        sessions={1: _make_attempt(ended_at=ended)},
    )
    state_path = tmp_path / "state.json"
    _write_state(state_path, payload)
    state = load_state(state_path)
    plans = plan_evictions(state, ttl_seconds=DEFAULT_TTL_SECONDS, now=_now())
    assert len(plans) == 1
    plan = plans[0]
    assert plan.wave_id == "P24-I01-W01"
    assert plan.attempt == 1


def test_plan_evictions_leaves_recent_attempts_alone(tmp_path: Path) -> None:
    from eawf.evidence._io import load_state

    ended_just_now = _now() - timedelta(seconds=10)
    payload = _build_state_with_wave_session(
        wave_id="P24-I01-W01",
        sessions={1: _make_attempt(ended_at=ended_just_now)},
    )
    state_path = tmp_path / "state.json"
    _write_state(state_path, payload)
    state = load_state(state_path)
    plans = plan_evictions(state, ttl_seconds=DEFAULT_TTL_SECONDS, now=_now())
    assert plans == []


def test_build_pruned_envelope_is_event_kind_and_scrubbed() -> None:
    """Per rule 16 the envelope MUST NOT carry a filesystem path."""
    plan = PrunePlan(
        wave_id="P24-I01-W01",
        attempt=1,
        session_log_handle="urn:eawf:v1:session-log:claude-code:abc",
        ended_at=_now(),
    )
    env = build_pruned_envelope(plan, now=_now())
    assert env.kind == StoreKind.EVENT
    assert env.scope_id == "P24-I01-W01"
    assert env.payload["event_type"] == "session_handle_pruned"
    serialised = env.model_dump_json()
    # No filesystem path should ever appear in the wire payload.
    assert "/Users/" not in serialised  # pragma: allowlist secret
    assert "/tmp/" not in serialised
    assert "\\\\" not in serialised  # Windows path separator (escaped)


def test_sweep_once_invokes_publish_for_each_expired_attempt(tmp_path: Path) -> None:
    published: list[object] = []
    ended = _now() - timedelta(seconds=DEFAULT_TTL_SECONDS + 1)
    payload = _build_state_with_wave_session(
        wave_id="P24-I01-W01",
        sessions={1: _make_attempt(ended_at=ended)},
    )
    state_path = tmp_path / "state.json"
    _write_state(state_path, payload)

    async def body() -> None:
        plans = await sweep_once(
            state_path=state_path,
            ttl_seconds=DEFAULT_TTL_SECONDS,
            publish=published.append,
            now=_now(),
        )
        assert len(plans) == 1

    _run(body)
    assert len(published) == 1


def test_sweep_once_returns_empty_list_when_state_missing(tmp_path: Path) -> None:
    async def body() -> None:
        plans = await sweep_once(state_path=tmp_path / "nope.json")
        assert plans == []

    _run(body)
