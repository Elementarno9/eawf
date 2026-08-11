"""Render AGENTS.md from a composed profile, preserving hand-edits in the file.

Per ``docs/policy/agents-claude-md.md``:

- Take a :class:`~eawf.platform.profiles.models.ComposedProfile` whose ``render_blocks``
  list declares the regions that should appear on disk.
- Filter to ``target == "AGENTS.md"`` blocks (other targets — ``.claude/...``
  skill/agent files — are handled by sibling renderers in W05+), then split
  them by :attr:`RenderBlock.tier` into Zone 1 (the always-on ``tier0`` layer)
  and Zone 2 (the lazy ``reference`` layer). Zone 1 renders first, Zone 2
  second; each zone is introduced by a managed boundary marker that is emitted
  only when that tier has at least one block, so a tier0-free profile never
  carries an empty Zone-1 region.
- For each block: render its body via Jinja2 against the bundled
  ``AGENTS.md.j2`` template (prose ``body_template`` verbatim, or a fixed
  ``Rationale``/``Mechanism``/``Verification`` layout for structured triad
  blocks). A block declaring ``placement: reference`` does NOT put that body in
  the managed file: the body is written to ``docs/rules/<id>.md`` — inside the
  same ``BEGIN``/``END`` managed-region markers the managed file uses, so the
  expansion is drift-checked exactly like an inline region — and the managed
  region gets one line naming the obligation and linking the expansion, which
  is how the always-loaded file stays inside a consumer's byte cap without any
  rule being deleted. Then call
  :func:`~eawf.surfaces.render.regions.replace_region` so an existing block with the
  same id is replaced in-place and a brand-new block is appended.
  Anything *outside* a managed region (hand-written paragraphs above, below,
  or between blocks) round-trips byte-stably — that is the contract that
  makes "re-render is safe" hold.
- Atomically write the new file content via tempfile + ``os.replace`` under a
  portalock — same discipline as :mod:`eawf.kernel.state.writer`.
- Update a :class:`~eawf.surfaces.render.manifest.Manifest` so drift detection
  (:mod:`eawf.surfaces.render.drift`) and ``eawf doctor`` know the renderer's view of
  what's on disk. Every managed region gets a row — the ones inside the managed
  file AND the one inside each ``docs/rules/<id>.md`` expansion — so a hand-edit
  or deletion of a moved rule is caught by the same detector, keeping the
  "do not hand-edit" banner an enforced contract rather than a request. The
  caller is responsible for persisting the manifest via
  :func:`~eawf.surfaces.render.manifest.save_atomic` — keeping the renderer pure means
  callers can batch multiple targets into a single manifest write.

The optional ``state``/``decisions_scope_id`` kwargs on
:func:`render_agents_md` plumb typed :class:`~eawf.kernel.state.models.Decision`
records into a managed ``decisions`` region (id ``DECISIONS_REGION_ID``),
so the on-disk Decisions section is driven by state.json rather than
hardcoded prose in a YAML profile body. The body text is produced by
:func:`render_decisions_section` — a pure function the caller can also
invoke directly when it wants the markdown without the file-writing side
effect (e.g. ``eawf decisions show``-style read-only renders).

Public API::

    RenderResult                    # dataclass: target + per-region status
    BlockByteSpan                   # dataclass: one block's UTF-8 byte span
    ByteCapReport                   # dataclass: byte total + dropped block ids
    lint_entity_title(title) -> list[str]
    block_byte_spans(text) -> list[BlockByteSpan]
    measure_agents_md_byte_cap(text, *, cap) -> ByteCapReport
    reference_file_path(root, block_id) -> Path
    render_reference_line(block) -> str
    render_reference_document(block, body) -> str
    render_decisions_section(decisions, *, scope_id) -> str
    render_agents_md(
        composed, target, manifest, *, generator, state, decisions_scope_id
    ) -> tuple[RenderResult, Manifest]
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from eawf.kernel.spec.intent import IntentBrief
from eawf.kernel.state.models import Decision, State
from eawf.platform.profiles.models import ComposedProfile, RenderBlock
from eawf.platform.render_block import RULE_REFERENCE_DIR
from eawf.surfaces.render import regions
from eawf.surfaces.render._atomic import atomic_write_text
from eawf.surfaces.render.manifest import Manifest, ManifestEntry

logger = logging.getLogger(__name__)


_TARGET_FILENAME: str = "AGENTS.md"
_TEMPLATE_NAME: str = "AGENTS.md.j2"
_TEMPLATES_PACKAGE: str = "eawf.platform.templates"

#: Managed-region id marking the start of Zone 1 (the always-on ``tier0``
#: layer). Emitted only when at least one ``tier0`` block targets AGENTS.md so a
#: tier0-free profile never renders an empty Zone-1 region. Its body is a fixed
#: heading line so the boundary is human-scannable in the rendered file; being a
#: managed region keeps it off the unmanaged-hand-edit round-trip accounting.
ZONE_TIER0_REGION_ID: str = "zone-tier0"

#: Managed-region id marking the start of Zone 2 (the lazy ``reference`` layer).
#: Emitted only when at least one ``reference`` block targets AGENTS.md.
ZONE_REFERENCE_REGION_ID: str = "zone-reference"

#: Version stamped on the two zone-boundary marker regions. Bumped only when the
#: rendered boundary body itself changes shape.
ZONE_REGION_VERSION: str = "1.0"

#: Fixed body of the Zone-1 boundary region.
ZONE_TIER0_BODY: str = "<!-- Zone 1: always-on (tier0) -->"

#: Fixed body of the Zone-2 boundary region.
ZONE_REFERENCE_BODY: str = "<!-- Zone 2: reference (lazy) -->"

#: Managed-region id under which the typed-Decisions section is rendered.
#: Stable so re-renders update in place (and so hand-edits outside the BEGIN…END
#: span round-trip byte-stably, per the same contract as profile render_blocks).
DECISIONS_REGION_ID: str = "decisions"

#: Version stamped on the ``decisions`` managed region's BEGIN marker. Bumped
#: only when the rendered body format itself changes — content-only churn (a
#: new D## row, an edited rationale) keeps the same version and surfaces in the
#: marker's ``hash=`` field instead.
DECISIONS_REGION_VERSION: str = "1.0"

#: Hard upper bound on an entity ``title`` length, mirroring the
#: ``max_length=72`` :class:`pydantic.Field` constraint on every
#: ``State`` entity (``Phase`` / ``Iter`` / ``Wave`` / ``Decision`` /
#: ``Hypothesis`` / ``BacklogItem`` / ``Incident``). Kept here so the
#: style backstop (:func:`lint_entity_title`) flags an over-cap title at
#: the same threshold the model rejects it.
ENTITY_TITLE_MAX: int = 72


@dataclass(frozen=True)
class RenderResult:
    """Summary of one :func:`render_agents_md` call.

    Attributes:
        target: Path the renderer wrote (or would have written, when no work
            was needed).
        regions_added: Region ids that did not exist on disk before the call
            and were appended to the file.
        regions_updated: Region ids whose body or version changed compared to
            what was on disk.
        regions_unchanged: Region ids whose body+version exactly matched the
            on-disk content. The renderer still rewrites the file (even when
            every region is unchanged) so the manifest's ``generated_at`` stays
            in sync; this list lets callers report a no-op cleanly.
        hand_edits_preserved: ``True`` when the on-disk file had any content
            outside managed regions (hand-written sections above/below/between
            blocks). ``False`` when the file was empty or had no unmanaged
            content. Useful for tooling messages like "preserved 3 hand-edited
            paragraphs".
    """

    target: Path
    regions_added: list[str] = field(default_factory=list)
    regions_updated: list[str] = field(default_factory=list)
    regions_unchanged: list[str] = field(default_factory=list)
    hand_edits_preserved: bool = False


def _load_environment() -> Environment:
    """Load a Jinja2 environment rooted at the bundled templates directory.

    Uses :func:`importlib.resources.files` so the renderer works equally
    from a wheel install, an editable install, and the source tree (mirrors
    :mod:`eawf.platform.profiles.loader`).
    """
    templates_dir = files(_TEMPLATES_PACKAGE)
    # Materialise to a filesystem path. importlib.resources.files() returns a
    # MultiplexedPath at module level — for a regular wheel install it is
    # already a real Path; ``str()`` is the documented way to get its location.
    templates_path = str(templates_dir)
    env = Environment(
        loader=FileSystemLoader(templates_path),
        undefined=StrictUndefined,
        keep_trailing_newline=False,
        # autoescape stays off — markdown output, not HTML; templates are trusted (bundled).
        autoescape=False,
    )
    return env


def _render_block_body(env: Environment, block: RenderBlock, composed: ComposedProfile) -> str:
    """Render one ``RenderBlock`` to its body string via the AGENTS.md template.

    Prose blocks (non-empty ``body_template``) emit their template verbatim;
    structured blocks (the ``rationale``/``mechanism``/``verification`` triad)
    emit a fixed ``Rationale``/``Mechanism``/``Verification`` sub-heading
    layout. The template branches on :attr:`RenderBlock.is_structured` — see
    ``src/eawf/platform/templates/AGENTS.md.j2`` for the surface policy.
    """
    template = env.get_template(_TEMPLATE_NAME)
    return template.render(block=block, composed=composed)


def _reference_dir(root: Path) -> Path:
    """Return the directory holding every reference-placed block's expansion."""
    return Path(root).joinpath(*RULE_REFERENCE_DIR.split("/"))


def reference_file_path(root: Path, block_id: str) -> Path:
    """Return the path holding the full body of reference-placed *block_id*.

    *root* is the directory containing the managed file (the workspace anchor
    for ``AGENTS.md``); the expansion lands at
    ``<root>/docs/rules/<block_id>.md`` per
    :data:`~eawf.platform.render_block.RULE_REFERENCE_DIR`.
    """
    return _reference_dir(root) / f"{block_id}.md"


def render_reference_line(block: RenderBlock) -> str:
    """Return the one-line managed-file body for a reference-placed *block*.

    The line names the obligation (the block's ``summary``) and the path to the
    full text, keyed by block id -- the same id the rules list already
    cross-references -- so a reader who needs the expansion knows both what it
    binds them to and where to read it.

    Raises:
        ValueError: *block* is not reference-placed, so it belongs in the
            managed file verbatim; or it is reference-placed but carries no
            summary, so there is no obligation to name on the line.
    """
    if not block.is_reference_placed:
        raise ValueError(f"render_block is not reference-placed: {block.id!r}")
    if block.summary is None:
        raise ValueError(f"reference-placed render_block has no summary: {block.id!r}")
    path = f"{RULE_REFERENCE_DIR}/{block.id}.md"
    return f"`{block.id}` — {block.summary.strip()} Full text: [{path}]({path})"


def _reference_region_body(block: RenderBlock, body: str) -> str:
    """Return the managed-region body of a reference block's expansion file.

    This is the drift-checked payload of ``docs/rules/<id>.md``: the block id
    as a title, the one-sentence obligation, and the block's rendered *body*
    verbatim. The generated-file banner sits *outside* the region so the
    recorded hash covers the rule text alone.

    Args:
        block: The reference-placed block being expanded.
        body: The block body as rendered for the managed file.

    Raises:
        ValueError: *block* is not reference-placed, or is reference-placed
            but carries no summary.
    """
    if not block.is_reference_placed:
        raise ValueError(f"render_block is not reference-placed: {block.id!r}")
    if block.summary is None:
        raise ValueError(f"reference-placed render_block has no summary: {block.id!r}")
    return f"# `{block.id}`\n\n{block.summary.strip()}\n\n{body.strip()}"


def render_reference_document(block: RenderBlock, body: str) -> str:
    """Return the full ``docs/rules/<id>.md`` content for a reference block.

    The document leads with a generated-file banner (hand-edits here are lost
    on the next render), then wraps :func:`reference_region_body` in the same
    ``BEGIN``/``END`` managed-region markers the managed file uses. The markers
    are what make the banner enforceable: the region flows through
    :func:`~eawf.surfaces.render.drift.detect_drift` like any inline block, so a
    hand-edit or deletion of a moved rule surfaces in ``eawf doctor`` instead of
    passing unnoticed. Nothing from the managed file is dropped, it only moves.

    Args:
        block: The reference-placed block being expanded.
        body: The block body as rendered for the managed file.

    Raises:
        ValueError: *block* is not reference-placed, or is reference-placed
            but carries no summary.
    """
    region_body = _reference_region_body(block, body)
    banner = (
        f"<!-- Generated from the eawf profile render block `{block.id}`. "
        f"Do not hand-edit: re-run `eawf sync`. -->"
    )
    marked = regions.replace_region("", id=block.id, version=block.version, body=region_body)
    return f"{banner}\n\n{marked}\n"


@dataclass
class _RegionEmitter:
    """Accumulate managed-region writes against a growing document string.

    One :meth:`emit` call inserts-or-replaces a single managed region (a zone
    boundary marker, a render block, or the typed Decisions injection) via
    :func:`~eawf.surfaces.render.regions.replace_region`, classifies it against
    the pre-render snapshot in :attr:`existing` as added / updated / unchanged,
    and records its body + version for downstream manifest emission. Pulling
    this state into a small object keeps :func:`render_agents_md` flat: the
    caller drives the tier partition and the emitter owns the bookkeeping.

    Attributes:
        text: The document under construction; each :meth:`emit` reassigns it.
        existing: Pre-render regions keyed by id, used to classify deltas.
        added: Region ids absent before this render.
        updated: Region ids whose body or version changed.
        unchanged: Region ids whose body+version matched the prior render.
        bodies: Rendered body per region id (manifest hash input).
        versions: Emitted version per region id (manifest version field).
    """

    text: str
    existing: dict[str, regions.Region]
    added: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    bodies: dict[str, str] = field(default_factory=dict)
    versions: dict[str, str] = field(default_factory=dict)

    def emit(self, region_id: str, version: str, body: str) -> None:
        """Insert-or-replace region *region_id* and record its delta class.

        Args:
            region_id: Managed-region id to write.
            version: ``<major>.<minor>`` version stamped on the BEGIN marker.
            body: Rendered region body (no markers, no boundary newlines).
        """
        self.bodies[region_id] = body
        self.versions[region_id] = version
        prev = self.existing.get(region_id)
        if prev is None:
            self.added.append(region_id)
        elif prev.body == body and prev.version == version:
            self.unchanged.append(region_id)
        else:
            self.updated.append(region_id)
        self.text = regions.replace_region(
            self.text,
            id=region_id,
            version=version,
            body=body,
        )


def render_decisions_section(
    decisions: dict[str, Decision] | None,
    *,
    scope_id: str | None = None,
) -> str:
    """Render the AGENTS.md Decisions section body from typed Decision records.

    The output is the *body* of the managed ``decisions`` region — caller is
    responsible for wrapping it with ``BEGIN/END`` markers (or letting
    :func:`render_agents_md` do that via the ``state`` kwarg).

    Format::

        ## Decisions

        ### D01: <summary>

        <rationale>

        Alternatives considered:

        - alt1
        - alt2

        ### D02: <summary>

        <rationale>

    Rows missing alternatives drop the "Alternatives considered:" line; the
    Nygard-ADR "Consequences:" block renders the same way (bulleted list,
    omitted when empty). Rows
    marked ``status=superseded`` or ``status=reversed`` carry a leading status
    badge on their heading so the reader knows the record is historical:

        ### D03 [superseded]: <summary>

    When *decisions* is empty (or after the optional ``scope_id`` filter
    leaves nothing) the function returns a stable ``## Decisions\\n\\nNone.``
    body so the rendered region stays in the file but signals emptiness.

    Args:
        decisions: Typed Decision records keyed by id (the
            :attr:`~eawf.kernel.state.models.State.decisions` mapping). ``None`` is
            treated as an empty dict for caller convenience (the state field
            defaults to ``{}`` but optional-typed sister fields sometimes
            arrive as ``None``).
        scope_id: When supplied, only Decisions whose
            :attr:`~eawf.kernel.state.models.Decision.scope_id` equals this value
            render. ``None`` (default) means "include all decisions" — useful
            for project-wide AGENTS.md emission where every Decision is in
            scope by definition.

    Returns:
        Markdown body (no managed-region markers, no trailing newline).
        Decisions are sorted lexicographically by id so the section is
        deterministic across renders.
    """
    pool: dict[str, Decision] = decisions or {}
    selected: list[Decision] = list(pool.values())
    if scope_id is not None:
        selected = [d for d in selected if d.scope_id == scope_id]
    selected.sort(key=lambda d: d.id)

    if not selected:
        logger.info(f"render_decisions_section scope_id={scope_id!r} count=0 result=empty-section")
        return "## Decisions\n\nNone."

    parts: list[str] = ["## Decisions"]
    for decision in selected:
        badge = ""
        if decision.status.value != "active":
            badge = f" [{decision.status.value}]"
        parts.append("")
        parts.append(f"### {decision.id}{badge}: {decision.title}")
        parts.append("")
        parts.append(decision.rationale.rstrip())
        if decision.alternatives:
            parts.append("")
            parts.append("Alternatives considered:")
            parts.append("")
            for alt in decision.alternatives:
                parts.append(f"- {alt}")
        if decision.consequences:
            parts.append("")
            parts.append("Consequences:")
            parts.append("")
            for consequence in decision.consequences:
                parts.append(f"- {consequence}")
    logger.info(f"render_decisions_section scope_id={scope_id!r} count={len(selected)} result=ok")
    return "\n".join(parts)


def render_intent_line(intent: IntentBrief | None) -> str:
    """Render an :class:`IntentBrief` as a single intent line, or ``""``.

    Emits the W24-audited :attr:`IntentBrief.problem` +
    :attr:`IntentBrief.desired_outcome` pair so consumers see the
    structured "current problem -> target state" split. A ``None``
    intent returns the empty string so the caller can interpolate the
    result unconditionally without needing a branching ``if`` around
    the line.

    Format::

        problem: <problem> -> desired_outcome: <desired_outcome>

    No trailing newline, no leading marker, so the caller controls
    list / row framing.

    Args:
        intent: The entity's :attr:`IntentBrief` (or ``None`` when the
            entity has not been wired with one).

    Returns:
        The rendered intent line, or ``""`` when *intent* is ``None``.
    """
    if intent is None:
        return ""
    return f"problem: {intent.problem} -> desired_outcome: {intent.desired_outcome}"


def lint_entity_title(title: str) -> list[str]:
    """Return human-readable violations of the entity-title naming rule.

    The entity-title naming rule (rendered into AGENTS.md by the
    ``entity-title-naming`` render block) asks for a ``title`` that is an
    imperative noun-phrase of at most :data:`ENTITY_TITLE_MAX` characters with
    no trailing period; the long-form purpose belongs in ``description``. This
    is the style backstop a reviewer (or a future title-authoring command) runs
    over a candidate title *before* it reaches the model — the
    ``max_length=72`` :class:`pydantic.Field` bound rejects an over-cap title at
    the ingestion boundary, but it does not catch a trailing period and it
    raises rather than collecting both failures, which is unhelpful when the
    caller wants to report every problem at once.

    Two failure modes are flagged, in declaration order:

    - **Over-cap** — ``len(title)`` exceeds :data:`ENTITY_TITLE_MAX`.
    - **Trailing period** — ``title`` ends with ``"."`` (after stripping
      trailing whitespace, so ``"foo. "`` is flagged too).

    A title that violates both modes yields both messages. An empty list means
    the title is clean.

    Args:
        title: Candidate entity title to check. Leading/trailing whitespace is
            considered part of the value (the model does not strip it), so a
            trailing-space-then-period still trips the trailing-period check.

    Returns:
        Zero or more violation messages, lowercase-led and period-free per the
        project error-phrasing convention, suitable for surfacing to a human
        author. Empty when *title* satisfies the rule.
    """
    violations: list[str] = []
    if len(title) > ENTITY_TITLE_MAX:
        violations.append(
            f"title exceeds {ENTITY_TITLE_MAX}-char cap: {len(title)} chars in {title!r}"
        )
    if title.rstrip().endswith("."):
        violations.append(f"title has a trailing period (titles are labels, not prose): {title!r}")
    return violations


#: Placeholder stems treated as "no real title" by
#: :func:`normalize_entity_title` so a description-derived candidate wins. The
#: model forbids an empty ``title`` (``min_length=1``), so a literally-empty
#: string never reaches this path through the state boundary; the set captures
#: the human placeholders an author leaves behind ("tbd", "todo", "...") that
#: are technically valid but carry no signal.
_TITLE_PLACEHOLDERS: frozenset[str] = frozenset({"", "tbd", "todo", "...", "title", "wip", "n/a"})


def _trim_title_to_cap(title: str) -> str:
    """Return *title* trimmed to :data:`ENTITY_TITLE_MAX` on a word boundary.

    A title already within the cap is returned unchanged. An over-cap title is
    cut to the last whole word that fits; when even the first word overflows the
    cap (no interior space to break on) the title is hard-sliced to the cap so
    the result always satisfies the bound.
    """
    if len(title) <= ENTITY_TITLE_MAX:
        return title
    window = title[:ENTITY_TITLE_MAX]
    cut = window.rfind(" ")
    if cut <= 0:
        return window.rstrip()
    return window[:cut].rstrip()


def _derive_title_from_description(description: str) -> str:
    """Return a candidate title built from *description*'s first clause.

    The first clause is the text up to the first sentence terminator
    (``"."``, ``"!"``, ``"?"``) or the whole string when none is present; the
    result is stripped of any trailing period and trimmed to the cap on a word
    boundary. An empty / whitespace-only description yields ``""`` so the caller
    can fall back.
    """
    head = description.strip()
    for terminator in (".", "!", "?"):
        idx = head.find(terminator)
        if idx != -1:
            head = head[:idx]
            break
    return _trim_title_to_cap(head.strip().rstrip("."))


def normalize_entity_title(title: str, description: str | None = None) -> str:
    """Return *title* normalized to the entity-title naming rule.

    Applies the three transforms the title-backfill tool needs, in order:

    1. **Derive from description** — when *title* is empty or a known
       placeholder (:data:`_TITLE_PLACEHOLDERS`, case-insensitive) and
       *description* carries prose, build a candidate from the description's
       first clause via :func:`_derive_title_from_description`.
    2. **Strip a trailing period** — titles are labels, not prose, so a
       trailing ``"."`` (after right-stripping whitespace) is removed.
    3. **Trim to the cap on a word boundary** — an over-:data:`ENTITY_TITLE_MAX`
       title is cut to the last whole word that fits.

    The function is pure and idempotent: a title already satisfying the rule is
    returned unchanged, and re-normalizing a normalized title is a no-op.

    Args:
        title: The current entity title.
        description: The entity's optional long-form description, used only as a
            fallback source when *title* is empty / a placeholder.

    Returns:
        The normalized title. May be empty only when both *title* is a
        placeholder and *description* yields no usable clause; the caller
        decides whether an empty result is actionable (the model rejects it at
        ingestion, so backfill leaves such an item unchanged and flags it).
    """
    candidate = title.strip()
    if candidate.casefold() in _TITLE_PLACEHOLDERS and description:
        derived = _derive_title_from_description(description)
        if derived:
            candidate = derived
    candidate = candidate.rstrip()
    while candidate.endswith("."):
        candidate = candidate[:-1].rstrip()
    return _trim_title_to_cap(candidate)


@dataclass(frozen=True)
class BlockByteSpan:
    """One managed render block's UTF-8 byte position in a rendered document.

    :func:`~eawf.surfaces.render.regions.find_regions` reports each region's span
    as Python *character* offsets. AGENTS.md carries non-ASCII prose (em-dashes,
    typographic quotes) so a character offset is NOT a byte offset, and a
    downstream reader that truncates at a *byte* cap (Codex's project-doc limit)
    needs byte positions. This dataclass carries the converted byte span.

    Attributes:
        id: Region id (the ``id=`` on the block's BEGIN marker).
        start_byte: UTF-8 byte offset where the block's BEGIN marker starts.
        end_byte: UTF-8 byte offset one past the block's END marker.
    """

    id: str
    start_byte: int
    end_byte: int


@dataclass(frozen=True)
class ByteCapReport:
    """Measurement of a rendered AGENTS.md against a byte cap.

    Attributes:
        total_bytes: UTF-8 byte size of the whole rendered document.
        cap: The byte cap the total was measured against.
        dropped_block_ids: Ordered ids of the managed render blocks whose BEGIN
            marker starts at or past :attr:`cap` — a reader that truncates the
            file at :attr:`cap` bytes never sees them.
    """

    total_bytes: int
    cap: int
    dropped_block_ids: list[str]

    @property
    def over_cap(self) -> bool:
        """Return ``True`` when the document exceeds its byte cap."""
        return self.total_bytes > self.cap


def block_byte_spans(text: str) -> list[BlockByteSpan]:
    """Return each managed render block's UTF-8 byte span in *text*, in order.

    The block boundaries come from
    :func:`~eawf.surfaces.render.regions.find_regions`; this helper converts
    their character-offset spans to byte offsets so a caller can compare a
    block's start position against a byte cap. Pure — no I/O, no globals.

    Raises:
        RegionParseError: *text* has malformed managed-region markers.
    """
    spans: list[BlockByteSpan] = []
    for region in regions.find_regions(text):
        start_char, end_char = region.span
        start_byte = len(text[:start_char].encode("utf-8"))
        end_byte = len(text[:end_char].encode("utf-8"))
        spans.append(BlockByteSpan(id=region.id, start_byte=start_byte, end_byte=end_byte))
    return spans


def measure_agents_md_byte_cap(text: str, *, cap: int) -> ByteCapReport:
    """Measure *text*'s UTF-8 byte size against *cap* and name dropped blocks.

    A block is "dropped" when its BEGIN marker starts at or past *cap*: a
    consumer that reads only the first *cap* bytes (Codex silently truncates a
    project doc at its byte limit) never sees that block, so its guidance is
    lost. Pure — no I/O, no globals; the caller supplies *text* and *cap*.

    Args:
        text: The fully rendered AGENTS.md document.
        cap: Positive byte budget to measure against.

    Raises:
        ValueError: *cap* is not positive.
        RegionParseError: *text* has malformed managed-region markers.
    """
    if cap <= 0:
        raise ValueError(f"cap must be positive: {cap!r}")
    total = len(text.encode("utf-8"))
    dropped = [span.id for span in block_byte_spans(text) if span.start_byte >= cap]
    return ByteCapReport(total_bytes=total, cap=cap, dropped_block_ids=dropped)


def _has_unmanaged_content(text: str) -> bool:
    """Return ``True`` when *text* has bytes outside any managed region.

    Used to populate :attr:`RenderResult.hand_edits_preserved`. We strip the
    BEGIN…END span of every parsed region from a copy of *text* and check
    whether non-whitespace remains.
    """
    if text == "":
        return False
    parsed = regions.find_regions(text)
    if not parsed:
        return text.strip() != ""
    # Walk regions in order, accumulate unmanaged spans.
    leftover_chunks: list[str] = []
    cursor = 0
    for region in parsed:
        start, end = region.span
        leftover_chunks.append(text[cursor:start])
        cursor = end
    leftover_chunks.append(text[cursor:])
    return any(chunk.strip() != "" for chunk in leftover_chunks)


@dataclass(frozen=True)
class _ReferenceEmission:
    """One reference-placed block's on-disk expansion, for manifest emission.

    Attributes:
        path: The ``docs/rules/<id>.md`` file the expansion was written to.
        region_id: Managed-region id inside that file (the block id).
        version: Version stamped on the expansion's BEGIN marker.
        body: The region body whose hash the manifest row records.
    """

    path: Path
    region_id: str
    version: str
    body: str


def _emit_block(
    *,
    emitter: _RegionEmitter,
    env: Environment,
    composed: ComposedProfile,
    block: RenderBlock,
    root: Path,
) -> _ReferenceEmission | None:
    """Emit one render block: full body inline, or a line plus a sibling file.

    A ``placement: reference`` block keeps its full body out of the managed
    file (which a consumer truncates at a byte cap) by writing it under *root*
    at ``docs/rules/<id>.md`` and leaving one line that names the obligation and
    links the expansion. Nothing is dropped -- it moves.

    Args:
        emitter: Accumulator owning the managed-region writes.
        env: Jinja2 environment the block body renders through.
        composed: Full composed profile, passed into the block template.
        block: The block to emit.
        root: Directory holding the managed file, parent of ``docs/rules/``.

    Returns:
        The :class:`_ReferenceEmission` describing the expansion file for a
        reference-placed block, so the caller can record a manifest row for it;
        ``None`` for a root-placed block, whose bytes live in the managed file
        and are already accounted for by *emitter*.
    """
    body = _render_block_body(env, block, composed)
    if not block.is_reference_placed:
        emitter.emit(block.id, block.version, body)
        return None
    path = reference_file_path(root, block.id)
    atomic_write_text(path, render_reference_document(block, body))
    emitter.emit(block.id, block.version, render_reference_line(block))
    return _ReferenceEmission(
        path=path,
        region_id=block.id,
        version=block.version,
        body=_reference_region_body(block, body),
    )


def _emit_zone(
    *,
    emitter: _RegionEmitter,
    env: Environment,
    composed: ComposedProfile,
    blocks: list[RenderBlock],
    root: Path,
) -> list[_ReferenceEmission]:
    """Emit every block of one zone, returning the expansions that were written.

    Args:
        emitter: Accumulator owning the managed-region writes.
        env: Jinja2 environment the block bodies render through.
        composed: Full composed profile, passed into each block template.
        blocks: The zone's blocks, in render order.
        root: Directory holding the managed file, parent of ``docs/rules/``.

    Returns:
        One :class:`_ReferenceEmission` per reference-placed block in *blocks*.
    """
    emissions: list[_ReferenceEmission] = []
    for block in blocks:
        emission = _emit_block(
            emitter=emitter,
            env=env,
            composed=composed,
            block=block,
            root=root,
        )
        if emission is not None:
            emissions.append(emission)
    return emissions


def render_agents_md(
    composed: ComposedProfile,
    target: Path,
    manifest: Manifest,
    *,
    generator: str = "eawf-render",
    state: State | None = None,
    decisions_scope_id: str | None = None,
) -> tuple[RenderResult, Manifest]:
    """Render the AGENTS.md-targeted regions of *composed* into *target*.

    Workflow:

    1. Read existing *target* if present (so unmanaged content can round-trip).
    2. Filter ``composed.render_blocks`` to ``target == "AGENTS.md"`` and
       partition the result by :attr:`RenderBlock.tier` into Zone 1
       (``tier0``) and Zone 2 (``reference``).
    3. Emit Zone 1 then Zone 2. Each zone is introduced by a managed boundary
       marker (emitted only when its tier is non-empty), followed by that
       tier's blocks rendered via Jinja2 + ``replace_region`` — insertions
       append, updates rewrite the BEGIN…END span, untouched regions are
       no-ops. A ``placement: reference`` block contributes only its
       :func:`render_reference_line`; its full body is written alongside at
       :func:`reference_file_path`, inside its own managed region so the
       expansion is drift-checked too.
    4. When *state* is supplied, append/replace a managed
       ``DECISIONS_REGION_ID`` region whose body is produced by
       :func:`render_decisions_section` against the typed
       :attr:`~eawf.kernel.state.models.State.decisions` map. This is how the
       AGENTS.md "Decisions" section stays in sync with state.json
       rather than carrying hardcoded prose in a YAML profile body.
    5. Acquire portalock on *target*, atomically write the new text via
       tempfile + ``os.replace`` (parent-dir fsync included).
    6. Build an updated :class:`Manifest` whose entries for *target* and for
       every ``docs/rules/<id>.md`` expansion under it match the regions just
       emitted; entries for *other* targets are preserved verbatim. Hash
       recorded is :func:`~eawf.surfaces.render.regions.compute_hash` of the
       rendered body text — same digest the BEGIN marker carries. An expansion
       row whose block is no longer reference-placed is dropped rather than
       carried, so ``eawf doctor`` never reports a phantom "missing" region.

    Args:
        composed: Composed profile body whose ``render_blocks`` drive the
            rendering. Blocks with ``target != "AGENTS.md"`` are skipped.
        target: Destination path. Parent directories are created on demand.
        manifest: Current manifest. NOT mutated — a new :class:`Manifest`
            value is returned. The caller is responsible for persisting it
            via :func:`~eawf.surfaces.render.manifest.save_atomic`.
        generator: Recorded on each :class:`ManifestEntry`. Defaults to
            ``"eawf-render"``; init / sync may pass profile-scoped names like
            ``"profile:python"``.
        state: Optional typed :class:`~eawf.kernel.state.models.State`. When
            supplied, a managed ``DECISIONS_REGION_ID`` region is emitted
            with body computed from ``state.decisions`` via
            :func:`render_decisions_section`. When ``None`` (current default)
            the Decisions region is left untouched — existing on-disk
            decisions content round-trips byte-stably and callers retain
            today's behaviour. This keeps the wizard / sync default migration
            opt-in until those callers are ready to hand State in.
        decisions_scope_id: Optional scope filter forwarded to
            :func:`render_decisions_section`. ``None`` (default) means
            "render every decision". Ignored when *state* is ``None``.

    Returns:
        ``(result, updated_manifest)`` — :class:`RenderResult` summarises the
        per-region delta; *updated_manifest* is a new value with the AGENTS.md
        entries overwritten and other targets carried through.
    """
    target = Path(target)
    # POSIX-form key so manifest entries are byte-identical across OSes —
    # ``str(Path)`` would embed backslashes on Windows and break drift checks
    # for repos shared between platforms.
    target_str = target.as_posix()

    tier0_blocks, reference_blocks = composed.partition_render_blocks_by_tier(_TARGET_FILENAME)

    existing_text = ""
    if target.exists():
        existing_text = target.read_text(encoding="utf-8")
    hand_edits_preserved = _has_unmanaged_content(existing_text)

    # Index existing regions for added/updated/unchanged classification.
    existing_regions = {r.id: r for r in regions.find_regions(existing_text)}

    env = _load_environment()
    timestamp = datetime.now(UTC).isoformat()
    emitter = _RegionEmitter(text=existing_text, existing=existing_regions)

    # Zone 1 (always-on tier0) is rendered first, Zone 2 (lazy reference)
    # second. The zone-boundary marker for a tier is emitted only when that
    # tier has at least one block, so a tier0-free profile never carries an
    # empty Zone-1 region. The marker is itself a managed region: it rides the
    # same insert-or-replace path, so the affordance-parity invariant holds in
    # both directions (every tier0 block id lands after the Zone-1 marker and
    # before the Zone-2 marker; no tier0 id leaks into the Zone-2 span).
    emissions: list[_ReferenceEmission] = []
    if tier0_blocks:
        emitter.emit(ZONE_TIER0_REGION_ID, ZONE_REGION_VERSION, ZONE_TIER0_BODY)
        emissions += _emit_zone(
            emitter=emitter,
            env=env,
            composed=composed,
            blocks=tier0_blocks,
            root=target.parent,
        )

    # A reference zone is needed when there are reference blocks OR when the
    # typed Decisions section is being injected (it is reference-tier content
    # and must never land among the tier0 blocks).
    needs_reference_zone = bool(reference_blocks) or state is not None
    if needs_reference_zone:
        emitter.emit(ZONE_REFERENCE_REGION_ID, ZONE_REGION_VERSION, ZONE_REFERENCE_BODY)
        emissions += _emit_zone(
            emitter=emitter,
            env=env,
            composed=composed,
            blocks=reference_blocks,
            root=target.parent,
        )

    if state is not None:
        decisions_body = render_decisions_section(
            state.decisions,
            scope_id=decisions_scope_id,
        )
        emitter.emit(DECISIONS_REGION_ID, DECISIONS_REGION_VERSION, decisions_body)

    # Ensure POSIX-compliant single trailing newline so end-of-file-fixer is a
    # no-op on the rendered file and re-renders stay byte-stable. Idempotent:
    # if the last region's END marker block already ends with '\n', this is a
    # no-op; otherwise we append exactly one '\n'.
    new_text = emitter.text
    if not new_text.endswith("\n"):
        new_text = new_text + "\n"

    atomic_write_text(target, new_text)

    # Preserve manifest entries for OTHER targets, refresh ours from this run.
    # "Ours" spans the managed file AND every expansion under this root's
    # docs/rules/: dropping the whole expansion prefix before re-adding this
    # run's rows is what reclaims the row of a block that stopped being
    # reference-placed, instead of leaving it to report "missing" forever.
    reference_prefix = f"{_reference_dir(target.parent).as_posix()}/"
    new_generated: dict[str, ManifestEntry] = {
        key: entry
        for key, entry in manifest.generated.items()
        if entry.target != target_str and not entry.target.startswith(reference_prefix)
    }
    for region_id, body in emitter.bodies.items():
        composite_key = f"{target_str}::{region_id}"
        new_generated[composite_key] = ManifestEntry(
            target=target_str,
            region_id=region_id,
            version=emitter.versions[region_id],
            hash=regions.compute_hash(body),
            generator=generator,
            generated_at=timestamp,
        )
    for emission in emissions:
        expansion_target = emission.path.as_posix()
        new_generated[f"{expansion_target}::{emission.region_id}"] = ManifestEntry(
            target=expansion_target,
            region_id=emission.region_id,
            version=emission.version,
            hash=regions.compute_hash(emission.body),
            generator=generator,
            generated_at=timestamp,
        )

    updated_manifest = Manifest(version=manifest.version, generated=new_generated)
    result = RenderResult(
        target=target,
        regions_added=emitter.added,
        regions_updated=emitter.updated,
        regions_unchanged=emitter.unchanged,
        hand_edits_preserved=hand_edits_preserved,
    )
    logger.info(
        f"render_agents_md target={target} "
        f"added={len(emitter.added)} updated={len(emitter.updated)} "
        f"unchanged={len(emitter.unchanged)} "
        f"expansions={len(emissions)} "
        f"hand_edits_preserved={hand_edits_preserved} "
        f"decisions_injected={state is not None}"
    )
    return result, updated_manifest
