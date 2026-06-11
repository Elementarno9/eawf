"""``InitWizardModal`` — TUI bootstrap chooser for ``/init``.

The modal is the Textual entry point for onboarding: it does not run
mutations directly. It renders a small set of concrete bootstrap actions
and returns the selected command plan to the host app, which can decide
whether to execute, toast, or hand the plan to a future daemon worker.

The available paths mirror existing CLI surfaces:

* ``quick-init`` — ``eawf init --quick --target <dir>`` for a fresh repo.
* ``register-repo`` — ``eawf repo add <dir> --set-active --yes`` for an
  already-initialised repo that needs the user registry entry.
* ``workspace-link`` — ``eawf repo link-workspace ...`` when the app can
  resolve both a workspace state and a repo path/code.
"""

from __future__ import annotations

import logging
import shlex
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Vertical, VerticalScroll
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.widgets import Static

from eawf.surfaces.tui.widgets.sigils import chrome

if TYPE_CHECKING:
    from textual.app import App

    from eawf.kernel.state.models import State, WorkspaceRepoRef

logger = logging.getLogger(__name__)

INIT_ACTION_QUICK = "quick-init"
INIT_ACTION_REGISTER = "register-repo"
INIT_ACTION_WORKSPACE_LINK = "workspace-link"

#: Render-mode label threaded into the sigil helper when the host App
#: exposes no ``render_mode`` (a bare standalone harness): the unicode
#: column is the default surface, ``"ascii"`` only when the App resolves it.
_DEFAULT_RENDER_MODE: str = "unicode"

#: The key-hint footer vocab, mirroring the other reskin overlays: a
#: middle-dot-separated chord list wrapped in brackets so the affordances
#: read in one calm line under the option list.
_KEY_HINT: str = "[ ↑/↓ select · Enter choose · Esc close ]"


@dataclass(frozen=True)
class InitWizardOption:
    """One selectable bootstrap action in the init wizard.

    Attributes:
        action: Stable action id returned to the host.
        label: Short row label shown in the modal.
        description: One-line explanation below the label.
        command: Concrete CLI command plan for the action.
    """

    action: str
    label: str
    description: str
    command: tuple[str, ...]


@dataclass(frozen=True)
class InitWizardResult:
    """Chosen init-wizard action returned through ``ModalScreen.dismiss``."""

    action: str
    command: tuple[str, ...]


@dataclass(frozen=True)
class InitWizardContext:
    """Resolved app context used to build init-wizard actions.

    Attributes:
        scope: App scope name (``repo`` / ``workspace`` / ``user``).
        target_dir: Directory targeted by quick init and registry add.
        state_path: App state path when one exists.
        workspace_code: Workspace code when the app is showing a workspace state.
        workspace_state_path: Workspace ``state.json`` path for link commands.
        repo_code: Repo code for a workspace-link command.
        repo_path: Repo path for registry add or workspace-link commands.
        init_needed: User-scope synthetic flag saying onboarding should be shown.
    """

    scope: str
    target_dir: Path
    state_path: Path | None = None
    workspace_code: str | None = None
    workspace_state_path: Path | None = None
    repo_code: str | None = None
    repo_path: Path | None = None
    init_needed: bool = False


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


def _repo_root_from_state_path(state_path: Path | None) -> Path | None:
    """Resolve ``<repo>`` from ``<repo>/.ea/state.json`` when possible."""
    if state_path is None:
        return None
    path = Path(state_path)
    if path.parent.name == ".ea":
        return path.parent.parent
    return path.parent


def _repo_ref_from_workspace(
    state: State | None,
    active_repo_path: Path | None,
) -> WorkspaceRepoRef | None:
    """Return the selected/current workspace repo ref, if any."""
    if state is None or state.workspace is None:
        return None
    if active_repo_path is not None:
        active = str(active_repo_path)
        for ref in state.workspace.repos.values():
            if ref.path == active:
                return ref
    current = state.workspace.current_repo_code
    if current is not None:
        return state.workspace.repos.get(current)
    return None


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
        ref = _repo_ref_from_workspace(state, active_repo_path)
        if ref is not None:
            repo_code = ref.code
            repo_path = Path(ref.path)
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
    )


def build_init_wizard_options(context: InitWizardContext) -> tuple[InitWizardOption, ...]:
    """Build the concrete action list for *context*."""
    options = [
        InitWizardOption(
            action=INIT_ACTION_QUICK,
            label="Quick init",
            description="Create .ea state, config, managed docs, and plugin preview here.",
            command=quick_init_command(context.target_dir),
        ),
        InitWizardOption(
            action=INIT_ACTION_REGISTER,
            label="Register repo",
            description="Add an already-initialised repo to the user registry.",
            command=register_repo_command(context.repo_path or context.target_dir),
        ),
    ]
    if (
        context.workspace_code is not None
        and context.repo_code is not None
        and context.workspace_state_path is not None
        and context.repo_path is not None
    ):
        options.append(
            InitWizardOption(
                action=INIT_ACTION_WORKSPACE_LINK,
                label="Link workspace",
                description="Cross-link this repo state with the current workspace state.",
                command=workspace_link_command(
                    workspace_code=context.workspace_code,
                    repo_code=context.repo_code,
                    workspace_state_path=context.workspace_state_path,
                    repo_path=context.repo_path,
                ),
            )
        )
    return tuple(options)


class InitWizardModal(ModalScreen[InitWizardResult | None]):
    """Bootstrap action chooser (``Enter`` returns a command plan).

    ``↑`` / ``↓`` move across available bootstrap paths, ``Enter`` returns
    the selected :class:`InitWizardResult`, and ``Esc`` dismisses with
    ``None``.
    """

    #: One wizard at a time -- a re-fired open over an already-open wizard is
    #: a no-op (deduped by :meth:`~eawf.surfaces.tui.app.EaApp.push_modal`, in
    #: addition to the App-level ``_init_wizard_open`` guard) rather than
    #: stacking a duplicate.
    dedupe_singleton: ClassVar[bool] = True

    DEFAULT_CSS: ClassVar[str] = """
    InitWizardModal {
        align: center middle;
    }
    InitWizardModal > #init-wizard-box {
        width: 76%;
        max-width: 108;
        height: auto;
        max-height: 84%;
        border: round $accent;
        background: $surface;
        padding: 1 2;
    }
    InitWizardModal .init-title {
        text-style: bold;
        color: $accent;
        height: 1;
    }
    InitWizardModal .init-subtitle {
        color: $text-muted;
        height: auto;
        margin-bottom: 1;
    }
    InitWizardModal .init-option {
        height: auto;
        color: $text-muted;
        padding: 0 1;
    }
    InitWizardModal .init-option.-selected {
        color: $accent;
        text-style: bold reverse;
    }
    InitWizardModal .init-description {
        height: auto;
        color: $text-muted;
        padding: 0 3;
    }
    InitWizardModal .init-command {
        height: auto;
        color: $text;
        padding: 0 3;
    }
    InitWizardModal .init-hint {
        color: $text-muted;
        height: 1;
        margin-top: 1;
    }
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("up", "move(-1)", "up", show=False),
        Binding("down", "move(1)", "down", show=False),
        Binding("k", "move(-1)", "up", show=False),
        Binding("j", "move(1)", "down", show=False),
        Binding("enter", "confirm", "confirm", show=False),
        Binding("escape", "cancel", "cancel", show=False),
    ]

    selected: reactive[int] = reactive(0)

    def __init__(self, context: InitWizardContext) -> None:
        """Construct the modal for *context*."""
        super().__init__()
        self._wizard_context = context
        self._options = build_init_wizard_options(context)

    def compose(self) -> ComposeResult:
        """Yield the sigil-marked title, options, previews, and key hint.

        The title leads with the shared ``dispatch`` chrome sigil (the heavy
        right-angle quote, or the ASCII ``>`` fallback) so the bootstrap
        chooser reads as a launch action, resolved through the single
        :mod:`~eawf.surfaces.tui.widgets.sigils` SHAPE home. The footer
        renders the key-hint chord vocab shared across the reskin overlays.
        """
        mode = self._render_mode()
        sigil = chrome("dispatch", mode=mode)
        with Vertical(id="init-wizard-box"):
            yield Static(f"{sigil} Initialize EAWF", classes="init-title")
            yield Static(self._subtitle(), classes="init-subtitle")
            with VerticalScroll():
                for index, option in enumerate(self._options):
                    yield Static(option.label, classes="init-option", id=f"init-option-{index}")
                    yield Static(option.description, classes="init-description")
                    yield Static(format_command(option.command), classes="init-command")
            yield Static(_KEY_HINT, classes="init-hint")

    def _subtitle(self) -> str:
        """Return the context line under the title."""
        if self._wizard_context.init_needed:
            return "No registered repo found. Pick a bootstrap path to continue."
        return f"Scope: {self._wizard_context.scope}. Pick a bootstrap path."

    def on_mount(self) -> None:
        """Paint the initial option highlight, then watch for a render flip.

        Wires a ``render_mode`` watcher so a unicode <-> ASCII flip repaints
        the title's dispatch sigil in the active glyph column.
        """
        if hasattr(self.app, "render_mode"):
            self.watch(self.app, "render_mode", self._on_render_mode)
        self._repaint_options()

    def _on_render_mode(self, _mode: object) -> None:
        """Repaint the title sigil when the App's render mode flips."""
        mode = self._render_mode()
        sigil = chrome("dispatch", mode=mode)
        self.query_one(".init-title", Static).update(f"{sigil} Initialize EAWF")

    def _render_mode(self) -> str:
        """Resolve the active render-mode label from the host app.

        Threads :attr:`~eawf.surfaces.tui.app.EaApp.render_mode` into the
        sigil helper so an ``ascii`` flip swaps the dispatch glyph to its
        ASCII column; falls back to the unicode column under a bare test
        harness whose host App carries no ``render_mode`` attribute.

        Returns:
            The render-mode label (``"ascii"`` or a unicode label).
        """
        return getattr(self.app, "render_mode", _DEFAULT_RENDER_MODE)

    def watch_selected(self) -> None:
        """Repaint the option highlight when the selection moves."""
        if self.is_mounted:
            self._repaint_options()

    def _repaint_options(self) -> None:
        """Toggle the ``-selected`` class onto the active option."""
        for index in range(len(self._options)):
            cell = self.query_one(f"#init-option-{index}", Static)
            cell.set_class(index == self.selected, "-selected")

    def action_move(self, delta: int) -> None:
        """Move the highlighted option by *delta*, wrapping at ends."""
        count = len(self._options)
        if count == 0:
            return
        self.selected = (self.selected + delta) % count

    def action_confirm(self) -> None:
        """Dismiss with the selected command plan."""
        option = self._options[self.selected]
        logger.info(f"init_wizard action={option.action!r}")
        self.dismiss(InitWizardResult(action=option.action, command=option.command))

    def action_cancel(self) -> None:
        """Dismiss without choosing a path."""
        logger.info("init_wizard action='cancel'")
        self.dismiss(None)


def open_init_wizard(
    app: App[None],
    *,
    callback: Callable[[InitWizardResult | None], None] | None = None,
) -> bool:
    """Push the init wizard onto *app*'s screen stack.

    Routes through ``push_modal`` when present so the global modal-depth cap
    applies. A callback can be provided by the host to receive
    :class:`InitWizardResult`.
    """
    modal = InitWizardModal(build_init_wizard_context(app))
    push_modal = getattr(app, "push_modal", None)
    if callable(push_modal):
        if callback is not None:
            return bool(push_modal(modal, callback=callback))
        return bool(push_modal(modal))
    app.push_screen(modal)
    return True


__all__ = [
    "INIT_ACTION_QUICK",
    "INIT_ACTION_REGISTER",
    "INIT_ACTION_WORKSPACE_LINK",
    "InitWizardContext",
    "InitWizardModal",
    "InitWizardOption",
    "InitWizardResult",
    "build_init_wizard_context",
    "build_init_wizard_options",
    "format_command",
    "open_init_wizard",
    "quick_init_command",
    "register_repo_command",
    "workspace_link_command",
]
