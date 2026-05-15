"""Audit-running overlay + failure action menu for the Eä Rich TUI (P20-I01-W07).

Read-only one-frame overlay surfaced when an audit is mid-run or has
recently failed. The overlay aggregates the three pieces of context an
operator wants when a ship-gate, review, or evaluation is in flight:

* **Audit summary** — id, scope, kind, status, verdict (typed accessors
  off :class:`eawf.state.models.Audit`).
* **Attached-runtime block** — process id + harness adapter id of the
  audit subagent currently running, when the caller threads in an
  :class:`AuditAttachment`. The overlay is strictly read-only — it
  never spawns a process and never kills one; the PID/adapter pair
  is rendered for operator situational-awareness only.
* **Remediation hints** — contextual one-liners derived from the
  audit's recorded ``verdict`` + ``status``. The hints are static
  prose surfaces; they do not query the network or shell out.
* **Action menu** — three remediation choices (``retry``,
  ``mark-blocked``, ``escalate``) rendered with a muted ``(v0.4)``
  marker so the operator knows the verbs are deferred. Pressing any
  action key returns a footer toast (``action deferred to v0.4``) via
  :func:`handle_action_key`; the overlay never executes the action.

Layout sketch::

    +----------------------------------------------------------+
    | Eä  EAWF / P20 / P20-I01  | overlay: audit A21-P16       |  <- header
    +----------------------------------------------------------+
    | id:        A21-P16                                       |
    | scope:     P20                                           |  <- summary
    | kind:      ship-gate                                     |
    | status:    failed                                        |
    | verdict:   major                                         |
    +----------------------------------------------------------+
    | attachment:                                              |
    |   pid:       12345                                       |  <- runtime
    |   adapter:   claude-code                                 |
    |   session:   SES-001                                     |
    +----------------------------------------------------------+
    | remediation hints:                                       |
    |   - rerun the failing checks once root-cause is logged   |  <- hints
    |   - audit verdict major: block ship until downgraded     |
    +----------------------------------------------------------+
    | actions (v0.4):                                          |
    |   r  retry          (v0.4)                               |  <- actions
    |   b  mark-blocked   (v0.4)                               |
    |   e  escalate       (v0.4)                               |
    +----------------------------------------------------------+

The overlay is a one-frame :class:`rich.layout.Layout` carrying the
shared header chassis (brand outside-left of breadcrumb per
``feedback_tui_branding``). It does NOT spin :class:`rich.live.Live` —
the caller (wave-board, quadrant) composes the overlay into its tick.

Keymap conventions follow ``feedback_tui_keymap_conventions``:
single-letter action shortcuts (``r``, ``b``, ``e``) live behind the
``(v0.4)`` deferred marker; the overlay returns the deferred toast
text rather than executing the action so the surface stays read-only
until v0.4 ships the mutating verbs.
"""

from __future__ import annotations

import logging
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from rich.console import RenderableType
from rich.layout import Layout
from rich.panel import Panel
from rich.text import Text

from eawf.state.enums import AuditStatus, AuditVerdict
from eawf.state.models import Audit, State
from eawf.tui.layout import build_brand_text, build_breadcrumb

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Harness adapter + action enumerations
# ---------------------------------------------------------------------------


#: Literal type for the harness adapter id rendered in the attachment
#: panel. Mirrors the dispatch-renderer adapter list (claude-code /
#: codex / opencode); ``unknown`` is the explicit fallback when the
#: caller cannot determine the adapter (read-only — never coerced).
HarnessAdapter = Literal["claude-code", "codex", "opencode", "unknown"]


#: Tuple of recognised harness adapters — useful for validation +
#: parametrised tests. The order matches D12 (v0.3 harness scope:
#: claude + codex + opencode).
KNOWN_HARNESS_ADAPTERS: tuple[HarnessAdapter, ...] = (
    "claude-code",
    "codex",
    "opencode",
    "unknown",
)


#: Literal type for the action-menu kinds. All three are deferred to
#: v0.4 — the overlay surfaces them as keymap rows but the actual
#: state mutations land in a future wave.
AuditActionKind = Literal["retry", "mark-blocked", "escalate"]


#: Ordered tuple of action kinds (the order rendered top-to-bottom in
#: the action panel). Stable across runs so the keymap is muscle-memory
#: friendly for the operator.
KNOWN_AUDIT_ACTIONS: tuple[AuditActionKind, ...] = (
    "retry",
    "mark-blocked",
    "escalate",
)


#: Keymap: shortcut letter -> action kind. Single-letter shortcuts so
#: the action menu stays one-shot for new readers. The shortcuts MUST
#: be unique; :func:`handle_action_key` raises when given anything
#: outside this registry.
ACTION_KEYMAP: dict[str, AuditActionKind] = {
    "r": "retry",
    "b": "mark-blocked",
    "e": "escalate",
}


#: Muted marker appended to every action row + the action-panel title.
#: Surfaces the "deferred to v0.4" status without losing the keymap.
ACTION_DEFERRED_MARKER: str = "(v0.4)"


#: Footer toast returned by :func:`handle_action_key` when the operator
#: presses an action shortcut. The caller renders the toast into the
#: parent loop's footer; the overlay itself never mutates state.
ACTION_DEFERRED_TOAST: str = "action deferred to v0.4"


# ---------------------------------------------------------------------------
# Transient attachment input (typed + strict)
# ---------------------------------------------------------------------------


class AuditAttachment(BaseModel):
    """Transient attachment record for the audit-running overlay.

    Carries the runtime PID + harness adapter id of an audit subagent
    that is currently attached to the operator's session. The overlay
    is strictly read-only — this model does NOT persist, it never
    spawns a process, and it never kills one. The caller (wave-board
    / quadrant tick loop) constructs the attachment from whatever
    transient source it already has (e.g. a hook payload, a dispatch
    envelope) and threads it through :func:`open_audit_overlay`.

    Why a typed model rather than a free dict — keeping the contract
    typed means a wrong field name fails fast at construction time
    rather than silently dropping the PID at render time. The model
    is :class:`pydantic.BaseModel` with ``extra="forbid"`` per the
    project-wide strict-config rule (AGENTS.md rule 2).

    The ``agent_session_id`` field is optional because not every
    attachment surfaces a state-resident session id (the dispatcher
    may attach before the session record exists in state.json). When
    set it MUST point at an
    :class:`~eawf.state.models.AgentSession` id; validation of the
    cross-reference lives at the caller, not here, so the overlay
    can be rendered even when state.json has not yet been refreshed.
    """

    model_config = ConfigDict(extra="forbid")

    pid: int = Field(ge=1, description="OS process id of the audit subagent (must be positive).")
    harness_adapter: HarnessAdapter = Field(
        description=(
            "Harness adapter id of the attached runtime (claude-code / codex / opencode / unknown)."
        )
    )
    agent_session_id: str | None = Field(
        default=None,
        description="Optional cross-reference to an AgentSession.id when state-resident.",
    )


# ---------------------------------------------------------------------------
# Remediation hints
# ---------------------------------------------------------------------------


#: Hint surfaced when the audit's :class:`AuditStatus` is ``running``.
HINT_RUNNING: str = "audit is still running; keep the subagent attached until it completes"

#: Hint surfaced when the audit's :class:`AuditStatus` is ``failed``.
HINT_FAILED: str = "rerun the failing checks once root-cause is logged"

#: Hint surfaced when the audit's :class:`AuditStatus` is ``pending``.
HINT_PENDING: str = "audit has not started yet; dispatch the audit subagent to begin"


#: Verdict-derived hints. Only ``major`` / ``minor`` carry remediation
#: prose; ``pass`` deliberately yields no hint because the audit is
#: already green and the overlay should not nag the operator.
HINT_VERDICT: dict[str, str] = {
    AuditVerdict.MAJOR.value: "audit verdict major: block ship until downgraded",
    AuditVerdict.MINOR.value: "audit verdict minor: triage findings before next phase close",
}


def remediation_hints(audit: Audit) -> list[str]:
    """Return contextual remediation hints for *audit*.

    Hints are derived from the audit's :attr:`Audit.status` and
    :attr:`Audit.verdict`. The list is empty when the audit is
    ``complete`` + ``pass`` because there is nothing to remediate.

    Args:
        audit: Typed :class:`Audit` record from state.

    Returns:
        Ordered list of hint strings (status-derived first, then
        verdict-derived). Empty when no remediation is warranted.
    """
    hints: list[str] = []
    status_value = audit.status.value if isinstance(audit.status, AuditStatus) else audit.status
    if status_value == AuditStatus.RUNNING.value:
        hints.append(HINT_RUNNING)
    elif status_value == AuditStatus.FAILED.value:
        hints.append(HINT_FAILED)
    elif status_value == AuditStatus.PENDING.value:
        hints.append(HINT_PENDING)
    # ``complete`` carries no status-derived hint; verdict still might.
    if audit.verdict is not None:
        verdict_value = (
            audit.verdict.value if isinstance(audit.verdict, AuditVerdict) else audit.verdict
        )
        verdict_hint = HINT_VERDICT.get(verdict_value)
        if verdict_hint is not None:
            hints.append(verdict_hint)
    return hints


# ---------------------------------------------------------------------------
# Panel builders
# ---------------------------------------------------------------------------


def build_audit_summary_panel(audit: Audit) -> Panel:
    """Build the audit-summary Panel.

    Args:
        audit: Typed :class:`Audit` record from state.

    Returns:
        :class:`Panel` titled ``audit`` showing id, scope, kind,
        status, verdict, and the report artifact link.
    """
    verdict_str = (
        audit.verdict.value if isinstance(audit.verdict, AuditVerdict) else (audit.verdict or "-")
    )
    lines: list[str] = [
        f"id:           {audit.id}",
        f"scope:        {audit.scope_id}",
        f"kind:         {audit.kind.value}",
        f"status:       {audit.status.value}",
        f"verdict:      {verdict_str}",
        f"report:       {audit.report_artifact_id or '-'}",
    ]
    return Panel(Text("\n".join(lines)), title="audit", border_style="cyan")


def build_attachment_panel(attachment: AuditAttachment | None) -> Panel:
    """Build the attached-runtime Panel.

    Renders PID + harness adapter id + optional agent-session id. When
    *attachment* is ``None`` the panel renders an explicit
    ``(no runtime attached)`` placeholder so the operator can tell the
    difference between "attached but pid=0" and "not attached at all".

    Args:
        attachment: Optional :class:`AuditAttachment` carrying the
            transient PID + adapter pair.

    Returns:
        :class:`Panel` titled ``attachment``.
    """
    if attachment is None:
        body = Text("  (no runtime attached)")
        return Panel(body, title="attachment", border_style="cyan")
    lines: list[str] = [
        f"  pid:       {attachment.pid}",
        f"  adapter:   {attachment.harness_adapter}",
        f"  session:   {attachment.agent_session_id or '-'}",
    ]
    return Panel(Text("\n".join(lines)), title="attachment", border_style="cyan")


def build_remediation_panel(hints: list[str]) -> Panel:
    """Build the remediation-hints Panel.

    Args:
        hints: Output of :func:`remediation_hints` (or a pre-built
            list when the caller wants to override).

    Returns:
        :class:`Panel` titled ``remediation hints``. Empty list
        collapses to a single ``-`` placeholder row.
    """
    lines: list[str] = ["  -"] if not hints else [f"  - {hint}" for hint in hints]
    return Panel(Text("\n".join(lines)), title="remediation hints", border_style="cyan")


def build_actions_panel() -> Panel:
    """Build the action-menu Panel.

    Renders the three remediation actions (``retry``, ``mark-blocked``,
    ``escalate``) with their single-letter shortcuts and the
    :data:`ACTION_DEFERRED_MARKER` so the operator can read the
    keymap without thinking the action is executable.

    The panel deliberately includes the marker in the title too — the
    title row is the first thing the operator's eye lands on, and
    repeating ``(v0.4)`` there makes the deferred status impossible
    to miss.

    Returns:
        :class:`Panel` titled ``actions (v0.4)``.
    """
    lines: list[str] = []
    # Walk KNOWN_AUDIT_ACTIONS in order so the rows match the keymap
    # display order (which is also the muscle-memory order).
    inverse: dict[AuditActionKind, str] = {kind: key for key, kind in ACTION_KEYMAP.items()}
    for kind in KNOWN_AUDIT_ACTIONS:
        key = inverse[kind]
        lines.append(f"  {key}  {kind:<14} {ACTION_DEFERRED_MARKER}")
    return Panel(
        Text("\n".join(lines)),
        title=f"actions {ACTION_DEFERRED_MARKER}",
        border_style="cyan",
    )


# ---------------------------------------------------------------------------
# Header + frame composition (mirrors overlays.py chassis)
# ---------------------------------------------------------------------------


def _build_overlay_header(state: State, *, overlay_title: str) -> Panel:
    """Header strip — brand + breadcrumb + overlay-title suffix.

    Reuses :func:`eawf.tui.layout.build_brand_text` /
    :func:`eawf.tui.layout.build_breadcrumb` so the brand is byte-
    identical to the wave-board (W03) / overlays (W04) headers.
    """
    breadcrumb = build_breadcrumb(state.model_dump(mode="json"))
    text = build_brand_text(breadcrumb)
    text.append(f"  | overlay: {overlay_title}", style="dim")
    return Panel(text, title=None, border_style="dim")


def _build_overlay_layout(
    state: State,
    *,
    overlay_title: str,
    summary: Panel,
    attachment: Panel,
    remediation: Panel,
    actions: Panel,
) -> Layout:
    """Compose header + four body panels into a one-frame :class:`Layout`.

    Body layout: four stacked rows so each section gets a stable slot
    on the screen. Heights are tuned for a 30-row terminal — the
    summary + attachment rows hold their content without scrolling,
    and the remediation + actions rows take the remaining space.
    """
    layout = Layout()
    layout.split_column(
        Layout(name="header", size=3),
        Layout(name="body", ratio=1),
    )
    body = Layout(name="audit_body")
    body.split_column(
        Layout(summary, name="summary", size=8),
        Layout(attachment, name="attachment", size=5),
        Layout(remediation, name="remediation", ratio=1),
        Layout(actions, name="actions", size=5),
    )
    layout["header"].update(_build_overlay_header(state, overlay_title=overlay_title))
    layout["body"].update(body)
    return layout


# ---------------------------------------------------------------------------
# Single-dispatch entry + action handler
# ---------------------------------------------------------------------------


def _resolve_audit(state: State, target_id: str) -> Audit:
    """Look up an audit by id; raise :class:`KeyError` on miss."""
    bucket = state.audits or {}
    record = bucket.get(target_id)
    if record is None:
        raise KeyError(f"unknown audit: {target_id!r}")
    return record


def open_audit_overlay(
    state: State,
    target_id: str,
    *,
    attachment: AuditAttachment | None = None,
) -> RenderableType:
    """Open the audit-running overlay for *target_id*.

    Resolves the audit id against :attr:`State.audits`, derives the
    remediation hints via :func:`remediation_hints`, and composes the
    four-panel body into a header-bearing :class:`Layout`. The
    *attachment* keyword threads the transient PID + adapter pair
    into the attachment panel; when omitted the panel renders an
    explicit ``(no runtime attached)`` placeholder.

    Args:
        state: Validated :class:`State` document.
        target_id: Audit id (must be present in ``state.audits``).
        attachment: Optional :class:`AuditAttachment` carrying the
            attached-runtime PID + harness adapter id.

    Returns:
        :class:`rich.console.RenderableType` (a :class:`Layout`) ready
        to be composed into the parent surface.

    Raises:
        TypeError: when *target_id* is not a string.
        KeyError: when *target_id* is not present in
            :attr:`State.audits`.
    """
    if not isinstance(target_id, str):
        raise TypeError(f"target_id must be str, got {type(target_id).__name__}")
    audit = _resolve_audit(state, target_id)
    logger.info(
        f"open_audit_overlay audit={audit.id!r} status={audit.status.value} "
        f"verdict={audit.verdict.value if audit.verdict else '-'} "
        f"attached={attachment is not None}"
    )
    summary = build_audit_summary_panel(audit)
    attachment_panel = build_attachment_panel(attachment)
    remediation_panel = build_remediation_panel(remediation_hints(audit))
    actions_panel = build_actions_panel()
    title = f"audit {audit.id}"
    return _build_overlay_layout(
        state,
        overlay_title=title,
        summary=summary,
        attachment=attachment_panel,
        remediation=remediation_panel,
        actions=actions_panel,
    )


def handle_action_key(key: str) -> str | None:
    """Handle a single keystroke against the action menu.

    Returns the footer toast text (:data:`ACTION_DEFERRED_TOAST`) when
    *key* matches a known action shortcut. The overlay never executes
    the action — every mutating verb is deferred to v0.4.

    Args:
        key: Single-character keystroke from the caller's tick loop.

    Returns:
        The toast string when *key* maps to a known action; ``None``
        when *key* is not part of :data:`ACTION_KEYMAP` (the caller
        forwards unknown keystrokes to its own dispatch table).

    Raises:
        TypeError: when *key* is not a string.
    """
    if not isinstance(key, str):
        raise TypeError(f"key must be str, got {type(key).__name__}")
    if key not in ACTION_KEYMAP:
        return None
    action = ACTION_KEYMAP[key]
    logger.info(f"handle_action_key key={key!r} action={action!r} deferred=v0.4")
    return ACTION_DEFERRED_TOAST


__all__ = [
    "ACTION_DEFERRED_MARKER",
    "ACTION_DEFERRED_TOAST",
    "ACTION_KEYMAP",
    "HINT_FAILED",
    "HINT_PENDING",
    "HINT_RUNNING",
    "HINT_VERDICT",
    "KNOWN_AUDIT_ACTIONS",
    "KNOWN_HARNESS_ADAPTERS",
    "AuditActionKind",
    "AuditAttachment",
    "HarnessAdapter",
    "build_actions_panel",
    "build_attachment_panel",
    "build_audit_summary_panel",
    "build_remediation_panel",
    "handle_action_key",
    "open_audit_overlay",
    "remediation_hints",
]
