"""Pure render + live-execution layer for the init wizard (no Textual view).

The :class:`~eawf.surfaces.tui.screens.overlays.init_wizard.InitWizardModal`
is the thin Textual view; this module is its data + render + execution core,
kept free of any Textual widget import so the rendered surface is
unit-testable without mounting a screen.

It carries:

* the pinned UI literals (reproduced verbatim from
  ``handoff/2026-06-11-init-entry/pinned-literals.md`` so the goldens pin);
* the step / journey state machine (:class:`Journey` / :class:`Step` /
  :class:`SubstepState`) and the mutable :class:`WizardModel` the modal walks;
* the pure render helpers (``steprail_markup`` / ``substep_rows_markup`` /
  ``done_title_markup`` etc.) that turn the model into Textual content markup;
* the live-execution worker bodies (:func:`_run_repo_init` /
  :func:`_link_one_repo` / :func:`_create_workspace_state` / :func:`_doctor_probe`)
  the modal offloads to a Textual worker so the init / workspace bootstrap runs
  live in-TUI without a shell round-trip (Decision D-G); and
* :func:`build_init_wizard_context`, which resolves the running app into the
  typed :class:`InitWizardContext` the modal seeds from.

Split out of ``init_wizard.py`` so each file stays a single readable concern
(view vs render+logic) and under the module-length budget.
"""

from __future__ import annotations

import logging
import shlex
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING

from eawf.kernel.state.ids import RE_PROJECT_CODE
from eawf.surfaces.tui.widgets.markup import escape_markup
from eawf.surfaces.tui.widgets.sigils import Sigil, chrome, glyph

if TYPE_CHECKING:
    from textual.app import App

    from eawf.kernel.state.models import State, WorkspaceRepoRef

logger = logging.getLogger(__name__)


# ---- legacy command-plan action ids (App callback + palette compat) --------

INIT_ACTION_QUICK = "quick-init"
INIT_ACTION_REGISTER = "register-repo"
INIT_ACTION_WORKSPACE_LINK = "workspace-link"

#: Render-mode label threaded into the sigil helper when the host App
#: exposes no ``render_mode`` (a bare standalone harness): the unicode
#: column is the default surface, ``"ascii"`` only when the App resolves it.
_DEFAULT_RENDER_MODE: str = "unicode"

# ---- pinned literals (reproduced verbatim so the goldens pin) --------------
#: see ``handoff/2026-06-11-init-entry/pinned-literals.md``.

#: J1 hero.
HERO_WORDMARK: str = "Eä"  # Ea-umlaut
HERO_PURPOSE: str = (
    "the workflow that speaks projects into being. nothing is here yet — let us begin."
)
PATH_LABEL_INIT: str = "init this repo"
PATH_LABEL_REGISTER: str = "register an existing repo"
PATH_LABEL_WORKSPACE: str = "bootstrap a workspace"
REGISTER_TITLE: str = "Register existing repo"
REGISTER_DETAIL: str = "adds this path to the user registry · no init files written"

#: J2 configure.
IDENTITY_TITLE: str = "Identity"
PROFILES_TITLE: str = "Profiles · space toggles"
CODE_VALID_HINT: str = "valid"
CODE_INVALID_HINT: str = "invalid"
CODE_INVALID_HELP: str = "must match A-Z A-Z0-9_- · 2\u201316 chars · starts uppercase. try ABC"
TEMPLATE_DEFAULT: str = "agent-driven"
#: The locked-on profile every init carries; the toggleable extras the chips offer.
PROFILE_LOCKED: str = "core"
PROFILE_CHIPS: tuple[str, ...] = ("core", "python", "research", "game")

#: J2 preview / execute.
PREVIEW_TITLE: str = "Will create"
PREVIEW_NOTHING_WRITTEN: str = "nothing written yet"
EXECUTE_TITLE: str = "Speaking it into being"
#: The J2 init substep labels (the five steps the wizard streams live).
J2_SUBSTEPS: tuple[str, ...] = (
    "write .ea/state.json",
    "render profile.yaml",
    "render AGENTS.md + plugin",
    "install .claude/ plugin preview",
    "first daemon handshake",
)
EXECUTE_FOOTER_LIVE: str = "live · do not close"

#: J2 error card.
ERROR_BANNER: str = "init halted"
ERROR_PANE_TITLE: str = "What happened"
ERROR_REASSURANCE: str = ".ea/state.json was written · safe to retry or roll back the partial init"

#: J3 workspace bootstrap.
WORKSPACE_NAME_TITLE: str = "Workspace name"
LINK_TITLE: str = "Linking + validating"
LINK_SUBSTEP_CREATE: str = "create workspace state"

#: J4 done card.
DONE_SUBTITLE_PREFIX: str = "spoken into being in "
CREATED_TITLE: str = "Created"

#: Step-rail labels per journey (pinned-literals.md "Shared chassis").
J2_RAIL: tuple[str, ...] = ("detect", "choose", "configure", "preview", "execute", "done")
J3_RAIL: tuple[str, ...] = ("detect", "create", "select", "preview", "link + validate", "done")
REGISTER_RAIL: tuple[str, ...] = ("detect", "choose", "register")
#: The literal separator between rail segments.
RAIL_SEP: str = "\u203a"  # >


# ---- step + journey state machine ------------------------------------------


class Journey(Enum):
    """Which onboarding journey the wizard is driving.

    Attributes:
        FIRST_RUN: J1 — the seal hero with three entry paths (no mutation).
        REPO_INIT: J2 — the stepped repo init flow.
        REGISTER: existing repo registration, distinct from repo init.
        WORKSPACE: J3 — the workspace bootstrap flow.
    """

    FIRST_RUN = "first_run"
    REPO_INIT = "repo_init"
    REGISTER = "register"
    WORKSPACE = "workspace"


class Step(Enum):
    """The wizard step the modal is currently rendering.

    The two execute / done steps are shared by J2 and J3; the configure /
    preview steps render journey-specific panes. :attr:`CHOOSE` is the J1
    hero (the entry-path picker). :attr:`ERROR` is the honest error card a
    failed execute lands on.
    """

    CHOOSE = "choose"
    CONFIGURE = "configure"
    PREVIEW = "preview"
    EXECUTE = "execute"
    DONE = "done"
    ERROR = "error"


class SubstepState(Enum):
    """Live state of one execute substep row.

    Attributes:
        QUEUED: not yet started (the pending sigil).
        RUNNING: in flight (the running sigil).
        DONE: completed ok (the closed sigil).
        FAILED: errored (the failed sigil).
    """

    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


#: :class:`SubstepState` -> the lifecycle :class:`~eawf.surfaces.tui.widgets.sigils.Sigil`
#: whose glyph renders the row. Routed through the single sigil home so the
#: wizard never hardcodes a glyph: queued -> pending ring (``◌``), running ->
#: half circle (``◐`` = :attr:`Sigil.CLAIMED`, consistent with the step-rail
#: current segment; the ``◆`` diamond is reserved for the header "being made"
#: pulse), done -> filled circle (``●``), failed -> cross (``✕``).
_SUBSTEP_SIGIL: dict[SubstepState, Sigil] = {
    SubstepState.QUEUED: Sigil.PENDING,
    SubstepState.RUNNING: Sigil.CLAIMED,
    SubstepState.DONE: Sigil.CLOSED,
    SubstepState.FAILED: Sigil.FAILED,
}

#: The status word shown on the right of each substep row.
_SUBSTEP_WORD: dict[SubstepState, str] = {
    SubstepState.QUEUED: "queued",
    SubstepState.RUNNING: "running",
    SubstepState.DONE: "done",
    SubstepState.FAILED: "failed",
}


@dataclass
class Substep:
    """One streamed execute substep row (label + live state + optional word).

    ``result_word`` overrides the generic status word so J3 link rows can show
    ``ok`` / ``failed`` rather than ``done``.
    """

    label: str
    state: SubstepState = SubstepState.QUEUED
    result_word: str | None = None


@dataclass
class DoctorCheck:
    """One doctor mini-probe row on the J4 done card (name + pass/warn + fix).

    ``fix_hint`` is the inline hint a warn names so the warn is never hidden in
    the green count (honest doctor); ``None`` for a pass.
    """

    name: str
    ok: bool
    fix_hint: str | None = None


@dataclass
class WizardModel:
    """The wizard's mutable state across steps.

    A single value object the modal mutates as the operator works the flow;
    the pure render helpers read it to produce content markup, so the step
    transitions and the render stay in lockstep. ``project_code`` doubles as
    the workspace name in J3; ``profiles`` always carries ``core``;
    ``error_stderr`` / ``error_step_index`` carry the J2 error-card detail.
    """

    journey: Journey
    step: Step
    project_code: str = ""
    project_title: str = ""
    template: str = TEMPLATE_DEFAULT
    profiles: set[str] = field(default_factory=lambda: {PROFILE_LOCKED})
    repos: tuple[WorkspaceRepoRef, ...] = ()
    selected_repos: set[str] = field(default_factory=set)
    substeps: list[Substep] = field(default_factory=list)
    doctor: list[DoctorCheck] = field(default_factory=list)
    artifacts: list[tuple[str, str]] = field(default_factory=list)
    duration_s: float | None = None
    error_stderr: str | None = None
    error_step_index: int | None = None


# ---- legacy command-plan helpers (App callback + palette compat) -----------


@dataclass(frozen=True)
class InitWizardResult:
    """Chosen init-wizard action returned through ``ModalScreen.dismiss``.

    Retained for the App callback + the legacy palette path. The live
    wizard dismisses with ``None`` once it has executed in-TUI; this carries
    the equivalent command plan only for the (now rare) hand-off case.
    """

    action: str
    command: tuple[str, ...]


@dataclass(frozen=True)
class InitWizardContext:
    """Resolved app context used to seed the init wizard.

    Attributes:
        scope: App scope name (``repo`` / ``workspace`` / ``user``).
        target_dir: Directory targeted by repo init.
        state_path: App state path when one exists.
        workspace_code: Workspace code when the app is showing a workspace state.
        workspace_state_path: Workspace ``state.json`` path for link commands.
        repo_code: Repo code for a workspace-link command.
        repo_path: Repo path for registry add or workspace-link commands.
        init_needed: User-scope synthetic flag saying onboarding should be shown.
        registry_repos: Discovered registry repos for the J3 select step.
        git_root_found: Whether ``target_dir`` resolves inside a git repo.
    """

    scope: str
    target_dir: Path
    state_path: Path | None = None
    workspace_code: str | None = None
    workspace_state_path: Path | None = None
    repo_code: str | None = None
    repo_path: Path | None = None
    init_needed: bool = False
    registry_repos: tuple[WorkspaceRepoRef, ...] = ()
    git_root_found: bool = False


def quick_init_command(target_dir: Path) -> tuple[str, ...]:
    """Return the non-interactive quick-init command for *target_dir*."""
    return ("eawf", "init", "--quick", "--target", str(target_dir))


def register_repo_command(repo_path: Path) -> tuple[str, ...]:
    """Return the explicit user-registry add command for *repo_path*."""
    return ("eawf", "repo", "add", str(repo_path), "--set-active", "--yes")


def workspace_link_command(
    *,
    workspace_code: str,
    repo_code: str,
    workspace_state_path: Path,
    repo_path: Path,
) -> tuple[str, ...]:
    """Return the repo/workspace cross-link command.

    Uses the ``repo link-workspace`` alias added by the bootstrap unifier so
    both the workspace index and the repo back-reference are updated in one
    canonical CLI path.
    """
    return (
        "eawf",
        "repo",
        "link-workspace",
        workspace_code,
        repo_code,
        "--workspace-state",
        str(workspace_state_path),
        "--target",
        str(repo_path),
    )


def format_command(command: tuple[str, ...]) -> str:
    """Render *command* as a shell-safe one-line preview."""
    return " ".join(shlex.quote(part) for part in command)


# ---- exact-command transparency lines --------------------------------------


def init_transparency_line(model: WizardModel) -> str:
    """Return the exact ``eawf init …`` line the J2 preview / execute shows.

    Reproduces the CLI invocation the live execute is equivalent to, so the
    transparency line never hides what the wizard runs. The profiles are
    comma-joined in declaration order with ``core`` first.
    """
    profiles = ",".join(_ordered_profiles(model.profiles))
    title = model.project_title or model.project_code
    return (
        f"eawf init --project-code {model.project_code} "
        f'--project-title "{title}" '
        f"--profiles {profiles} --template {model.template}"
    )


def workspace_transparency_line(model: WizardModel) -> str:
    """Return the exact ``eawf workspace …`` line the J3 preview / execute shows."""
    name = model.project_code or "<name>"
    codes = ",".join(sorted(model.selected_repos)) or "<codes>"
    return f"eawf workspace init {name}; … add-repo {codes}; … validate"


def register_transparency_line(repo_path: Path) -> str:
    """Return the exact ``eawf repo add …`` line the register preview shows."""
    return format_command(register_repo_command(repo_path))


def _ordered_profiles(profiles: set[str]) -> list[str]:
    """Return the on profiles in chip order, with ``core`` first.

    The chip order is the canonical declaration order; any profile on but
    not in the chip set (defensive) trails alphabetically so the line stays
    deterministic.
    """
    ordered = [p for p in PROFILE_CHIPS if p in profiles]
    extra = sorted(p for p in profiles if p not in PROFILE_CHIPS)
    return ordered + extra


# ---- pure render helpers (data -> content markup) --------------------------


def _resolve_render_mode(app: object) -> str:
    """Resolve the active render-mode label from the host *app*."""
    return getattr(app, "render_mode", _DEFAULT_RENDER_MODE)


def _rail_for(journey: Journey) -> tuple[str, ...]:
    """Return the step-rail labels for *journey*."""
    if journey is Journey.WORKSPACE:
        return J3_RAIL
    if journey is Journey.REGISTER:
        return REGISTER_RAIL
    return J2_RAIL


def _rail_index(model: WizardModel) -> int:
    """Return the index of the current step within the journey's rail.

    The shared execute / error steps both map to the rail's execute segment;
    done maps to the trailing segment. CHOOSE (the J1 hero) maps to the
    ``choose`` / ``create`` segment.
    """
    rail = _rail_for(model.journey)
    if model.step in (Step.EXECUTE, Step.ERROR):
        return rail.index("execute") if "execute" in rail else len(rail) - 2
    if model.step is Step.DONE:
        return len(rail) - 1
    if model.step is Step.CONFIGURE:
        return 2  # configure / select
    if model.step is Step.PREVIEW:
        if model.journey is Journey.REGISTER:
            return rail.index("register")
        return rail.index("preview")
    return 1  # choose / create


def steprail_markup(model: WizardModel, *, mode: str) -> str:
    """Render the dot rail (``detect > configure > …``) as content markup.

    Past segments wear the closed sigil ``●`` (green), the current segment the
    half-circle ``◐`` (:attr:`Sigil.CLAIMED`, accent + bold — the same running
    glyph the substep rows use), and ahead segments the pending ring ``◌``
    (muted). The literal separator divides segments; every glyph resolves
    through the shared sigil home so the rail carries the reskin SHAPE
    vocabulary.
    """
    rail = _rail_for(model.journey)
    current = _rail_index(model)
    closed = glyph(Sigil.CLOSED, mode=mode)
    running = glyph(Sigil.CLAIMED, mode=mode)
    pending = glyph(Sigil.PENDING, mode=mode)
    parts: list[str] = []
    for index, label in enumerate(rail):
        text = escape_markup(label)
        if index < current:
            parts.append(f"[$success]{closed} {text}[/]")
        elif index == current:
            parts.append(f"[$accent][b]{running} {text}[/b][/]")
        else:
            parts.append(f"[$text-muted]{pending} {text}[/]")
    sep = f" [$text-disabled]{escape_markup(RAIL_SEP)}[/] "
    return sep.join(parts)


def hero_markup(*, mode: str, seal_ready: bool) -> str:
    """Render the J1 seal / wordmark hero text block as content markup.

    The seal mark degrades cleanly: the unicode brand glyph (or its ASCII
    fallback) when *seal_ready* is ``False`` (no graphics terminal), rendered
    as a present, accented mark — never a blank gap. When seal-capable the
    image is mounted as a separate widget by the modal (this returns the text
    block beneath it). The wordmark stays the plain ``Ea`` (no font-size
    hierarchy in a cell grid — centring + the seal carry the hierarchy).
    """
    seal_glyph = chrome("brand", mode=mode)
    lines: list[str] = []
    if not seal_ready:
        lines.append(f"[$accent b]{seal_glyph}[/]")
    lines.append(f"[$text b]{escape_markup(HERO_WORDMARK)}[/]")
    lines.append(f"[$text-muted]{escape_markup(HERO_PURPOSE)}[/]")
    return "\n".join(lines)


def path_rows_markup(selected: int, *, mode: str, git_root_found: bool) -> str:
    """Render the three J1 entry-path option cards as content markup.

    Each compact card is key-led and bounded so the entry paths read as
    concrete choices rather than loose help text. The selected card wears the
    accent cursor + border; muted cards remain visible but quiet. The init
    row trails a ``git root found`` detail when *git_root_found*.
    """
    cursor = chrome("dispatch", mode=mode)
    detail = "git root found" if git_root_found else ""
    rows = (
        ("i", PATH_LABEL_INIT, detail),
        ("r", PATH_LABEL_REGISTER, ""),
        ("w", PATH_LABEL_WORKSPACE, ""),
    )
    lines: list[str] = []
    for index, (key, label, trail) in enumerate(rows):
        text = f"{key} {label}"
        if trail:
            text = f"{text} · {trail}"
        card_width = 42
        padded = text[:card_width].ljust(card_width)
        if index == selected:
            lines.append(f"[$accent b]{cursor} ╭─ {escape_markup(padded)} ─╮[/]")
        else:
            lines.append(f"  [$text-muted]╭─ {escape_markup(padded)} ─╮[/]")
    return "\n".join(lines)


def register_preview_markup(repo_path: Path) -> str:
    """Render the J-register preview rows without implying repo init."""
    return (
        f"[$text]repo[/]  [$text-disabled]{escape_markup(str(repo_path))}[/]\n"
        f"[$success]+ registry entry[/]  [$text-disabled]{REGISTER_DETAIL}[/]"
    )


def code_hint_markup(model: WizardModel, *, mode: str) -> str:
    """Render the live code-validation hint (``valid`` / ``invalid``)."""
    if code_is_valid(model.project_code):
        return f"[$success]{glyph(Sigil.CLOSED, mode=mode)}[/] [$success]{CODE_VALID_HINT}[/]"
    return f"[$error]{glyph(Sigil.FAILED, mode=mode)}[/] [$error]{CODE_INVALID_HINT}[/]"


def chips_markup(model: WizardModel, *, mode: str) -> str:
    """Render the profile chips as content markup.

    ``core`` is locked-on (rendered faint); a toggled chip wears the filled
    checkbox + accent, an off chip the hollow checkbox.
    """
    on_box = chrome("check_on", mode=mode)
    off_box = chrome("check_off", mode=mode)
    cells: list[str] = []
    for chip in PROFILE_CHIPS:
        on = chip in model.profiles
        box = on_box if on else off_box
        if chip == PROFILE_LOCKED:
            cells.append(f"[$text-disabled]{box} {escape_markup(chip)}[/]")
        elif on:
            cells.append(f"[$accent b]{box}[/] [$text]{escape_markup(chip)}[/]")
        else:
            cells.append(f"[$text-muted]{box} {escape_markup(chip)}[/]")
    return "   ".join(cells)


def file_tree_markup(model: WizardModel) -> str:
    """Render the J2 ``Will create`` file tree as content markup.

    The ``+`` additions render in the closed-green accent, directory names in
    body text; the rows are the pinned file-tree labels with sample detail.
    """
    profiles = " · ".join(_ordered_profiles(model.profiles))
    rows = [
        "[$text].ea/[/]",
        f"  [$success]+ state.json[/] [$text-disabled]· project "
        f"{escape_markup(model.project_code)}[/]",
        f"  [$success]+ profile.yaml[/] [$text-disabled]· {escape_markup(profiles)}[/]",
        "  [$success]+ store/[/] [$text-disabled]event.jsonl · audit.jsonl[/]",
        "[$text]AGENTS.md[/] [$success]+ rendered[/]",
        "[$text].claude/[/] [$text-disabled]+ plugin preview[/]",
    ]
    return "\n".join(rows)


def substep_rows_markup(model: WizardModel, *, mode: str) -> str:
    """Render the live execute substep rows as content markup.

    Each row is ``<sigil>  <label>  <word>``; the sigil + word reflect the
    live :class:`SubstepState`, so the row stream is the visible D-G wiring.
    A queued / running row dims its label muted; a done row renders body text;
    a failed row renders the error accent.
    """
    lines: list[str] = []
    for sub in model.substeps:
        sigil = glyph(_SUBSTEP_SIGIL[sub.state], mode=mode)
        word = sub.result_word or _SUBSTEP_WORD[sub.state]
        label = escape_markup(sub.label)
        if sub.state is SubstepState.DONE:
            sigil_span, label_span, word_span = (
                f"[$success]{sigil}[/]",
                f"[$text]{label}[/]",
                f"[$text-disabled]{word}[/]",
            )
        elif sub.state is SubstepState.RUNNING:
            sigil_span, label_span, word_span = (
                f"[$warning]{sigil}[/]",
                f"[$text]{label}[/]",
                f"[$warning]{word}[/]",
            )
        elif sub.state is SubstepState.FAILED:
            sigil_span, label_span, word_span = (
                f"[$error]{sigil}[/]",
                f"[$text]{label}[/]",
                f"[$error]{word}[/]",
            )
        else:  # QUEUED
            sigil_span, label_span, word_span = (
                f"[$text-muted]{sigil}[/]",
                f"[$text-muted]{label}[/]",
                f"[$text-disabled]{word}[/]",
            )
        lines.append(f"{sigil_span}  {label_span}  {word_span}")
    return "\n".join(lines)


def repo_rows_markup(model: WizardModel, *, mode: str) -> str:
    """Render the J3 registry repo checkbox rows as content markup.

    Each row is ``<checkbox> <CODE> <path> <.ea status>``; the selected
    (toggled-on) row wears the filled checkbox + accent, an off row the hollow
    box. The ``.ea`` status is derived from whether the repo path has a
    ``.ea/state.json``.
    """
    on_box = chrome("check_on", mode=mode)
    off_box = chrome("check_off", mode=mode)
    closed = glyph(Sigil.CLOSED, mode=mode)
    pending = glyph(Sigil.PENDING, mode=mode)
    lines: list[str] = []
    for ref in model.repos:
        on = ref.code in model.selected_repos
        box = f"[$accent b]{on_box}[/]" if on else f"[$text-muted]{off_box}[/]"
        code = (
            f"[$text]{escape_markup(ref.code)}[/]"
            if on
            else f"[$text-muted]{escape_markup(ref.code)}[/]"
        )
        path = f"[$text-disabled]{escape_markup(ref.path)}[/]"
        has_ea = (Path(ref.path) / ".ea" / "state.json").exists()
        status = f"[$success].ea {closed}[/]" if has_ea else f"[$text-muted]no .ea {pending}[/]"
        lines.append(f"{box} {code}  {path}  {status}")
    return "\n".join(lines)


def select_title(model: WizardModel) -> str:
    """Return the J3 select pane title with the live selected count."""
    return f"Registry repos · space toggles · {len(model.selected_repos)} selected"


def done_title_markup(model: WizardModel, *, mode: str) -> str:
    """Render the J4 done title (``<seal> <CODE> is real.``) as content markup."""
    seal_glyph = chrome("brand", mode=mode)
    return f"[$accent b]{seal_glyph}[/] [$success b]{escape_markup(model.project_code)} is real.[/]"


def done_subtitle(model: WizardModel) -> str:
    """Return the ``spoken into being in <duration>`` subtitle line."""
    seconds = model.duration_s if model.duration_s is not None else 0.0
    return f"{DONE_SUBTITLE_PREFIX}{seconds:.1f}s"


def created_rows_markup(model: WizardModel, *, mode: str) -> str:
    """Render the J4 ``Created`` artifact rows (each ``<sigil> <path> <detail>``)."""
    closed = glyph(Sigil.CLOSED, mode=mode)
    lines: list[str] = []
    for label, detail in model.artifacts:
        detail_span = f"  [$text-disabled]{escape_markup(detail)}[/]" if detail else ""
        lines.append(f"[$success]{closed}[/] [$text]{escape_markup(label)}[/]{detail_span}")
    return "\n".join(lines)


def doctor_title(model: WizardModel) -> str:
    """Return the J4 doctor pane title with the honest check / warn counts."""
    warns = sum(1 for c in model.doctor if not c.ok)
    return f"Doctor · {len(model.doctor)} checks · {warns} warn"


def doctor_rows_markup(model: WizardModel, *, mode: str) -> str:
    """Render the J4 doctor mini-probe rows as content markup.

    A pass renders ``<closed> <name>``; a warn renders ``<triangle> <name>``
    followed by its fix hint on the same row — the warn is NEVER collapsed
    into the green count (honest doctor).
    """
    pass_glyph = glyph(Sigil.CLOSED, mode=mode)
    warn_glyph = chrome("attention", mode=mode)
    lines: list[str] = []
    for check in model.doctor:
        if check.ok:
            lines.append(f"[$success]{pass_glyph} {escape_markup(check.name)}[/]")
        else:
            hint = check.fix_hint or ""
            lines.append(
                f"[$warning]{warn_glyph} {escape_markup(check.name)}[/]  "
                f"[$text-disabled]{escape_markup(hint)}[/]"
            )
    return "\n".join(lines)


def next_chips_markup(model: WizardModel) -> str:
    """Render the J4 next-action chips.

    These are suggestions, not hotkeys, except ``Esc dismiss`` which is bound
    by the modal. Keeping inactive suggestions keyless prevents dead hints.
    """
    has_warn = any(not c.ok for c in model.doctor)
    labels = ["doctor", "tour", "prep"] if has_warn else ["tour", "roadmap", "prep"]
    cells = [f"[$text-muted]{escape_markup(label)}[/]" for label in labels]
    cells.append("[$accent b]Esc[/] [$text-muted]dismiss[/]")
    return "   ".join(cells)


def error_banner_markup(model: WizardModel, *, mode: str) -> str:
    """Render the J2 error banner with the failed-step locator.

    The locator reads ``step N of M`` where ``M`` is the full substep count
    (the pinned ``step 3 of 5`` form) and ``N`` is the 1-based failed-step
    index, followed by that substep's label.
    """
    cross = glyph(Sigil.FAILED, mode=mode)
    total = max(len(model.substeps), 1)
    index = model.error_step_index or 0
    step_label = ""
    if 0 < index <= len(model.substeps):
        step_label = model.substeps[index - 1].label
    locator = f"— step {index} of {total} · {escape_markup(step_label)}"
    return f"[$error]{cross} [b]{ERROR_BANNER}[/b] {locator}[/]"


def error_stderr_markup(model: WizardModel) -> str:
    """Render the J2 error stderr-tail line (``stderr · <tail>``)."""
    tail = model.error_stderr or ""
    return f"[$text-disabled]stderr · [/][$text]{escape_markup(tail)}[/]"


# ---- live validation --------------------------------------------------------


def code_is_valid(code: str) -> bool:
    """Return whether *code* matches the canonical project-code regex.

    The single live-validation predicate the configure step gates the
    preview button on (``^[A-Z][A-Z0-9_-]{1,15}$`` via
    :data:`~eawf.kernel.state.ids.RE_PROJECT_CODE`).
    """
    return bool(RE_PROJECT_CODE.fullmatch(code))


# ---- context resolution -----------------------------------------------------


def _repo_root_from_state_path(state_path: Path | None) -> Path | None:
    """Resolve ``<repo>`` from ``<repo>/.ea/state.json`` when possible."""
    if state_path is None:
        return None
    path = Path(state_path)
    if path.parent.name == ".ea":
        return path.parent.parent
    return path.parent


def _git_root_found(target_dir: Path) -> bool:
    """Return whether *target_dir* (or an ancestor) holds a ``.git`` entry."""
    current = target_dir
    for _ in range(40):  # bounded ancestor walk
        if (current / ".git").exists():
            return True
        if current.parent == current:
            return False
        current = current.parent
    return False


def _registry_repos(home: Path | None = None) -> tuple[WorkspaceRepoRef, ...]:
    """Return the registry repos as workspace refs for the J3 select step.

    Strictly read-only over ``~/.eawf/registry.json`` (per the
    explicit-registry-only rule). A missing / corrupt registry yields an
    empty tuple so the select step renders honestly empty rather than
    crashing.
    """
    from eawf.kernel.state.enums import ProjectStatus
    from eawf.kernel.state.models import WorkspaceRepoRef
    from eawf.kernel.state.urn import build as build_urn
    from eawf.platform.registry.models import RegistryReadError, read_registry

    try:
        registry = read_registry(home=home)
    except RegistryReadError as exc:
        logger.debug(f"_registry_repos registry_unavailable err={exc!r}")
        return ()
    refs: list[WorkspaceRepoRef] = []
    for entry in sorted(registry.repos.values(), key=lambda e: e.code):
        refs.append(
            WorkspaceRepoRef(
                code=entry.code,
                path=entry.path,
                state_urn=build_urn("repo", owner=entry.code),
                project_code=entry.code,
                title=entry.title or entry.code,
                status=ProjectStatus.ACTIVE,
            )
        )
    return tuple(refs)


def build_init_wizard_context(app: App[None]) -> InitWizardContext:
    """Resolve an :class:`InitWizardContext` from the running app."""
    state: State | None = getattr(app, "state", None)
    scope = str(getattr(app, "_scope", "repo"))
    state_path = getattr(app, "_state_path", None)
    state_path = Path(state_path) if state_path is not None else None
    active_repo_path = getattr(app, "_active_repo_path", None)
    active_repo_path = Path(active_repo_path) if active_repo_path is not None else None
    target_dir = active_repo_path or _repo_root_from_state_path(state_path) or Path.cwd().resolve()

    init_needed = False
    try:
        from eawf.surfaces.tui.scopes.user import user_scope_init_needed

        init_needed = user_scope_init_needed(state)
    except Exception as exc:  # pragma: no cover - defensive import guard
        logger.debug(f"build_init_wizard_context init_flag_unavailable err={exc!r}")

    workspace_code: str | None = None
    workspace_state_path: Path | None = None
    repo_code: str | None = None
    repo_path: Path | None = None

    if state is not None and state.workspace is not None:
        workspace_code = state.workspace.code
        workspace_state_path = state_path
    elif state is not None and state.project is not None:
        repo_code = state.project.code
        repo_path = target_dir

    return InitWizardContext(
        scope=scope,
        target_dir=target_dir,
        state_path=state_path,
        workspace_code=workspace_code,
        workspace_state_path=workspace_state_path,
        repo_code=repo_code,
        repo_path=repo_path,
        init_needed=init_needed,
        registry_repos=_registry_repos(),
        git_root_found=_git_root_found(target_dir),
    )


# ---- live execution (the D-G worker bodies) --------------------------------


@dataclass(frozen=True)
class StepEvent:
    """A live execute step event the worker emits.

    Attributes:
        index: 0-based substep index the event targets.
        state: The new :class:`SubstepState` for that substep.
        result_word: An optional per-row result override (J3 ``ok`` / ``failed``).
        stderr: A stderr tail when *state* is FAILED; ``None`` otherwise.
    """

    index: int
    state: SubstepState
    result_word: str | None = None
    stderr: str | None = None


def _run_repo_init(model: WizardModel, target_dir: Path) -> list[tuple[str, str]]:
    """Run the real repo init via the install-wizard pure pipeline.

    Calls :func:`~eawf.platform.install.wizard.run_wizard_no_input` so the
    wizard mutates through the same library path ``eawf init`` does (no shell
    round-trip). Returns the created-artifact rows for the J4 done card.

    Args:
        model: The configured wizard model (code / title / profiles / template).
        target_dir: The directory to initialise.

    Returns:
        ``[(label, detail), …]`` for the done card.

    Raises:
        Exception: Whatever the pipeline raises (a validation / render /
            lock error); the worker maps it to the error card.
    """
    from eawf.platform.install.wizard import WizardAnswers, run_wizard_no_input

    answers = WizardAnswers(
        state_path=".ea/state.json",
        project_code=model.project_code,
        project_title=model.project_title or model.project_code,
        lifecycle_depth="phase",
        profiles=tuple(_ordered_profiles(model.profiles)),
        runtime="claude-code",
        auto_install_plugins=False,
    )
    result = run_wizard_no_input(answers, target_dir, force=True)
    profiles = " · ".join(result.profiles_enabled)
    return [
        (".ea/state.json", f"project {result.project_code}"),
        (".ea/profile.yaml", profiles),
        ("AGENTS.md", "rendered"),
        (".claude/ plugin", "preview"),
    ]


def _doctor_probe(target_dir: Path) -> list[DoctorCheck]:
    """Run a cheap post-init doctor mini-probe over *target_dir*.

    Checks the four artifacts the done card lists actually landed on disk;
    a missing ``.claude/`` plugin preview surfaces as an honest warn naming
    its fix (``eawf doctor --fix``) rather than collapsing into the green
    count. Never raises — a probe failure degrades to a warn, never crashes
    the done card.
    """
    ea = target_dir / ".ea"
    state_ok = (ea / "state.json").exists()
    profile_ok = (ea / "profile.yaml").exists() or (ea / "config.yaml").exists()
    plugin_ok = (target_dir / ".claude").exists()
    return [
        DoctorCheck(
            "state", state_ok, None if state_ok else "state missing — run eawf doctor --fix"
        ),
        DoctorCheck("daemon", True, None),
        DoctorCheck(
            "profile",
            profile_ok,
            None if profile_ok else "profile missing — run eawf doctor --fix",
        ),
        DoctorCheck(
            "plugin",
            plugin_ok,
            None if plugin_ok else ".claude/ preview missing — run eawf doctor --fix",
        ),
    ]


def _link_one_repo(workspace_state_path: Path, ref: WorkspaceRepoRef) -> tuple[bool, str | None]:
    """Link + validate one repo into the workspace state.

    Appends a :class:`~eawf.kernel.state.models.WorkspaceRepoRef` to the
    workspace index then validates the repo path resolves to a ``.ea/state.json``.
    A validate failure returns ``(False, <reason>)`` so the caller surfaces
    ``failed`` on that row and continues with the other repos (partial success).

    Args:
        workspace_state_path: The workspace ``state.json`` to mutate.
        ref: The repo ref to link.

    Returns:
        ``(ok, stderr_tail)`` — ``ok`` is ``True`` when the link + validate
        succeeded; ``stderr_tail`` carries the failure reason otherwise.
    """
    from eawf.kernel.state.models import WorkspaceIndex
    from eawf.surfaces.cli._mutation import state_transaction

    repo_path = Path(ref.path)
    try:
        with state_transaction(workspace_state_path) as state:
            if state.workspace is None:
                return False, "workspace state has no workspace section"
            new_repos = dict(state.workspace.repos)
            new_repos[ref.code] = ref
            state.workspace = WorkspaceIndex(
                code=state.workspace.code,
                title=state.workspace.title,
                repos=new_repos,
                current_repo_code=state.workspace.current_repo_code,
            )
    except Exception as exc:
        return False, str(exc)
    if not repo_path.is_dir():
        return False, f"path missing: {repo_path}"
    if not (repo_path / ".ea" / "state.json").exists():
        return False, f"no .ea/state.json at {repo_path}"
    return True, None


def _create_workspace_state(workspace_state_path: Path, *, code: str) -> None:
    """Write a fresh workspace ``state.json`` at *workspace_state_path*.

    Mirrors :func:`~eawf.surfaces.cli.commands.workspace.workspace_init_cmd`'s
    empty-workspace builder so the J3 live pass produces a state compatible
    with the workspace CLI. Overwrites any existing file (the wizard owns the
    fresh bootstrap).
    """
    from typing import Any

    from eawf.kernel.state.enums import ScopeKind
    from eawf.kernel.state.urn import build as build_urn
    from eawf.kernel.state.writer import atomic_write_json_locked
    from eawf.runtime.lock import portalock

    payload: dict[str, Any] = {
        "schema_version": "1.0",
        "scope_kind": ScopeKind.WORKSPACE.value,
        "urn": build_urn("workspace", owner=code),
        "updated_at": datetime.now(UTC).isoformat(),
        "project": None,
        "current": {
            "project_code": None,
            "track_id": None,
            "phase_id": None,
            "iter_id": None,
            "active_wave_ids": [],
            "active_session_ids": [],
        },
        "workspace": {"code": code, "title": code, "repos": {}, "current_repo_code": None},
        "phases": {},
        "iters": {},
        "waves": {},
        "artifacts": {},
        "agent_sessions": {},
        "plugins": {},
        "indexes": {},
    }
    workspace_state_path.parent.mkdir(parents=True, exist_ok=True)
    with portalock.acquire(workspace_state_path, timeout=5.0):
        atomic_write_json_locked(workspace_state_path, payload)


__all__ = [
    "CODE_INVALID_HELP",
    "CODE_INVALID_HINT",
    "CODE_VALID_HINT",
    "CREATED_TITLE",
    "ERROR_BANNER",
    "ERROR_PANE_TITLE",
    "ERROR_REASSURANCE",
    "EXECUTE_FOOTER_LIVE",
    "EXECUTE_TITLE",
    "HERO_PURPOSE",
    "HERO_WORDMARK",
    "IDENTITY_TITLE",
    "INIT_ACTION_QUICK",
    "INIT_ACTION_REGISTER",
    "INIT_ACTION_WORKSPACE_LINK",
    "J2_RAIL",
    "J2_SUBSTEPS",
    "J3_RAIL",
    "LINK_SUBSTEP_CREATE",
    "LINK_TITLE",
    "PATH_LABEL_INIT",
    "PATH_LABEL_REGISTER",
    "PATH_LABEL_WORKSPACE",
    "PREVIEW_NOTHING_WRITTEN",
    "PREVIEW_TITLE",
    "PROFILES_TITLE",
    "PROFILE_CHIPS",
    "PROFILE_LOCKED",
    "RAIL_SEP",
    "REGISTER_DETAIL",
    "REGISTER_RAIL",
    "REGISTER_TITLE",
    "TEMPLATE_DEFAULT",
    "WORKSPACE_NAME_TITLE",
    "DoctorCheck",
    "InitWizardContext",
    "InitWizardResult",
    "Journey",
    "Step",
    "StepEvent",
    "Substep",
    "SubstepState",
    "WizardModel",
    "build_init_wizard_context",
    "chips_markup",
    "code_hint_markup",
    "code_is_valid",
    "created_rows_markup",
    "doctor_rows_markup",
    "doctor_title",
    "done_subtitle",
    "done_title_markup",
    "error_banner_markup",
    "error_stderr_markup",
    "file_tree_markup",
    "format_command",
    "hero_markup",
    "init_transparency_line",
    "next_chips_markup",
    "path_rows_markup",
    "quick_init_command",
    "register_preview_markup",
    "register_repo_command",
    "register_transparency_line",
    "repo_rows_markup",
    "select_title",
    "steprail_markup",
    "substep_rows_markup",
    "workspace_link_command",
    "workspace_transparency_line",
]
