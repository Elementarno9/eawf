"""Interactive ``eawf init`` wizard tests driven via prompt_toolkit pipes.

The interactive surface uses :mod:`questionary` (which sits on
:mod:`prompt_toolkit`) — driving it deterministically requires injecting
a :class:`prompt_toolkit.input.PipeInput` and a
:class:`prompt_toolkit.output.DummyOutput` into the application session
before each ``ask()`` call. ``\\r`` ends a prompt, ``\\x03`` (ETX) is
treated as a Ctrl-C / cancellation, and space toggles a checkbox entry.

Coverage:

- :func:`~eawf.install.wizard._ask_step` round-trips for each of the five
  :class:`~eawf.install.steps.WizardKind` variants (``text``, ``bool``,
  ``choice``, ``multichoice``, ``path``).
- Cancellation: a Ctrl-C byte at any prompt raises
  :class:`~eawf.install.wizard.WizardCancelled`.
- End-to-end: feeding every step's input through a single pipe lets the
  wizard land a real ``state.json`` + ``AGENTS.md`` + render manifest on
  ``tmp_path`` (proves :func:`run_wizard_interactive` delegates to the
  pure pipeline and surfaces a populated :class:`WizardResult`).
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from prompt_toolkit.application import create_app_session
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.input.base import PipeInput
from prompt_toolkit.output import DummyOutput

from eawf.install.steps import (
    STEP_ACCEPTANCE_LINT,
    STEP_ACCEPTANCE_TESTS,
    STEP_ACCEPTANCE_TYPECHECK,
    STEP_LIFECYCLE_DEPTH,
    STEP_MCP,
    STEP_PLUGINS,
    STEP_PROFILES,
    STEP_PROJECT_CODE,
    STEP_PROJECT_TITLE,
    STEP_RUNTIME,
    STEP_STATE_PATH,
    STEP_WRITE_CONFIRM,
)
from eawf.install.wizard import (
    WizardCancelled,
    _ask_step,
    run_wizard_interactive,
)


@pytest.fixture
def pipe_session() -> Iterator[PipeInput]:
    """Yield a configured :class:`PipeInput` inside a prompt_toolkit session.

    The session installs ``DummyOutput`` so questionary writes never reach
    the real terminal. Tests push keystrokes via :meth:`PipeInput.send_text`
    before they call any :mod:`questionary` function.
    """
    with create_pipe_input() as inp, create_app_session(input=inp, output=DummyOutput()):
        yield inp


# ---- _ask_step per step kind ----------------------------------------------


def test_ask_step_text_returns_typed_string(pipe_session: PipeInput) -> None:
    pipe_session.send_text("HELLO\r")
    value = _ask_step(STEP_PROJECT_CODE)
    assert value == "HELLO"


def test_ask_step_text_keeps_default_on_empty_enter(pipe_session: PipeInput) -> None:
    # Default is empty string for project_title — pressing enter without
    # typing falls through to the default.
    pipe_session.send_text("\r")
    value = _ask_step(STEP_PROJECT_TITLE)
    assert value == STEP_PROJECT_TITLE.default


def test_ask_step_bool_returns_true_for_y(pipe_session: PipeInput) -> None:
    pipe_session.send_text("y\r")
    value = _ask_step(STEP_ACCEPTANCE_TESTS)
    assert value is True


def test_ask_step_bool_returns_false_for_n(pipe_session: PipeInput) -> None:
    pipe_session.send_text("n\r")
    value = _ask_step(STEP_ACCEPTANCE_TESTS)
    assert value is False


def test_ask_step_choice_returns_default_on_enter(pipe_session: PipeInput) -> None:
    # Default for STEP_LIFECYCLE_DEPTH is "phase"; enter on the highlighted
    # default returns it verbatim.
    pipe_session.send_text("\r")
    value = _ask_step(STEP_LIFECYCLE_DEPTH)
    assert value == "phase"


def test_ask_step_choice_runtime_returns_default(pipe_session: PipeInput) -> None:
    pipe_session.send_text("\r")
    value = _ask_step(STEP_RUNTIME)
    assert value == "claude-code"


def test_ask_step_path_returns_default_on_enter(pipe_session: PipeInput) -> None:
    pipe_session.send_text("\r")
    value = _ask_step(STEP_STATE_PATH)
    assert value == ".ea/state.json"


def test_ask_step_multichoice_profiles_returns_default_preselected(
    pipe_session: PipeInput,
) -> None:
    # STEP_PROFILES is preselected via Choice(checked=True) for the default
    # tuple. Pressing enter without toggling commits the preselection.
    pipe_session.send_text("\r")
    value = _ask_step(STEP_PROFILES)
    assert isinstance(value, tuple)
    # ``core`` is the canonical default and must round-trip on a no-op enter.
    assert "core" in value


def test_ask_step_multichoice_plugins_text_fallback_returns_empty_tuple(
    pipe_session: PipeInput,
) -> None:
    # STEP_PLUGINS has no static choices — falls back to comma-separated text
    # input. Empty input → empty tuple.
    pipe_session.send_text("\r")
    value = _ask_step(STEP_PLUGINS)
    assert value == ()


def test_ask_step_multichoice_mcp_text_fallback_splits_csv(
    pipe_session: PipeInput,
) -> None:
    pipe_session.send_text("alpha, beta ,gamma\r")
    value = _ask_step(STEP_MCP)
    assert value == ("alpha", "beta", "gamma")


# ---- cancellation ---------------------------------------------------------


def test_ask_step_text_ctrl_c_raises_wizard_cancelled(
    pipe_session: PipeInput,
) -> None:
    pipe_session.send_text("\x03")  # ETX — questionary returns None
    with pytest.raises(WizardCancelled, match="project_code"):
        _ask_step(STEP_PROJECT_CODE)


def test_ask_step_choice_ctrl_c_raises_wizard_cancelled(
    pipe_session: PipeInput,
) -> None:
    pipe_session.send_text("\x03")
    with pytest.raises(WizardCancelled, match="lifecycle_depth"):
        _ask_step(STEP_LIFECYCLE_DEPTH)


# ---- end-to-end round trip ------------------------------------------------


def _send_full_wizard_inputs(inp: PipeInput, *, project_code: str) -> None:
    """Push enough keystrokes to drive every WIZARD_STEPS prompt to completion.

    Order matches :data:`~eawf.install.steps.WIZARD_STEPS`:
    state_path → project_code → project_title → lifecycle_depth →
    profiles → runtime → plugins → mcp → acceptance_{tests,lint,typecheck} →
    write_confirm.
    """
    inp.send_text("\r")  # state_path: keep default ".ea/state.json"
    inp.send_text(f"{project_code}\r")  # project_code
    inp.send_text("Demo Project\r")  # project_title
    inp.send_text("\r")  # lifecycle_depth: keep "phase" (default)
    inp.send_text("\r")  # profiles checkbox: keep preselected default ("core",)
    inp.send_text("\r")  # runtime: keep "claude-code" (default)
    inp.send_text("\r")  # plugins (text fallback): empty
    inp.send_text("\r")  # mcp (text fallback): empty
    inp.send_text("y\r")  # acceptance_tests
    inp.send_text("y\r")  # acceptance_lint
    inp.send_text("y\r")  # acceptance_typecheck
    inp.send_text("y\r")  # write_confirm


def test_run_wizard_interactive_full_round_trip_writes_state(
    tmp_path: Path,
) -> None:
    project_code = "WZTEST"
    with create_pipe_input() as inp, create_app_session(input=inp, output=DummyOutput()):
        _send_full_wizard_inputs(inp, project_code=project_code)
        result = run_wizard_interactive(tmp_path, force=False)
    assert result.project_code == project_code
    assert result.profiles_enabled == ["core"]
    assert (tmp_path / ".ea" / "state.json").exists()
    assert (tmp_path / "AGENTS.md").exists()


def test_run_wizard_interactive_cancels_at_first_prompt(
    tmp_path: Path,
) -> None:
    with create_pipe_input() as inp, create_app_session(input=inp, output=DummyOutput()):
        inp.send_text("\x03")  # Ctrl-C at the first prompt
        with pytest.raises(WizardCancelled):
            run_wizard_interactive(tmp_path, force=False)
    # Cancellation must NOT leave a half-initialised .ea/ behind.
    assert not (tmp_path / ".ea" / "state.json").exists()


# Ensure the unused step references are explicit imports rather than
# silent dependencies; mypy + ruff already enforce, but a runtime sanity
# check avoids a future lint-driven removal that breaks the multichoice
# defaults assumption.
_REFERENCED_STEPS = (
    STEP_ACCEPTANCE_LINT,
    STEP_ACCEPTANCE_TYPECHECK,
    STEP_WRITE_CONFIRM,
)


# ---- inline-validate / auto-uppercase regression pins ---------------------


def test_step_project_code_validate_accepts_uppercase_canonical_form() -> None:
    """The inline validator returns ``True`` for a canonical uppercase code."""
    assert STEP_PROJECT_CODE.validate is not None
    assert STEP_PROJECT_CODE.validate("DEMO") is True


def test_wizard_inline_validate_lowercase_project_code() -> None:
    """The inline validator accepts lowercase input by uppercasing before regex match.

    The fix repairs the user-reported wizard UX: a lowercase entry like
    ``"fsdf"`` previously survived all 12 steps before a Pydantic
    ``ValidationError`` blob landed at the end. The inline callback now
    accepts it (after uppercasing) so the recorded value can be auto-
    normalised by ``filter=str.upper``.
    """
    assert STEP_PROJECT_CODE.validate is not None
    assert STEP_PROJECT_CODE.validate("fsdf") is True
    assert STEP_PROJECT_CODE.filter is not None
    assert STEP_PROJECT_CODE.filter("fsdf") == "FSDF"


def test_wizard_inline_validate_rejects_malformed_input_with_friendly_msg() -> None:
    """Truly invalid input returns the operator-facing friendly string, not a Pydantic blob."""
    assert STEP_PROJECT_CODE.validate is not None
    out = STEP_PROJECT_CODE.validate("1bad")  # leading digit fails the regex
    assert isinstance(out, str)
    assert "must be 2-16 characters" in out
    assert "Pydantic" not in out and "ValidationError" not in out


def test_wizard_lowercase_project_code_auto_uppercased(tmp_path: Path) -> None:
    """End-to-end: a lowercase entry round-trips into an uppercase ``project_code`` in state.json.

    Drives the full questionary wizard via prompt_toolkit pipe input.
    The lowercase entry passes the inline validate (uppercase-then-
    match), then ``filter=str.upper`` normalises the recorded answer,
    and the wizard pipeline writes a state.json whose ``current.project_code``
    is uppercase.
    """
    project_code_lower = "wztest"
    expected_upper = "WZTEST"
    with create_pipe_input() as inp, create_app_session(input=inp, output=DummyOutput()):
        _send_full_wizard_inputs(inp, project_code=project_code_lower)
        result = run_wizard_interactive(tmp_path, force=False)
    assert result.project_code == expected_upper
    import orjson

    payload = orjson.loads((tmp_path / ".ea" / "state.json").read_bytes())
    assert payload["current"]["project_code"] == expected_upper
