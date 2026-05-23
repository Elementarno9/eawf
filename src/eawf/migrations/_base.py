"""``Migration`` protocol + chain runner for ``state.json`` schema bumps.

A *migration* is a typed transformation between two adjacent
``schema_version`` values. Each step exposes:

* ``from_version`` / ``to_version`` — the adjacent edge it covers.
* ``apply(state_dict) -> state_dict`` — the raw-dict transform.
* ``check_pre(state_dict)`` — Pydantic-load the input against the
  *from-version* model so a malformed input fails fast (MIG-F4).
* ``check_post(state_dict)`` — Pydantic-load the output against the
  *to-version* model so a bad transform fails fast (MIG-F5).

The chain runner walks the registry from the on-disk ``schema_version``
to the requested target, building an ordered list of steps. For each
step it runs the pre-condition, applies the transform, then runs the
post-condition before moving on — so a mid-chain failure is detected at
the offending step rather than after the whole chain.

**Canonical-writer route (AGENTS rule 4 / D-SUP-01).** The final write
does NOT call :func:`eawf.state.writer.atomic_write_json` directly. It
routes through :func:`write_canonical`, which acquires
:func:`eawf.lock.portalock.acquire` and then calls
:func:`eawf.state.writer.atomic_write_json_locked` — the exact lock +
write primitive the daemon ``state.mutate`` handler and the
``state_transaction`` chokepoint use. Routing through the shared lock +
``atomic_write_json_locked`` primitive keeps the migration on the
canonical writer path without forcing a full ``State.model_validate``
inside ``state_transaction`` — a later migration edge will introduce a
breaking field change whose intermediate output cannot load against the
post-edge model, so the migration runner deliberately stays off the
model-validating chokepoint.

**Backup discipline.** Before any write, :func:`run_chain` snapshots the
pre-migration payload to ``state.json.bak.v<from>.v<to>`` adjacent to the
state file (gitignored via ``.ea/state.json.bak.*`` / ``state.json.bak.*``).
On a mid-chain failure the runner restores the on-disk state from that
backup (MIG-F3 / MIG-F5) so a half-applied chain never lands.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Protocol, get_args, runtime_checkable

from eawf.lock import portalock
from eawf.state.writer import atomic_write_json_locked

logger = logging.getLogger(__name__)


#: Target ``schema_version`` the bare ``eawf migrate`` (no ``--to``) bumps
#: toward. Kept here (not derived from the live model's ``Literal``) so the
#: migrate verb has a stable default target: a future edge may advance the
#: default ahead of the model's accepted set during a multi-wave bump, and
#: the guard (:func:`guard_target_supported`) is the boundary that refuses
#: a default running past what the model can re-load.
_DEFAULT_TARGET_VERSION = "1.1"


class MigrationError(Exception):
    """Base class for migration-chain failures.

    Raised by :func:`build_migration_chain` when no path exists between
    the requested versions, and subclassed by :class:`MigrationStepError`.
    """


class MigrationStepError(MigrationError):
    """A single migration step failed its pre/post invariant or transform.

    Carries the offending step's edge so the CLI can name it in the
    ``MIGRATION_STEP_FAILED`` envelope.

    Attributes:
        from_version: The step's source version.
        to_version: The step's target version.
        phase: ``"pre"``, ``"apply"``, or ``"post"`` — which lifecycle
            stage of the step raised.
    """

    def __init__(self, *, from_version: str, to_version: str, phase: str, message: str) -> None:
        self.from_version = from_version
        self.to_version = to_version
        self.phase = phase
        super().__init__(
            f"migration step {from_version}->{to_version} failed at {phase}: {message}"
        )


@runtime_checkable
class Migration(Protocol):
    """One adjacent-version transform for ``state.json``.

    Pre: the input dict is verified-loadable against the *from-version*
    Pydantic model (enforced by :meth:`check_pre`).
    Post: the output dict is verified-loadable against the *to-version*
    Pydantic model (enforced by :meth:`check_post`).
    """

    from_version: str
    to_version: str

    def apply(self, state_dict: dict[str, Any]) -> dict[str, Any]:
        """Transform the raw ``state.json`` dict from->to version."""
        ...

    def check_pre(self, state_dict: dict[str, Any]) -> None:
        """Validate *state_dict* against the from-version model.

        Raises:
            Exception: When the input is not loadable against the
                from-version Pydantic model.
        """
        ...

    def check_post(self, state_dict: dict[str, Any]) -> None:
        """Validate *state_dict* against the to-version model.

        Raises:
            Exception: When the output is not loadable against the
                to-version Pydantic model.
        """
        ...


def current_target_version() -> str:
    """Return the default migration target for the bare ``eawf migrate``."""
    return _DEFAULT_TARGET_VERSION


def _version_key(version: str) -> tuple[int, ...]:
    """Return a sortable integer tuple for a dotted ``MAJOR.MINOR`` version.

    Schema versions are simple dotted-numeric strings (``"1.0"``,
    ``"1.1"``) so a plain ``tuple(int, ...)`` orders them correctly
    without a third-party version parser.

    Raises:
        ValueError: When *version* is not a dotted run of integers.
    """
    try:
        return tuple(int(part) for part in version.split("."))
    except ValueError as exc:
        raise ValueError(f"unparseable schema version: {version!r}") from exc


def model_supported_max_version() -> str:
    """Return the highest ``schema_version`` the live ``State`` model loads.

    Derived from :class:`eawf.state.models.State`'s ``schema_version``
    ``Literal`` args so the migrate guard never advances past a version
    the model cannot re-validate. Read-only: bumping the supported set is
    owned by the model, not this helper.
    """
    # Imported lazily: ``eawf.state.models`` (and its transitive
    # ``eawf.sandbox.policy``) are heavy modules the CLI tree-build /
    # shell-completion cold path must not load. Resolving the import at
    # call time keeps ``import eawf.cli.app`` (which eagerly registers the
    # ``migrate`` command) off those modules' import graph.
    from eawf.state.models import State

    supported: tuple[str, ...] = tuple(
        str(arg) for arg in get_args(State.model_fields["schema_version"].annotation)
    )
    return max(supported, key=_version_key)


def guard_target_supported(to_version: str) -> None:
    """Refuse a migration target the live ``State`` model cannot load.

    The bare ``eawf migrate`` default target may run ahead of the live
    model while a later wave advances the model's ``schema_version``
    ``Literal``. Migrating to such a target writes a payload every
    subsequent read rejects with a ``ValidationError`` — bricking the
    repo. This fail-fast boundary refuses that target *before* any write.

    Args:
        to_version: The requested target ``schema_version``.

    Raises:
        MigrationError: When *to_version* exceeds the model-supported max.
            Caught by the CLI and surfaced as ``MIGRATION_TARGET_UNKNOWN``.
    """
    supported_max = model_supported_max_version()
    if _version_key(to_version) > _version_key(supported_max):
        raise MigrationError(
            f"migration target {to_version!r} exceeds model-supported max "
            f"{supported_max!r}; the live State model cannot load it"
        )


def build_migration_chain(
    registry: dict[str, Migration],
    *,
    from_version: str,
    to_version: str,
) -> list[Migration]:
    """Return the ordered list of steps that walk *from* -> *to*.

    The *registry* is keyed by ``from_version`` so the walk is a simple
    linear chase: start at *from_version*, follow each step's
    ``to_version`` until *to_version* is reached.

    Args:
        registry: ``from_version`` -> :class:`Migration` lookup.
        from_version: The on-disk ``schema_version``.
        to_version: The requested target version.

    Returns:
        Ordered steps. Empty when ``from_version == to_version`` (no-op).

    Raises:
        MigrationError: When the chain cannot reach *to_version* (an
            unknown target or a gap in the registry — MIG-F1).
    """
    if from_version == to_version:
        return []
    chain: list[Migration] = []
    cursor = from_version
    seen: set[str] = set()
    while cursor != to_version:
        if cursor in seen:
            raise MigrationError(f"migration registry has a cycle at version {cursor!r}")
        seen.add(cursor)
        step = registry.get(cursor)
        if step is None:
            available = ", ".join(sorted(registry)) or "(none)"
            raise MigrationError(
                f"no migration from version {cursor!r} toward {to_version!r} "
                f"(available from-versions: {available})"
            )
        chain.append(step)
        cursor = step.to_version
    logger.info(f"build_migration_chain from={from_version} to={to_version} steps={len(chain)}")
    return chain


def backup_path_for(state_path: Path, *, from_version: str, to_version: str) -> Path:
    """Return the gitignored backup path adjacent to *state_path*.

    Shape: ``state.json.bak.v<from>.v<to>`` (C10 §5.5.3 / D18a). The
    ``state.json.bak.*`` glob in ``.gitignore`` keeps it untracked.
    """
    return state_path.with_name(f"{state_path.name}.bak.v{from_version}.v{to_version}")


def write_canonical(state_path: Path, payload: dict[str, Any], *, timeout: float = 5.0) -> None:
    """Persist *payload* to *state_path* through the canonical writer path.

    Acquires :func:`eawf.lock.portalock.acquire` then calls
    :func:`eawf.state.writer.atomic_write_json_locked` — the exact lock +
    write primitive the daemon ``state.mutate`` handler and the
    ``state_transaction`` chokepoint use (AGENTS rule 4 / D-SUP-01). The
    lock-acquiring ``atomic_write_json`` bypass is deliberately NOT used:
    every state write must serialise under the sibling lock the canonical
    chokepoint holds.

    Args:
        state_path: Absolute path to ``state.json``.
        payload: JSON-serialisable migrated state dict.
        timeout: Lock-acquisition timeout in seconds.

    Raises:
        portalock.LockTimeout: When the sibling lock cannot be acquired
            within *timeout*.
    """
    with portalock.acquire(state_path, timeout=timeout):
        atomic_write_json_locked(state_path, payload)
    logger.info(f"write_canonical path={state_path!r} keys={len(payload)}")


def _run_step(step: Migration, state_dict: dict[str, Any]) -> dict[str, Any]:
    """Run one step's pre-check -> apply -> post-check; return the result.

    Raises:
        MigrationStepError: When any of the three stages raises; the
            phase (``pre`` / ``apply`` / ``post``) is recorded on the
            error so the caller can name it (MIG-F3/F4/F5).
    """
    try:
        step.check_pre(state_dict)
    except Exception as exc:
        raise MigrationStepError(
            from_version=step.from_version,
            to_version=step.to_version,
            phase="pre",
            message=str(exc),
        ) from exc
    try:
        result = step.apply(state_dict)
    except Exception as exc:
        raise MigrationStepError(
            from_version=step.from_version,
            to_version=step.to_version,
            phase="apply",
            message=str(exc),
        ) from exc
    try:
        step.check_post(result)
    except Exception as exc:
        raise MigrationStepError(
            from_version=step.from_version,
            to_version=step.to_version,
            phase="post",
            message=str(exc),
        ) from exc
    logger.info(f"_run_step ok from={step.from_version} to={step.to_version}")
    return result


def run_chain(
    state_path: Path,
    *,
    chain: list[Migration],
    from_version: str,
    to_version: str,
    dry_run: bool = False,
    backup: bool = True,
) -> dict[str, Any]:
    """Apply *chain* to ``state.json`` with per-step invariants + backup.

    Procedure:

    1. Refuse a *to_version* the live ``State`` model cannot load
       (:func:`guard_target_supported`) — before any read or write so a
       bricking target never touches disk.
    2. Read + decode the raw on-disk state dict.
    3. When ``backup`` and not ``dry_run``: snapshot the pre-migration
       payload to :func:`backup_path_for` (gitignored).
    4. For each step: run the pre-condition, apply, then the
       post-condition (:func:`_run_step`). The candidate dict threads
       through the chain.
    5. When ``dry_run``: skip the write entirely and return the result
       dict (the caller reports what *would* change).
    6. Otherwise persist the result through :func:`write_canonical` (the
       daemon canonical-writer path) and return it.

    On a mid-chain :class:`MigrationStepError`, when a backup was taken,
    restore the on-disk state from the backup before re-raising so a
    half-applied chain never lands (MIG-F3).

    Args:
        state_path: Absolute path to ``state.json``.
        chain: Ordered :class:`Migration` steps (from
            :func:`build_migration_chain`).
        from_version: The on-disk ``schema_version`` (for the backup name).
        to_version: The requested target (for the backup name).
        dry_run: When True, compute the result but write nothing.
        backup: When True (and not ``dry_run``), snapshot before writing.

    Returns:
        The migrated state dict (whether or not it was persisted).

    Raises:
        MigrationError: When *to_version* exceeds the model-supported max;
            no read, backup, or write occurs.
        MigrationStepError: When a step fails; the on-disk state is
            restored from the backup first when one was taken.
        FileNotFoundError: When *state_path* does not exist.
    """
    guard_target_supported(to_version)
    if not state_path.exists():
        raise FileNotFoundError(f"state file not found: {state_path!r}")
    raw = state_path.read_bytes()
    state_dict: dict[str, Any] = json.loads(raw)

    backup_target: Path | None = None
    if backup and not dry_run:
        backup_target = backup_path_for(
            state_path, from_version=from_version, to_version=to_version
        )
        backup_target.write_text(json.dumps(state_dict, indent=2, sort_keys=True), encoding="utf-8")
        logger.info(f"run_chain backup={backup_target!r}")

    candidate = state_dict
    try:
        for step in chain:
            candidate = _run_step(step, candidate)
    except MigrationStepError:
        if backup_target is not None:
            # Restore the pre-migration state so a half-applied chain
            # never lands (MIG-F3). Route the restore through the same
            # canonical writer the forward path uses.
            restored: dict[str, Any] = json.loads(backup_target.read_text(encoding="utf-8"))
            write_canonical(state_path, restored)
            logger.warning(f"run_chain restored={state_path!r} from={backup_target!r}")
        raise

    if dry_run:
        logger.info(f"run_chain dry-run from={from_version} to={to_version} no-write")
        return candidate

    write_canonical(state_path, candidate)
    return candidate


#: Default migration registry — populated by importing the concrete step
#: modules. Keyed by ``from_version`` so :func:`build_migration_chain`
#: walks the edges linearly.
DEFAULT_REGISTRY: dict[str, Migration] = {}


def _register(step: Migration) -> Migration:
    """Insert *step* into :data:`DEFAULT_REGISTRY` keyed by ``from_version``."""
    DEFAULT_REGISTRY[step.from_version] = step
    return step


__all__ = [
    "DEFAULT_REGISTRY",
    "Migration",
    "MigrationError",
    "MigrationStepError",
    "_register",
    "backup_path_for",
    "build_migration_chain",
    "current_target_version",
    "guard_target_supported",
    "model_supported_max_version",
    "run_chain",
    "write_canonical",
]
