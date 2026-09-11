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
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Annotated, Final, Literal, Self

from pydantic import Field, model_validator

from eawf.kernel.migration.epoch2.errors import MigrationTargetNotDisposableError
from eawf.kernel.migration.epoch2.rules import StrictMigrationModel

logger = logging.getLogger(__name__)


#: The file a tree carries to declare that an apply may write into it.
CANARY_DECLARATION_FILENAME: Final = "epoch2-disposable-canary.json"

#: The directory inside the target that holds every generation, the
#: selection pointer, the epoch marker and the cutover journal. One new
#: directory rather than four new files keeps the surface the commit
#: policy has to classify down to a single row.
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


def declaration_path(root: Path) -> Path:
    """Return where a tree rooted at ``root`` declares itself disposable."""
    return root / CANARY_DECLARATION_FILENAME


def read_declaration(root: Path) -> CanaryDeclaration:
    """Return the disposability declaration the tree at ``root`` carries.

    Args:
        root: The target tree's root directory.

    Returns:
        The parsed declaration.

    Raises:
        MigrationTargetNotDisposableError: The declaration is absent,
            unreadable, not JSON, or does not satisfy the contract. Every
            one of those is the same answer -- this tree has not said an
            apply may write into it -- so they carry one code.
    """
    path = declaration_path(root)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise MigrationTargetNotDisposableError(
            f"{root.name}/{CANARY_DECLARATION_FILENAME} is absent or unreadable "
            f"({error.__class__.__name__}), so this tree has not declared itself a "
            "disposable canary and the apply refuses to write into it"
        ) from error
    except json.JSONDecodeError as error:
        raise MigrationTargetNotDisposableError(
            f"{root.name}/{CANARY_DECLARATION_FILENAME} is not valid JSON: {error}"
        ) from error
    try:
        return CanaryDeclaration.model_validate(payload)
    except ValueError as error:
        raise MigrationTargetNotDisposableError(
            f"{root.name}/{CANARY_DECLARATION_FILENAME} does not declare a disposable "
            f"canary: {error}"
        ) from error


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
        declaration: What the tree declared about itself.
    """

    root: Path
    declaration: CanaryDeclaration

    @classmethod
    def require(cls, root: Path) -> DisposableTarget:
        """Clear the fence for the tree at ``root``, or refuse.

        Args:
            root: The target tree's root directory.

        Returns:
            The fence-cleared target.

        Raises:
            MigrationTargetNotDisposableError: The tree carries no valid
                declaration.
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
                f"{self.root.name}/{CANARY_DECLARATION_FILENAME} does not match the "
                "declaration this target was built with"
            )
        return self

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
    "CANARY_DECLARATION_FILENAME",
    "GENERATIONS_DIRNAME",
    "MAINTENANCE_LOCATOR",
    "MARKER_FILENAME",
    "RESTORE_DIRNAME",
    "RESTORE_MANIFEST_FILENAME",
    "SELECTION_FILENAME",
    "TARGET_AUTHORITY_LOCATORS",
    "CanaryDeclaration",
    "DisposableTarget",
    "declaration_path",
    "read_declaration",
]
