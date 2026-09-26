"""The one gate that decides whether an apply may write here at all.

An epoch-2 apply is not a reversible edit. It builds a whole new
generation from a pinned revision of the corpus and re-points the tree at
it, and the step that re-points is one-way once a native mutation lands
against the new generation. Until that path has been rehearsed end to
end, the only trees allowed to receive one are trees whose owner has
written down, inside the tree, that losing it costs nothing.

The declaration is a file rather than a flag, an environment variable or
a path heuristic, and that choice is the whole point. A flag is supplied
by whoever runs the command, which is exactly the party a fence is meant
to constrain. A path heuristic ("it is under a temp directory") is a
guess about intent that a symlink or a changed default can invalidate. A
file inside the target says what the *owner of the data* decided, and it
has to be put there deliberately.

:class:`DisposableTarget` carries the fence in its type. Every path the
apply may write is derived from an instance of it, and an instance
cannot exist unless the declaration was on disk at construction time --
the model re-reads the file in its own validator rather than trusting the
field it was handed. A later stage therefore cannot route around the
fence by holding a plain :class:`~pathlib.Path`: there is no write helper
that accepts one.

A tree that is *not* throwaway -- a live repository whose owner chooses to
move it to epoch 2 -- declares an opt-in instead. Saying "disposable" there
would be a false statement inside the fence built to prevent exactly that,
so the opt-in says something else: which verified backup the owner took
before the cutover. The fence admits either declaration, but a tree may
carry only one of them, because the two claims disagree about what losing
the tree would cost.
"""

from __future__ import annotations

import json
import logging
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Final, Literal, Self

from pydantic import Field, model_validator

from eawf.kernel.migration.epoch2.errors import MigrationTargetNotDisposableError
from eawf.kernel.migration.epoch2.rules import StrictMigrationModel

logger = logging.getLogger(__name__)


#: The file a tree carries to declare that an apply may write into it.
#: It is never committed: a clone that inherited it would be disposable
#: without its new owner saying so, so each checkout declares for itself.
CANARY_DECLARATION_FILENAME: Final = "epoch2-disposable-canary.json"

#: The file a live repository carries to opt into epoch 2 without calling
#: itself disposable. Unlike the disposable declaration it is a fact about
#: the repository rather than about one checkout, so a clone inherits it.
OPT_IN_DECLARATION_FILENAME: Final = "epoch2-opt-in.json"

#: The shape of a backup snapshot identifier, as the backup store names
#: its timestamp directories.
BACKUP_TS_PATTERN: Final = r"^\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}Z$"

#: The directory inside the target that holds every generation, the
#: selection pointer, the epoch marker and the cutover journal. One
#: directory keeps the cutover's footprint in one place, but the commit
#: policy still classifies it file family by file family: a clone needs
#: the pointer, the marker and each generation's document and ledgers,
#: and none of the indexes, staging builds, restore copies or journal.
GENERATIONS_DIRNAME: Final = "generations"

#: The append-only journal recording how far one apply got.
JOURNAL_FILENAME: Final = "journal.jsonl"

#: The pointer naming the generation the tree currently reads from.
SELECTION_FILENAME: Final = "selected.json"

#: The marker written last, after which readers are in epoch 2.
MARKER_FILENAME: Final = "EPOCH2_ACTIVE.json"

#: The directory holding the pre-cutover copy of every authority surface.
#: It lives beside the generations rather than above them so one rename of
#: ``generations/`` carries the restore point with the tree it restores.
RESTORE_DIRNAME: Final = "restore"

#: The file pinning what each copied surface digested to before the write.
RESTORE_MANIFEST_FILENAME: Final = "restore-manifest.json"

#: The machine-local marker held for the duration of the write window.
#: It lives under ``local/`` because a maintenance window is a fact about
#: one machine's run, not a fact a clone should inherit.
MAINTENANCE_LOCATOR: Final = "local/epoch2-maintenance.json"

#: The authority surfaces that live inside the target tree, as locators
#: relative to its root. The apply holds an exclusive lock on each one
#: for the whole write window and pins each one's digest in the restore
#: point, whether or not the file exists yet.
TARGET_AUTHORITY_LOCATORS: Final[tuple[str, ...]] = (
    "state.json",
    "config.yaml",
    "store/audit.jsonl",
    "store/event.jsonl",
    "telemetry.db",
)


class CanaryDeclaration(StrictMigrationModel):
    """What a tree has to say about itself to receive an apply.

    Attributes:
        disposable: Always ``True``. The field exists so the file states
            the claim in words rather than by its own presence -- a file
            that arrives by a stray copy still has to carry the sentence.
        declared_by: Who declared it, recorded so a surprised operator
            has someone to ask.
        purpose: Why this tree is throwaway, in one line.
    """

    disposable: Literal[True]
    declared_by: Annotated[str, Field(min_length=1, max_length=64)]
    purpose: Annotated[str, Field(min_length=1, max_length=200)]


class OptInDeclaration(StrictMigrationModel):
    """What a live repository says to receive an apply without being throwaway.

    The declaration pins a backup rather than waiving one: the apply
    re-reads the named snapshot and refuses unless it still digests to the
    value written here, so the owner's claim "I can get this back" is
    checked rather than taken on trust.

    Attributes:
        opt_in: Always ``True``, stated in words for the same reason the
            disposable declaration states its claim.
        declared_by: Who opted the repository in.
        purpose: Why the repository is moving to epoch 2, in one line.
        backup_ts: The backup snapshot taken before the cutover, as
            ``eawf backup create`` names it.
        backup_digest: What that snapshot digested to when it was taken,
            as ``eawf backup create`` reports it.
    """

    opt_in: Literal[True]
    declared_by: Annotated[str, Field(min_length=1, max_length=64)]
    purpose: Annotated[str, Field(min_length=1, max_length=200)]
    backup_ts: Annotated[str, Field(pattern=BACKUP_TS_PATTERN)]
    backup_digest: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class DeclarationKind(StrEnum):
    """Which of the two declarations admitted a tree to the cutover."""

    DISPOSABLE = "disposable"
    OPT_IN = "opt_in"


#: Either declaration a tree may carry.
TargetDeclaration = CanaryDeclaration | OptInDeclaration


def declaration_path(root: Path) -> Path:
    """Return where a tree rooted at ``root`` declares itself disposable."""
    return root / CANARY_DECLARATION_FILENAME


def opt_in_path(root: Path) -> Path:
    """Return where a tree rooted at ``root`` declares its opt-in."""
    return root / OPT_IN_DECLARATION_FILENAME


def _parse_declaration[M: StrictMigrationModel](
    root: Path, *, filename: str, model: type[M], claim: str
) -> M:
    """Parse one declaration file, refusing anything short of a valid one.

    Args:
        root: The target tree's root directory.
        filename: The declaration's file name inside the tree.
        model: The contract the file must satisfy.
        claim: What the declaration claims, for the refusal message.

    Returns:
        The parsed declaration.

    Raises:
        MigrationTargetNotDisposableError: The file is absent, unreadable,
            not JSON, or does not satisfy the contract.
    """
    try:
        payload = json.loads((root / filename).read_text(encoding="utf-8"))
    except OSError as error:
        raise MigrationTargetNotDisposableError(
            f"{root.name}/{filename} is absent or unreadable "
            f"({error.__class__.__name__}), so this tree has not declared itself {claim} "
            f"(nor carries {OPT_IN_DECLARATION_FILENAME}) and the apply refuses to write "
            "into it"
        ) from error
    except json.JSONDecodeError as error:
        raise MigrationTargetNotDisposableError(
            f"{root.name}/{filename} is not valid JSON: {error}"
        ) from error
    try:
        return model.model_validate(payload)
    except ValueError as error:
        raise MigrationTargetNotDisposableError(
            f"{root.name}/{filename} does not declare {claim}: {error}"
        ) from error


def read_declaration(root: Path) -> TargetDeclaration:
    """Return the declaration the tree at ``root`` carries.

    Args:
        root: The target tree's root directory.

    Returns:
        The opt-in declaration when the tree carries one, otherwise the
        disposable-canary declaration.

    Raises:
        MigrationTargetNotDisposableError: Neither declaration is present
            and valid, or the tree carries both. Every one of those is the
            same answer -- this tree has not said, unambiguously, that an
            apply may write into it -- so they carry one code.
    """
    opt_in = opt_in_path(root).is_file()
    if opt_in and declaration_path(root).exists():
        raise MigrationTargetNotDisposableError(
            f"{root.name} carries both {CANARY_DECLARATION_FILENAME} and "
            f"{OPT_IN_DECLARATION_FILENAME}; a tree is either throwaway or opted in, and "
            "the apply will not guess which claim is the true one"
        )
    if opt_in:
        return _parse_declaration(
            root,
            filename=OPT_IN_DECLARATION_FILENAME,
            model=OptInDeclaration,
            claim="an epoch-2 opt-in",
        )
    return _parse_declaration(
        root,
        filename=CANARY_DECLARATION_FILENAME,
        model=CanaryDeclaration,
        claim="a disposable canary",
    )


def _require_declared(locator: str) -> str:
    """Return ``locator`` when it names a declared authority surface.

    Args:
        locator: The tree-relative locator a caller wants addressed.

    Returns:
        The locator unchanged.

    Raises:
        ValueError: The locator is not declared. Restricting the set to
            the declared surfaces is what keeps a traversal fragment or a
            caller-composed path from reaching a write helper at all.
    """
    if locator not in TARGET_AUTHORITY_LOCATORS:
        raise ValueError(
            f"{locator!r} is not a declared authority surface, so it is not a path the "
            f"cutover may snapshot or restore (declared: {', '.join(TARGET_AUTHORITY_LOCATORS)})"
        )
    return locator


class DisposableTarget(StrictMigrationModel):
    """A tree that has declared an apply may write into it.

    Holding one of these is the proof that the fence was cleared. The
    apply derives every path it writes from :attr:`root` through the
    accessors below rather than from a caller-supplied path, so a write
    helper cannot be handed a directory that never passed the gate.

    Attributes:
        root: The target tree's root directory.
        declaration: What the tree declared about itself -- disposable,
            or opted in against a named backup.
    """

    root: Path
    declaration: TargetDeclaration

    @classmethod
    def require(cls, root: Path) -> DisposableTarget:
        """Clear the fence for the tree at ``root``, or refuse.

        Args:
            root: The target tree's root directory.

        Returns:
            The fence-cleared target.

        Raises:
            MigrationTargetNotDisposableError: The tree carries no valid
                declaration, or carries both kinds.
        """
        resolved = Path(root)
        target = cls(root=resolved, declaration=read_declaration(resolved))
        logger.info(f"require root={resolved.name} declared_by={target.declaration.declared_by}")
        return target

    @model_validator(mode="after")
    def _declaration_is_the_one_on_disk(self) -> Self:
        """Re-read the declaration so a hand-built instance cannot pass.

        Returns:
            The validated target.

        Raises:
            MigrationTargetNotDisposableError: The tree carries no
                declaration, or carries one that differs from the field.
                Constructing the model directly is therefore no cheaper
                than clearing the fence.
        """
        if read_declaration(self.root) != self.declaration:
            raise MigrationTargetNotDisposableError(
                f"{self.root.name} does not carry the declaration this target was built with"
            )
        return self

    @property
    def kind(self) -> DeclarationKind:
        """Return which declaration admitted the tree."""
        if isinstance(self.declaration, OptInDeclaration):
            return DeclarationKind.OPT_IN
        return DeclarationKind.DISPOSABLE

    @property
    def generations_dir(self) -> Path:
        """Return the directory holding every generation and the journal."""
        return self.root / GENERATIONS_DIRNAME

    @property
    def journal_path(self) -> Path:
        """Return the append-only cutover journal's path."""
        return self.generations_dir / JOURNAL_FILENAME

    @property
    def selection_path(self) -> Path:
        """Return the pointer naming the currently selected generation."""
        return self.generations_dir / SELECTION_FILENAME

    @property
    def marker_path(self) -> Path:
        """Return the epoch marker, which the apply writes last."""
        return self.generations_dir / MARKER_FILENAME

    @property
    def maintenance_path(self) -> Path:
        """Return the machine-local maintenance marker's path."""
        return self.root / MAINTENANCE_LOCATOR

    def generation_path(self, generation_id: str) -> Path:
        """Return the directory one generation's tiers live under.

        Args:
            generation_id: The generation's identifier.

        Returns:
            ``<root>/generations/<generation_id>``.

        Raises:
            ValueError: The identifier carries a path separator or a
                parent reference, which would place a generation outside
                the fenced tree.
        """
        if not generation_id or "/" in generation_id or generation_id.startswith("."):
            raise ValueError(
                f"generation id {generation_id!r} is not a plain directory name, so it "
                "could address a tree the fence never cleared"
            )
        return self.generations_dir / generation_id

    @property
    def restore_dir(self) -> Path:
        """Return the directory holding the pre-cutover copy of each surface."""
        return self.generations_dir / RESTORE_DIRNAME

    @property
    def restore_manifest_path(self) -> Path:
        """Return the manifest pinning what a restore writes back."""
        return self.restore_dir / RESTORE_MANIFEST_FILENAME

    def authority_path(self, locator: str) -> Path:
        """Return where one declared authority surface lives in the tree.

        Args:
            locator: The surface's tree-relative locator.

        Returns:
            ``<root>/<locator>``.

        Raises:
            ValueError: The locator is not one of the declared authority
                surfaces. A restore writes the tree's authority files and
                nothing else, so the set it may address is closed rather
                than whatever a caller passes.
        """
        return self.root / _require_declared(locator)

    def restore_copy_path(self, locator: str) -> Path:
        """Return where the pre-cutover copy of one surface is kept.

        Args:
            locator: The surface's tree-relative locator.

        Returns:
            ``<root>/generations/restore/<locator>``.

        Raises:
            ValueError: The locator is not a declared authority surface.
        """
        return self.restore_dir / _require_declared(locator)

    def authority_surfaces(self) -> tuple[tuple[str, Path], ...]:
        """Return each in-tree authority surface as ``(locator, path)``.

        Returns:
            The surfaces in locator order, whether or not the file
            exists: a surface that is absent today is one a fallback
            writer could create during the window, so it is locked too.
        """
        return tuple(
            (locator, self.root / locator) for locator in sorted(TARGET_AUTHORITY_LOCATORS)
        )


__all__ = [
    "BACKUP_TS_PATTERN",
    "CANARY_DECLARATION_FILENAME",
    "GENERATIONS_DIRNAME",
    "MAINTENANCE_LOCATOR",
    "MARKER_FILENAME",
    "OPT_IN_DECLARATION_FILENAME",
    "RESTORE_DIRNAME",
    "RESTORE_MANIFEST_FILENAME",
    "SELECTION_FILENAME",
    "TARGET_AUTHORITY_LOCATORS",
    "CanaryDeclaration",
    "DeclarationKind",
    "DisposableTarget",
    "OptInDeclaration",
    "TargetDeclaration",
    "declaration_path",
    "opt_in_path",
    "read_declaration",
]
