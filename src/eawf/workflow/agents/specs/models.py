"""Typed role-contract model and the Markdown unwrapping its role bodies share.

:class:`RoleContract` is the typed projection of one role's invariants a
runtime adapter accepts when it opens a session.
:func:`_unwrap_markdown_soft_wraps` collapses the soft-wrapped lines of a
role body while keeping its block structure.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class _SpecModel(BaseModel):
    """Base spec model — forbids unknown keys (project rule 2)."""

    model_config = ConfigDict(extra="forbid")


class RoleContract(_SpecModel):
    """Typed projection of role-level invariants for one dispatch.

    The runtime adapters accept a :class:`RoleContract` when a session is
    opened. The contract is a *projection*: it copies the canonical fields
    off :class:`~eawf.workflow.agents.specs.roles.RoleSpec` so a surface
    consumes one typed shape instead of re-walking the registry.

    Attributes:
        role: The role identifier (e.g. ``"executor"``). Bare ``role``
            here because the surrounding :class:`RoleContract` type
            already disambiguates per AGENTS naming rule 17.
        summary: One-sentence role description (mirrors
            :attr:`RoleSpec.summary`).
        system_prompt: The role contract Markdown — method, output
            contract, anti-patterns — rendered as the
            ``## Role contract`` section of the dispatch prompt.
        allowed_tools: Tools the role may invoke at dispatch time. The
            dispatcher feeds this into per-runtime tool grants
            (``settings.json`` for claude-code, ``allowed_tools`` on the
            SDK envelope, etc.).
        denied_tools: Tools the role MUST NOT invoke.
        model: Preferred model identifier, or ``None`` to inherit the
            dispatcher's default.
        memory: Whether the role retains memory across invocations.
        report_schema_ref: Typed-report store-kind reference for the
            role's ``agent_end`` reports.
        stop_conditions: Role-specific stop conditions surfaced under
            the ``## Stop conditions`` section. Empty when the role has
            no role-specific stop conditions beyond the defaults.
    """

    role: str
    summary: str
    system_prompt: str
    allowed_tools: list[str] = Field(default_factory=list)
    denied_tools: list[str] = Field(default_factory=list)
    model: str | None = None
    memory: bool = False
    report_schema_ref: str = Field(min_length=1)
    stop_conditions: list[str] = Field(default_factory=list)


def _unwrap_markdown_soft_wraps(markdown: str) -> str:
    """Collapse soft-wrapped Markdown lines while preserving block boundaries."""
    rendered: list[str] = []
    paragraph: str | None = None
    in_fenced_block = False

    def flush_paragraph() -> None:
        nonlocal paragraph
        if paragraph is not None:
            rendered.append(paragraph.rstrip())
            paragraph = None

    for line in markdown.rstrip("\n").splitlines():
        stripped = line.strip()
        starts_fence = stripped.startswith("```") or stripped.startswith("~~~")

        if in_fenced_block:
            rendered.append(line.rstrip())
            if starts_fence:
                in_fenced_block = False
            continue

        if starts_fence:
            flush_paragraph()
            rendered.append(line.rstrip())
            in_fenced_block = True
            continue

        if not stripped:
            flush_paragraph()
            rendered.append("")
            continue

        if paragraph is None:
            paragraph = line.rstrip()
            continue

        if _starts_markdown_block(line):
            flush_paragraph()
            paragraph = line.rstrip()
            continue

        paragraph = f"{paragraph.rstrip()} {stripped}"

    flush_paragraph()
    return "\n".join(rendered)


def _starts_markdown_block(line: str) -> bool:
    """Return ``True`` when ``line`` starts a Markdown block."""
    stripped = line.lstrip()
    if line.startswith("    "):
        return True
    if stripped.startswith(("#", "> ", "- ", "* ", "+ ", "|")):
        return True
    if stripped in {"---", "***", "___"}:
        return True
    prefix, separator, _ = stripped.partition(". ")
    return bool(separator) and prefix.isdecimal()


__all__ = ["RoleContract"]
