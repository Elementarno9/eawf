"""Golden ASCII snapshot tests for the U6 Sandbox-events mode (digit 9).

The Sandbox-events mode renders the spawn-safety floor's denial timeline -- the
``argv-deny`` / ``egress-block`` / ``env-scrub`` / ``cwd-guard`` rows the floor
persisted when it refused something. The designer's "pin now" directive wants
the mode's *frozen literals* captured as a byte-stable golden so a later layout
or copy edit surfaces in review rather than silently:

* the honest-empty surface (the byte-fixed em-dash notice the floor shows when
  it refused nothing -- the pane's whole value is that the timeline IS empty in
  the happy path); and
* a populated denial timeline over the pinned sample envelopes (one row per
  enforcement kind, newest-first, each leading with its severity sigil).

The capture path mirrors the rest of the ``tui`` golden suite
(:func:`~eawf.surfaces.tui.snapshot.assert_screen_snapshot` over a settled
``run_test`` Pilot); the autouse ``conftest`` fixtures suppress the
daemon-degraded banner and force the unicode-glyph seal so the frame is
byte-stable across machines. The enforcement events are seeded into a copied
fixture's event store so the populated capture reads a deterministic feed.

Regenerate the goldens after an intentional layout change with::

    EAWF_SNAPSHOT_REGEN=1 uv run pytest tests/snapshots/tui/test_sandbox_events_mode.py
"""

from __future__ import annotations

import asyncio
import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest

from eawf.kernel.state.enums import StoreKind
from eawf.kernel.store.append import append_envelope
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.kinds.event import EventPayload
from eawf.kernel.store.paths import store_path
from eawf.surfaces.tui.app import EaApp
from eawf.surfaces.tui.snapshot import assert_screen_snapshot, settle_screen

#: Fixed terminal geometry, matching the rest of the ``tui`` golden suite.
_SIZE = (120, 40)

_FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "states" / "valid"
_REPO_STATE = _FIXTURES / "03-phase-iter-wave-active.json"

# Fail loudly on a path mistake: the binder degrades a missing fixture to an
# empty-scope placeholder, so a wrong path would silently snapshot the
# placeholder rather than the populated screen.
assert _REPO_STATE.is_file(), f"missing snapshot fixture: {_REPO_STATE}"

_GOLDEN = Path(__file__).resolve().parent / "golden"

#: The digit key that switches to the Sandbox-events mode.
_SANDBOX_DIGIT = "9"


@pytest.fixture(autouse=True)
def _isolate_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point registry resolution at an empty ``tmp_path`` home.

    Keeps a stray scope switch (and any registry read) deterministic and off
    the operator's real registry -- no machine-specific repo path leaks into a
    golden.
    """
    monkeypatch.setattr(Path, "home", lambda: tmp_path)


def _enforcement_envelope(
    *,
    ts: datetime,
    session: str,
    kind: str,
    target: str,
    severity: str,
) -> Envelope:
    """Build one persisted sandbox-enforcement event envelope.

    Mirrors the production persistence shape
    (:func:`eawf.runtime.daemon.dispatch_runner.persist_enforcement_event`): a
    ``StoreKind.EVENT`` envelope whose ``event_type`` is
    ``sandbox.enforcement.<kind>`` and whose ``payload.extras`` carries the five
    named fields the timeline pane reads.
    """
    payload = EventPayload(
        timestamp=ts,
        event_type=f"sandbox.enforcement.{kind}",
        actor="daemon",
        command="dispatch_runner.persist_enforcement_event",
        args_hash="",
        status=severity,
        message=f"sandbox_enforcement kind={kind} target={target!r} severity={severity}",
        extras={
            "ts": ts.isoformat(),
            "session": session,
            "kind": kind,
            "target": target,
            "severity": severity,
        },
    ).model_dump(mode="json")
    return Envelope(
        id=f"EV-{kind}-{session}",
        kind=StoreKind.EVENT,
        scope_id="urn:eawf:v1:state:QR",
        created_at=ts,
        updated_at=None,
        summary=f"sandbox_enforcement {kind}",
        payload=payload,
    )


def _sample_envelopes() -> list[Envelope]:
    """Build one enforcement event per kind (three blocks + a warn cwd-guard).

    The fixed-second timestamps + pinned targets are the *frozen literals* the
    populated golden captures, so a column / copy drift in the timeline shows up
    in review.
    """
    base = datetime(2026, 6, 11, 9, 30, 0, tzinfo=UTC)
    return [
        _enforcement_envelope(
            ts=base.replace(second=1),
            session="EX-P01-W01-1",
            kind="argv-deny",
            target="rm -rf /",
            severity="block",
        ),
        _enforcement_envelope(
            ts=base.replace(second=2),
            session="EX-P01-W01-1",
            kind="egress-block",
            target="evil.example.com:443",
            severity="block",
        ),
        _enforcement_envelope(
            ts=base.replace(second=3),
            session="EX-P01-W01-1",
            kind="env-scrub",
            target="ANTHROPIC_API_KEY",
            severity="block",
        ),
        _enforcement_envelope(
            ts=base.replace(second=4),
            session="EX-P01-W01-1",
            kind="cwd-guard",
            target="/tmp/escape",
            severity="warn",
        ),
    ]


def _state_with_events(tmp_path: Path, envelopes: list[Envelope]) -> Path:
    """Copy the repo fixture into ``tmp_path`` and seed its event store.

    Returns the writable ``state.json`` path so a mounted app reads the seeded
    enforcement rows off ``<state_dir>/store/event.jsonl``.
    """
    state_path = tmp_path / "state.json"
    shutil.copyfile(_REPO_STATE, state_path)
    event_path = store_path(state_path, StoreKind.EVENT)
    event_path.parent.mkdir(parents=True, exist_ok=True)
    for env in envelopes:
        append_envelope(event_path, env)
    return state_path


def test_sandbox_events_empty_snapshot() -> None:
    """The honest-empty surface (the pinned em-dash notice) is byte-stable."""

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_REPO_STATE)
        async with app.run_test(size=_SIZE) as pilot:
            await settle_screen(pilot)
            await pilot.press(_SANDBOX_DIGIT)
            await settle_screen(pilot)
            assert_screen_snapshot(app, _GOLDEN / "sandbox_events_empty.txt")

    asyncio.run(body())


def test_sandbox_events_populated_snapshot(tmp_path: Path) -> None:
    """The populated denial timeline (frozen sample rows) is byte-stable."""
    state_path = _state_with_events(tmp_path, _sample_envelopes())

    async def body() -> None:
        app = EaApp(scope="repo", state_path=state_path)
        async with app.run_test(size=_SIZE) as pilot:
            await settle_screen(pilot)
            await pilot.press(_SANDBOX_DIGIT)
            await settle_screen(pilot)
            assert_screen_snapshot(app, _GOLDEN / "sandbox_events_populated.txt")

    asyncio.run(body())
