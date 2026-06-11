"""Tests for the sandbox-enforcement timeline pane (P30-I10-W09, mode digit 9).

The Sandbox-events mode renders the spawn-safety floor's denial timeline:
the ``argv-deny`` / ``egress-block`` / ``env-scrub`` / ``cwd-guard`` rows the
floor persisted to the on-disk event feed when it refused something. Each row
leads with a severity sigil (the hard-deny cross for a ``block`` decision, the
warn triangle for a ``warn`` / ``info`` one), then the wall-clock time, the
spawning session, and the denied target. These tests pin:

* the pure row reader (:func:`load_enforcement_rows`) -- it filters the event
  feed down to ``sandbox.enforcement.*`` rows, reads the five named fields off
  ``payload.extras``, and is total over a missing / malformed / non-
  enforcement feed;
* the pure row formatter (:func:`format_enforcement_row` /
  :func:`format_enforcement_markup`) -- the ``<sigil> <time> <session> <kind>
  <target>`` layout + the severity sigil column;
* the pinned honest-empty literal (a REAL em-dash, byte-for-byte) when the
  floor refused nothing;
* digit-9 registration -- the mode is its OWN standalone ``ModeSpec`` row, the
  registry extends to 1..9, and a live digit-9 press switches to it;
* affordance parity -- each advertised footer key + the ``g`` jump binding
  resolves to a live :class:`~textual.binding.Binding`.

Determinism follows the project Pilot-worker rule: each Pilot body drains
workers via :func:`~eawf.surfaces.tui.snapshot.settle_screen` before asserting.
"""

from __future__ import annotations

import asyncio
import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest
from textual.binding import Binding
from textual.widgets import Static

from eawf.kernel.state.enums import StoreKind
from eawf.kernel.store.append import append_envelope
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.kinds.event import EventPayload
from eawf.kernel.store.paths import store_path
from eawf.surfaces.tui.app import EaApp
from eawf.surfaces.tui.modes.registry import MODE_REGISTRY, mode_bindings, mode_for_name
from eawf.surfaces.tui.modes.sandbox_events import (
    EMPTY_NOTICE,
    ENFORCEMENT_KINDS,
    TIMELINE_ROW_CLASS,
    EnforcementRow,
    SandboxEventsModeScreen,
    format_enforcement_markup,
    format_enforcement_row,
    load_enforcement_rows,
)
from eawf.surfaces.tui.snapshot import settle_screen
from eawf.surfaces.tui.widgets.sigils import Sigil, enforcement_sigil, glyph

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "states" / "valid"
_REPO = _FIXTURES / "03-phase-iter-wave-active.json"

#: The digit key that switches to the Sandbox-events mode.
_SANDBOX_DIGIT = "9"


@pytest.fixture(autouse=True)
def _isolate_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point registry resolution at an empty ``tmp_path`` home.

    Keeps a stray ``u`` scope switch (and any registry read) deterministic and
    off the operator's real registry.
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
    (:func:`eawf.runtime.daemon.dispatch_runner.persist_enforcement_event`):
    a ``StoreKind.EVENT`` envelope whose ``event_type`` is
    ``sandbox.enforcement.<kind>`` and whose ``payload.extras`` carries the
    five named fields the pane reads.
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


def _state_with_events(tmp_path: Path, envelopes: list[Envelope]) -> Path:
    """Copy the repo fixture into ``tmp_path`` and seed its event store.

    Returns the writable ``state.json`` path so a mounted app reads the seeded
    enforcement rows off ``<state_dir>/store/event.jsonl``.
    """
    state_path = tmp_path / "state.json"
    shutil.copyfile(_REPO, state_path)
    event_path = store_path(state_path, StoreKind.EVENT)
    event_path.parent.mkdir(parents=True, exist_ok=True)
    for env in envelopes:
        append_envelope(event_path, env)
    return state_path


def _sample_envelopes() -> list[Envelope]:
    """Build one enforcement event per kind (block + a warn cwd-guard)."""
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


# --------------------------------------------------------------------------
# load_enforcement_rows -- pure, total over the feed
# --------------------------------------------------------------------------


def test_load_enforcement_rows_filters_to_enforcement_events(tmp_path: Path) -> None:
    """Only ``sandbox.enforcement.*`` rows are returned, newest-first."""
    state_path = _state_with_events(tmp_path, _sample_envelopes())
    event_path = store_path(state_path, StoreKind.EVENT)
    rows = load_enforcement_rows(event_path)
    assert len(rows) == 4
    # newest-first: the cwd-guard (second=4) leads.
    assert rows[0].kind == "cwd-guard"
    assert rows[-1].kind == "argv-deny"
    assert {row.kind for row in rows} == set(ENFORCEMENT_KINDS)


def test_load_enforcement_rows_reads_the_five_named_fields(tmp_path: Path) -> None:
    """Each row carries ts / session / kind / target / severity off extras."""
    state_path = _state_with_events(tmp_path, _sample_envelopes())
    rows = load_enforcement_rows(store_path(state_path, StoreKind.EVENT))
    argv = next(row for row in rows if row.kind == "argv-deny")
    assert argv.session == "EX-P01-W01-1"
    assert argv.target == "rm -rf /"
    assert argv.severity == "block"
    assert argv.timestamp == "09:30:01"


def test_load_enforcement_rows_skips_non_enforcement_events(tmp_path: Path) -> None:
    """A plain (non-enforcement) event in the same feed is filtered out."""
    plain = Envelope(
        id="EV-plain",
        kind=StoreKind.EVENT,
        scope_id="urn:eawf:v1:state:QR",
        created_at=datetime(2026, 6, 11, 9, 0, 0, tzinfo=UTC),
        updated_at=None,
        summary="wave closed",
        payload={"event_type": "wave.close", "status": "ok"},
    )
    state_path = _state_with_events(tmp_path, [plain, *_sample_envelopes()])
    rows = load_enforcement_rows(store_path(state_path, StoreKind.EVENT))
    assert len(rows) == 4
    assert all(row.kind in ENFORCEMENT_KINDS for row in rows)


def test_load_enforcement_rows_missing_file_is_empty(tmp_path: Path) -> None:
    """boundary: a missing event store yields an empty tuple (honest-empty)."""
    assert load_enforcement_rows(tmp_path / "absent.jsonl") == ()


def test_load_enforcement_rows_none_path_is_empty() -> None:
    """boundary: a ``None`` path (no bound scope) yields an empty tuple."""
    assert load_enforcement_rows(None) == ()


def test_load_enforcement_rows_skips_malformed_lines(tmp_path: Path) -> None:
    """A malformed JSONL line is skipped, not fatal -- the read stays total."""
    state_path = _state_with_events(tmp_path, _sample_envelopes())
    event_path = store_path(state_path, StoreKind.EVENT)
    with event_path.open("a", encoding="utf-8") as handle:
        handle.write("{ this is not json\n")
    rows = load_enforcement_rows(event_path)
    assert len(rows) == 4


def test_load_enforcement_rows_respects_the_limit(tmp_path: Path) -> None:
    """boundary: only the most-recent *limit* rows are returned."""
    state_path = _state_with_events(tmp_path, _sample_envelopes())
    rows = load_enforcement_rows(store_path(state_path, StoreKind.EVENT), limit=2)
    assert len(rows) == 2
    # newest-first under the cap.
    assert rows[0].kind == "cwd-guard"
    assert rows[1].kind == "env-scrub"


# --------------------------------------------------------------------------
# format_enforcement_row / markup -- pure layout + severity sigil
# --------------------------------------------------------------------------


def _row(*, severity: str = "block", kind: str = "argv-deny") -> EnforcementRow:
    return EnforcementRow(
        timestamp="09:30:01",
        session="EX-P01-W01-1",
        kind=kind,
        target="rm -rf /",
        severity=severity,
    )


def test_format_enforcement_row_layout() -> None:
    """A row is ``<sigil> <time> <session padded> <kind padded> <target>``."""
    line = format_enforcement_row(_row())
    # Leading two-cell severity column: the hard-deny sigil + one trailing space.
    assert line[1] == " "
    assert "09:30:01" in line
    assert "EX-P01-W01-1" in line
    assert "argv-deny" in line
    assert line.endswith("rm -rf /")


def test_format_enforcement_row_block_leads_with_hard_deny_sigil() -> None:
    """A ``block`` row leads with the hard-deny (FAILED) cross sigil."""
    line = format_enforcement_row(_row(severity="block"))
    assert line[0] == glyph(Sigil.FAILED, mode="ascii")


def test_format_enforcement_markup_block_is_failed_cross() -> None:
    """The markup column resolves the hard-deny cross for a ``block`` row."""
    markup = format_enforcement_markup(_row(severity="block"), mode="unicode")
    assert glyph(Sigil.FAILED, mode="unicode") in markup


def test_format_enforcement_markup_warn_is_triangle() -> None:
    """The markup column resolves the warn triangle for a ``warn`` row."""
    warn_glyph = enforcement_sigil("warn").render(mode="unicode")
    markup = format_enforcement_markup(_row(severity="warn"), mode="unicode")
    assert warn_glyph in markup
    # ... and a warn row is NOT the hard-deny cross.
    assert warn_glyph != glyph(Sigil.FAILED, mode="unicode")


def test_format_enforcement_markup_escapes_bracket_target() -> None:
    """A target carrying literal brackets renders verbatim (escaped)."""
    row = EnforcementRow(
        timestamp="09:30:01",
        session="s",
        kind="argv-deny",
        target="cmd [danger]",
        severity="block",
    )
    markup = format_enforcement_markup(row, mode="unicode")
    assert r"cmd \[danger]" in markup


# --------------------------------------------------------------------------
# Honest-empty literal -- the pinned em-dash spec literal
# --------------------------------------------------------------------------


def test_empty_notice_pins_the_em_dash_literal() -> None:
    """The honest-empty literal is byte-for-byte the spec text (REAL em-dash)."""
    assert EMPTY_NOTICE == "no sandbox events — nothing was denied"
    assert "—" in EMPTY_NOTICE  # U+2014 em-dash, not a hyphen
    assert "-" not in EMPTY_NOTICE.replace("—", "")


def test_honest_empty_renders_when_floor_denied_nothing() -> None:
    """With no enforcement rows the pane shows the pinned honest-empty notice."""

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_REPO)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            await pilot.press(_SANDBOX_DIGIT)
            await settle_screen(pilot)
            assert app.current_mode == "sandbox_events"
            empties = app.screen.query(".sandbox-events-empty")
            assert len(empties) == 1
            assert str(empties.first(Static).render()) == EMPTY_NOTICE

    asyncio.run(body())


def test_populated_timeline_renders_a_row_per_denial(tmp_path: Path) -> None:
    """With persisted denials the pane renders one timeline row per event."""
    state_path = _state_with_events(tmp_path, _sample_envelopes())

    async def body() -> None:
        app = EaApp(scope="repo", state_path=state_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            await pilot.press(_SANDBOX_DIGIT)
            await settle_screen(pilot)
            rows = app.screen.query(f".{TIMELINE_ROW_CLASS}")
            assert len(rows) == 4
            # No honest-empty notice once rows are present.
            assert not app.screen.query(".sandbox-events-empty")
            joined = "\n".join(str(row.render()) for row in rows.results(Static))
            assert "evil.example.com:443" in joined
            assert "ANTHROPIC_API_KEY" in joined

    asyncio.run(body())


# --------------------------------------------------------------------------
# Digit-9 registration -- the mode is its OWN standalone ModeSpec
# --------------------------------------------------------------------------


def test_sandbox_events_registered_at_digit_nine() -> None:
    """The mode is registered as its own ``ModeSpec`` at digit 9, name + title."""
    spec = mode_for_name("sandbox_events")
    assert spec is not None
    assert spec.digit == "9"
    assert spec.title == "Sandbox"
    # ... and it is the last (highest-digit) row, extending the range to 1..9.
    assert MODE_REGISTRY[-1] is spec
    assert [s.digit for s in MODE_REGISTRY] == [str(n) for n in range(1, 10)]


def test_digit_nine_mode_binding_exists() -> None:
    """A ``9 -> switch_mode('sandbox_events')`` binding is wired app-wide."""
    bindings = {b.key: b.action for b in mode_bindings()}
    assert bindings.get("9") == "switch_mode('sandbox_events')"


def test_digit_nine_press_switches_to_the_mode() -> None:
    """A live digit-9 press switches the app into the sandbox-events mode."""

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_REPO)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            assert app.current_mode == "home"
            await pilot.press(_SANDBOX_DIGIT)
            await settle_screen(pilot)
            assert app.current_mode == "sandbox_events"
            assert isinstance(app.screen, SandboxEventsModeScreen)

    asyncio.run(body())


# --------------------------------------------------------------------------
# Affordance parity -- every advertised key + g resolves to a live Binding
# --------------------------------------------------------------------------


def test_sandbox_events_binds_navigation_and_reload_keys() -> None:
    """up / down / Enter / g each resolve to a live Binding (affordance parity)."""
    keys = {
        binding.key: binding.action
        for binding in SandboxEventsModeScreen.BINDINGS
        if isinstance(binding, Binding)
    }
    assert keys.get("up") == "scroll_up"
    assert keys.get("down") == "scroll_down"
    assert keys.get("enter") == "reload"
    assert keys.get("g") == "scroll_home"


def test_advertised_footer_keys_each_resolve_to_a_binding() -> None:
    """Every advertised footer key resolves to a live Binding (no dead click).

    The pane advertises ``↑↓`` / ``Enter`` / ``/`` / ``?`` / ``q`` / ``w/r/u``;
    the C2-named ``Esc`` resolves on the base chassis. Each must answer to a
    real :class:`~textual.binding.Binding` reachable from the mounted screen --
    the affordance-parity gate's per-mode shape.
    """

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_REPO)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            await pilot.press(_SANDBOX_DIGIT)
            await settle_screen(pilot)
            # The merged screen bindings (own + base chassis, resolved across
            # the MRO by Textual) -- the live key->Binding chain a keypress
            # walks. Every advertised footer key + the C2-named keys must
            # answer to a real Binding here (no dead click).
            bound = app.screen._bindings.key_to_bindings
            for key in ("up", "down", "enter", "g", "slash", "question_mark", "q", "escape"):
                assert key in bound, f"advertised key {key!r} has no live Binding"

    asyncio.run(body())


def test_footer_hints_advertise_canonical_tokens_only() -> None:
    """The advertised footer hints use only canonical key tokens.

    ``g`` is a live binding but is NOT advertised in the hint strip (it is not
    a canonical footer token), so the cross-surface canonical-token gate stays
    green. The arrow + Enter + chrome tokens are the advertised ones.
    """
    hints = " ".join(SandboxEventsModeScreen.FOOTER_HINTS)
    assert "↑↓ select" in hints
    assert "Enter open" in hints
    assert "/ palette" in hints
    # The g jump key is deliberately not advertised (non-canonical token).
    assert "g " not in hints
