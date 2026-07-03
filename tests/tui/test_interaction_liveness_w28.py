"""Interaction-liveness gate over the footer-advertised keys of every mode.

A live Pilot probe found footer-advertised keys that silently no-oped: the
evidence pane's arrows / ``p`` / ``Enter`` and the research board's ``Enter``
resolved to handlers that did nothing visible. Those specific defects are fixed
(waves W24-W27), and this suite turns the whole defect CLASS into a CI gate.

Two halves, matching the wave's success criteria:

* **The helper.** :func:`assert_footer_key_responds` (in
  :mod:`eawf.surfaces.tui.snapshot.pilot_harness`) presses a key and asserts a
  visible response -- a text-frame delta, a toast, a screen change, or a
  selection-cursor move. It lifts the older ``mutating_action_keys_resolve``
  check from "an ``action_<name>`` handler EXISTS" up to "the key VISIBLY
  RESPONDS". The unit tests below pin the helper directly: a frame-changing key
  passes, a silent no-op raises, and a toast-only key passes through the toast
  channel. The cursor channel is pinned too, because a row-cursor move repaints
  only a style highlight that the plain-text capture cannot see.

* **The sweep.** :func:`test_mode_footer_keys_respond_or_are_exempted` mounts
  the TUI once per mode (all nine: home / autopilot / research_board / trust /
  doctor / evidence / feed / agent_watch / sandbox_events, digits 1-9), reads
  the mode screen's footer-advertised key strip, and asserts every mode-owned
  key visibly responds. A key whose action is legitimately unavailable in the
  fixture state (an honest-empty pane with no rows to act on, or a control that
  needs an ACTIVE session) is declared in the visible :data:`_EXEMPTIONS` table
  with a concrete per-key reason -- never skipped silently. App-chrome globals
  (scope switch / config / refresh / palette / help / quit) are advertised on
  every surface and are guarded by their own suites, so the sweep scopes to the
  mode screen's OWN keys.

The gate is scoped to the bottom footer strip (:class:`Footer` hints). The
research board's separate top mode-key line is out of scope here.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import ClassVar, cast

import orjson
import pytest
from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.pilot import Pilot
from textual.widgets import DataTable, Static

from eawf.kernel.state.enums import AgentReportVerdict, AgentSessionRole, Confidence
from eawf.kernel.state.models import State
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.kinds.agent_report import (
    AgentReportHeader,
    AgentReportPayload,
    ExecutorReportBody,
    store_kind_for_role,
)
from eawf.kernel.store.paths import store_path
from eawf.surfaces.tui.app import EaApp
from eawf.surfaces.tui.modes.evidence import EvidenceModeScreen
from eawf.surfaces.tui.modes.registry import MODE_REGISTRY, ModeSpec
from eawf.surfaces.tui.snapshot.pilot_harness import (
    assert_footer_key_responds,
    probe_footer_key_response,
    settle_screen,
)
from eawf.surfaces.tui.widgets.footer import Footer
from eawf.surfaces.tui.widgets.git_pane import GitFields
from eawf.surfaces.tui.widgets.roadmap_tree import RoadmapTree
from eawf.workflow.verify.models import CloseReadiness, CriterionView, GateResult

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "states" / "valid"
#: A rich repo fixture: an ACTIVE phase / iter / in-progress wave, so the home
#: roadmap tree has a walkable structure and the evidence report join has a wave
#: to bind to.
_REPO_FIXTURE = _FIXTURES / "03-phase-iter-wave-active.json"
_SEEDED_WAVE = "P01-I01-W01"
_SIZE = (200, 60)

#: Digit order + expected mode names the sweep guards (the ratified accelerator
#: axis). Pinned here so a registry reshape that breaks the mode set fails the
#: sweep loudly rather than silently narrowing coverage.
_EXPECTED_MODES: tuple[tuple[str, str], ...] = (
    ("1", "home"),
    ("2", "autopilot"),
    ("3", "research_board"),
    ("4", "trust"),
    ("5", "doctor"),
    ("6", "evidence"),
    ("7", "feed"),
    ("8", "agent_watch"),
    ("9", "sandbox_events"),
)

#: Footer tokens advertised on every surface (scope switch / config / refresh /
#: palette / help / quit). They are app-chrome, not a mode's own keys, so the
#: mode sweep skips them; their liveness is guarded by the scope / palette /
#: help / quit suites.
_APP_CHROME_TOKENS: frozenset[str] = frozenset({"w/r/u", "c", "F5", "/", "?", "q"})

#: Footer key tokens that are not literally their pilot key string. Single-letter
#: tokens (``a`` / ``d`` / ``H`` / ``v`` / ``p`` ...) press as themselves.
_TOKEN_TO_KEY: dict[str, str] = {
    "↑↓": "down",  # up/down arrows -> a single down press moves the cursor
    "←→": "left",  # left/right arrows -> a single left press collapses/ascends
    "Enter": "enter",
    "Esc": "escape",
    "space": "space",
}

_UP_DOWN = "↑↓"

#: Per-(mode, token) exemptions: an advertised key whose action is legitimately
#: unavailable in the fixture state, with the concrete reason. Every entry is a
#: SELECT / OPEN key over an honest-empty pane (no rows to move a cursor over or
#: open) -- the same class the fixed evidence / research defects belonged to,
#: but where the fixture genuinely stages no rows for the pane. A key here is
#: NOT dead code: its handler resolves and no-ops honestly; it simply has
#: nothing to act on until the pane populates. The evidence pane is the counter-
#: example the sweep DOES exercise (reports are seeded), so the fixed arrows /
#: Enter / peek defect stays guarded.
_EXEMPTIONS: dict[tuple[str, str], str] = {
    ("autopilot", _UP_DOWN): (
        "no ready-wave frontier rows in the fixture state to move a selection over"
    ),
    ("research_board", _UP_DOWN): (
        "no research-campaign rows in the fixture state to move a selection over"
    ),
    ("trust", _UP_DOWN): ("no trust-scorecard rows in the fixture state to move a selection over"),
    ("feed", _UP_DOWN): ("no live-feed event rows in the fixture state to move a selection over"),
    ("sandbox_events", _UP_DOWN): (
        "no sandbox-enforcement rows in the fixture state; the floor refused nothing"
    ),
    ("sandbox_events", "Enter"): (
        "no sandbox-enforcement rows in the fixture state to open; the floor refused nothing"
    ),
}


# --------------------------------------------------------------------------
# Autouse isolation (registry + git probe), mirroring the sibling mode suites
# --------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Redirect registry + Doctor instrument-probe writes into ``tmp_path``.

    A ``u`` scope read touches ``~/.eawf/registry.json`` and the Doctor mode
    writes an instrument-probe cache; both are redirected off the real home so
    the sweep never mutates developer state.
    """
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("EA_INSTRUMENT_PROBE", str(tmp_path / "instrument-probe.json"))


@pytest.fixture(autouse=True)
def _stub_git(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the workspace git probe to a deterministic clean tree."""
    monkeypatch.setattr(
        "eawf.surfaces.tui.widgets.workspace_table.gather_git_fields",
        lambda _path: GitFields(
            branch="main", dirty="clean", ahead_behind="up-to-date", recent_commits=()
        ),
    )


# --------------------------------------------------------------------------
# State + report-store seeding
# --------------------------------------------------------------------------


def _append_executor_report(state_path: Path, base_id: str, attempt: int) -> None:
    """Append one executor-report envelope keyed by ``base_id`` to the store."""
    body = ExecutorReportBody(
        role="executor",
        verdict=AgentReportVerdict.PASS,
        confidence=Confidence.HIGH,
        summary=f"attempt {attempt} complete",
        wave_id=base_id,
        outcome="done",
    )
    report_id = f"AR-executor-{base_id}-{attempt:02d}"
    header = AgentReportHeader(
        report_id=report_id,
        role=AgentSessionRole.EXECUTOR,
        session_id=f"SES-{attempt}",
        scope_id=base_id,
        base_id=base_id,
        attempt=attempt,
        runtime="codex",
        generated_at=NOW,
        summary=f"attempt {attempt} complete",
    )
    envelope = Envelope(
        id=report_id,
        kind=store_kind_for_role(AgentSessionRole.EXECUTOR),
        scope_id=base_id,
        created_at=NOW + timedelta(minutes=attempt),
        updated_at=None,
        summary=body.summary,
        payload=AgentReportPayload(header=header, body=body).model_dump(mode="json"),
    )
    path = store_path(state_path, store_kind_for_role(AgentSessionRole.EXECUTOR))
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(envelope.model_dump_json() + "\n")


def _seeded_state_path(tmp_path: Path) -> Path:
    """Write the rich fixture to a writable path + seed two evidence reports.

    Loading the committed fixture and re-writing it under ``tmp_path`` gives a
    writable sibling ``store/`` so the evidence pane's report table populates
    (two rows, so a Down press moves the row cursor). The state itself is a
    verbatim copy of the fixture -- only the report store is seeded beside it.
    """
    state = State.model_validate(orjson.loads(_REPO_FIXTURE.read_bytes()))
    state_path = tmp_path / ".ea" / "state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_bytes(orjson.dumps(state.model_dump(mode="json")))
    for attempt in (1, 2):
        _append_executor_report(state_path, _SEEDED_WAVE, attempt)
    return state_path


# --------------------------------------------------------------------------
# Sweep helpers
# --------------------------------------------------------------------------


def _advertised_tokens(app: EaApp) -> tuple[str, ...]:
    """Return the leading key token of each footer-advertised hint fragment.

    Reads the active screen's :class:`Footer` ``hints`` reactive (the ordered
    strip the operator sees) and slices the key token off each ``"<token>
    <action>"`` fragment. Empty when the screen carries no footer (a bare
    harness).
    """
    footers = app.screen.query(Footer)
    if not footers:
        return ()
    return tuple(hint.split(" ", 1)[0] for hint in footers.first(Footer).hints)


def _sweep_order(tokens: list[str]) -> list[str]:
    """Order tokens so a mode-leaving key (``Esc``) is pressed last.

    ``Esc`` on the agent-watch zoom leaves the mode (back to the Feed), so
    testing it before the mode's other keys would press those keys in the wrong
    mode. Python's stable sort keeps every other token in advertised order.
    """
    return sorted(tokens, key=lambda token: token == "Esc")


async def _prime_mode(app: EaApp, pilot: Pilot[object], mode_name: str) -> None:
    """Focus / bind the mode-owned surface a nav key needs before the sweep.

    * **home** focuses the roadmap tree: the scope screen boots with the
      attention band holding focus, so the tree's advertised arrow / Enter keys
      route to it only once it is focused (the operator's Tab step).
    * **evidence** binds a minimal close-readiness ledger: the ``p`` peek drills
      the ledger criterion, which the render seam never computes live (it would
      spawn gate subprocesses), so a fixture ledger is pushed in the same way the
      daemon close envelope supplies one.
    """
    if mode_name == "home":
        trees = app.screen.query(RoadmapTree)
        if trees:
            trees.first(RoadmapTree).focus()
            await settle_screen(pilot)
    elif mode_name == "evidence" and isinstance(app.screen, EvidenceModeScreen):
        app.screen.set_readiness(
            CloseReadiness(
                ready=False,
                criteria=[
                    CriterionView(
                        id="CR-01",
                        source="spec",
                        status="pass",
                        gate_results=[GateResult(gate_id="G-01", status="pass")],
                    ),
                ],
            )
        )
        await settle_screen(pilot)


# --------------------------------------------------------------------------
# Unit tests -- pin the helper itself (criterion 1)
# --------------------------------------------------------------------------


class _LivenessProbeApp(App[None]):
    """A bare app with one key per response class: mutate / no-op / toast."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("a", "mutate", "mutate"),
        Binding("b", "noop", "noop"),
        Binding("t", "toast", "toast"),
    ]

    def compose(self) -> ComposeResult:
        """Yield a single body line the mutate action rewrites."""
        yield Static("original", id="probe-body")

    def action_mutate(self) -> None:
        """Rewrite the body line -- a visible text-frame delta."""
        self.query_one("#probe-body", Static).update("changed")

    def action_noop(self) -> None:
        """Resolve to a handler that does nothing -- a silent no-op."""
        return None

    def action_toast(self) -> None:
        """Emit only a toast -- no frame edit, response via the rack."""
        self.notify("liveness toast")


class _CursorProbeApp(App[None]):
    """A bare app whose focused table moves a row cursor without text change."""

    def compose(self) -> ComposeResult:
        """Yield a focusable table seeded on mount."""
        yield DataTable(id="probe-table")

    def on_mount(self) -> None:
        """Seed three rows and focus the table so Down moves its cursor."""
        table = self.query_one("#probe-table", DataTable)
        table.add_column("cell")
        table.add_rows([("row-0",), ("row-1",), ("row-2",)])
        table.focus()


def test_assert_footer_key_responds_frame_delta_passes() -> None:
    """A key that rewrites a visible line passes (frame-delta channel)."""

    async def body() -> None:
        app = _LivenessProbeApp()
        async with app.run_test(size=(80, 24)) as raw:
            pilot = cast(Pilot[object], raw)
            await settle_screen(pilot)
            response = await assert_footer_key_responds(pilot, "a", hint="a mutate")
            assert response.frame_changed is True
            assert response.responds is True

    asyncio.run(body())


def test_assert_footer_key_responds_silent_noop_raises() -> None:
    """A key whose handler does nothing raises AssertionError (the caught defect)."""

    async def body() -> None:
        app = _LivenessProbeApp()
        async with app.run_test(size=(80, 24)) as raw:
            pilot = cast(Pilot[object], raw)
            await settle_screen(pilot)
            with pytest.raises(AssertionError, match="no visible response"):
                await assert_footer_key_responds(pilot, "b", hint="b noop")

    asyncio.run(body())


def test_assert_footer_key_responds_toast_only_passes() -> None:
    """A key that only emits a toast passes via the toast channel (no frame delta)."""

    async def body() -> None:
        app = _LivenessProbeApp()
        async with app.run_test(size=(80, 24)) as raw:
            pilot = cast(Pilot[object], raw)
            await settle_screen(pilot)
            response = await assert_footer_key_responds(pilot, "t", hint="t toast")
            assert response.toast_added is True
            assert response.frame_changed is False
            assert response.responds is True

    asyncio.run(body())


def test_probe_footer_key_response_cursor_move_is_visible() -> None:
    """A row-cursor move passes via the cursor channel though the text is unchanged.

    This pins the reason the helper reads the focused widget's cursor: a
    DataTable selection move repaints only a style highlight, so the plain-text
    frame capture is byte-identical before and after. Without the cursor channel
    the helper would wrongly flag every arrow-key selection as dead.
    """

    async def body() -> None:
        app = _CursorProbeApp()
        async with app.run_test(size=(80, 24)) as raw:
            pilot = cast(Pilot[object], raw)
            await settle_screen(pilot)
            response = await probe_footer_key_response(pilot, "down")
            assert response.cursor_moved is True
            assert response.frame_changed is False
            assert response.responds is True

    asyncio.run(body())


# --------------------------------------------------------------------------
# Sweep -- all nine modes vs the footer-advertised key set (criterion 2)
# --------------------------------------------------------------------------


def test_sweep_covers_all_nine_modes() -> None:
    """The registry the sweep parametrises over is exactly the nine ratified modes."""
    assert [(spec.digit, spec.name) for spec in MODE_REGISTRY] == list(_EXPECTED_MODES)


def test_exemptions_reference_real_modes() -> None:
    """Every exemption names a registered mode and carries a concrete reason."""
    names = {spec.name for spec in MODE_REGISTRY}
    for (mode_name, token), reason in _EXEMPTIONS.items():
        assert mode_name in names, f"exemption names unknown mode: {mode_name!r}"
        assert reason.strip(), f"empty exemption reason for ({mode_name!r}, {token!r})"


def test_evidence_advertised_keys_respond_regression_pin(tmp_path: Path) -> None:
    """The evidence pane's arrows / Enter / peek each respond (the W27 fix, un-exempted).

    A named regression pin for the headline defect: the live probe found the
    evidence arrows, ``Enter``, and ``p`` peek silently no-oping. This asserts
    each visibly responds with reports seeded + a ledger bound, independent of
    the sweep's exemption table -- so the fix cannot be quietly regressed by
    adding an exemption.
    """
    state_path = _seeded_state_path(tmp_path)

    async def body() -> None:
        app = EaApp(scope="repo", state_path=state_path)
        async with app.run_test(size=_SIZE) as raw:
            pilot = cast(Pilot[object], raw)
            await settle_screen(pilot)
            await pilot.press("6")
            await settle_screen(pilot)
            assert isinstance(app.screen, EvidenceModeScreen)
            await _prime_mode(app, pilot, "evidence")
            # Down moves the report row cursor.
            await assert_footer_key_responds(pilot, "down", hint="evidence up-down")
            # Enter opens the report-detail modal; pop it before the peek.
            await assert_footer_key_responds(pilot, "enter", hint="evidence Enter")
            await pilot.press("escape")
            await settle_screen(pilot)
            # p peeks the ledger criterion (opens the evidence drill modal).
            await assert_footer_key_responds(pilot, "p", hint="evidence p")

    asyncio.run(body())


@pytest.mark.parametrize("spec", MODE_REGISTRY, ids=lambda spec: spec.name)
def test_mode_footer_keys_respond_or_are_exempted(spec: ModeSpec, tmp_path: Path) -> None:
    """Every mode-owned footer key visibly responds, or is explicitly exempted.

    The core gate: switch to *spec*'s mode, read its footer-advertised key
    strip, and for each mode-owned token either assert
    :func:`assert_footer_key_responds` (the key moves the surface) or confirm a
    concrete :data:`_EXEMPTIONS` reason. A dead key that is neither responsive
    nor exempted fails here -- the exact regression class the wave gates.
    """
    state_path = _seeded_state_path(tmp_path)

    async def body() -> None:
        app = EaApp(scope="repo", state_path=state_path)
        async with app.run_test(size=_SIZE) as raw:
            pilot = cast(Pilot[object], raw)
            await settle_screen(pilot)
            await pilot.press(spec.digit)
            await settle_screen(pilot)
            assert app.current_mode == spec.name
            await _prime_mode(app, pilot, spec.name)

            tokens = _advertised_tokens(app)
            # No stale exemption: every exemption declared for this mode must be
            # an actually-advertised token (guards against silent drift).
            declared = {token for (mode_name, token) in _EXEMPTIONS if mode_name == spec.name}
            assert declared <= set(tokens), (
                f"stale exemption(s) for {spec.name!r}: {sorted(declared - set(tokens))!r}"
            )

            mode_owned = [token for token in tokens if token not in _APP_CHROME_TOKENS]
            for token in _sweep_order(mode_owned):
                reason = _EXEMPTIONS.get((spec.name, token))
                if reason is not None:
                    continue
                key = _TOKEN_TO_KEY.get(token, token)
                response = await assert_footer_key_responds(pilot, key, hint=f"{spec.name} {token}")
                # Teardown so the next key starts from the mode's base screen:
                # a key that left the mode is re-entered; a pushed modal is popped.
                if app.current_mode != spec.name:
                    await pilot.press(spec.digit)
                    await settle_screen(pilot)
                    await _prime_mode(app, pilot, spec.name)
                elif response.screen_changed:
                    await pilot.press("escape")
                    await settle_screen(pilot)

    asyncio.run(body())
