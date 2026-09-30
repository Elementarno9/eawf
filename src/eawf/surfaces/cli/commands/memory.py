"""Typer sub-app for ``eawf memory ...``.

This module is the facade for the ``memory`` command group
(P27-I05-W09). It owns the :data:`memory_app` Typer group and the
shared helpers every handler composes (store-path resolvers, the
read-only state loader, the confidence / status flag parsers, the
args-hash helper). The concrete command bodies live in two sibling
modules:

- :mod:`eawf.surfaces.cli.commands.memory_write` — mutating verbs ``add`` /
  ``promote`` / ``prune`` / ``gc`` / ``tier`` / ``compact``.
- :mod:`eawf.surfaces.cli.commands.memory_read` — read-only verbs ``list`` /
  ``render-context`` / ``view`` / ``stale``.

Each sibling imports the app + shared helpers from this module and
attaches its handlers via ``@memory_app.command(...)``. Importing this
module imports the siblings (at the bottom, after every shared symbol
is defined), so the decorators run and ``memory_app`` carries its full
verb set. The ``registry`` mount of ``memory_app`` keeps resolving
from this module unchanged.

Sub-commands:

- ``add``            — file a new memory note.
- ``promote``        — copy a store record into a new note, or retire a note
  into a decision the decision ledger holds (``--to artifact``).
- ``prune``          — soft-delete: flip matched notes' status to ``PRUNED``.
- ``gc`` / ``tier``  — move notes between the working and archival tiers.
- ``list``           — list memory notes (optionally filtered).
- ``compact``        — wrap :func:`eawf.kernel.store.compact.compact_store` for ``memory.jsonl``.
- ``render-context`` — produce a token-budgeted context block.
- ``view``           — show one memory note with its body.
- ``stale``          — list stale candidates (low-confidence + over-age).

On an epoch-2 tree the notes live on the selected generation's memory
ledger. The mutating verbs send the daemon's native ``memory.*`` verbs,
which commit each revision as a ledger line; the reads read the ledger
directly, since reading needs no daemon. On an epoch-1 tree the reads
still answer from ``state.memory_index`` and ``memory.jsonl``; the
flag-day gate refuses the mutating verbs there before they dispatch.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import TYPE_CHECKING

import orjson
import typer

from eawf.kernel.state.enums import Confidence, MemoryStatus, StoreKind
from eawf.surfaces.cli import errors as cli_errors
from eawf.surfaces.cli.flags import GlobalFlags

if TYPE_CHECKING:
    from eawf.kernel.state.models import State
    from eawf.kernel.store.kinds.memory import MemoryNote

#: Mirrors :data:`eawf.platform.memory.render_context.DEFAULT_BUDGET`; inlined as a
#: literal so the ``memory render-context --budget`` default does not import
#: the heavy ``memory.render_context`` subtree at command-tree build time.
_DEFAULT_BUDGET: int = 4096

logger = logging.getLogger(__name__)

memory_app = typer.Typer(
    name="memory",
    help="Manage curated durable memory entries.",
    no_args_is_help=True,
)


_CONFIDENCE_FROM_FLAG: dict[str, Confidence] = {
    "h": Confidence.HIGH,
    "high": Confidence.HIGH,
    "m": Confidence.MEDIUM,
    "medium": Confidence.MEDIUM,
    "l": Confidence.LOW,
    "low": Confidence.LOW,
}


def _memory_path_for(state_path: Path) -> Path:
    """Return the canonical memory-store JSONL location next to ``state.json``."""
    from eawf.kernel.store.paths import store_path

    return store_path(state_path, StoreKind.MEMORY)


def _events_path_for(state_path: Path) -> Path:
    """Return the canonical events-store JSONL location next to ``state.json``."""
    from eawf.kernel.store.paths import store_path

    return store_path(state_path, StoreKind.EVENT)


def _load_state(state_path: Path) -> State:
    """Read + schema-validate a state document. Used by read-only handlers."""
    from eawf.kernel.validate.strict import validate_state

    if not state_path.exists():
        raise cli_errors.UserError(f"state file not found: {state_path}", kind="NotFound")
    payload = orjson.loads(state_path.read_bytes())
    report = validate_state(payload, strict_optional=False)
    if report.state is None:
        raise cli_errors.ValidationError(
            f"state validation failed: {'; '.join(report.schema_errors)}"
        )
    if report.violations:
        raise cli_errors.ValidationError(
            f"state invariant violations: {[v.code for v in report.violations]}"
        )
    return report.state


def _load_notes(state_path: Path) -> dict[str, MemoryNote]:
    """Return the notes the tree holding *state_path* stands at, from its epoch's store."""
    from eawf.platform.memory.book import generation_memory_ledger, notes_from_state, read_book

    ledger = generation_memory_ledger(state_path.parent)
    if ledger is None:
        return notes_from_state(_load_state(state_path), _memory_path_for(state_path))
    return {mid: standing.note for mid, standing in read_book(ledger).items()}


def _memory_rpc(
    method: str, params: dict[str, object], *, state_path: Path, flags: GlobalFlags, verb_text: str
) -> dict[str, object]:
    """Send one native ``memory.*`` verb for the tree holding *state_path*.

    Raises:
        UserError: The daemon refused the request; a missing note, source
            or decision is ``NotFound`` and every other refusal is
            ``InvalidInput``, with the daemon's own message.
        CliError: The daemon was unreachable or failed outside the
            refusal vocabulary.
    """
    from eawf.surfaces.cli._daemon_client import DaemonRpcError
    from eawf.surfaces.cli.commands.domain import _native_answer

    wire = {"repo_root": str(state_path.parent.parent), **params}
    try:
        return _native_answer(method, wire, flags=flags, verb_text=verb_text)
    except DaemonRpcError as exc:
        if exc.code != cli_errors.RPC_VALIDATION_FAILED:
            raise cli_errors.cli_error_for_rpc(exc.code, exc.message) from exc
        message = exc.message.removeprefix("validation_failed: ")
        code = message.split(":", 1)[0]
        kind = "NotFound" if code.endswith("_not_found") else "InvalidInput"
        raise cli_errors.UserError(message, kind=kind) from exc


def _resolve_confidence(raw: str | None) -> Confidence:
    if raw is None:
        return Confidence.MEDIUM
    key = raw.strip().lower()
    if key not in _CONFIDENCE_FROM_FLAG:
        raise cli_errors.UserError(
            f"--confidence must be one of h/m/l (or high/medium/low); got {raw!r}",
            kind="InvalidInput",
        )
    return _CONFIDENCE_FROM_FLAG[key]


def _resolve_status(raw: str | None) -> MemoryStatus | None:
    if raw is None:
        return None
    try:
        return MemoryStatus(raw.strip().lower())
    except ValueError as exc:
        raise cli_errors.UserError(
            f"--status must be one of {[s.value for s in MemoryStatus]}; got {raw!r}",
            kind="InvalidInput",
        ) from exc


def _args_hash(args: dict[str, object]) -> str:
    return hashlib.sha256(orjson.dumps(args, option=orjson.OPT_SORT_KEYS)).hexdigest()


# ---- command registration ---------------------------------------------------
# Importing the sibling modules runs their ``@memory_app.command(...)``
# decorators so the app above carries its full verb set. The imports sit
# at the bottom, after every shared symbol is defined, so the siblings
# can import the app and helpers from this module without a circular-
# import failure.
from eawf.surfaces.cli.commands import memory_read as _memory_read  # noqa: E402, F401
from eawf.surfaces.cli.commands import memory_write as _memory_write  # noqa: E402, F401

# Re-export to keep the linter quiet about the imported helpers used only above.
__all__ = ["memory_app"]
