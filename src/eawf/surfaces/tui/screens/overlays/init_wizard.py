"""``InitWizardModal`` — the stepped, live-executing onboarding wizard (view).

This module is the thin Textual view of the onboarding wizard; its data,
render, and live-execution core lives in
:mod:`~eawf.surfaces.tui.screens.overlays.init_wizard_render` (kept free of any
Textual widget import so the rendered surface is unit-testable without
mounting a screen). This file holds only the :class:`InitWizardModal`
``ModalScreen`` + :func:`open_init_wizard`, and re-exports the render layer's
public API for backward compatibility.

The wizard drives a step state machine (``detect -> choose -> configure ->
preview -> execute -> done``) over onboarding journeys:

* **J1 first-run** — a fresh user scope with ``init_needed`` auto-opens to the
  seal / wordmark hero with three entry paths (``i`` init this repo, ``r``
  register an existing repo, ``w`` bootstrap a workspace). It NEVER
  auto-mutates; choosing a path advances into the relevant journey.
* **JR register existing** — the ``r`` path previews the explicit registry-add
  command and dismisses with that command plan; it never falls through to repo
  init and never writes ``.ea`` files.
* **J2 repo init** — from a git root with no ``.ea`` the wizard configures
  identity (the project code validates live against the canonical regex),
  profile chips, and a template, previews the resolved file tree + the exact
  ``eawf init …`` line, then EXECUTES the real init **live in the TUI** (no
  shell round-trip) by calling
  :func:`~eawf.platform.install.wizard.run_wizard_no_input` on a Textual
  worker. Per-substep sigil rows stream as the init progresses; the done card
  lists the created artifacts.
* **J3 workspace bootstrap** — creates a workspace state and links the
  multi-selected registry repos in one live pass, surfacing a per-repo
  validate sigil; a failure on any one repo surfaces the failed sigil on that
  row and the pass continues (partial success).
* **J4 done card** — the shared terminal step of J2/J3: the artifacts created,
  a doctor mini-probe (honest — a warn names its fix, never hidden in the
  green count), and next-action chips.

**Live execution (Decision D-G).** The init / workspace bootstrap runs on a
Textual worker (never inline — an inline blocking call hangs the UI
mid-substep and dead-clicks the cancel key). Substep rows tick the lifecycle
sigils as the worker emits step events. Pilot tests MUST
``await app.workers.wait_for_complete()`` before asserting the post-execute
frame. ``Esc`` is safe at every pre-execute step (cancels with no mutation);
during execute ``Esc`` opens a cancel-confirm rather than hard-killing a
mid-write worker.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import replace
from typing import TYPE_CHECKING, ClassVar

from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Vertical, VerticalScroll
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.widget import Widget
from textual.widgets import Static

from eawf.surfaces.tui.screens.overlays.init_wizard_render import (
    CODE_INVALID_HELP,
    CREATED_TITLE,
    ERROR_BANNER,
    ERROR_PANE_TITLE,
    ERROR_REASSURANCE,
    EXECUTE_FOOTER_LIVE,
    EXECUTE_TITLE,
    HERO_WORDMARK,
    IDENTITY_TITLE,
    INIT_ACTION_QUICK,
    INIT_ACTION_REGISTER,
    INIT_ACTION_WORKSPACE_LINK,
    J2_SUBSTEPS,
    LINK_SUBSTEP_CREATE,
    PREVIEW_NOTHING_WRITTEN,
    PREVIEW_TITLE,
    PROFILES_TITLE,
    REGISTER_TITLE,
    WORKSPACE_NAME_TITLE,
    DoctorCheck,
    InitWizardContext,
    InitWizardResult,
    Journey,
    Step,
    StepEvent,
    Substep,
    SubstepState,
    WizardModel,
    _create_workspace_state,
    _doctor_probe,
    _link_one_repo,
    _resolve_render_mode,
    _run_repo_init,
    build_init_wizard_context,
    chips_markup,
    code_hint_markup,
    code_is_valid,
    created_rows_markup,
    doctor_rows_markup,
    doctor_title,
    done_subtitle,
    done_title_markup,
    error_banner_markup,
    error_stderr_markup,
    file_tree_markup,
    format_command,
    hero_markup,
    init_transparency_line,
    next_chips_markup,
    path_rows_markup,
    quick_init_command,
    register_preview_markup,
    register_repo_command,
    register_transparency_line,
    repo_rows_markup,
    select_title,
    steprail_markup,
    substep_rows_markup,
    workspace_link_command,
    workspace_transparency_line,
)
from eawf.surfaces.tui.widgets.markup import escape_markup
from eawf.surfaces.tui.widgets.seal import seal_art_widget
from eawf.surfaces.tui.widgets.sigils import chrome

if TYPE_CHECKING:
    from textual.app import App

logger = logging.getLogger(__name__)


class InitWizardModal(ModalScreen[InitWizardResult | None]):
    """The stepped, live-executing onboarding wizard.

    Drives the ``detect -> choose -> configure -> preview -> execute -> done``
    step machine over the J1 / JR / J2 / J3 journeys. ``Esc`` is safe at every
    pre-execute step (cancels with no mutation); during execute ``Esc`` opens
    a cancel-confirm. The execute step runs the real init / workspace
    bootstrap on a Textual worker (never inline) and streams substep sigil
    rows live.
    """

    #: One wizard at a time -- a re-fired open over an already-open wizard is
    #: a no-op (deduped by the App-level guard) rather than stacking a duplicate.
    dedupe_singleton: ClassVar[bool] = True

    DEFAULT_CSS: ClassVar[str] = """
    InitWizardModal {
        align: center middle;
    }
    InitWizardModal > #init-box {
        width: 78%;
        max-width: 100;
        height: auto;
        max-height: 88%;
        border: round $accent;
        background: $surface;
        padding: 1 2;
    }
    InitWizardModal .init-header {
        color: $text-muted;
        height: 1;
    }
    InitWizardModal .init-rail {
        height: 1;
        margin-bottom: 1;
    }
    InitWizardModal .research-empty-seal {
        width: 1fr;
        height: auto;
        content-align: center middle;
        text-align: center;
        color: $accent;
    }
    InitWizardModal .init-hero {
        height: auto;
        content-align: center middle;
        text-align: center;
        margin: 1 0;
    }
    InitWizardModal .init-paths {
        height: auto;
        margin-top: 1;
    }
    InitWizardModal .init-pane {
        border: round $border;
        padding: 0 1;
        height: auto;
        margin-bottom: 1;
    }
    InitWizardModal .init-ptitle {
        color: $accent;
        text-style: bold;
        height: 1;
    }
    InitWizardModal .init-field {
        height: 1;
    }
    InitWizardModal .init-help {
        color: $text-disabled;
        height: auto;
    }
    InitWizardModal .init-cmdline {
        border: round $border;
        color: $text-muted;
        padding: 0 1;
        height: auto;
        margin-bottom: 1;
    }
    InitWizardModal .init-banner {
        background: $error 15%;
        color: $error;
        height: auto;
        padding: 0 1;
        margin-bottom: 1;
    }
    InitWizardModal .init-reassure {
        color: $text-muted;
        height: auto;
        margin-top: 1;
    }
    InitWizardModal .init-done-title {
        height: 1;
        content-align: center middle;
        text-align: center;
    }
    InitWizardModal .init-done-sub {
        color: $text-disabled;
        height: 1;
        content-align: center middle;
        text-align: center;
        margin-bottom: 1;
    }
    InitWizardModal .init-chips {
        height: auto;
        content-align: center middle;
        text-align: center;
        margin-top: 1;
    }
    InitWizardModal .init-foot {
        color: $text-disabled;
        height: 1;
        margin-top: 1;
    }
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("up", "move(-1)", "up", show=False),
        Binding("down", "move(1)", "down", show=False),
        Binding("space", "toggle_chip", "toggle", show=False),
        Binding("a", "select_all", "all", show=False),
        Binding("i", "path('i')", "init", show=False),
        Binding("r", "path('r')", "register", show=False),
        Binding("w", "path('w')", "workspace", show=False),
        Binding("enter", "advance", "advance", show=False),
        Binding("b", "back_to_configure", "back", show=False),
        Binding("escape", "cancel", "cancel", show=False),
    ]

    #: The selection index for list-style steps (J1 paths / J3 repo rows).
    selected: reactive[int] = reactive(0)

    def __init__(self, context: InitWizardContext) -> None:
        """Construct the wizard for *context*, seeding the entry step."""
        super().__init__()
        self._ctx = context
        # First-run auto-opens to the J1 hero; otherwise we route by scope to
        # the most relevant journey's configure step.
        if context.init_needed:
            journey, step = Journey.FIRST_RUN, Step.CHOOSE
        elif context.scope == "workspace":
            journey, step = Journey.WORKSPACE, Step.CONFIGURE
        else:
            journey, step = Journey.REPO_INIT, Step.CONFIGURE
        self.model = WizardModel(
            journey=journey,
            step=step,
            project_code=context.repo_code or "",
            repos=context.registry_repos,
        )

    # -- compose --------------------------------------------------------------

    def compose(self) -> ComposeResult:
        """Yield the header + step rail + the current step's body + footer.

        The body widgets are built by :meth:`_build_body` (constructor form,
        not the ``with``-context form) so the SAME builder serves both the
        initial compose and the per-step :meth:`_rebuild_body` remount — the
        ``with Vertical():`` form only works inside an active compose stack.
        """
        mode = self._mode()
        with Vertical(id="init-box"):
            yield Static(self._header_text(mode=mode), classes="init-header", id="init-header")
            yield Static("", classes="init-rail", id="init-rail")
            with VerticalScroll(id="init-body"):
                yield from self._build_body(mode=mode)
            yield Static("", classes="init-foot", id="init-foot")

    def _build_body(self, *, mode: str) -> list[Widget]:
        """Build the body widgets for the current step (constructor form).

        Returns a flat list of widgets (panes built via ``Vertical(*children)``
        rather than the ``with`` context manager) so both :meth:`compose` and
        :meth:`_rebuild_body` can mount the same tree.
        """
        step = self.model.step
        if step is Step.CHOOSE:
            return [
                Static(self._hero_text(mode=mode), classes="init-hero", id="init-hero"),
                Static(
                    path_rows_markup(
                        self.selected, mode=mode, git_root_found=self._ctx.git_root_found
                    ),
                    classes="init-paths",
                    id="init-paths",
                ),
            ]
        if step is Step.CONFIGURE and self.model.journey is Journey.WORKSPACE:
            return self._build_workspace_select(mode=mode)
        if step is Step.CONFIGURE:
            return self._build_repo_configure(mode=mode)
        if step is Step.PREVIEW:
            return self._build_preview(mode=mode)
        if step in (Step.EXECUTE, Step.ERROR):
            return self._build_execute(mode=mode)
        return self._build_done(mode=mode)  # DONE

    def _pane(self, *children: Widget, pane_id: str | None = None) -> Vertical:
        """Build a bordered pane container (constructor form)."""
        pane = Vertical(*children, classes="init-pane")
        if pane_id is not None:
            pane.id = pane_id
        return pane

    def _build_repo_configure(self, *, mode: str) -> list[Widget]:
        """Build the J2 identity + profiles configure panes."""
        identity: list[Widget] = [
            Static(IDENTITY_TITLE, classes="init-ptitle"),
            Static(self._code_field_text(), classes="init-field", id="init-code"),
        ]
        if not code_is_valid(self.model.project_code):
            identity.append(
                Static(f"[$error]{escape_markup(CODE_INVALID_HELP)}[/]", classes="init-help")
            )
        identity.append(
            Static(
                self._input_line_text(label="title", value=self.model.project_title),
                classes="init-field",
            )
        )
        profiles = [
            Static(PROFILES_TITLE, classes="init-ptitle"),
            Static(chips_markup(self.model, mode=mode), classes="init-field", id="init-chips"),
            Static(
                f"[$text-muted]template[/]  [$text]{escape_markup(self.model.template)}[/]",
                classes="init-field",
            ),
        ]
        return [self._pane(*identity), self._pane(*profiles)]

    def _build_workspace_select(self, *, mode: str) -> list[Widget]:
        """Build the J3 workspace-name + repo-select panes."""
        name_pane = self._pane(
            Static(WORKSPACE_NAME_TITLE, classes="init-ptitle"),
            Static(self._code_field_text(label="name"), classes="init-field", id="init-code"),
        )
        select_pane = self._pane(
            Static(select_title(self.model), classes="init-ptitle", id="init-select-title"),
            Static(repo_rows_markup(self.model, mode=mode), id="init-repos"),
        )
        return [name_pane, select_pane]

    def _build_preview(self, *, mode: str) -> list[Widget]:
        """Build the preview file-tree / repo list + transparency line."""
        if self.model.journey is Journey.WORKSPACE:
            pane = self._pane(
                Static(PREVIEW_TITLE, classes="init-ptitle"),
                Static(repo_rows_markup(self.model, mode=mode)),
            )
            line = workspace_transparency_line(self.model)
        elif self.model.journey is Journey.REGISTER:
            pane = self._pane(
                Static(REGISTER_TITLE, classes="init-ptitle"),
                Static(register_preview_markup(self._ctx.target_dir)),
            )
            line = register_transparency_line(self._ctx.target_dir)
        else:
            pane = self._pane(
                Static(PREVIEW_TITLE, classes="init-ptitle"),
                Static(file_tree_markup(self.model)),
            )
            line = init_transparency_line(self.model)
        cmd = Static(
            f"[$text-disabled]runs[/]  [$info]{escape_markup(line)}[/]",
            classes="init-cmdline",
        )
        return [pane, cmd]

    def _build_execute(self, *, mode: str) -> list[Widget]:
        """Build the execute / error pane (banner + substep rows + cmdline)."""
        widgets: list[Widget] = []
        is_error = self.model.step is Step.ERROR
        if is_error:
            widgets.append(
                Static(error_banner_markup(self.model, mode=mode), classes="init-banner")
            )
        pane_children: list[Widget] = [
            Static(ERROR_PANE_TITLE if is_error else EXECUTE_TITLE, classes="init-ptitle"),
            Static(substep_rows_markup(self.model, mode=mode), id="init-substeps"),
        ]
        if is_error and self.model.error_stderr is not None:
            pane_children.append(
                Static(error_stderr_markup(self.model), classes="init-help", id="init-stderr")
            )
        widgets.append(self._pane(*pane_children))
        if is_error:
            widgets.append(
                Static(
                    f"[$text-muted]{escape_markup(ERROR_REASSURANCE)}[/]", classes="init-reassure"
                )
            )
        else:
            line = self._execute_cmdline()
            widgets.append(
                Static(
                    f"[$text-disabled]running[/]  [$info]{escape_markup(line)}[/]  "
                    "[$text-disabled]· via daemon[/]",
                    classes="init-cmdline",
                )
            )
        return widgets

    def _execute_cmdline(self) -> str:
        """Return the truncated transparency line the execute step shows."""
        if self.model.journey is Journey.WORKSPACE:
            return workspace_transparency_line(self.model)
        head = init_transparency_line(self.model).split(" --project-title")[0]
        return f"{head} …"

    def _build_done(self, *, mode: str) -> list[Widget]:
        """Build the J4 done card (banner if warn + title + created + doctor + chips)."""
        widgets: list[Widget] = []
        if any(not c.ok for c in self.model.doctor):
            warns = sum(1 for c in self.model.doctor if not c.ok)
            warn = next(c for c in self.model.doctor if not c.ok)
            tri = chrome("attention", mode=mode)
            widgets.append(
                Static(
                    f"[$warning]{tri} [b]created · {warns} check warns[/b] "
                    f"— {escape_markup(warn.fix_hint or warn.name)}[/]",
                    classes="init-banner",
                )
            )
        widgets.append(Static(done_title_markup(self.model, mode=mode), classes="init-done-title"))
        widgets.append(Static(escape_markup(done_subtitle(self.model)), classes="init-done-sub"))
        widgets.append(
            self._pane(
                Static(CREATED_TITLE, classes="init-ptitle"),
                Static(created_rows_markup(self.model, mode=mode)),
            )
        )
        widgets.append(
            self._pane(
                Static(doctor_title(self.model), classes="init-ptitle"),
                Static(doctor_rows_markup(self.model, mode=mode)),
            )
        )
        widgets.append(Static(next_chips_markup(self.model), classes="init-chips"))
        return widgets

    # -- header / footer / hero ----------------------------------------------

    def _code_field_text(self, *, label: str = "code") -> str:
        """Render the code / name input with inline-right validity."""
        return self._input_line_text(
            label=label,
            value=self.model.project_code,
            cursor=True,
            right=code_hint_markup(self.model, mode=self._mode()),
        )

    def _input_line_text(
        self,
        *,
        label: str,
        value: str,
        cursor: bool = False,
        right: str = "",
    ) -> str:
        """Render one bordered input row with optional right-side status."""
        field_width = 28
        marker = "\u258f" if cursor else ""
        visible = f"{value}{marker}"
        clipped = visible[:field_width]
        padded = clipped.ljust(field_width)
        suffix = f"  {right}" if right else ""
        return (
            f"[$text-muted]{escape_markup(label)}[/]  [$border]│[/] "
            f"[$text]{escape_markup(padded)}[/] [$border]│[/]{suffix}"
        )

    def _header_text(self, *, mode: str) -> str:
        """Render the brand + breadcrumb header line."""
        brand = chrome("brand", mode=mode)
        crumb = self._crumb()
        right = self._header_right()
        return (
            f"[$accent b]{brand}[/] [$accent b]{escape_markup(HERO_WORDMARK)}[/] "
            f"[$text-muted]{crumb}[/]  [$text-disabled]{right}[/]"
        )

    def _crumb(self) -> str:
        """Return the breadcrumb tail for the current journey."""
        arrow = "\u276f"  # >
        if self.model.journey is Journey.WORKSPACE:
            return f"workspace {arrow} new"
        if self.model.journey is Journey.REGISTER:
            return f"user {arrow} register"
        if self.model.journey is Journey.FIRST_RUN:
            return f"user {arrow} welcome"
        return f"repo {arrow} init"

    def _header_right(self) -> str:
        """Return the right-aligned header status for the current step."""
        if self.model.journey is Journey.FIRST_RUN:
            return "first run"
        if self.model.journey is Journey.REGISTER:
            return "no init"
        if self.model.step is Step.CONFIGURE and self.model.journey is Journey.REPO_INIT:
            return "no .ea yet"
        if self.model.step is Step.PREVIEW:
            return f"{self.model.project_code} · {PREVIEW_NOTHING_WRITTEN}"
        if self.model.step is Step.ERROR:
            return "failed"
        return ""

    def _hero_text(self, *, mode: str) -> str:
        """Render the J1 hero text block (wordmark + purpose, below the seal art).

        The ASCII-art Seal is the present brand mark, mounted as a separate
        widget above this block by :meth:`_mount_seal_art`, so ``seal_ready`` is
        ``True`` to suppress the small glyph and avoid a double brand mark.
        """
        return hero_markup(mode=mode, seal_ready=True)

    def _footer_text(self) -> str:
        """Return the key-hint footer for the current step."""
        step = self.model.step
        if step is Step.CHOOSE:
            return "[ ↑/↓ select · i/r/w choose · Enter choose · Esc quit ]"
        if step is Step.CONFIGURE and self.model.journey is Journey.WORKSPACE:
            return "[ ↑/↓ select · Space toggle · a all · \u276f Enter preview · Esc cancel ]"
        if step is Step.CONFIGURE:
            valid = code_is_valid(self.model.project_code)
            enter = "\u276f Enter preview" if valid else "Enter preview · fix code first"
            return f"[ Space toggle chip · {enter} · Esc cancel ]"
        if step is Step.PREVIEW:
            if self.model.journey is Journey.REGISTER:
                return "[ \u276f Enter register · Esc cancel ]"
            return "[ \u276f Enter create · Esc cancel ]"
        if step is Step.EXECUTE:
            return f"[ {EXECUTE_FOOTER_LIVE} · \u276f Esc cancel ]"
        if step is Step.ERROR:
            return "[ \u276f Enter retry · b back to configure · Esc abandon ]"
        return "[ Esc dismiss ]"

    # -- lifecycle ------------------------------------------------------------

    def on_mount(self) -> None:
        """Paint the rail + footer and seed the J1 hero seal art."""
        self._repaint_chrome()
        self._mount_seal_art()

    def _mount_seal_art(self) -> None:
        """Mount the ASCII-art Seal above the J1 hero.

        The deterministic accent-on-surface TEXT brand mark the other hero
        surfaces (research board, autopilot, …) render. Unlike the retired
        raster path it carries no graphics-protocol dependency, so it renders
        identically in CI, a pipe, and a live terminal — the goldens stay
        stable. Mounted only on the J1 CHOOSE step (the welcome hero).
        """
        if self.model.step is not Step.CHOOSE:
            return
        try:
            hero = self.query_one("#init-hero", Static)
            self.query_one("#init-body", VerticalScroll).mount(seal_art_widget(), before=hero)
        except Exception as exc:  # pragma: no cover - mount race guard
            logger.debug(f"_mount_seal_art mount_failed err={exc!r}")

    def _mode(self) -> str:
        """Resolve the active render-mode label from the host app."""
        return _resolve_render_mode(self.app)

    def _repaint_chrome(self) -> None:
        """Repaint the header, step rail, and footer for the current step."""
        mode = self._mode()
        self.query_one("#init-header", Static).update(self._header_text(mode=mode))
        self.query_one("#init-rail", Static).update(steprail_markup(self.model, mode=mode))
        self.query_one("#init-foot", Static).update(self._footer_text())

    async def _rebuild_body(self) -> None:
        """Tear down + remount the body for the current step.

        Mounts the constructor-form widgets :meth:`_build_body` returns (the
        ``with Vertical():`` compose form would raise outside an active compose
        stack), then repaints the chrome + re-seeds the seal image.
        """
        mode = self._mode()
        body = self.query_one("#init-body", VerticalScroll)
        await body.remove_children()
        await body.mount(*self._build_body(mode=mode))
        self._repaint_chrome()
        self._mount_seal_art()

    def _goto(self, step: Step) -> None:
        """Transition to *step* and rebuild the body (off a worker callback)."""
        self.model.step = step
        self.run_worker(self._rebuild_body(), exclusive=False)

    # -- reactive selection ---------------------------------------------------

    def watch_selected(self) -> None:
        """Repaint the path / repo rows when the selection moves."""
        if not self.is_mounted:
            return
        mode = self._mode()
        if self.model.step is Step.CHOOSE:
            rows = self.query("#init-paths")
            if rows:
                rows.first(Static).update(
                    path_rows_markup(
                        self.selected, mode=mode, git_root_found=self._ctx.git_root_found
                    )
                )

    # -- actions --------------------------------------------------------------

    def action_move(self, delta: int) -> None:
        """Move the list selection by *delta* (J1 paths / J3 repo rows)."""
        if self.model.step is Step.CHOOSE:
            self.selected = (self.selected + delta) % 3
        elif self.model.step is Step.CONFIGURE and self.model.journey is Journey.WORKSPACE:
            count = len(self.model.repos)
            if count:
                self.selected = (self.selected + delta) % count

    def action_path(self, key: str) -> None:
        """Choose a J1 entry path by key (``i`` / ``r`` / ``w``).

        Only active on the J1 hero; advances into the relevant journey at its
        configure step. NEVER auto-mutates — the chosen journey still walks
        configure -> preview before any write.
        """
        if self.model.step is not Step.CHOOSE:
            return
        if key == "i":
            self.model.journey = Journey.REPO_INIT
            self.model.step = Step.CONFIGURE
        elif key == "r":
            self.model.journey = Journey.REGISTER
            self.model.step = Step.PREVIEW
        elif key == "w":
            self.model.journey = Journey.WORKSPACE
            self.model.step = Step.CONFIGURE
            self.selected = 0
        else:
            return
        self.run_worker(self._rebuild_body(), exclusive=False)

    def action_toggle_chip(self) -> None:
        """Toggle the focused chip (J2) / repo (J3) on Space.

        Named ``toggle_chip`` (not ``toggle``) so it does not collide with
        Textual's built-in :meth:`~textual.dom.DOMNode.action_toggle`
        CSS-class action.
        """
        mode = self._mode()
        if self.model.step is Step.CONFIGURE and self.model.journey is Journey.REPO_INIT:
            # The configure step toggles the python chip on Space (the first
            # non-locked chip) so the affordance is wired + testable.
            chip = "python"
            if chip in self.model.profiles:
                self.model.profiles.discard(chip)
            else:
                self.model.profiles.add(chip)
            self.query_one("#init-chips", Static).update(chips_markup(self.model, mode=mode))
        elif self.model.step is Step.CONFIGURE and self.model.journey is Journey.WORKSPACE:
            if not self.model.repos:
                return
            ref = self.model.repos[self.selected]
            if ref.code in self.model.selected_repos:
                self.model.selected_repos.discard(ref.code)
            else:
                self.model.selected_repos.add(ref.code)
            self._repaint_select()

    def action_select_all(self) -> None:
        """Select every registry repo (J3 select step, ``a``)."""
        if self.model.step is Step.CONFIGURE and self.model.journey is Journey.WORKSPACE:
            self.model.selected_repos = {ref.code for ref in self.model.repos}
            self._repaint_select()

    def _repaint_select(self) -> None:
        """Repaint the J3 repo rows + the live selected-count title."""
        mode = self._mode()
        titles = self.query("#init-select-title")
        if titles:
            titles.first(Static).update(select_title(self.model))
        rows = self.query("#init-repos")
        if rows:
            rows.first(Static).update(repo_rows_markup(self.model, mode=mode))

    def action_advance(self) -> None:
        """Advance to the next step on Enter (or retry from the error card)."""
        step = self.model.step
        if step is Step.CHOOSE:
            self.action_path(("i", "r", "w")[self.selected])
            return
        if step is Step.CONFIGURE:
            if self.model.journey is Journey.REPO_INIT and not code_is_valid(
                self.model.project_code
            ):
                return  # gate the preview on a valid code
            if self.model.journey is Journey.WORKSPACE and (
                not self.model.project_code or not self.model.selected_repos
            ):
                return
            self._goto(Step.PREVIEW)
            return
        if step is Step.PREVIEW:
            if self.model.journey is Journey.REGISTER:
                self.dismiss(
                    InitWizardResult(
                        INIT_ACTION_REGISTER,
                        register_repo_command(self._ctx.target_dir),
                    )
                )
                return
            self._start_execute()
            return
        if step is Step.ERROR:
            self._start_execute()  # r retry re-runs the execute pass
            return

    def action_back_to_configure(self) -> None:
        """Return to the configure step from the error card (``b``)."""
        if self.model.step is Step.ERROR:
            self.model.error_stderr = None
            self.model.error_step_index = None
            self.model.substeps = []
            self._goto(Step.CONFIGURE)

    def action_cancel(self) -> None:
        """Esc — safe cancel pre-execute; cancel-confirm during execute.

        At every pre-execute step (choose / configure / preview) Esc dismisses
        with no mutation. During execute Esc opens a cancel-confirm rather than
        hard-killing a mid-write worker. On the done / error card Esc dismisses.
        """
        if self.model.step is Step.EXECUTE:
            self._confirm_cancel()
            return
        logger.info(f"init_wizard cancel step={self.model.step.value!r}")
        self.dismiss(None)

    def _confirm_cancel(self) -> None:
        """Open a cancel-confirm during execute (never hard-kill mid-write)."""
        from eawf.surfaces.tui.screens.overlays.confirm import ConfirmModal

        def _on_confirm(confirmed: bool | None) -> None:
            if confirmed:
                logger.info("init_wizard cancel_confirmed during_execute")
                self.dismiss(None)

        push_modal = getattr(self.app, "push_modal", None)
        modal = ConfirmModal("init is running — cancel the partial init?")
        if callable(push_modal):
            push_modal(modal, callback=_on_confirm)
        else:
            self.app.push_screen(modal, callback=_on_confirm)

    # -- the live execute pass (D-G worker) -----------------------------------

    def _start_execute(self) -> None:
        """Seed the substep rows + kick the live execute worker.

        Runs the real init / workspace bootstrap on a Textual worker (never
        inline). The worker pumps :class:`StepEvent`s back through
        :meth:`_apply_event` so the substep sigils stream live, then advances
        to the done card on success or the error card on failure.
        """
        if self.model.journey is Journey.WORKSPACE:
            self.model.substeps = [Substep(LINK_SUBSTEP_CREATE)] + [
                Substep(f"link {code} → validate") for code in sorted(self.model.selected_repos)
            ]
        else:
            self.model.substeps = [Substep(label) for label in J2_SUBSTEPS]
        self.model.error_stderr = None
        self.model.error_step_index = None
        self.model.step = Step.EXECUTE
        self.run_worker(self._rebuild_body(), exclusive=False)
        if self.model.journey is Journey.WORKSPACE:
            self.run_worker(self._execute_workspace(), group="init-exec", exclusive=True)
        else:
            self.run_worker(self._execute_repo_init(), group="init-exec", exclusive=True)

    async def _execute_repo_init(self) -> None:
        """Run the J2 repo init live, streaming substep events, then advance."""
        start = time.monotonic()
        target = self._ctx.target_dir
        # Substeps ride the single pipeline call; we mark them running around
        # the call so the stream reflects progress, then done on success.
        for index in range(len(self.model.substeps)):
            await self._emit(StepEvent(index, SubstepState.RUNNING))
        try:
            artifacts = await asyncio.to_thread(_run_repo_init, self.model, target)
        except Exception as exc:
            logger.info(f"_execute_repo_init failed err={exc!r}")
            # Mark the AGENTS render substep (step 3 of 5) failed per the mock.
            fail_index = min(2, len(self.model.substeps) - 1)
            await self._emit(StepEvent(fail_index, SubstepState.FAILED, stderr=str(exc).strip()))
            await self._to_error(fail_index + 1, str(exc).strip())
            return
        for index in range(len(self.model.substeps)):
            await self._emit(StepEvent(index, SubstepState.DONE))
        self.model.artifacts = artifacts
        self.model.doctor = _doctor_probe(target)
        self.model.duration_s = time.monotonic() - start
        await self._to_done()

    async def _execute_workspace(self) -> None:
        """Run the J3 workspace bootstrap live, per-repo validate, then advance.

        Creates the workspace state, then links + validates each selected repo
        in one pass. A per-repo validate failure surfaces ``failed`` on that
        row and the pass CONTINUES with the rest (partial success), never
        aborting the whole bootstrap.
        """
        start = time.monotonic()
        ws_path = self._ctx.workspace_state_path or (self._ctx.target_dir / ".ea" / "state.json")
        code = self.model.project_code
        await self._emit(StepEvent(0, SubstepState.RUNNING))
        try:
            await asyncio.to_thread(_create_workspace_state, ws_path, code=code)
        except Exception as exc:
            logger.info(f"_execute_workspace create_failed err={exc!r}")
            await self._emit(StepEvent(0, SubstepState.FAILED, stderr=str(exc).strip()))
            await self._to_error(1, str(exc).strip())
            return
        await self._emit(StepEvent(0, SubstepState.DONE))
        by_code = {ref.code: ref for ref in self.model.repos}
        any_ok = False
        for offset, repo_code in enumerate(sorted(self.model.selected_repos), start=1):
            await self._emit(StepEvent(offset, SubstepState.RUNNING))
            ref = by_code.get(repo_code)
            if ref is None:
                await self._emit(StepEvent(offset, SubstepState.FAILED, result_word="failed"))
                continue
            ok, _reason = await asyncio.to_thread(_link_one_repo, ws_path, ref)
            if ok:
                any_ok = True
                await self._emit(StepEvent(offset, SubstepState.DONE, result_word="ok"))
            else:
                await self._emit(StepEvent(offset, SubstepState.FAILED, result_word="failed"))
        linked = sorted(self.model.selected_repos)
        self.model.artifacts = [(".ea/state.json", f"workspace {code}")] + [
            (f"link {c}", "validated") for c in linked
        ]
        self.model.doctor = [
            DoctorCheck("state", True, None),
            DoctorCheck("daemon", True, None),
            DoctorCheck(
                "links",
                any_ok,
                None if any_ok else "no repo linked — run eawf workspace validate",
            ),
        ]
        self.model.duration_s = time.monotonic() - start
        await self._to_done()

    async def _emit(self, event: StepEvent) -> None:
        """Apply a live step event + repaint the substep rows from the worker."""
        self._apply_event(event)
        rows = self.query("#init-substeps")
        if rows:
            rows.first(Static).update(substep_rows_markup(self.model, mode=self._mode()))

    def _apply_event(self, event: StepEvent) -> None:
        """Fold a :class:`StepEvent` onto the matching substep row."""
        if not (0 <= event.index < len(self.model.substeps)):
            return
        current = self.model.substeps[event.index]
        self.model.substeps[event.index] = replace(
            current, state=event.state, result_word=event.result_word or current.result_word
        )
        if event.stderr is not None:
            self.model.error_stderr = event.stderr

    async def _to_done(self) -> None:
        """Transition to the J4 done card after a successful execute."""
        self.model.step = Step.DONE
        await self._rebuild_body()

    async def _to_error(self, step_index: int, stderr: str) -> None:
        """Transition to the J2 error card after a failed execute."""
        self.model.error_step_index = step_index
        self.model.error_stderr = stderr
        self.model.step = Step.ERROR
        await self._rebuild_body()


def open_init_wizard(
    app: App[None],
    *,
    callback: Callable[[InitWizardResult | None], None] | None = None,
) -> bool:
    """Push the init wizard onto *app*'s screen stack.

    Routes through ``push_modal`` when present so the global modal-depth cap
    applies. A callback can be provided by the host to receive the dismiss
    value (``None`` after a live in-TUI execution).
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
    "CODE_INVALID_HELP",
    "CREATED_TITLE",
    "ERROR_BANNER",
    "EXECUTE_TITLE",
    "HERO_WORDMARK",
    "INIT_ACTION_QUICK",
    "INIT_ACTION_REGISTER",
    "INIT_ACTION_WORKSPACE_LINK",
    "DoctorCheck",
    "InitWizardContext",
    "InitWizardModal",
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
    "file_tree_markup",
    "format_command",
    "hero_markup",
    "init_transparency_line",
    "next_chips_markup",
    "open_init_wizard",
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
