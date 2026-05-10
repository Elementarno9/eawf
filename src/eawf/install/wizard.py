"""``eawf init`` wizard — questionary interactive surface + pure ``--no-input`` pipeline.

Per ``eawf-v0.1-plan.md`` §P03 W05 the init wizard has two surfaces sharing
one engine:

1. **Pure pipeline.** :func:`run_wizard_no_input` takes a fully populated
   :class:`WizardAnswers` and writes ``state.json`` + ``.ea/config.yaml`` +
   ``AGENTS.md`` + ``CLAUDE.md`` + the render manifest. No user interaction.
   Re-running with the same answers is byte-stable on the rendered files.

2. **Interactive questionary loop.** :func:`run_wizard_interactive` walks
   :data:`~eawf.install.steps.WIZARD_STEPS` sequentially, dispatching each
   step kind to the matching :mod:`questionary` prompt, then delegates to
   the same pure pipeline. Answers stream through a typed, sequential
   loop so the surface stays minimal and the dependency footprint stays
   small (questionary pulls :mod:`prompt_toolkit`, no reactive UI stack).

The pure pipeline is the contract — the interactive surface is a usability
nicety. CI exercises ``--no-input`` directly via
:class:`typer.testing.CliRunner`; :func:`run_wizard_interactive` is covered
by piping :class:`prompt_toolkit.input.PipeInput` into a session-scoped
input source (see ``tests/integration/test_cli_init_interactive.py``).

Public API:

- :class:`WizardAnswers` — Pydantic v2 model mirroring the 12 step ids,
  forbidding extras and validating ``project_code`` plus ``profiles`` membership.
- :class:`WizardResult` — Pydantic v2 model summarising the artefacts written.
- :class:`WizardCancelled` — raised when the operator aborts a prompt
  (Ctrl-C / EOF). Subclass of :class:`~eawf.cli.errors.UserDeclined` so the
  CLI handler maps it to the canonical ``USER_DECLINED`` exit code.
- :func:`run_wizard_no_input` — pure pipeline.
- :func:`run_wizard_interactive` — questionary TTY entry-point.

Compatibility note (``enable_profile``):

The existing :func:`eawf.config.profile.enable_profile` writes through a
config-layer file and assumes the profile id is recorded under
``profiles.enabled`` afterwards. The wizard already writes that section
itself (so the rendered config is byte-stable across re-runs) — calling
``enable_profile`` again would only matter for state-key materialisation.
For the v0.1 init flow we sidestep ``enable_profile`` entirely and call the
private :func:`eawf.config.profile._materialise_state_keys` helper directly
on the freshly-written ``state.json``. This keeps the init transaction
minimal — no second write to ``config.yaml`` from inside ``enable_profile``,
no risk of a re-ordered ``profiles.enabled`` entry on a re-run — and is the
documented W05 escape hatch when ``enable_profile`` is intolerant of the
init flow.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from eawf.cli.errors import InvalidInput, UserDeclined
from eawf.config.profile import _atomic_write_yaml, _materialise_state_keys
from eawf.install.steps import (
    STEP_LIFECYCLE_DEPTH,
    STEP_RUNTIME,
    WIZARD_STEPS,
    WizardStep,
)
from eawf.lock import portalock
from eawf.profiles.compose import compose
from eawf.profiles.loader import list_profiles, load_profile
from eawf.render.agents_md import render_agents_md
from eawf.render.claude_shim import render_claude_md
from eawf.render.manifest import Manifest
from eawf.render.manifest import save_atomic as save_manifest_atomic
from eawf.state.enums import ScopeKind
from eawf.state.ids import RE_PROJECT_CODE
from eawf.state.urn import build as build_urn
from eawf.state.writer import atomic_write_json_locked

logger = logging.getLogger(__name__)


# Files we treat as "the .ea is already initialised". Anything else inside
# .ea/ (locks, transient probe caches) is tolerated even on a fresh init —
# they belong to other tooling. The list mirrors the v0.1 contract: state +
# config are the persistent on-disk state; everything else is rebuildable.
_CANONICAL_EA_FILES: tuple[str, ...] = ("state.json", "config.yaml")


# Single source of truth for the runtime/lifecycle-depth allow-lists. The
# canonical declaration lives on the matching :class:`WizardStep`; mirroring
# it here as a frozenset means the field validators stay typo-proof and any
# future edit to ``STEP_RUNTIME.choices`` / ``STEP_LIFECYCLE_DEPTH.choices``
# propagates automatically — no parallel hard-coded set in this module.
#
# ``WizardStep.choices`` is ``tuple[str, ...] | None`` because non-``choice``
# kinds carry no enumeration. For these two steps the v0.1 contract
# guarantees the choices are present (see :mod:`eawf.install.steps` and the
# :func:`test_wizard_step_choices_only_for_choice_kind` pin); we assert that
# invariant at module load so a broken steps manifest fails loudly here
# instead of silently producing an empty allow-list.
assert STEP_RUNTIME.choices is not None, "STEP_RUNTIME.choices must be populated"
assert STEP_LIFECYCLE_DEPTH.choices is not None, "STEP_LIFECYCLE_DEPTH.choices must be populated"
_RUNTIME_CHOICES: frozenset[str] = frozenset(STEP_RUNTIME.choices)
_LIFECYCLE_DEPTH_CHOICES: frozenset[str] = frozenset(STEP_LIFECYCLE_DEPTH.choices)


class WizardAnswers(BaseModel):
    """Pydantic-validated answers to all 12 wizard prompts.

    The field names match :data:`~eawf.install.steps.WIZARD_STEPS` ids so the
    interactive surface can map widget values straight onto this model with
    ``WizardAnswers(**{step.id: value, ...})``. Any extra key is rejected
    upfront so a typo in either surface fails loudly.

    Validation rules:

    - ``project_code`` matches the canonical project-code regex
      (``^[A-Z][A-Z0-9_-]{1,15}$`` — see :mod:`eawf.state.ids`). The wizard
      rejects empty strings: ``project_code`` is the one prompt without a
      sensible default.
    - ``profiles`` must be a non-empty tuple of strings, each of which is a
      known profile per :func:`~eawf.profiles.loader.list_profiles`.
    - ``runtime`` and ``lifecycle_depth`` are validated against the static
      enumerations declared by their respective steps.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    state_path: str
    project_code: Annotated[str, Field(min_length=2, max_length=16)]
    project_title: str
    lifecycle_depth: str
    profiles: tuple[str, ...]
    runtime: str
    plugins: tuple[str, ...] = ()
    mcp: tuple[str, ...] = ()
    acceptance_tests: bool = True
    acceptance_lint: bool = True
    acceptance_typecheck: bool = True
    # ``write_confirm`` is reserved for the **interactive** wizard surface.
    # In the v0.1 ``--no-input`` pipeline it has no effect — the pipeline
    # always proceeds straight to the writes. The field is retained so the
    # interactive questionary flow can later gate a "confirm? [Y/n]" summary
    # prompt on it without reshaping :class:`WizardAnswers`. The matching
    # ``--write-confirm`` CLI flag was deliberately not surfaced (per the
    # P03 W05 review): exposing a flag with no behaviour is misleading.
    write_confirm: bool = True

    @field_validator("project_code")
    @classmethod
    def _validate_project_code(cls, value: str) -> str:
        """Reject anything that does not match the canonical project-code regex."""
        if not RE_PROJECT_CODE.fullmatch(value):
            raise ValueError(f"project_code {value!r} must match {RE_PROJECT_CODE.pattern}")
        return value

    @field_validator("profiles")
    @classmethod
    def _validate_profiles(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """Require a non-empty tuple of profiles, each a known id."""
        if not value:
            raise ValueError("at least one profile must be selected")
        known = set(list_profiles())
        unknown = sorted(set(value) - known)
        if unknown:
            raise ValueError(f"unknown profile(s): {unknown}; choose from {sorted(known)}")
        return value

    @field_validator("runtime")
    @classmethod
    def _validate_runtime(cls, value: str) -> str:
        """Match the static set declared by ``STEP_RUNTIME.choices``.

        The allow-list is derived from :data:`_RUNTIME_CHOICES` at module
        load so a future edit to ``STEP_RUNTIME.choices`` propagates here
        without a parallel hard-coded literal.
        """
        if value not in _RUNTIME_CHOICES:
            raise ValueError(f"runtime {value!r} not in {sorted(_RUNTIME_CHOICES)}")
        return value

    @field_validator("lifecycle_depth")
    @classmethod
    def _validate_lifecycle_depth(cls, value: str) -> str:
        """Match the static set declared by ``STEP_LIFECYCLE_DEPTH.choices``.

        The allow-list is derived from :data:`_LIFECYCLE_DEPTH_CHOICES` at
        module load so a future edit to ``STEP_LIFECYCLE_DEPTH.choices``
        propagates here without a parallel hard-coded literal.
        """
        if value not in _LIFECYCLE_DEPTH_CHOICES:
            raise ValueError(f"lifecycle_depth {value!r} not in {sorted(_LIFECYCLE_DEPTH_CHOICES)}")
        return value


class WizardResult(BaseModel):
    """Summary of files written by :func:`run_wizard_no_input`.

    Returned to the caller (CLI handler or test harness) so the JSON envelope
    can describe exactly what landed on disk. Paths are absolute so the
    operator can copy them verbatim — relative paths would force the caller
    to know the resolved ``target_dir``.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    state_path: Path
    config_path: Path
    agents_md_path: Path
    claude_md_path: Path
    manifest_path: Path
    profiles_enabled: list[str]
    project_code: str
    materialised_state_keys: list[str]


def _pre_existing_canonical_files(ea_dir: Path) -> list[str]:
    """Return the canonical files already present under ``ea_dir``.

    Used by the ``force`` gate. Only :data:`_CANONICAL_EA_FILES` count — a
    stray ``locks/`` subdirectory or an editor swapfile is not enough to
    block init, because those are reproducible artefacts.
    """
    if not ea_dir.exists():
        return []
    found: list[str] = []
    for name in _CANONICAL_EA_FILES:
        if (ea_dir / name).exists():
            found.append(name)
    return found


def _build_initial_state(*, project_code: str, project_title: str) -> dict[str, Any]:
    """Build a minimal-but-valid ``state.json`` payload for a fresh init.

    Mirrors the shape used by :func:`eawf.cli.commands.lifecycle.project_init`
    so the two entry-points produce identical state files. We deliberately
    do NOT instantiate a :class:`~eawf.state.models.Project` here — the
    project record requires ``domains`` which the wizard does not collect,
    and the v0.1 init contract permits ``project = null`` (the operator can
    follow up with ``eawf project init``).
    """
    timestamp = datetime.now(UTC).isoformat()
    return {
        "schema_version": "1.0",
        "scope_kind": ScopeKind.REPO.value,
        "urn": build_urn("state", owner=project_code),
        "updated_at": timestamp,
        "project": None,
        "current": {
            "project_code": project_code,
            "subproject_id": None,
            "phase_id": None,
            "iter_id": None,
            "active_wave_ids": [],
            "active_session_ids": [],
        },
        "workspace": None,
        "phases": {},
        "iters": {},
        "waves": {},
        "artifacts": {},
        "agent_sessions": {},
        "plugins": {},
        "indexes": {"project_title": project_title},
    }


def _build_config_yaml(answers: WizardAnswers) -> dict[str, Any]:
    """Serialise ``answers`` into the canonical ``.ea/config.yaml`` shape.

    Schema (sorted on write by ``_atomic_write_yaml``)::

        {
          "schema_version": "1.0",
          "profiles":   {"enabled": [...]},
          "runtime":    {"kind": "claude-code"},
          "lifecycle":  {"depth": "phase"},
          "acceptance": {"tests": True, "lint": True, "typecheck": True},
          "plugins":    {"enabled": [...]},
          "mcp":        {"enabled": [...]},
        }

    Sorting (by ``yaml.safe_dump(sort_keys=True)``) keeps the file
    byte-stable across re-runs — golden snapshots and pre-commit's
    end-of-file-fixer become idempotent on the second pass.
    """
    return {
        "schema_version": "1.0",
        "profiles": {"enabled": list(answers.profiles)},
        "runtime": {"kind": answers.runtime},
        "lifecycle": {"depth": answers.lifecycle_depth},
        "acceptance": {
            "tests": answers.acceptance_tests,
            "lint": answers.acceptance_lint,
            "typecheck": answers.acceptance_typecheck,
        },
        "plugins": {"enabled": list(answers.plugins)},
        "mcp": {"enabled": list(answers.mcp)},
    }


def run_wizard_no_input(
    answers: WizardAnswers,
    target_dir: Path,
    *,
    force: bool = False,
) -> WizardResult:
    """Materialise the full ``.ea/`` + AGENTS.md + CLAUDE.md from *answers*.

    Workflow:

    1. Refuse to overwrite an existing ``.ea/state.json`` or ``.ea/config.yaml``
       unless ``force=True``. Other detritus under ``.ea/`` (locks, probe
       caches) is tolerated.
    2. Resolve the state path. If ``answers.state_path`` is relative it is
       anchored at ``target_dir``.
    3. Acquire the sibling lock on the state path and write the minimal
       state document via :func:`atomic_write_json_locked`. The lock
       prevents a concurrent ``eawf project init`` from racing the write.
    4. Write ``.ea/config.yaml`` via :func:`_atomic_write_yaml` (held under
       its own lock).
    5. Materialise ``state_extensions.fields_required`` for every selected
       profile via :func:`eawf.config.profile._materialise_state_keys` —
       the freshly-written state already exists, so the helper is happy.
    6. Compose the selected profiles, render ``AGENTS.md``, persist the
       manifest, then write the ``CLAUDE.md`` shim. Manifest path is
       ``<target_dir>/.ea/indexes/generated.json``.

    **Failure semantics.** This function is **not transactional**. The
    write order is ``state.json`` → ``config.yaml`` → ``AGENTS.md`` →
    manifest → ``CLAUDE.md``; a failure midway leaves whichever files were
    written before the failure on disk. There is no automatic rollback in
    v0.1.

    Recovery for v0.1 is one of:

    - **Clean retry.** Re-run with ``--force`` (or call this function with
      ``force=True``). The pipeline overwrites ``state.json`` /
      ``config.yaml`` and re-renders ``AGENTS.md`` / manifest / ``CLAUDE.md``
      from scratch — this is the documented recovery path.
    - **Manual cleanup.** Delete ``<target_dir>/.ea/``,
      ``<target_dir>/AGENTS.md`` and ``<target_dir>/CLAUDE.md``, then
      re-run without ``--force``.

    Phase 6 (or later) is expected to wrap this sequence in a two-phase
    commit pattern (write to a temp prefix, atomic-rename on success); the
    public signature stays identical.

    Args:
        answers: Validated :class:`WizardAnswers` instance.
        target_dir: Absolute target directory. Created on demand.
        force: When True, overwrite an existing ``.ea/`` even if it has
            canonical files. The default rejection is opinionated — the
            operator must opt in to clobbering an init they did before.
            Also the documented recovery path after a partial init.

    Returns:
        :class:`WizardResult` with absolute paths and a list of materialised
        state keys (so the CLI envelope can surface "added 3 keys to state").

    Raises:
        InvalidInput: ``.ea/`` already contains canonical files and
            ``force`` is False.
    """
    target_dir = target_dir.resolve()
    target_dir.mkdir(parents=True, exist_ok=True)
    ea_dir = target_dir / ".ea"
    pre_existing = _pre_existing_canonical_files(ea_dir)
    if pre_existing and not force:
        raise InvalidInput(
            f".ea already initialised at {ea_dir} (found "
            f"{sorted(pre_existing)}); pass --force to overwrite"
        )

    state_path_raw = Path(answers.state_path)
    state_path = state_path_raw if state_path_raw.is_absolute() else target_dir / state_path_raw
    state_path = state_path.resolve()
    state_path.parent.mkdir(parents=True, exist_ok=True)

    # State first — locked write, holds the same sibling lock the rest of the
    # CLI honours so a concurrent project-init cannot race.
    state_payload = _build_initial_state(
        project_code=answers.project_code,
        project_title=answers.project_title,
    )
    with portalock.acquire(state_path, timeout=5.0):
        atomic_write_json_locked(state_path, state_payload)

    # Config yaml — second write, separate lock. Its layout is what the
    # layered-config loader will see on the next ``eawf config get``.
    config_path = (ea_dir / "config.yaml").resolve()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    with portalock.acquire(config_path, timeout=5.0):
        _atomic_write_yaml(config_path, _build_config_yaml(answers))

    # Materialise state keys per profile. We avoid ``enable_profile`` here
    # (see module docstring) and call ``_materialise_state_keys`` directly,
    # collecting the union of newly added keys for the response envelope.
    materialised: list[str] = []
    for profile_id in answers.profiles:
        profile = load_profile(profile_id)
        required = list(profile.state_extensions.fields_required)
        if required:
            added = _materialise_state_keys(state_path, required)
            materialised.extend(added)

    # Render AGENTS.md (composed body) + manifest + CLAUDE.md shim.
    composed = compose([load_profile(p) for p in answers.profiles])
    agents_md_path = (target_dir / "AGENTS.md").resolve()
    manifest_path = (ea_dir / "indexes" / "generated.json").resolve()
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    seed_manifest = Manifest(version=1, generated={})
    _, updated_manifest = render_agents_md(
        composed,
        agents_md_path,
        seed_manifest,
        generator="eawf-init",
    )
    save_manifest_atomic(manifest_path, updated_manifest)
    claude_md_path = (target_dir / "CLAUDE.md").resolve()
    render_claude_md(claude_md_path)

    logger.info(
        f"run_wizard_no_input: project_code={answers.project_code} "
        f"profiles={list(answers.profiles)} state={state_path} "
        f"config={config_path} agents_md={agents_md_path} "
        f"materialised={materialised}"
    )

    return WizardResult(
        state_path=state_path,
        config_path=config_path,
        agents_md_path=agents_md_path,
        claude_md_path=claude_md_path,
        manifest_path=manifest_path,
        profiles_enabled=list(answers.profiles),
        project_code=answers.project_code,
        materialised_state_keys=materialised,
    )


# ---- questionary interactive surface ----------------------------------------


class WizardCancelled(UserDeclined):
    """Operator aborted a wizard prompt (Ctrl-C / EOF / Esc).

    Subclass of :class:`~eawf.cli.errors.UserDeclined` so the CLI handler's
    existing ``except cli_errors.CliError`` clause catches it and exits with
    the canonical ``USER_DECLINED`` code.
    """


# Pinned questionary style — cyan question marks / pointers, green answer
# text, dim grey instruction. Matches the demo at ``/tmp/eawf-questionary-demo.py``
# (do NOT commit the demo). The colours are 256-colour hex codes so a TTY
# without truecolour still renders sensibly.
_MODERN_STYLE: tuple[tuple[str, str], ...] = (
    ("qmark", "fg:#5fafff bold"),
    ("question", "bold"),
    ("answer", "fg:#87ff87 bold"),
    ("pointer", "fg:#5fafff bold"),
    ("highlighted", "fg:#5fafff bold"),
    ("selected", "fg:#87ff87"),
    ("instruction", "fg:#666666 italic"),
)
_CHECK_GLYPH: str = "[bold green]✔[/]"


def _build_style() -> Any:
    """Lazily import ``questionary.Style`` to keep ``--no-input`` lean.

    The pure pipeline must not pay the prompt_toolkit / questionary import
    tax — the import is therefore deferred to the moment the interactive
    surface actually needs a style object.
    """
    from questionary import Style

    return Style(list(_MODERN_STYLE))


def _ensure_answer(value: Any, step: WizardStep) -> Any:
    """Return *value* or raise :class:`WizardCancelled` when it is ``None``.

    questionary returns ``None`` from ``.ask()`` whenever the operator hits
    Ctrl-C / Esc / EOF. The public CLI surface treats that as a
    user-declined cancellation rather than a validation failure — exit
    code ``USER_DECLINED`` is reserved for exactly this case.
    """
    if value is None:
        raise WizardCancelled(f"wizard cancelled at step {step.id!r}")
    return value


def _ask_step(step: WizardStep) -> Any:
    """Dispatch *step* to the matching questionary prompt and return the typed value.

    Handles all five :class:`~eawf.install.steps.WizardKind` variants:

    - ``text`` / ``path`` — :func:`questionary.text` /
      :func:`questionary.path` with the step's default pre-filled.
    - ``bool`` — :func:`questionary.confirm` with the boolean default.
    - ``choice`` — :func:`questionary.select` with the static choices and
      typeahead filter (``use_search_filter=True`` mandates
      ``use_jk_keys=False`` and ``use_emacs_keys=False`` per the questionary
      contract — using ``j``/``k`` as filter keys collides with vim-style
      navigation).
    - ``multichoice`` — :func:`questionary.checkbox` for the ``profiles``
      step (choices are sourced from
      :func:`~eawf.profiles.loader.list_profiles` at runtime), or a
      free-form comma-separated text fallback for ``plugins`` / ``mcp``
      where the v0.1 contract has no static enumeration.
    """
    import questionary
    from questionary import Choice

    style = _build_style()

    if step.kind == "text":
        return _ensure_answer(
            questionary.text(step.prompt, default=str(step.default), style=style).ask(),
            step,
        )
    if step.kind == "bool":
        return _ensure_answer(
            questionary.confirm(step.prompt, default=bool(step.default), style=style).ask(),
            step,
        )
    if step.kind == "path":
        return _ensure_answer(
            questionary.path(step.prompt, default=str(step.default), style=style).ask(),
            step,
        )
    if step.kind == "choice":
        choices = list(step.choices or [])
        return _ensure_answer(
            questionary.select(
                step.prompt,
                choices=choices,
                default=str(step.default),
                style=style,
                use_search_filter=True,
                use_jk_keys=False,
                use_emacs_keys=False,
            ).ask(),
            step,
        )
    if step.kind == "multichoice":
        if step.id == "profiles":
            available = list(list_profiles())
            preselected = set(step.default or ())
            opts = [Choice(name, checked=(name in preselected)) for name in available]
            value = _ensure_answer(
                questionary.checkbox(
                    step.prompt,
                    choices=opts,
                    style=style,
                    use_search_filter=True,
                    use_jk_keys=False,
                    use_emacs_keys=False,
                ).ask(),
                step,
            )
            return tuple(value)
        # ``plugins`` / ``mcp`` — no static enumeration in the v0.1 contract.
        # Fall back to a comma-separated free-form text input so the operator
        # can list ad-hoc identifiers without us inventing a static menu.
        default = ",".join(step.default or ())
        raw = _ensure_answer(
            questionary.text(
                f"{step.prompt} (comma-separated, blank for none)",
                default=default,
                style=style,
            ).ask(),
            step,
        )
        return tuple(item.strip() for item in str(raw).split(",") if item.strip())
    raise InvalidInput(f"unknown wizard step kind: {step.kind}")


def run_wizard_interactive(target_dir: Path, *, force: bool = False) -> WizardResult:
    """Drive the questionary TTY wizard, then materialise via :func:`run_wizard_no_input`.

    Walks :data:`~eawf.install.steps.WIZARD_STEPS` sequentially. Each step
    is dispatched to :func:`_ask_step`, the typed answer is collected into
    a dict keyed by ``step.id``, and a one-line summary is printed via the
    shared :class:`rich.console.Console` (matching the demo's collapse
    style at ``/tmp/eawf-questionary-demo.py``).

    Heavy imports (``questionary``, ``rich``) are deferred to the function
    body so pure ``--no-input`` callers never pay the prompt_toolkit /
    rich import tax.

    Args:
        target_dir: Absolute target directory. Forwarded verbatim to
            :func:`run_wizard_no_input` after the operator finishes.
        force: When True, overwrite an existing ``.ea/`` even if it has
            canonical files; forwarded to :func:`run_wizard_no_input`.

    Returns:
        :class:`WizardResult` from the underlying pipeline.

    Raises:
        WizardCancelled: When the operator aborts a prompt (Ctrl-C / EOF /
            Esc). Maps to ``USER_DECLINED`` exit code.
        InvalidInput: When the collected answers fail Pydantic validation.
            The interactive surface keeps the same exception taxonomy as
            ``--no-input`` so the CLI handler maps to ``INVALID_INPUT``
            uniformly.
    """
    from rich.console import Console

    console = Console()
    answers: dict[str, Any] = {}
    for step in WIZARD_STEPS:
        value = _ask_step(step)
        answers[step.id] = value
        # Collapse-to-summary line so the operator sees what the wizard
        # captured at a glance after the prompt scrolls past.
        console.print(f"  {_CHECK_GLYPH} [dim]{step.prompt}[/] [bold]{value}[/]")

    validated = WizardAnswers(**answers)
    return run_wizard_no_input(validated, target_dir, force=force)


__all__ = [
    "WizardAnswers",
    "WizardCancelled",
    "WizardResult",
    "run_wizard_interactive",
    "run_wizard_no_input",
]
