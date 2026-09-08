"""Backlog import rules: the resolution classifier and the obsolescence sweep.

A closed epoch-1 backlog row records its fate as prose. The classifier
turns that prose into a reason by resolving the identifiers it names
against the source corpus, in a fixed arm order, so the same string
always yields the same arm -- a keyword bag would not. Every arm yields
``DROPPED``: delivery under a different record is that record's
completion, not the draft's own.

The obsolescence sweep answers a different question about the same rows:
whether a row names something the cutover deletes and so must not import
as a live draft. Its vocabulary comes from the shared allowlist rather
than from prose here.
"""

from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Iterable, Mapping
from enum import StrEnum
from typing import Annotated, Any

from pydantic import Field

from eawf.kernel.migration.epoch2.allowlist import LegacySymbolAllowlist
from eawf.kernel.migration.epoch2.errors import MigrationCountMismatchError
from eawf.kernel.migration.epoch2.rules import (
    MappingRuleVersion,
    StrictMigrationModel,
    build_rule_version,
    canonical_json,
)

logger = logging.getLogger(__name__)

# The canonical wave id is P##-I##-W##. Resolution prose also uses a
# P##-W## phase-wave shorthand, which resolves only when exactly one wave
# carries that phase and wave ordinal.
WAVE_ID_FULL_PATTERN = re.compile(r"\bP\d{2,}-I\d{2,}-W\d{2,}\b")
WAVE_ID_SHORT_PATTERN = re.compile(r"\bP\d{2,}-W\d{2,}\b")
BACKLOG_ID_PATTERN = re.compile(r"\bB\d{2,}\b")
AUDIT_ID_PATTERN = re.compile(r"\bA\d{2,}\b")

# An allowlist entry shaped like one of these is an identifier, and an
# identifier is provenance rather than a verdict about a row's substance.
_ID_SHAPED_TERM = re.compile(
    r"^(?:P\d{2,}(?:-I\d{2,})?(?:-W\d{2,})?|B\d{2,}|A\d{2,})$", re.IGNORECASE
)

CLOSED_SOURCE_STATUS = "closed"
DROPPED_TARGET_STATUS = "DROPPED"

# The fields the obsolescence sweep reads. Ordered, and every nested
# string beneath them is flattened in sorted-key order, so the sweep is a
# pure function of the row and re-runs byte-identically.
OBSOLESCENCE_SCAN_FIELDS: tuple[str, ...] = ("title", "intent", "description", "resolution")

AMBIGUOUS_SHORTHAND_ANNOTATION = "ambiguous_shorthand_not_disambiguated"


class ResolutionArm(StrEnum):
    """The four arms, evaluated in declaration order and never reordered."""

    DELIVERED_BY_TASK = "delivered_by_task"
    SUPERSEDED_BY_DRAFT = "superseded_by_draft"
    OBSOLETED_BY_AUDIT = "obsoleted_by_audit"
    CLOSED_WITH_COMMIT_ONLY = "closed_with_commit_only"


RESOLUTION_ARM_ORDER: tuple[ResolutionArm, ...] = tuple(ResolutionArm)


class ResolutionCorpus(StrictMigrationModel):
    """The identifier populations a resolution string is resolved against.

    Attributes:
        wave_ids: Every canonical wave id in the source.
        wave_short_index: ``"P14|W02"`` to the wave ids sharing that phase
            and wave ordinal, in source order.
        backlog_ids: Every backlog row id in the source.
        audit_ids: The audit population, already unioned across the
            document collection and the audit ledger.
    """

    wave_ids: frozenset[str]
    wave_short_index: Mapping[str, tuple[str, ...]]
    backlog_ids: frozenset[str]
    audit_ids: frozenset[str]

    @classmethod
    def build(
        cls,
        *,
        wave_ids: Iterable[str],
        backlog_ids: Iterable[str],
        audit_ids: Iterable[str],
    ) -> ResolutionCorpus:
        """Build a corpus, deriving the phase-wave shorthand index.

        Args:
            wave_ids: Canonical ``P##-I##-W##`` wave ids, in source order.
            backlog_ids: Backlog row ids.
            audit_ids: The unioned audit population.

        Returns:
            The corpus, ready to classify against.

        Raises:
            ValueError: When a wave id is not canonically shaped, which
                would silently drop it out of the shorthand index and
                make an ambiguous shorthand look unique.
        """
        ordered = tuple(wave_ids)
        short: dict[str, list[str]] = {}
        for wave_id in ordered:
            if not WAVE_ID_FULL_PATTERN.fullmatch(wave_id):
                raise ValueError(f"wave id {wave_id!r} is not canonical P##-I##-W##")
            phase, _iter, wave = wave_id.split("-")
            short.setdefault(f"{phase}|{wave}", []).append(wave_id)
        return cls(
            wave_ids=frozenset(ordered),
            wave_short_index={key: tuple(value) for key, value in short.items()},
            backlog_ids=frozenset(backlog_ids),
            audit_ids=frozenset(audit_ids),
        )


class AmbiguousShorthand(StrictMigrationModel):
    """A ``P##-W##`` shorthand matching more than one wave.

    Recorded in full rather than resolved: picking the lowest iter would
    invent a delivery the source never named.
    """

    token: Annotated[str, Field(min_length=1)]
    candidates: tuple[str, ...]


class BacklogResolution(StrictMigrationModel):
    """The classified fate of one closed backlog row."""

    backlog_id: Annotated[str, Field(min_length=1)]
    arm: ResolutionArm
    target_status: Annotated[str, Field(min_length=1)]
    delivered_by_wave_id: str | None
    also_mentions_wave_ids: tuple[str, ...]
    superseded_by_backlog_id: str | None
    obsoleted_by_audit_id: str | None
    close_commit: str | None
    close_note: str | None
    ambiguous_delivered_by: tuple[AmbiguousShorthand, ...]
    annotations: tuple[str, ...]


def _resolution_text(row: Mapping[str, Any]) -> str:
    """Return the resolution prose of ``row`` as a string."""
    return str(row.get("resolution") or "")


def classify_backlog_resolution(
    *,
    backlog_id: str,
    row: Mapping[str, Any],
    corpus: ResolutionCorpus,
) -> BacklogResolution:
    """Classify one closed backlog row into exactly one of the four arms.

    Args:
        backlog_id: The row's own id, excluded from the supersession arm
            so a row can never supersede itself.
        row: The source backlog row.
        corpus: The identifier populations to resolve against.

    Returns:
        The classified resolution. ``target_status`` is always
        ``DROPPED``.

    Raises:
        ValueError: When ``row`` is not closed. An open row has no
            resolution to classify.
        MigrationCountMismatchError: When the row reaches no arm --
            no resolvable identifier, no commit and no resolution text,
            so the fourth arm has nothing to record and the drop would be
            unexplained.
    """
    source_status = row.get("status")
    if source_status != CLOSED_SOURCE_STATUS:
        raise ValueError(
            f"backlog row {backlog_id!r} has status {source_status!r}; "
            f"only {CLOSED_SOURCE_STATUS!r} rows are classified"
        )

    text = _resolution_text(row)
    annotations: list[str] = []
    ambiguous: list[AmbiguousShorthand] = []

    delivering = [token for token in WAVE_ID_FULL_PATTERN.findall(text) if token in corpus.wave_ids]
    for token in WAVE_ID_SHORT_PATTERN.findall(text):
        phase, wave = token.split("-")
        candidates = corpus.wave_short_index.get(f"{phase}|{wave}", ())
        if len(candidates) == 1:
            delivering.append(candidates[0])
        elif len(candidates) > 1:
            ambiguous.append(AmbiguousShorthand(token=token, candidates=candidates))
            annotations.append(AMBIGUOUS_SHORTHAND_ANNOTATION)

    superseding = [
        token
        for token in BACKLOG_ID_PATTERN.findall(text)
        if token != backlog_id and token in corpus.backlog_ids
    ]
    obsoleting = [token for token in AUDIT_ID_PATTERN.findall(text) if token in corpus.audit_ids]

    commit = row.get("commit") or None

    if delivering:
        arm = ResolutionArm.DELIVERED_BY_TASK
        delivered_by: str | None = delivering[0]
        also_mentions = tuple(delivering[1:])
        superseded_by: str | None = None
        obsoleted_by: str | None = None
        close_commit: str | None = None
        close_note: str | None = None
    elif superseding:
        arm = ResolutionArm.SUPERSEDED_BY_DRAFT
        delivered_by, also_mentions = None, ()
        superseded_by, obsoleted_by = superseding[0], None
        close_commit, close_note = None, None
    elif obsoleting:
        arm = ResolutionArm.OBSOLETED_BY_AUDIT
        delivered_by, also_mentions = None, ()
        superseded_by, obsoleted_by = None, obsoleting[0]
        close_commit, close_note = None, None
    elif commit or text:
        arm = ResolutionArm.CLOSED_WITH_COMMIT_ONLY
        delivered_by, also_mentions = None, ()
        superseded_by, obsoleted_by = None, None
        close_commit, close_note = commit, text or None
    else:
        raise MigrationCountMismatchError(
            f"closed backlog row {backlog_id!r} reaches no classifier arm: "
            "no resolvable identifier, no commit and no resolution text"
        )

    annotations.append(f"classified_{arm.value}")
    return BacklogResolution(
        backlog_id=backlog_id,
        arm=arm,
        target_status=DROPPED_TARGET_STATUS,
        delivered_by_wave_id=delivered_by,
        also_mentions_wave_ids=also_mentions,
        superseded_by_backlog_id=superseded_by,
        obsoleted_by_audit_id=obsoleted_by,
        close_commit=close_commit,
        close_note=close_note,
        ambiguous_delivered_by=tuple(ambiguous),
        annotations=tuple(annotations),
    )


class ObsolescenceVerdict(StrictMigrationModel):
    """Whether one backlog row names something the cutover deletes."""

    backlog_id: Annotated[str, Field(min_length=1)]
    obsolete: bool
    matched_terms: tuple[str, ...]


class ObsolescenceSweepResult(StrictMigrationModel):
    """The sweep over a whole backlog, with a digest over its own output."""

    rows_scanned: int
    obsolete_ids: tuple[str, ...]
    verdicts: tuple[ObsolescenceVerdict, ...]

    def digest(self) -> str:
        """Return a sha256 hex digest over the canonical sweep output.

        Two runs over the same rows and the same allowlist produce the
        same digest, which is what makes the disposition survive a
        changed backlog: the sweep is re-runnable, not a one-time call.
        """
        return hashlib.sha256(canonical_json(self.model_dump(mode="json"))).hexdigest()


def _flatten_strings(value: Any, out: list[str]) -> None:
    """Append every string reachable from ``value`` to ``out``.

    Dict keys are visited in sorted order so the flattened text -- and
    therefore the sweep verdict -- does not depend on insertion order.
    """
    if isinstance(value, str):
        out.append(value)
    elif isinstance(value, Mapping):
        for key in sorted(value):
            _flatten_strings(value[key], out)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _flatten_strings(item, out)


def scanned_text(row: Mapping[str, Any]) -> str:
    """Return the lowercased prose of ``row`` the sweep matches against."""
    collected: list[str] = []
    for field in OBSOLESCENCE_SCAN_FIELDS:
        _flatten_strings(row.get(field), collected)
    return " ".join(collected).lower()


def deleted_terms_of(allowlist: LegacySymbolAllowlist) -> tuple[str, ...]:
    """Return the lowercased deleted vocabulary the sweep matches on.

    Args:
        allowlist: The shared allowed-legacy-symbol allowlist.

    Returns:
        The deleted terms, lowercased, in file order.

    Raises:
        ValueError: When a deleted term is identifier-shaped. An id
            mention is provenance, not a statement that a row's substance
            dies, so an id in the deleted set would obsolete rows on
            provenance alone.
    """
    terms = tuple(term.lower() for term in allowlist.deleted_terms)
    id_shaped = [term for term in terms if _ID_SHAPED_TERM.match(term)]
    if id_shaped:
        raise ValueError(
            f"allowlist deleted set carries identifier-shaped terms {id_shaped!r}; "
            "an identifier is provenance, never a deletion verdict"
        )
    return terms


def sweep_backlog_obsolescence(
    *,
    rows: Mapping[str, Mapping[str, Any]],
    allowlist: LegacySymbolAllowlist,
) -> ObsolescenceSweepResult:
    """Mark every backlog row that names an entity or verb the cutover deletes.

    Renamed terms are deliberately absent from the deleted set: ``wave``,
    ``iter``, ``phase``, ``backlog`` and ``agent session`` survive under
    new names, so a row naming one of them imports unchanged.

    Args:
        rows: Backlog rows keyed by id.
        allowlist: The shared allowed-legacy-symbol allowlist, the sole
            source of the deleted vocabulary.

    Returns:
        One verdict per row in sorted-id order, plus the obsolete ids.

    Raises:
        ValueError: When the allowlist's deleted set carries an
            identifier-shaped term.
    """
    terms = deleted_terms_of(allowlist)
    verdicts: list[ObsolescenceVerdict] = []
    obsolete_ids: list[str] = []
    for backlog_id in sorted(rows):
        text = scanned_text(rows[backlog_id])
        matched = tuple(term for term in terms if term in text)
        if matched:
            obsolete_ids.append(backlog_id)
        verdicts.append(
            ObsolescenceVerdict(
                backlog_id=backlog_id,
                obsolete=bool(matched),
                matched_terms=matched,
            )
        )
    return ObsolescenceSweepResult(
        rows_scanned=len(rows),
        obsolete_ids=tuple(obsolete_ids),
        verdicts=tuple(verdicts),
    )


def classifier_rule_payload() -> dict[str, Any]:
    """Return the digestable form of the classifier's arm order."""
    return {
        "arms": [arm.value for arm in RESOLUTION_ARM_ORDER],
        "target_status": DROPPED_TARGET_STATUS,
        "patterns": {
            "wave_full": WAVE_ID_FULL_PATTERN.pattern,
            "wave_short": WAVE_ID_SHORT_PATTERN.pattern,
            "backlog": BACKLOG_ID_PATTERN.pattern,
            "audit": AUDIT_ID_PATTERN.pattern,
        },
    }


BACKLOG_CLASSIFIER_RULE: MappingRuleVersion = build_rule_version(
    rule_id="DOM-044",
    source_kind="backlog",
    title="A closed backlog row drops through a total ordered four-arm classifier",
    payload=classifier_rule_payload(),
)


def obsolescence_rule(allowlist: LegacySymbolAllowlist) -> MappingRuleVersion:
    """Return the sweep's rule version, digested over ``allowlist``.

    The digest covers the allowlist rather than a copy of the vocabulary,
    which is what binds the sweep to the shared file: editing the file
    moves the rule digest and no manifest can claim the old vocabulary.

    Args:
        allowlist: The shared allowed-legacy-symbol allowlist.

    Returns:
        The pinned rule version.
    """
    return build_rule_version(
        rule_id="DOM-018",
        source_kind="backlog",
        title="Obsolescence reads the allowed-legacy-symbol allowlist; a rename is not a deletion",
        payload={
            "scan_fields": list(OBSOLESCENCE_SCAN_FIELDS),
            "allowlist": allowlist.as_payload(),
        },
    )
