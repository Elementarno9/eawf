"""Validate, keep and re-select whole rule-projection generations.

A render computes a shadow generation off disk: every output and the
manifest describing them. :func:`verify_generation` checks it before it is
selected, the render stores it here, and only then does the transaction
write its targets, the manifest last, so the manifest on disk always names a
generation that is stored whole.

A rollback never translates the current output backwards. It loads a stored
generation, verifies it again, because a stored file is input read from
disk, and selects it through the same transaction a render uses.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar, Final

from pydantic import AwareDatetime, ValidationError

from eawf.platform.rules.modules import MODULE_VIEW_COMMAND
from eawf.platform.rules.records import RuleModel
from eawf.platform.rules.render import (
    ProjectionPlan,
    ProjectionWrite,
    RuleProjectionError,
    RuleProjectionShadowError,
    _atomic_write_bytes,
    _read_prior_manifest,
    write_rule_projections,
)
from eawf.platform.rules.views import view_target

logger = logging.getLogger(__name__)

#: Where selected generations are kept, relative to the repository root.
GENERATION_STORE_PATH: Final[str] = ".ea/indexes/rule-generations"

#: How many of the most recently selected generations the store keeps.
RETAINED_GENERATIONS: Final[int] = 8

_VIEW_COMMAND: Final[re.Pattern[str]] = re.compile(
    rf"`{re.escape(MODULE_VIEW_COMMAND)} (?P<reference>[^`\s]+)`"
)
_IMPORT_LINE: Final[re.Pattern[str]] = re.compile(r"^@(?P<path>\S+)$")


class RuleGenerationError(RuleProjectionError):
    """No stored generation matches a rollback."""

    code: ClassVar[str] = "rule_projection_generation"


class StoredGeneration(RuleModel):
    """One selected generation as the store keeps it.

    Attributes:
        selected_at: When the generation was last selected.
        plan: The complete output set and its manifest.
    """

    selected_at: AwareDatetime
    plan: ProjectionPlan


def verify_generation(plan: ProjectionPlan) -> None:
    """Refuse a generation that is not whole, over budget or unloadable.

    Checks that every output's digest and size match the manifest and the
    generation digest recomputes (trust), that every projection fits the
    cap it records (budgets), that every import shim imports only
    projections of the same generation (host loading), and that every view
    command a projection names reads a view of the same generation
    (commands).

    Args:
        plan: The generation to check.

    Raises:
        RuleProjectionShadowError: Naming every failed check.
    """
    failures = [*_whole_failures(plan), *_loading_failures(plan)]
    if failures:
        raise RuleProjectionShadowError("; ".join(failures))


def _whole_failures(plan: ProjectionPlan) -> list[str]:
    """Name every output that differs from the manifest or exceeds its cap.

    Args:
        plan: The generation to check.

    Returns:
        One line per trust or budget failure.
    """
    manifest = plan.manifest
    failures: list[str] = []
    for projection in plan.projections:
        record = projection.record
        data = projection.text.encode("utf-8")
        if record.output_digest != _sha256(data) or record.byte_count != len(data):
            failures.append(f"{record.target} does not match its manifest row")
        if record.byte_count > record.cap_bytes:
            failures.append(
                f"{record.target} is {record.byte_count} bytes, "
                f"over its {record.cap_bytes}-byte cap"
            )
    digests = [
        *(p.record.output_digest for p in plan.projections),
        *(_sha256(file.text.encode("utf-8")) for file in plan.generated),
    ]
    if manifest.projections != tuple(p.record for p in plan.projections):
        failures.append("the manifest rows are not the projections' rows")
    if manifest.generated != tuple(file.target for file in plan.generated):
        failures.append("the manifest does not list the generated files")
    if manifest.generation != _sha256(json.dumps(digests, separators=(",", ":")).encode()):
        failures.append(f"generation {manifest.generation} does not recompute from its outputs")
    return failures


def _loading_failures(plan: ProjectionPlan) -> list[str]:
    """Name every shim import and view command the generation cannot satisfy.

    Args:
        plan: The generation to check.

    Returns:
        One line per host-loading or command failure.
    """
    projections = {p.record.target for p in plan.projections}
    generated = {file.target for file in plan.generated}
    failures: list[str] = []
    for file in plan.generated:
        imports = [_IMPORT_LINE.fullmatch(line) for line in file.text.splitlines()]
        if imports and all(imports):
            failures.extend(
                f"{file.target} imports {match['path']}, which the generation does not render"
                for match in imports
                if match is not None and match["path"] not in projections
            )
    for projection in plan.projections:
        failures.extend(
            f"{projection.record.target} names `{MODULE_VIEW_COMMAND} {match['reference']}` "
            f"but the generation renders no such view"
            for match in _VIEW_COMMAND.finditer(projection.text)
            if view_target(match["reference"]) not in generated
        )
    return failures


def store_generation(repo_root: Path, plan: ProjectionPlan) -> Path:
    """Keep a generation about to be selected, pruning the oldest past the limit.

    Args:
        repo_root: The repository root.
        plan: The verified generation.

    Returns:
        The stored file.

    Raises:
        OSError: When the store cannot be written.
    """
    stored = StoredGeneration(selected_at=datetime.now(UTC), plan=plan)
    path = generation_path(repo_root, plan.manifest.generation)
    _atomic_write_bytes(path, stored.model_dump_json().encode("utf-8"))
    for stale in stored_generations(repo_root)[RETAINED_GENERATIONS:]:
        generation_path(repo_root, stale.plan.manifest.generation).unlink(missing_ok=True)
    return path


def generation_path(repo_root: Path, generation: str) -> Path:
    """Return where the store keeps one generation.

    Args:
        repo_root: The repository root.
        generation: The ``sha256:`` generation digest.

    Returns:
        ``<store>/<hex digest>.json``.
    """
    return repo_root / GENERATION_STORE_PATH / f"{generation.removeprefix('sha256:')}.json"


def stored_generations(repo_root: Path) -> tuple[StoredGeneration, ...]:
    """Read every readable stored generation, most recently selected first.

    Args:
        repo_root: The repository root.

    Returns:
        The stored generations; an unreadable file is skipped with a
        warning, since it cannot be selected whole.
    """
    store = repo_root / GENERATION_STORE_PATH
    generations: list[StoredGeneration] = []
    for path in sorted(store.glob("*.json")) if store.is_dir() else ():
        try:
            generations.append(StoredGeneration.model_validate_json(path.read_bytes()))
        except ValidationError as exc:
            logger.warning(f"rule generation unreadable path={path.name} error={exc}")
    return tuple(sorted(generations, key=lambda stored: stored.selected_at, reverse=True))


def rollback_rule_projections(repo_root: Path, generation: str | None) -> ProjectionWrite:
    """Select a complete stored generation in place of the current one.

    Args:
        repo_root: The repository root.
        generation: The generation digest to select, or ``None`` for the most
            recently selected generation other than the current one.

    Returns:
        What the transaction wrote and removed.

    Raises:
        RuleGenerationError: When no stored generation matches.
        RuleProjectionShadowError: When the stored generation fails
            validation.
        RuleProjectionHandEditError: When a target was edited by hand.
        RuleProjectionOwnedError: When a target holds operator prose.
        OSError: When a write fails, after restoring every target.
    """
    prior = _read_prior_manifest(repo_root)
    current = prior.generation if prior else None
    candidates = [
        stored.plan
        for stored in stored_generations(repo_root)
        if (stored.plan.manifest.generation == generation)
        or (generation is None and stored.plan.manifest.generation != current)
    ]
    if not candidates:
        wanted = generation or "a generation before the current one"
        raise RuleGenerationError(
            f"no stored rule generation matches {wanted}; the store keeps the last "
            f"{RETAINED_GENERATIONS} selected generations"
        )
    written = write_rule_projections(repo_root, candidates[0])
    logger.info(f"rule projections rolled back generation={written.manifest.generation}")
    return written


def _sha256(data: bytes) -> str:
    """Return the ``sha256:`` digest of ``data``."""
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


__all__ = [
    "GENERATION_STORE_PATH",
    "RETAINED_GENERATIONS",
    "RuleGenerationError",
    "StoredGeneration",
    "generation_path",
    "rollback_rule_projections",
    "store_generation",
    "stored_generations",
    "verify_generation",
]
