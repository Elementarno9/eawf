"""Entity title/description backfill across all five lifecycle / decision kinds.

The doc-clarity standard (``.ea/local/research/2026-05-29-doc-clarity.md``)
found that entity ``title`` / ``description`` are the worst-quality prose
class: wave titles leak a conventional-commit prefix (``feat:`` / ``docs:``),
phase and decision titles chain transient cluster codes joined by ``+``
("cluster-code soup"), and most descriptions are empty or merely restate the
title. The original backfill tool (P29-I02-W09) swept only the backlog; this
module generalizes the same safe mechanism to **all five** entity kinds —
phase, iter, wave, backlog, decision.

Why this is safe in bulk: no test, golden fixture, or integrity hash pins the
live ``state.json`` entity titles. The only guard is each typed model's
length bound, and this tool re-validates every mutated entity through its
model on apply, so an over-cap title is rejected at the boundary exactly as a
direct edit would be. The shape is the **migration-transform**: rewrite the
raw field, re-validate against the typed model — the same pattern the
``v1_0 -> v1_1`` step used when it truncated over-length titles.

Normalization reuses the landed pieces rather than re-implementing them:

- :func:`eawf.surfaces.render.agents_md.normalize_entity_title` for the
  period-strip / over-cap word-boundary trim / placeholder-derive transform.
- :data:`eawf.platform.profiles.clarity.COMMIT_SUBJECT_PREFIX_EXEMPT` for the
  conventional-commit type tokens stripped off a wave title (the same token
  set the EAWF016 title-clarity lint rejects on a title), so the lint and the
  rewrite never drift.

**Linkage hazard (regression-tested).** A ``P<NN>`` lifecycle id can appear as
a *substring* of a decision title (``"Adopt P29 spawn rebuild"``). The
normalization deliberately performs **no** lifecycle-id matching or
substitution, so a phase id inside a decision title is preserved verbatim —
only the conventional-commit prefix (waves), ``+``-join soup, trailing period,
and over-cap tail are touched.

**Lifecycle edit constraints.** Each entity kind freezes at its terminal
statuses (a closed wave, a closed / abandoned iter, a superseded / reversed /
obsolete decision, a closed backlog item). The sweep reports those rows but
never mutates them, mirroring the backlog tool's closed-item rule and the
lifecycle edit transitions that reject edits on a frozen entity.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal, get_args

from pydantic import BaseModel, ConfigDict, ValidationError

from eawf.platform.profiles.clarity import COMMIT_SUBJECT_PREFIX_EXEMPT
from eawf.surfaces.render.agents_md import lint_entity_title, normalize_entity_title

if TYPE_CHECKING:
    from eawf.kernel.state.models import State
    from eawf.kernel.store.envelope import Envelope

logger = logging.getLogger(__name__)

#: The five entity kinds the generalized backfill sweeps. The original tool
#: (P29-I02-W09) covered only ``backlog``; the doc-clarity rewrite extends it
#: to every lifecycle / decision kind that carries a bounded ``title`` plus an
#: optional ``description``.
EntityKind = Literal["phase", "iter", "wave", "backlog", "decision"]
ENTITY_KINDS: tuple[EntityKind, ...] = get_args(EntityKind)

# Leading conventional-commit type prefix on a wave title (``feat: ...``).
# Built from the canonical exempt set so the strip here and the EAWF016 title
# rule that *rejects* the prefix share one token list and never drift.
_CC_PREFIX = re.compile(
    rf"^(?:{'|'.join(sorted(COMMIT_SUBJECT_PREFIX_EXEMPT))}):\s*",
    re.IGNORECASE,
)
# Three-or-more ``+``-joined tokens (``A+B+C``) — the "cluster-code soup" the
# EAWF016 lint flags. A two-token ``A+B`` reads as a single conjunction and is
# left alone; soup starts at three. The ``C++`` language name is carved out.
_CLUSTER_SOUP = re.compile(r"[A-Za-z0-9]+(?:\+[A-Za-z0-9/]+){2,}")


@dataclass(frozen=True)
class _KindConfig:
    """Per-kind backfill rules.

    Attributes:
        label: Human label for the kind, surfaced in the report row.
        attr: ``State`` attribute holding the ``dict[str, model]`` for this
            kind (e.g. ``"waves"``).
        terminal_statuses: ``status`` string-values that freeze an entity —
            a row in one of these statuses is reported but never mutated.
        strip_commit_prefix: Whether to strip a leading conventional-commit
            type prefix off the title (only waves carry them en masse).
    """

    label: str
    attr: str
    terminal_statuses: frozenset[str]
    strip_commit_prefix: bool


#: Per-kind configuration. Terminal-status sets mirror the lifecycle edit
#: transitions: a frozen entity rejects an edit, so the sweep leaves it alone.
_KIND_CONFIG: dict[EntityKind, _KindConfig] = {
    "phase": _KindConfig(
        label="phase",
        attr="phases",
        terminal_statuses=frozenset({"closed", "archived"}),
        strip_commit_prefix=False,
    ),
    "iter": _KindConfig(
        label="iter",
        attr="iters",
        terminal_statuses=frozenset({"closed", "abandoned"}),
        strip_commit_prefix=False,
    ),
    "wave": _KindConfig(
        label="wave",
        attr="waves",
        terminal_statuses=frozenset({"closed", "failed", "abandoned"}),
        strip_commit_prefix=True,
    ),
    "backlog": _KindConfig(
        label="backlog",
        attr="backlog",
        terminal_statuses=frozenset({"closed"}),
        strip_commit_prefix=False,
    ),
    "decision": _KindConfig(
        label="decision",
        attr="decisions",
        terminal_statuses=frozenset({"superseded", "reversed", "obsolete"}),
        strip_commit_prefix=False,
    ),
}


class TitleBackfillRow(BaseModel):
    """One entity's before/after under the title backfill.

    Captures the entity's title before and after normalization, the
    style-lint violations the *current* title trips (the read-only sweep
    signal), and whether the normalized title would change the stored value.
    A row is emitted for every swept entity — including frozen (terminal) and
    no-change rows — so the dry-run diff is complete.
    """

    model_config = ConfigDict(extra="forbid")

    kind: EntityKind
    entity_id: str
    before: str
    after: str
    changed: bool
    frozen: bool
    violations: list[str]


class TitleBackfillReport(BaseModel):
    """Aggregate report for one generalized title-backfill run.

    ``rows`` carries every swept entity across the requested kinds in
    ``(kind, id)`` order; ``applied`` is ``True`` only when the run mutated
    state. The summary counters let the CLI render a one-line headline without
    re-walking ``rows``.
    """

    model_config = ConfigDict(extra="forbid")

    applied: bool
    total: int
    changed: int
    violations: int
    rows: list[TitleBackfillRow]


def normalize_title(
    title: str,
    description: str | None = None,
    *,
    strip_commit_prefix: bool = False,
) -> str:
    """Return *title* normalized to the entity-title clarity rule.

    Layers the doc-clarity title transforms in order, then defers the cap /
    period / placeholder transforms to the shared
    :func:`eawf.surfaces.render.agents_md.normalize_entity_title` so the
    backfill and the rest of the codebase share one cap/period implementation:

    1. **Strip a conventional-commit prefix** — when ``strip_commit_prefix``
       is set (wave titles), a leading ``feat:`` / ``docs:`` / ``fix:`` (any
       of the commit-subject type tokens) is removed. Titles are labels, not
       commit subjects.
    2. **Collapse cluster-code soup** — three-or-more ``+``-joined tokens
       (``A+B+C``) have their ``+`` separators replaced with spaces so the
       label reads as words, with a ``C++`` carve-out. A two-token ``A+B``
       conjunction is left intact.
    3. **Period-strip / over-cap trim / placeholder-derive** — delegated to
       :func:`normalize_entity_title`.

    The function performs **no** lifecycle-id matching, so a ``P<NN>`` inside
    a decision title is preserved verbatim (the linkage hazard from the
    doc-clarity brief). It is pure and idempotent: re-normalizing a normalized
    title is a no-op.

    Args:
        title: The current entity title.
        description: Optional long-form description, used only as a fallback
            title source when *title* is an empty placeholder.
        strip_commit_prefix: When ``True``, strip a leading conventional-commit
            type prefix (wave titles); when ``False`` (the default) leave the
            head untouched.

    Returns:
        The normalized title (always within the 72-char cap). May be empty
        only when *title* is a placeholder and *description* yields no usable
        clause; the caller treats an empty result as a no-op because the model
        forbids an empty title.
    """
    candidate = title.strip()
    if strip_commit_prefix:
        candidate = _CC_PREFIX.sub("", candidate, count=1).strip()
    if "C++" not in candidate and _CLUSTER_SOUP.search(candidate):
        candidate = candidate.replace("+", " ")
        candidate = re.sub(r"\s{2,}", " ", candidate).strip()
    return normalize_entity_title(candidate, description)


def _resolve_kinds(kinds: Iterable[str] | None) -> tuple[EntityKind, ...]:
    """Return the requested kinds in canonical order, or all five.

    This is the validation boundary that narrows arbitrary operator-supplied
    strings to the typed :data:`EntityKind` literal: every returned value is
    guaranteed to be one of :data:`ENTITY_KINDS`.

    Args:
        kinds: An iterable of entity-kind names to restrict the sweep to, or
            ``None`` to sweep all five.

    Returns:
        The selected kinds in :data:`ENTITY_KINDS` order (deduplicated), so a
        run's row ordering is deterministic regardless of caller input order.

    Raises:
        ValueError: when *kinds* names a value outside :data:`ENTITY_KINDS`.
    """
    if kinds is None:
        return ENTITY_KINDS
    requested = set(kinds)
    unknown = requested - set(ENTITY_KINDS)
    if unknown:
        names = ", ".join(sorted(unknown))
        raise ValueError(f"unknown entity kind(s): {names}")
    return tuple(kind for kind in ENTITY_KINDS if kind in requested)


def backfill_entity_titles(
    state: State,
    *,
    apply: bool = False,
    kinds: Iterable[str] | None = None,
) -> tuple[TitleBackfillReport, Envelope | None]:
    """Sweep every entity's title across all five kinds and (optionally) fix it.

    Walks each requested kind's entity map in id-sorted order, normalizing each
    title via :func:`normalize_title` (commit-prefix strip for waves,
    cluster-soup collapse, period-strip, over-cap trim, placeholder-derive) and
    recording the current title's style-lint violations via
    :func:`eawf.surfaces.render.agents_md.lint_entity_title`, so a pure
    ``apply=False`` call doubles as the read-only sweep / dry-run diff.

    When ``apply`` is ``True``, each changed entity is **re-validated through
    its typed model** (``model_validate`` over the dumped-then-patched dict),
    which is the over-cap guard: a normalized title is always within the cap,
    but the re-validation makes the bound the single owner of the title length
    and rejects any out-of-bound value exactly as a direct edit would. A single
    ``state.backfill_titles`` event summarising the changed-entity count is
    returned; a run that changes nothing (or any ``apply=False`` run) returns
    ``None`` for the envelope.

    Terminal-status entities (a closed wave, a closed / abandoned iter, a
    superseded / reversed / obsolete decision, a closed backlog item) are swept
    for reporting but never mutated, honoring the per-kind lifecycle edit
    constraint. An entity whose normalization collapses to an empty string (a
    placeholder title with no usable description) is left unchanged and still
    reported, because the model rejects an empty ``title`` at ingestion.

    Args:
        state: The loaded :class:`State` to sweep / mutate in place.
        apply: When ``True`` persist the normalized titles; when ``False``
            (the default) report only and leave ``state`` untouched.
        kinds: Optional subset of :data:`ENTITY_KINDS` to restrict the sweep
            to; ``None`` (the default) sweeps all five.

    Returns:
        A ``(report, envelope)`` pair. ``report`` always describes every swept
        entity; ``envelope`` is the single ``state.backfill_titles`` event when
        ``apply`` mutated at least one entity, else ``None``.

    Raises:
        ValueError: when *kinds* names a value outside :data:`ENTITY_KINDS`.
    """
    selected = _resolve_kinds(kinds)
    rows: list[TitleBackfillRow] = []
    changed_total = 0

    for kind in selected:
        config = _KIND_CONFIG[kind]
        entities = getattr(state, config.attr) or {}
        for entity_id in sorted(entities):
            entity = entities[entity_id]
            before = entity.title
            normalized = normalize_title(
                before,
                entity.description,
                strip_commit_prefix=config.strip_commit_prefix,
            )
            frozen = str(entity.status) in config.terminal_statuses
            # An empty normalization (placeholder title, no usable
            # description) cannot be persisted: the model forbids an empty
            # title. Treat it as a no-op so the sweep still flags the entity
            # without proposing an invalid write.
            would_change = not frozen and normalized != "" and normalized != before
            rows.append(
                TitleBackfillRow(
                    kind=kind,
                    entity_id=entity_id,
                    before=before,
                    after=normalized if would_change else before,
                    changed=would_change,
                    frozen=frozen,
                    violations=lint_entity_title(before),
                )
            )
            if apply and would_change:
                _apply_title(entities, entity_id, entity, normalized)
                changed_total += 1

    report = TitleBackfillReport(
        applied=apply and changed_total > 0,
        total=len(rows),
        changed=sum(1 for row in rows if row.changed),
        violations=sum(len(row.violations) for row in rows),
        rows=rows,
    )

    if not (apply and changed_total):
        logger.info(
            f"backfill_entity_titles apply={apply} total={report.total} "
            f"changed={report.changed} violations={report.violations}"
        )
        return report, None

    now = datetime.now(UTC)
    state.updated_at = now
    logger.info(
        f"backfill_entity_titles apply=True total={report.total} "
        f"changed={changed_total} violations={report.violations}"
    )
    from eawf.workflow.evidence import _io

    changed_by_kind = {
        kind: [row.entity_id for row in rows if row.kind == kind and row.changed]
        for kind in selected
    }
    event = _io.event_envelope(
        event_id=f"EVT-backfill-titles-{int(now.timestamp() * 1000)}",
        scope_id=None,
        event_type="state.backfill_titles",
        actor="cli",
        command="backfill titles",
        args={kind: ids for kind, ids in changed_by_kind.items() if ids},
        summary=f"backfill titles changed={changed_total} entities",
    )
    return report, event


def _apply_title(
    entities: dict[str, BaseModel],
    entity_id: str,
    entity: BaseModel,
    normalized: str,
) -> None:
    """Re-validate *entity* with the normalized title and write it back.

    The over-cap guard: the patched dict is fed back through
    ``model_validate`` so the typed model's length bound (and every other
    invariant, including the description-clarity floor) owns acceptance. A
    rejected payload propagates the model's :class:`ValidationError`, exactly
    as a direct edit would surface it.

    Args:
        entities: The kind's ``dict[str, model]`` map, mutated in place.
        entity_id: The id of the entity to replace.
        entity: The current typed entity.
        normalized: The new, normalized title.

    Raises:
        ValidationError: when the patched entity fails its model invariants
            (e.g. a title over the 72-char cap).
    """
    patched = {**entity.model_dump(), "title": normalized}
    try:
        entities[entity_id] = type(entity).model_validate(patched)
    except ValidationError:
        logger.exception(f"_apply_title rejected entity={entity_id!r} title={normalized!r}")
        raise


__all__ = [
    "ENTITY_KINDS",
    "EntityKind",
    "TitleBackfillReport",
    "TitleBackfillRow",
    "backfill_entity_titles",
    "normalize_title",
]
