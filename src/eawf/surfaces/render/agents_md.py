"""Render AGENTS.md from a composed profile, preserving hand-edits in the file.

Per ``docs/policy/agents-claude-md.md``:

- Take a :class:`~eawf.profiles.models.ComposedProfile` whose ``render_blocks``
  list declares the regions that should appear on disk.
- Filter to ``target == "AGENTS.md"`` blocks (other targets — ``.claude/...``
  skill/agent files — are handled by sibling renderers in W05+).
- For each block: render its body via Jinja2 against the bundled
  ``AGENTS.md.j2`` template (prose ``body_template`` verbatim, or a fixed
  ``Rationale``/``Mechanism``/``Verification`` layout for structured triad
  blocks), then call
  :func:`~eawf.surfaces.render.regions.replace_region` so an existing block with the
  same id is replaced in-place and a brand-new block is appended.
  Anything *outside* a managed region (hand-written paragraphs above, below,
  or between blocks) round-trips byte-stably — that is the contract that
  makes "re-render is safe" hold.
- Atomically write the new file content via tempfile + ``os.replace`` under a
  portalock — same discipline as :mod:`eawf.kernel.state.writer`.
- Update a :class:`~eawf.surfaces.render.manifest.Manifest` so drift detection
  (:mod:`eawf.surfaces.render.drift`) and ``eawf doctor`` know the renderer's view of
  what's on disk. The caller is responsible for persisting the manifest via
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
    lint_entity_title(title) -> list[str]
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

from eawf.kernel.state.models import Decision, State
from eawf.profiles.models import ComposedProfile, RenderBlock
from eawf.surfaces.render import regions
from eawf.surfaces.render._atomic import atomic_write_text
from eawf.surfaces.render.manifest import Manifest, ManifestEntry

logger = logging.getLogger(__name__)


_TARGET_FILENAME: str = "AGENTS.md"
_TEMPLATE_NAME: str = "AGENTS.md.j2"
_TEMPLATES_PACKAGE: str = "eawf.templates"

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
    :mod:`eawf.profiles.loader`).
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
    ``src/eawf/templates/AGENTS.md.j2`` for the surface policy.
    """
    template = env.get_template(_TEMPLATE_NAME)
    return template.render(block=block, composed=composed)


def _extract_targeted_blocks(composed: ComposedProfile) -> list[RenderBlock]:
    """Return only render_blocks targeting the AGENTS.md file, in caller order."""
    return [b for b in composed.render_blocks if b.target == _TARGET_FILENAME]


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
    2. Filter ``composed.render_blocks`` to ``target == "AGENTS.md"``.
    3. For each block, render its body via Jinja2 + ``replace_region`` —
       insertions append, updates rewrite the BEGIN…END span, untouched
       regions are no-ops.
    4. When *state* is supplied, append/replace a managed
       ``DECISIONS_REGION_ID`` region whose body is produced by
       :func:`render_decisions_section` against the typed
       :attr:`~eawf.kernel.state.models.State.decisions` map. This is how the
       AGENTS.md "Decisions" section stays in sync with state.json
       rather than carrying hardcoded prose in a YAML profile body.
    5. Acquire portalock on *target*, atomically write the new text via
       tempfile + ``os.replace`` (parent-dir fsync included).
    6. Build an updated :class:`Manifest` whose entries for *target* match
       the regions just emitted; entries for *other* targets are preserved
       verbatim. Hash recorded is :func:`~eawf.surfaces.render.regions.compute_hash`
       of the rendered body text — same digest the BEGIN marker carries.

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

    blocks = _extract_targeted_blocks(composed)

    existing_text = ""
    if target.exists():
        existing_text = target.read_text(encoding="utf-8")
    hand_edits_preserved = _has_unmanaged_content(existing_text)

    # Index existing regions for added/updated/unchanged classification.
    existing_regions = {r.id: r for r in regions.find_regions(existing_text)}

    env = _load_environment()
    new_text = existing_text
    added: list[str] = []
    updated: list[str] = []
    unchanged: list[str] = []
    timestamp = datetime.now(UTC).isoformat()
    rendered_bodies: dict[str, str] = {}
    # Track per-region versions for manifest emission. Profile-driven blocks
    # use ``block.version``; the typed Decisions injection uses the module
    # constant ``DECISIONS_REGION_VERSION``.
    rendered_versions: dict[str, str] = {}

    for block in blocks:
        body = _render_block_body(env, block, composed)
        rendered_bodies[block.id] = body
        rendered_versions[block.id] = block.version
        prev = existing_regions.get(block.id)
        if prev is None:
            added.append(block.id)
        elif prev.body == body and prev.version == block.version:
            unchanged.append(block.id)
        else:
            updated.append(block.id)
        new_text = regions.replace_region(
            new_text,
            id=block.id,
            version=block.version,
            body=body,
        )

    if state is not None:
        decisions_body = render_decisions_section(
            state.decisions,
            scope_id=decisions_scope_id,
        )
        rendered_bodies[DECISIONS_REGION_ID] = decisions_body
        rendered_versions[DECISIONS_REGION_ID] = DECISIONS_REGION_VERSION
        prev = existing_regions.get(DECISIONS_REGION_ID)
        if prev is None:
            added.append(DECISIONS_REGION_ID)
        elif prev.body == decisions_body and prev.version == DECISIONS_REGION_VERSION:
            unchanged.append(DECISIONS_REGION_ID)
        else:
            updated.append(DECISIONS_REGION_ID)
        new_text = regions.replace_region(
            new_text,
            id=DECISIONS_REGION_ID,
            version=DECISIONS_REGION_VERSION,
            body=decisions_body,
        )

    # Ensure POSIX-compliant single trailing newline so end-of-file-fixer is a
    # no-op on the rendered file and re-renders stay byte-stable. Idempotent:
    # if the last region's END marker block already ends with '\n', this is a
    # no-op; otherwise we append exactly one '\n'.
    if not new_text.endswith("\n"):
        new_text = new_text + "\n"

    atomic_write_text(target, new_text)

    # Preserve manifest entries for OTHER targets, refresh ours from this run.
    new_generated: dict[str, ManifestEntry] = {
        key: entry for key, entry in manifest.generated.items() if entry.target != target_str
    }
    for region_id, body in rendered_bodies.items():
        composite_key = f"{target_str}::{region_id}"
        new_generated[composite_key] = ManifestEntry(
            target=target_str,
            region_id=region_id,
            version=rendered_versions[region_id],
            hash=regions.compute_hash(body),
            generator=generator,
            generated_at=timestamp,
        )

    updated_manifest = Manifest(version=manifest.version, generated=new_generated)
    result = RenderResult(
        target=target,
        regions_added=added,
        regions_updated=updated,
        regions_unchanged=unchanged,
        hand_edits_preserved=hand_edits_preserved,
    )
    logger.info(
        f"render_agents_md target={target} "
        f"added={len(added)} updated={len(updated)} unchanged={len(unchanged)} "
        f"hand_edits_preserved={hand_edits_preserved} "
        f"decisions_injected={state is not None}"
    )
    return result, updated_manifest
