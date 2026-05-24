"""Ordered list of wizard steps shared by the interactive and ``--no-input`` modes.

Per ``docs/architecture/installation.md``, ``eawf init`` walks the
operator through twelve decisions before materialising a fresh ``.ea/``
directory. Both the questionary TTY surface (:mod:`eawf.platform.install.wizard`)
and the ``--no-input`` non-interactive path consume this single, ordered
list so the two surfaces can never drift on either prompt count or prompt
order.

Each step is described by an immutable :class:`WizardStep` carrying:

- ``id``      — answer key in :class:`~eawf.platform.install.wizard.WizardAnswers`.
- ``prompt``  — short human-readable label (questionary renders it;
  ``--no-input`` surfaces it only on validation errors).
- ``kind``    — narrow Literal of the input shape; chosen so each kind maps
  to a single questionary prompt *and* a single argparse-side option type.
- ``default`` — value used when the operator skips the prompt OR omits the
  flag in ``--no-input``. None of the defaults are required-fields; the
  wizard layer enforces "required" via Pydantic, not via a sentinel here.
- ``cli_flag``— canonical Typer flag for the matching ``cli/commands/init.py``
  parameter. Documented as a one-shot manifest so future "regenerate the CLI
  from the steps" tooling can introspect it without reparsing the Typer app.
- ``choices`` — explicit enumeration for ``kind="choice"``; ``None`` for
  free-form ``text``/``path`` and for ``bool`` (only ``True`` / ``False``).

The full list is exported as :data:`WIZARD_STEPS` and pinned to length 12 by
:mod:`tests.unit.test_install_wizard_steps` so a future contributor cannot
silently add or drop a step.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from eawf.kernel.state.ids import RE_PROJECT_CODE

# Narrow set of input shapes. Adding a new kind requires adding a matching
# branch in both the questionary surface and :class:`WizardAnswers`;
# keeping the Literal narrow surfaces "I added a kind but forgot the
# renderer" at type check time.
WizardKind = Literal["text", "choice", "multichoice", "bool", "path"]


@dataclass(frozen=True)
class WizardStep:
    """One prompt in the ``eawf init`` wizard.

    Attributes:
        id: Stable answer key (matches the field name on
            :class:`~eawf.platform.install.wizard.WizardAnswers`).
        prompt: One-line label. Used by questionary for the prompt text
            and by ``--no-input`` validation errors when a required answer
            is missing.
        kind: One of ``text``, ``choice``, ``multichoice``, ``bool``,
            ``path`` — see :data:`WizardKind`.
        default: Default value if the operator does not provide one.
            Type intentionally :class:`Any` because each kind imposes its own
            shape (``str`` for text/path/choice, tuple of strings for
            multichoice, ``bool`` for bool).
        cli_flag: Canonical Typer-side flag name (e.g. ``"--state-path"``).
            Documented here so the steps list is a single-source-of-truth
            manifest for the wizard surface.
        choices: Allowed values for ``kind="choice"``. Always ``None`` for
            other kinds.
        validate: Optional inline-validation callable threaded through
            ``questionary.text``/``questionary.path`` so a malformed entry
            is rejected at the prompt rather than after the wizard
            finishes. Returns ``True`` on success or a friendly error
            string on failure (questionary contract).
        filter: Optional post-collection normaliser applied before the
            answer is returned to the wizard runner. Currently used for
            ``project_code`` to auto-uppercase the entered value.
    """

    id: str
    prompt: str
    kind: WizardKind
    default: Any
    cli_flag: str
    choices: tuple[str, ...] | None = None
    validate: Callable[[str], bool | str] | None = None
    filter: Callable[[str], str] | None = None


def _validate_project_code_input(value: str) -> bool | str:
    """Questionary-side inline validator for :data:`STEP_PROJECT_CODE`.

    Uppercases the entry before matching the canonical regex so a
    lowercase entry like ``"demo"`` is accepted; :data:`STEP_PROJECT_CODE`
    pairs this with ``filter=str.upper`` so the recorded answer is
    normalised to uppercase before reaching :class:`WizardAnswers`.
    Returns ``True`` on success or the friendly error string surfaced
    inline by questionary on failure.
    """
    if RE_PROJECT_CODE.fullmatch(value.upper()):
        return True
    return "Project code must be 2-16 characters, start with A-Z, then A-Z/0-9/-/_ only."


# The twelve canonical wizard steps. Ordering matches ``docs/architecture/installation.md``.
# Each id is referenced verbatim by :class:`WizardAnswers`; renaming an id
# is therefore a breaking change that requires a parallel edit in
# :mod:`eawf.platform.install.wizard`.
STEP_STATE_PATH = WizardStep(
    id="state_path",
    prompt="Where should the state file live?",
    kind="path",
    default=".ea/state.json",
    cli_flag="--state-path",
)

STEP_PROJECT_CODE = WizardStep(
    id="project_code",
    prompt="Project code (2-16 chars, A-Z/0-9/-/_; auto-uppercased)?",
    kind="text",
    default="",
    cli_flag="--project-code",
    validate=_validate_project_code_input,
    filter=str.upper,
)

STEP_PROJECT_TITLE = WizardStep(
    id="project_title",
    prompt="Project title (free-form)?",
    kind="text",
    default="",
    cli_flag="--project-title",
)

STEP_LIFECYCLE_DEPTH = WizardStep(
    id="lifecycle_depth",
    prompt="Default lifecycle depth?",
    kind="choice",
    default="phase",
    cli_flag="--lifecycle-depth",
    choices=("phase", "iter", "wave"),
)

STEP_PROFILES = WizardStep(
    id="profiles",
    prompt="Which profiles should be enabled?",
    kind="multichoice",
    default=("core",),
    cli_flag="--profile",
)

STEP_RUNTIME = WizardStep(
    id="runtime",
    prompt="Default runtime?",
    kind="choice",
    default="claude-code",
    cli_flag="--runtime",
    choices=("claude-code", "codex", "opencode", "generic"),
)

STEP_PLUGINS = WizardStep(
    id="plugins",
    prompt="Optional plugins (repeatable)?",
    kind="multichoice",
    default=(),
    cli_flag="--plugin",
)

STEP_MCP = WizardStep(
    id="mcp",
    prompt="Optional MCP servers (repeatable)?",
    kind="multichoice",
    default=(),
    cli_flag="--mcp",
)

STEP_ACCEPTANCE_TESTS = WizardStep(
    id="acceptance_tests",
    prompt="Require tests as an acceptance gate?",
    kind="bool",
    default=True,
    cli_flag="--acceptance-tests/--no-acceptance-tests",
)

STEP_ACCEPTANCE_LINT = WizardStep(
    id="acceptance_lint",
    prompt="Require lint as an acceptance gate?",
    kind="bool",
    default=True,
    cli_flag="--acceptance-lint/--no-acceptance-lint",
)

STEP_ACCEPTANCE_TYPECHECK = WizardStep(
    id="acceptance_typecheck",
    prompt="Require typecheck as an acceptance gate?",
    kind="bool",
    default=True,
    cli_flag="--acceptance-typecheck/--no-acceptance-typecheck",
)

STEP_WRITE_CONFIRM = WizardStep(
    id="write_confirm",
    prompt="Confirm before writing files?",
    kind="bool",
    default=True,
    cli_flag="--write-confirm/--no-write-confirm",
)


# Pin order — both surfaces consume it as-is. Tests assert the count and id
# uniqueness, so a refactor cannot silently elide a step.
WIZARD_STEPS: tuple[WizardStep, ...] = (
    STEP_STATE_PATH,
    STEP_PROJECT_CODE,
    STEP_PROJECT_TITLE,
    STEP_LIFECYCLE_DEPTH,
    STEP_PROFILES,
    STEP_RUNTIME,
    STEP_PLUGINS,
    STEP_MCP,
    STEP_ACCEPTANCE_TESTS,
    STEP_ACCEPTANCE_LINT,
    STEP_ACCEPTANCE_TYPECHECK,
    STEP_WRITE_CONFIRM,
)


assert len({s.id for s in WIZARD_STEPS}) == len(WIZARD_STEPS), "WIZARD_STEPS ids must be unique"


__all__ = [
    "STEP_ACCEPTANCE_LINT",
    "STEP_ACCEPTANCE_TESTS",
    "STEP_ACCEPTANCE_TYPECHECK",
    "STEP_LIFECYCLE_DEPTH",
    "STEP_MCP",
    "STEP_PLUGINS",
    "STEP_PROFILES",
    "STEP_PROJECT_CODE",
    "STEP_PROJECT_TITLE",
    "STEP_RUNTIME",
    "STEP_STATE_PATH",
    "STEP_WRITE_CONFIRM",
    "WIZARD_STEPS",
    "WizardKind",
    "WizardStep",
]
