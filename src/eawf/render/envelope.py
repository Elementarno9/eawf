"""Render-output envelope (JSON ⇄ markdown) for tool/agent I/O.

Eä commands and skills exchange a structured envelope with three parts:

- ``header`` — typed :class:`EnvelopeHeader` carrying ``skill``, ``scope_id``,
  ``session``, ``started_at``/``finished_at``, ``status``, and
  ``instrument_probe``. Frozen at Phase 4 W01.
- ``body`` — free-form payload. The payload is either a markdown string
  (legacy/raw passthrough) or a typed Pydantic body model (one of the 17
  per-skill bodies under :mod:`eawf.skills.bodies`). The wire-form keeps
  the JSON serialisation byte-stable across either shape.
- ``footer`` — typed :class:`EnvelopeFooter` with ``persisted_artifacts``,
  ``persisted_store_records``, ``state_mutations``, ``evidence_refs``,
  ``next_valid_actions``, ``warnings``, and the optional
  ``repair_commands`` (mandatory for ``status in {blocked, failed}``).

The markdown wire-form interleaves a YAML frontmatter block (header), the
raw body, and an HTML comment block carrying the footer YAML so the
markdown is renderable as-is by any markdown viewer that ignores HTML
comments while still being losslessly recoverable. ``yaml.safe_dump`` is
called with ``sort_keys=True`` so the rendering is deterministic.

The two helpers — :func:`to_markdown` and :func:`from_markdown` — must be
exact inverses for any envelope built from JSON-safe values:
``from_markdown(to_markdown(env)) == env`` and
``to_markdown(env) == to_markdown(from_markdown(to_markdown(env)))``.

Back-compat. Phase 0-3 callers (and the doctor smoke check) pass raw
``dict`` objects to ``OutputEnvelope(header=..., footer=...)``. Pydantic
v2's :meth:`model_validate` adapts those dicts into the typed
:class:`EnvelopeHeader`/:class:`EnvelopeFooter` automatically — no
caller-side change is required for the dict shape that matched the
frozen field set. Callers that previously stuffed arbitrary keys into
``header``/``footer`` will surface a :class:`pydantic.ValidationError`
because both models enforce ``extra="forbid"``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

# Frozen status enum for the envelope header — the single source of truth
# for the closed five-value set. Every skill's terminal status is one of
# these; the set is deliberately closed and MUST NOT grow (a new status
# would silently break the runtime-adapter status projections and the
# byte-stable envelope round-trip). ``partial`` is the fifth and final
# member; ``eawf.runtimes.plugin_manifest`` re-exports this literal rather
# than redefining it so the freeze stays single-sourced.
EnvelopeStatus = Literal["ok", "needs_user", "blocked", "failed", "partial"]

# Canonical builtin skill names. Workspace/user overlays may also emit
# envelopes, so ``SkillName`` is intentionally open while this tuple preserves
# deterministic builtin ordering for CLI tables and plugin rendering.
# The eleven core/meta + /blitz skills are followed by the six
# skill-surface bodies.
CANONICAL_SKILL_NAMES: tuple[str, ...] = (
    "/research",
    "/prep",
    "/audit",
    "/ship",
    "/review",
    "/polish",
    "/init",
    "/roadmap",
    "/differentiate",
    "/flow",
    "/blitz",
    "/coauthor",
    "/memory",
    "/agent-dispatch",
    "/compress",
    "/wave-spec",
    "/security-review",
)
SkillName = str

# Per-instrument probe value. Mirrors the design spec §3.1 envelope shape.
InstrumentStatus = Literal["ok", "missing", "degraded"]


class EnvelopeWarning(BaseModel):
    """Single warning entry under :attr:`EnvelopeFooter.warnings`.

    Per `docs/architecture/envelope.md` footer block. ``code`` is a short ID
    (``instrument_missing``, ``hook_blocked``, …); ``detail`` is a
    human-readable sentence.
    """

    model_config = ConfigDict(extra="forbid")

    code: str
    detail: str


class EnvelopeHeader(BaseModel):
    """Typed envelope header.

    Frozen at Phase 4 W01 per design spec §3.1. All Phase 4 W02-W07
    skills populate this directly; pre-W01 callers (e.g. the doctor smoke
    check) get implicit coercion via :meth:`OutputEnvelope.model_validate`.

    Attributes:
        skill: Frozen literal of the skill that produced the envelope.
        scope_id: Eä URN for the active state scope.
        session: Eä URN for the agent session that produced the envelope.
        started_at: When the skill began running.
        finished_at: When the skill finished (must be ``>= started_at``).
        status: Terminal status — one of the five envelope statuses.
        instrument_probe: Per-tool probe map (e.g. ``{"git": "ok"}``).
    """

    model_config = ConfigDict(extra="forbid")

    skill: SkillName
    scope_id: str
    session: str
    started_at: datetime
    finished_at: datetime
    status: EnvelopeStatus
    instrument_probe: dict[str, InstrumentStatus] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _finished_after_started(self) -> EnvelopeHeader:
        if self.finished_at < self.started_at:
            raise ValueError(
                f"finished_at must be >= started_at; got "
                f"finished_at={self.finished_at.isoformat()} < "
                f"started_at={self.started_at.isoformat()}"
            )
        return self


class EnvelopeFooter(BaseModel):
    """Typed envelope footer.

    Frozen at Phase 4 W01 per design spec §3.1. ``repair_commands`` is
    optional in the schema but the strict validator requires it for
    ``status in {blocked, failed}``.

    Attributes:
        persisted_artifacts: URNs of artifacts the skill persisted.
        persisted_store_records: URNs of store records (events, research
            briefs, etc.) appended during the run.
        state_mutations: JSONPath-ish strings naming each state mutation.
        evidence_refs: URNs of supporting evidence (commits, audits, …).
        next_valid_actions: CLI command strings the user/agent can run next.
        warnings: List of :class:`EnvelopeWarning` entries.
        repair_commands: When ``header.status in {blocked, failed}`` this
            is a non-empty list of CLI commands the agent should run to
            recover. Optional for the other three statuses.
    """

    model_config = ConfigDict(extra="forbid")

    persisted_artifacts: list[str] = Field(default_factory=list)
    persisted_store_records: list[str] = Field(default_factory=list)
    state_mutations: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    next_valid_actions: list[str] = Field(default_factory=list)
    warnings: list[EnvelopeWarning] = Field(default_factory=list)
    repair_commands: list[str] | None = None


# Body shape: either a free-form markdown string (Phase 0-3 raw bodies) or
# any of the typed body models from :mod:`eawf.skills.bodies`. We use
# ``dict[str, Any]`` (rather than a discriminated union) for the typed
# branch because skills can attach arbitrary skill-specific bodies; the
# CLI adapter validates the dict against the named body model when
# wiring up Phase 4 W02/W03 skills.
EnvelopeBody = str | dict[str, Any]


class OutputEnvelope(BaseModel):
    """Three-part envelope for skill/agent output.

    Attributes:
        header: Typed :class:`EnvelopeHeader`.
        body: Markdown string or typed body dict per :mod:`eawf.skills.bodies`.
        footer: Typed :class:`EnvelopeFooter`.
    """

    model_config = ConfigDict(extra="forbid")

    header: EnvelopeHeader
    body: EnvelopeBody
    footer: EnvelopeFooter


# Markers that frame the YAML frontmatter and the footer comment in the
# markdown wire-form. Kept as module-level constants so tests can grep
# for them without re-deriving the string literal.
_FRONTMATTER_FENCE: str = "---"
_FOOTER_OPEN: str = "<!-- eawf:footer"
_FOOTER_CLOSE: str = "-->"

# Sentinel marker used by the markdown wire-form when the body is a typed
# dict rather than a raw string. The body block is wrapped in an HTML
# comment so the YAML payload is unambiguous on the parse side.
_BODY_DICT_OPEN: str = "<!-- eawf:body"
_BODY_DICT_CLOSE: str = "-->"


def _dump_yaml(data: dict[str, Any]) -> str:
    """Deterministic YAML dump used for header/footer/typed-body blocks.

    ``sort_keys=True`` is required for the byte-stable round-trip; we also
    set ``default_flow_style=False`` so nested containers render as block
    YAML (the more readable form).
    """
    return yaml.safe_dump(
        data,
        sort_keys=True,
        default_flow_style=False,
        allow_unicode=True,
    )


def _load_yaml(text: str) -> dict[str, Any]:
    """Load YAML and require a mapping at the top level.

    The envelope contract states header/footer are dicts. ``yaml.safe_load``
    happily returns ``None`` for empty input or a list/scalar for malformed
    input — we coerce ``None`` to an empty dict and reject anything else.
    """
    parsed = yaml.safe_load(text) if text.strip() else {}
    if parsed is None:
        return {}
    if not isinstance(parsed, dict):
        raise ValueError(f"expected YAML mapping for header/footer; got {type(parsed).__name__}")
    return parsed


def _header_to_dict(header: EnvelopeHeader) -> dict[str, Any]:
    """Dump the header to a dict suitable for ``yaml.safe_dump``.

    Pydantic v2 emits ``datetime`` instances as ``datetime`` objects when
    ``mode="python"``; we use ``mode="json"`` so the dump is YAML-safe
    (ISO-8601 strings) and the round-trip is byte-stable.
    """
    return header.model_dump(mode="json")


def _footer_to_dict(footer: EnvelopeFooter) -> dict[str, Any]:
    """Dump the footer to a dict suitable for ``yaml.safe_dump``.

    ``exclude_none=True`` keeps the YAML compact: when ``repair_commands``
    is unset (the common ok/needs_user/partial case), the key is omitted
    rather than emitted as ``null``. The strict validator still enforces
    presence on the blocked/failed paths.
    """
    return footer.model_dump(mode="json", exclude_none=True)


def to_markdown(env: OutputEnvelope) -> str:
    """Serialise *env* into the markdown wire-form.

    Layout (string body)::

        ---
        <yaml.safe_dump(header)>
        ---
        <body string>
        <!-- eawf:footer
        <yaml.safe_dump(footer)>
        -->

    Layout (dict body)::

        ---
        <yaml.safe_dump(header)>
        ---
        <!-- eawf:body
        <yaml.safe_dump(body)>
        -->
        <!-- eawf:footer
        <yaml.safe_dump(footer)>
        -->

    The body is interpolated verbatim — including its leading and
    trailing whitespace — so a string-bodied round-trip preserves the
    user-visible rendering byte-for-byte. Dict bodies are emitted in
    sorted-key YAML for the same byte-stability guarantee.
    """
    header_yaml = _dump_yaml(_header_to_dict(env.header))
    footer_yaml = _dump_yaml(_footer_to_dict(env.footer))
    if isinstance(env.body, str):
        body_block = env.body
    else:
        body_yaml = _dump_yaml(env.body)
        body_block = f"{_BODY_DICT_OPEN}\n{body_yaml}{_BODY_DICT_CLOSE}\n"
    # ``yaml.safe_dump`` always terminates output with ``\n``; we rely on
    # that so the fence/marker layout below stays well-formed without
    # extra rstrip dance. The body is sandwiched with explicit ``\n``
    # markers we strip back out in :func:`from_markdown`.
    return (
        f"{_FRONTMATTER_FENCE}\n"
        f"{header_yaml}"
        f"{_FRONTMATTER_FENCE}\n"
        f"{body_block}"
        f"{_FOOTER_OPEN}\n"
        f"{footer_yaml}"
        f"{_FOOTER_CLOSE}\n"
    )


def from_markdown(text: str) -> OutputEnvelope:
    """Parse the markdown wire-form back into an :class:`OutputEnvelope`.

    Args:
        text: Markdown produced by :func:`to_markdown` (or hand-authored
            in the same shape).

    Raises:
        ValueError: ``text`` does not start with the frontmatter fence,
            is missing the closing fence, or lacks the footer comment
            block. The CLI handler maps these to :class:`ValidationFailed`.
    """
    open_fence = f"{_FRONTMATTER_FENCE}\n"
    if not text.startswith(open_fence):
        raise ValueError(f"missing frontmatter fence: input must start with {open_fence!r}")

    after_open = text[len(open_fence) :]
    close_marker = f"\n{_FRONTMATTER_FENCE}\n"
    close_idx = after_open.find(close_marker)
    if close_idx < 0:
        raise ValueError(f"missing closing frontmatter fence: expected {close_marker!r}")
    # ``close_idx`` is the index of the leading ``\n`` of the closing fence;
    # the YAML payload runs from start-of-after_open to that ``\n`` (the
    # ``\n`` belongs to ``yaml.safe_dump``'s trailing newline so we keep it).
    header_yaml = after_open[: close_idx + 1]
    after_header = after_open[close_idx + len(close_marker) :]

    footer_open_marker = f"{_FOOTER_OPEN}\n"
    footer_idx = after_header.rfind(footer_open_marker)
    if footer_idx < 0:
        raise ValueError(f"missing footer comment open marker: expected {footer_open_marker!r}")

    body_region = after_header[:footer_idx]
    after_footer_open = after_header[footer_idx + len(footer_open_marker) :]
    footer_close_marker = f"{_FOOTER_CLOSE}\n"
    if not after_footer_open.endswith(footer_close_marker):
        # Tolerate the no-trailing-newline variant for hand-authored input.
        if not after_footer_open.endswith(_FOOTER_CLOSE):
            raise ValueError(f"missing footer comment close marker: expected {_FOOTER_CLOSE!r}")
        footer_yaml = after_footer_open[: -len(_FOOTER_CLOSE)]
    else:
        footer_yaml = after_footer_open[: -len(footer_close_marker)]

    # Body branch: detect the dict-body comment block. The block is the
    # only thing in ``body_region``, so a strict ``startswith`` /
    # ``endswith`` check disambiguates without false positives.
    body_dict_open_marker = f"{_BODY_DICT_OPEN}\n"
    body_dict_close_marker = f"{_BODY_DICT_CLOSE}\n"
    body: EnvelopeBody
    if body_region.startswith(body_dict_open_marker) and body_region.endswith(
        body_dict_close_marker
    ):
        body_yaml = body_region[len(body_dict_open_marker) : -len(body_dict_close_marker)]
        body = _load_yaml(body_yaml)
    else:
        body = body_region

    header = _load_yaml(header_yaml)
    footer = _load_yaml(footer_yaml)
    return OutputEnvelope.model_validate({"header": header, "body": body, "footer": footer})


__all__ = [
    "CANONICAL_SKILL_NAMES",
    "EnvelopeBody",
    "EnvelopeFooter",
    "EnvelopeHeader",
    "EnvelopeStatus",
    "EnvelopeWarning",
    "InstrumentStatus",
    "OutputEnvelope",
    "SkillName",
    "from_markdown",
    "to_markdown",
]
