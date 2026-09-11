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


class MigrationTerminalInDocumentError(MigrationRuleError):
    """The staged document retains a record that never changes again.

    The whole point of the tiering is that the compare-and-swap document
    holds work in flight and nothing else. A terminal record left in it
    would be rewritten on every mutation for the rest of the tree's life,
    which is the cost the cutover exists to remove.
    """

    code: ClassVar[str] = "migration_terminal_in_document"


class MigrationHomePathLeakError(MigrationRuleError):
    """The staged tree carries a concrete home-directory path.

    The importer never edits a source string, so a leak cannot be scrubbed
    out on the way through: it is reported against the source row that
    carries it and the cutover refuses, which leaves the operator a corpus
    to fix rather than an import that quietly rewrote their data.
    """

    code: ClassVar[str] = "migration_home_path_leak"


class MigrationTierUndeclaredError(MigrationRuleError):
    """A staged record routes to a collection with no write rule.

    Every collection the importer targets is declared either at the
    document tier or as a ledger collection whose residency is known. A
    record that reaches neither has no home, and guessing one is how bytes
    land somewhere plausible and wrong.
    """

    code: ClassVar[str] = "migration_tier_undeclared"


class MigrationTargetNotDisposableError(MigrationRuleError):
    """The apply was aimed at a tree nobody declared throwaway.

    An epoch-2 apply builds a whole new generation and re-points the
    tree at it. Until the cutover has been rehearsed end to end, the
    only trees that may receive one are the ones whose owner wrote down,
    in the tree itself, that losing it costs nothing. Absence of the
    declaration is a refusal rather than a prompt: a tree that cannot
    say it is disposable is, for this purpose, production.
    """

    code: ClassVar[str] = "migration_target_not_disposable"


class MigrationNotQuiescentError(MigrationRuleError):
    """Something was still holding the tree when the apply asked for it.

    The cutover reads every authority surface once and writes a
    generation from what it read. A session, lease, write-ahead record
    or managed worktree that is still live can mutate a surface between
    the read and the select, so the apply refuses and names every holder
    rather than racing them.
    """

    code: ClassVar[str] = "migration_not_quiescent"


class MigrationWorkspaceNotRegisteredError(MigrationRuleError):
    """The addressing workspace the apply was given is not registered.

    Every epoch-2 URN is minted under a workspace, so an unregistered
    one would address the whole imported corpus at a key nothing
    resolves. The check runs before the first URN is minted, because a
    corpus minted under a bad key is not repaired by noticing later.
    """

    code: ClassVar[str] = "workspace_not_registered"


class MigrationPlanDigestStaleError(MigrationRuleError):
    """The approved plan is not the plan the apply just recomputed.

    The apply re-runs plan mode under the authority locks and compares
    the approval digest it gets with the one the operator approved. A
    difference means the corpus, the rules or the addressing moved after
    the approval, so the operator approved work other than the work
    about to run.
    """

    code: ClassVar[str] = "migration_plan_digest_stale"


class MigrationReadSmokeFailedError(MigrationRuleError):
    """The freshly built generation did not read back the way it was written.

    The select is the one-way part of the cutover, so the generation is
    read through the public readers first. A count that disagrees with
    the manifest means the tree on disk is not the tree the manifest
    describes, and selecting it would make the manifest a fiction.
    """

    code: ClassVar[str] = "migration_read_smoke_failed"


class MigrationJournalBrokenError(MigrationRuleError):
    """The cutover journal does not verify as its own append-only chain.

    Each row digests the row before it, so a rewritten, reordered or
    removed row breaks the chain. A journal that cannot be trusted
    cannot answer how far a previous apply got, which is the only
    question it exists to answer.
    """

    code: ClassVar[str] = "migration_journal_broken"
