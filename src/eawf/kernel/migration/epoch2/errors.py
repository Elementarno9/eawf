"""Stable failure codes the epoch-2 importer raises.

Each error carries a machine-readable ``code`` because the cutover plan
reports failures as codes, not as prose: an operator triaging a refused
import matches on the code and the surrounding tooling never has to
parse an English message.
"""

from __future__ import annotations

from typing import ClassVar


class MigrationRuleError(Exception):
    """Base class for a refusal raised by an importer rule.

    Attributes:
        code: The stable failure code reported in the cutover plan.
    """

    code: ClassVar[str] = "migration_rule_error"


class MigrationCountMismatchError(MigrationRuleError):
    """A source row reached no arm of a rule that must be total.

    Raised when a source status falls outside the closed status map, or
    when a closed backlog row reaches none of the four classifier arms.
    Either way the source census and the target census can no longer
    reconcile, so the plan fails rather than importing the row under a
    guessed state.
    """

    code: ClassVar[str] = "migration_count_mismatch"


class MigrationFabricationDetectedError(MigrationRuleError):
    """A rule was about to emit a record the source does not support.

    The importer preserves source facts; it never invents one to make a
    reference resolve or a field non-null.
    """

    code: ClassVar[str] = "migration_fabrication_detected"


class MigrationSourceUnreadableError(MigrationRuleError):
    """A declared read surface is missing or cannot be parsed at all.

    The census is total over the source, so an absent surface is not an
    empty one: a snapshot that cannot present every declared surface
    would under-report the corpus and silently shrink the import.
    """

    code: ClassVar[str] = "migration_source_unreadable"


class MigrationSourceMutatedError(MigrationRuleError):
    """A read surface changed bytes after the barrier was taken.

    Everything downstream of the barrier is computed from one pinned
    revision of the source. A surface that moves underneath it makes the
    census describe a corpus that no longer exists.
    """

    code: ClassVar[str] = "migration_source_mutated"


class MigrationDuplicateKeyError(MigrationRuleError):
    """The source document carries the same object key twice.

    A JSON parser keeps the last occurrence, so a duplicate key silently
    discards rows. The census refuses the document instead of importing
    whichever half the parser happened to keep.
    """

    code: ClassVar[str] = "migration_duplicate_key"


class MigrationCollectionOmittedError(MigrationRuleError):
    """A collection with a declared disposition is absent from the source.

    Absent is not empty. A collection nobody can count is a collection
    whose fate the cutover cannot prove, so the plan fails.
    """

    code: ClassVar[str] = "migration_collection_omitted"


class MigrationCollectionUnknownError(MigrationRuleError):
    """The source carries a collection no disposition row declares.

    An undeclared collection is one nobody decided the fate of; letting
    it through would drift past the cutover unimported and unrecorded.
    """

    code: ClassVar[str] = "migration_collection_unknown"


class MigrationRowValidationError(MigrationRuleError):
    """One or more source rows violate the schema declared for them.

    Every offending row is named in the message. A malformed row is a
    plan failure rather than a skip, because a skipped row is a row the
    target census can never reconcile against the source census.
    """

    code: ClassVar[str] = "migration_row_validation"


class MigrationSourceChangedError(MigrationRuleError):
    """The source moved after a plan digest was taken over it.

    Distinct from :class:`MigrationSourceMutatedError`, which catches a
    surface moving *inside* one read barrier. This one catches the longer
    window: a plan is approved by its digest, an operator applies it
    later, and in between the corpus changed. The manifest would then
    describe rows the apply never reads.
    """

    code: ClassVar[str] = "migration_source_changed"


class MigrationPlanNotApplicableError(MigrationRuleError):
    """The plan names rows the cutover cannot place, so apply is refused.

    A row nobody decided the target of cannot be written and cannot be
    dropped. Refusing here keeps the target census reconcilable against
    the source census, which is the one invariant the whole cutover rests
    on.
    """

    code: ClassVar[str] = "migration_plan_not_applicable"


class MigrationValidationDivergedError(MigrationRuleError):
    """Two validation passes over one pinned revision did not agree.

    The cutover validates twice and compares the bytes. A difference
    means the import is not a function of the source, so nothing about
    the first pass can be trusted -- including the part that said the
    import was clean.
    """

    code: ClassVar[str] = "migration_validation_diverged"
