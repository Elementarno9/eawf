"""Clickable-reference modal and preview helpers for the TUI.

The :class:`ReferenceModal` reuses the cosmic-terminal detail-chassis look
its sibling :class:`~eawf.surfaces.tui.screens.overlays.detail.DetailModal`
established: the card title carries the overview chrome-glyph mnemonic from
the shared :mod:`~eawf.surfaces.tui.widgets.sigils` vocabulary, and the
field rows are aligned ``[$accent]<label>:[/] <value>`` pairs so the
colons line up in one column. A reference card therefore reads as a
single-tab slice of the detail card rather than a differently-styled
overlay.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, ClassVar

from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import VerticalScroll
from textual.markup import escape
from textual.screen import ModalScreen
from textual.widgets import Static

from eawf.kernel.state.urn import parse as parse_urn
from eawf.surfaces.render.link_wrap import ReferenceKind, iter_refs, linkify_text
from eawf.surfaces.tui.widgets import sigils
from eawf.surfaces.tui.widgets.eu_bar import DEFAULT_RENDER_MODE, RenderMode

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReferenceTarget:
    """Navigation target for one clickable reference."""

    kind: ReferenceKind
    target: str


@dataclass(frozen=True)
class ReferenceCard:
    """Resolved reference modal payload."""

    kind: ReferenceKind
    target: str
    title: str
    rows: tuple[tuple[str, str], ...]


def _fmt(value: object) -> str:
    """Stringify state-model values for compact modal rows."""
    if value is None:
        return "-"
    raw_value = getattr(value, "value", None)
    if raw_value is not None:
        return str(raw_value)
    if isinstance(value, list):
        return ", ".join(str(item) for item in value) if value else "-"
    if isinstance(value, dict):
        return f"{len(value)} item(s)"
    return str(value)


def _target_id(kind: ReferenceKind, target: str) -> str:
    """Normalise a clicked target into the state-table key shape."""
    if target.startswith("urn:eawf:v1:"):
        try:
            parsed = parse_urn(target)
        except ValueError as exc:
            logger.debug(f"_target_id parse_failed target={target!r} exc={exc!r}")
            return target
        raw = parsed.id or parsed.owner
        if kind == "spec":
            return raw.replace("/", "-").upper()
        if kind in {"phase", "iter", "wave", "hypothesis"}:
            return raw.upper()
        return raw
    if kind == "spec":
        return target.replace("/", "-").upper()
    if kind in {"phase", "iter", "wave", "hypothesis"}:
        return target.upper()
    return target


def _lookup(mapping: dict[str, Any] | None, target: str) -> Any | None:
    """Lookup *target* in a state mapping, with uppercase fallback."""
    if not mapping:
        return None
    if target in mapping:
        return mapping[target]
    return mapping.get(target.upper())


def _rows(record: object, field_names: tuple[str, ...]) -> tuple[tuple[str, str], ...]:
    """Build display rows from selected model fields."""
    out: list[tuple[str, str]] = []
    for field_name in field_names:
        if not hasattr(record, field_name):
            continue
        value = getattr(record, field_name)
        if value is None:
            continue
        out.append((field_name, _fmt(value)))
    return tuple(out)


def _fallback_card(
    kind: ReferenceKind,
    target: str,
    note: str = "no state row found",
) -> ReferenceCard:
    """Return a total fallback card for unresolved references."""
    return ReferenceCard(
        kind=kind,
        target=target,
        title=f"{kind} {target}",
        rows=(("kind", kind), ("target", target), ("note", note)),
    )


def _repo_card(state: Any | None, kind: ReferenceKind, target: str) -> ReferenceCard:
    project = getattr(state, "project", None)
    wanted = _target_id(kind, target)
    if project is None:
        return _fallback_card(kind, target)
    project_keys = {project.code, project.slug, project.repo_urn}
    if wanted not in project_keys and target not in project_keys:
        return _fallback_card(kind, target)
    return ReferenceCard(
        kind=kind,
        target=wanted,
        title=f"{kind} {project.code}",
        rows=_rows(project, ("code", "slug", "title", "status", "default_branch", "repo_urn")),
    )


def _phase_card(state: Any | None, kind: ReferenceKind, target: str) -> ReferenceCard:
    phase_id = _target_id(kind, target)
    phase = _lookup(getattr(state, "phases", None), phase_id)
    if phase is None:
        return _fallback_card(kind, target)
    return ReferenceCard(
        kind=kind,
        target=phase.id,
        title=f"phase {phase.id}",
        rows=_rows(
            phase,
            ("id", "title", "description", "status", "scope_id", "iter_ids", "audit_id"),
        ),
    )


def _iter_card(state: Any | None, kind: ReferenceKind, target: str) -> ReferenceCard:
    iter_id = _target_id(kind, target)
    it = _lookup(getattr(state, "iters", None), iter_id)
    if it is None:
        return _fallback_card(kind, target)
    return ReferenceCard(
        kind=kind,
        target=it.id,
        title=f"iter {it.id}",
        rows=_rows(
            it,
            ("id", "title", "description", "status", "phase_id", "wave_ids", "audit_id"),
        ),
    )


def _wave_card(state: Any | None, kind: ReferenceKind, target: str) -> ReferenceCard:
    wave_id = _target_id(kind, target)
    wave = _lookup(getattr(state, "waves", None), wave_id)
    if wave is None:
        return _fallback_card(kind, target)
    return ReferenceCard(
        kind=kind,
        target=wave.id,
        title=f"wave {wave.id}",
        rows=_rows(
            wave,
            (
                "id",
                "title",
                "description",
                "status",
                "iter_id",
                "agent_role",
                "effort_bucket",
                "deps",
                "blocks",
                "commit",
            ),
        ),
    )


def _hypothesis_card(state: Any | None, kind: ReferenceKind, target: str) -> ReferenceCard:
    hyp_id = _target_id(kind, target)
    hyp = _lookup(getattr(state, "hypotheses", None), hyp_id)
    if hyp is None:
        return _fallback_card(kind, target)
    return ReferenceCard(
        kind=kind,
        target=hyp.id,
        title=f"hypothesis {hyp.id}",
        rows=_rows(
            hyp,
            ("id", "title", "description", "status", "verdict", "metric", "audit_id"),
        ),
    )


def _decision_card(state: Any | None, kind: ReferenceKind, target: str) -> ReferenceCard:
    decision_id = _target_id(kind, target)
    decision = _lookup(getattr(state, "decisions", None), decision_id)
    if decision is None:
        return _fallback_card(kind, target)
    return ReferenceCard(
        kind=kind,
        target=decision.id,
        title=f"decision {decision.id}",
        rows=_rows(
            decision,
            ("id", "title", "description", "status", "scope_id", "rationale", "superseded_by"),
        ),
    )


def _audit_card(state: Any | None, kind: ReferenceKind, target: str) -> ReferenceCard:
    audit_id = _target_id(kind, target)
    audit = _lookup(getattr(state, "audits", None), audit_id)
    if audit is None:
        return _fallback_card(kind, target)
    return ReferenceCard(
        kind=kind,
        target=audit.id,
        title=f"audit {audit.id}",
        rows=_rows(
            audit,
            ("id", "kind", "status", "verdict", "scope_id", "report_artifact_id", "created_at"),
        ),
    )


def _artifact_card(state: Any | None, kind: ReferenceKind, target: str) -> ReferenceCard:
    artifact_id = _target_id(kind, target)
    artifact = _lookup(getattr(state, "artifacts", None), artifact_id)
    if artifact is None:
        return _fallback_card(kind, target)
    return ReferenceCard(
        kind=kind,
        target=artifact.id,
        title=f"artifact {artifact.id}",
        rows=_rows(artifact, ("id", "kind", "uri", "urn", "sha256", "size_bytes", "created_at")),
    )


def _memory_card(state: Any | None, kind: ReferenceKind, target: str) -> ReferenceCard:
    memory_id = _target_id(kind, target)
    memory = _lookup(getattr(state, "memory_index", None), memory_id)
    if memory is None:
        return _fallback_card(kind, target)
    return ReferenceCard(
        kind=kind,
        target=memory.id,
        title=f"memory {memory.id}",
        rows=_rows(
            memory,
            ("id", "summary", "confidence", "status", "tier", "scope_id", "review_due"),
        ),
    )


def _report_card(state: Any | None, kind: ReferenceKind, target: str) -> ReferenceCard:
    report_id = _target_id(kind, target)
    session = _lookup(getattr(state, "agent_sessions", None), report_id)
    if session is not None:
        return ReferenceCard(
            kind=kind,
            target=session.id,
            title=f"report {session.id}",
            rows=_rows(
                session,
                ("id", "role", "runtime", "scope_id", "status", "claimed_wave_ids", "summary"),
            ),
        )
    return _fallback_card(kind, target, note="store-backed report")


def _event_card(_state: Any | None, kind: ReferenceKind, target: str) -> ReferenceCard:
    return _fallback_card(kind, target, note="event log record")


def _profile_card(_state: Any | None, kind: ReferenceKind, target: str) -> ReferenceCard:
    return _fallback_card(kind, target, note="profile manifest")


def _spec_card(state: Any | None, kind: ReferenceKind, target: str) -> ReferenceCard:
    spec_id = _target_id(kind, target)
    rows = [("id", spec_id), ("kind", "spec")]
    if _lookup(getattr(state, "waves", None), spec_id) is not None:
        rows.append(("entity", "wave"))
    elif _lookup(getattr(state, "iters", None), spec_id) is not None:
        rows.append(("entity", "iter"))
    elif _lookup(getattr(state, "phases", None), spec_id) is not None:
        rows.append(("entity", "phase"))
    else:
        rows.append(("note", "spec row not loaded in state"))
    return ReferenceCard(kind=kind, target=spec_id, title=f"spec {spec_id}", rows=tuple(rows))


_CARD_BUILDERS = {
    "repo": _repo_card,
    "project": _repo_card,
    "phase": _phase_card,
    "iter": _iter_card,
    "wave": _wave_card,
    "hypothesis": _hypothesis_card,
    "decision": _decision_card,
    "audit": _audit_card,
    "artifact": _artifact_card,
    "memory": _memory_card,
    "report": _report_card,
    "event": _event_card,
    "profile": _profile_card,
    "spec": _spec_card,
}


def resolve_reference(state: Any | None, kind: ReferenceKind, target: str) -> ReferenceCard:
    """Resolve a typed reference into a modal card."""
    builder = _CARD_BUILDERS[kind]
    return builder(state, kind, target)


def reference_preview(state: Any | None, kind: ReferenceKind, target: str) -> str:
    """Return one-line hover preview for a typed reference."""
    card = resolve_reference(state, kind, target)
    row = card.rows[0] if card.rows else ("target", card.target)
    return f"{card.title} - {row[0]}: {row[1]}"


def tooltip_for_text(state: Any | None, text: str, *, max_refs: int = 3) -> str | None:
    """Return hover tooltip preview text for all refs in *text*."""
    refs = iter_refs(text)
    if not refs:
        return None
    lines = [reference_preview(state, ref.kind, ref.target) for ref in refs[:max_refs]]
    if len(refs) > max_refs:
        lines.append(f"+{len(refs) - max_refs} more")
    return "\n".join(lines)


class ReferenceModal(ModalScreen[None]):
    """Small per-reference modal opened from clickable refs or ``/goto``."""

    DEFAULT_CSS: ClassVar[str] = """
    ReferenceModal {
        align: center middle;
    }
    ReferenceModal > #reference-card {
        width: 78%;
        max-width: 110;
        height: auto;
        max-height: 80%;
        border: solid $accent;
        background: $surface;
        padding: 1 2;
    }
    ReferenceModal .reference-title {
        text-style: bold;
        color: $accent;
        height: 1;
    }
    ReferenceModal .reference-row {
        height: auto;
    }
    ReferenceModal .reference-hint {
        color: $text-muted;
        height: 1;
        margin-top: 1;
    }
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "close", "close", show=False),
        Binding("alt+left", "nav_back", "back", show=False),
        Binding("alt+right", "nav_forward", "forward", show=False),
    ]

    def __init__(self, card: ReferenceCard, *, state: Any | None = None) -> None:
        """Construct a reference modal."""
        super().__init__()
        self._card = card
        self._state = state

    def _render_mode(self) -> RenderMode:
        """Return the App's resolved render mode, defaulting when unbound.

        Reads :attr:`~eawf.surfaces.tui.app.EaApp.render_mode` so the title
        chrome glyph picks the right column. A bare harness whose host App
        carries no ``render_mode`` (a direct construction outside the full
        app) falls back to the shared default.

        Returns:
            The active render mode (``"unicode"`` / ``"ascii"``).
        """
        return getattr(self.app, "render_mode", DEFAULT_RENDER_MODE)

    def compose(self) -> ComposeResult:
        """Yield the reference card rows in the shared detail-chassis look.

        The title carries the overview chrome-glyph mnemonic (the same mark
        the detail card's ``overview`` tab uses) and the field rows are
        aligned ``label: value`` pairs, so a reference card reads as a
        single-tab slice of the detail chassis.
        """
        overview_glyph = sigils.chrome("overview", mode=self._render_mode())
        with VerticalScroll(id="reference-card"):
            yield Static(f"{overview_glyph} {self._card.title}", classes="reference-title")
            label_width = max((len(label) for label, _ in self._card.rows), default=0)
            for label, value in self._card.rows:
                padded = f"{label}:".ljust(label_width + 1)
                row = Static(
                    f"[$accent]{escape(padded)}[/] {linkify_text(value)}",
                    classes="reference-row",
                )
                row.tooltip = tooltip_for_text(self._state, value)
                yield row
            yield Static("[ Alt+Left/Alt+Right nav - Esc close ]", classes="reference-hint")

    def action_nav_back(self) -> None:
        """Navigate to previous reference target."""
        action = getattr(self.app, "action_reference_back", None)
        if callable(action):
            action()

    def action_nav_forward(self) -> None:
        """Navigate to next reference target."""
        action = getattr(self.app, "action_reference_forward", None)
        if callable(action):
            action()

    def action_close(self) -> None:
        """Dismiss the modal."""
        self.dismiss(None)


__all__ = [
    "ReferenceCard",
    "ReferenceModal",
    "ReferenceTarget",
    "reference_preview",
    "resolve_reference",
    "tooltip_for_text",
]
