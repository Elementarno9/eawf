"""Read an epoch-2 generation back into the epoch-1 lifecycle shape, stdlib only.

After the cutover a repository's ``.ea/state.json`` is frozen: every
lifecycle move lands in the selected generation instead. The commit-msg
hook still has to answer the epoch-1 questions it always answered -- does
this wave exist, is it claimed, which phase is in flight -- so this module
projects the generation's Milestones, Batches and Tasks onto the
``phases`` / ``iters`` / ``waves`` / ``current`` maps the hook validates.

The hook runs under system ``python3`` and cannot import the package, so
the two halves of that job are restated here rather than imported:

- the epoch test mirrors ``eawf.kernel.state.epoch2.authority.resolve_authority``:
  a root is epoch 2 only when exactly one declaration parses and the
  marker parses and names a well-formed generation. Anything short of that
  reads as epoch 1, as it does for the resolver.
- the status map is the inverse of
  ``eawf.kernel.migration.epoch2.status_map``. Imported lifecycle rows keep
  their epoch-1 id as the record key, which is what an ``Eawf-Wave``
  trailer names; rows keyed any other way are native work, so the
  projection leaves them out.
- the native Task key grammar mirrors the task family of
  ``eawf.kernel.identity.keys``: ``<PROJECT>-####``, which is what an
  ``Task`` trailer names. :func:`native_task_status` reads such a row
  in its native spelling, since it has no epoch-1 counterpart.

All three copies are pinned against the package by
``tests/unit/test_commit_prefix_lint_epoch2.py``.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

GENERATIONS_DIRNAME = "generations"
MARKER_FILENAME = "EPOCH2_ACTIVE.json"
OPT_IN_DECLARATION_FILENAME = "epoch2-opt-in.json"
CANARY_DECLARATION_FILENAME = "epoch2-disposable-canary.json"
GENERATION_DOCUMENT = "state.json"
LEDGER_DIRNAME = "ledger"
LEDGER_SUFFIX = ".jsonl"

_GENERATION_ID_RE = re.compile(r"^gen-[0-9a-f]{16}$")
_PHASE_KEY_RE = re.compile(r"^P\d{2,}$")
_ITER_KEY_RE = re.compile(r"^P\d{2,}-I\d{2,}$")
_WAVE_KEY_RE = re.compile(r"^P\d{2,}-I\d{2,}-W\d{2,}$")
NATIVE_TASK_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_-]{1,15}-\d{4}$")

# The inverse of the importer's status maps. A native status with no
# epoch-1 spelling reads as its own lowercase name, which the hook treats
# as in flight -- the conservative reading for a phase-blocking check.
_MILESTONE_STATUSES = {
    "PLANNED": "planned",
    "ACTIVE": "active",
    "COMPLETED": "closed",
    "CANCELLED": "archived",
}
_BATCH_STATUSES = {
    "PLANNED": "planned",
    "ACTIVE": "active",
    "COMPLETED": "closed",
    "CANCELLED": "abandoned",
}
_TASK_STATUSES = {
    "PLANNED": "pending",
    "CLAIMED": "claimed",
    "RUNNING": "in_progress",
    "COMPLETED": "closed",
    "CANCELLED": "abandoned",
    "FAILED": "failed",
}


def _read_json_object(path: Path) -> dict[str, Any] | None:
    """Return the JSON object at *path*, or ``None`` when absent or malformed."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except OSError, ValueError:
        return None
    return raw if isinstance(raw, dict) else None


def epoch2_generation(ea_dir: Path) -> Path | None:
    """Return the selected generation directory when *ea_dir* is in epoch 2.

    Args:
        ea_dir: The tree root, the directory holding ``state.json``.

    Returns:
        ``<ea_dir>/generations/<generation_id>`` when exactly one declaration
        states its claim and the marker names a well-formed generation;
        ``None`` otherwise, which means the epoch-1 document is authoritative.
    """
    opt_in = _read_json_object(ea_dir / OPT_IN_DECLARATION_FILENAME)
    canary = _read_json_object(ea_dir / CANARY_DECLARATION_FILENAME)
    declared = [
        declaration
        for declaration, claim in ((opt_in, "opt_in"), (canary, "disposable"))
        if declaration is not None and declaration.get(claim) is True
    ]
    if len(declared) != 1:
        return None
    marker = _read_json_object(ea_dir / GENERATIONS_DIRNAME / MARKER_FILENAME)
    if marker is None or marker.get("epoch") != 2:
        return None
    generation_id = marker.get("generation_id")
    if not isinstance(generation_id, str) or not _GENERATION_ID_RE.match(generation_id):
        return None
    return ea_dir / GENERATIONS_DIRNAME / generation_id


def _collection_rows(generation: Path, collection: str) -> dict[str, dict[str, Any]]:
    """Return one collection's current rows, keyed by record key.

    A terminal row is compacted out of the document into the ledger, so the
    ledger is read first, last line per key winning, and the document's
    in-flight rows then override it.

    Raises:
        ValueError: A ledger line is not a JSON object, or the document's
            collection is not an object.
    """
    rows: dict[str, dict[str, Any]] = {}
    ledger = generation / LEDGER_DIRNAME / f"{collection}{LEDGER_SUFFIX}"
    if ledger.is_file():
        for line in ledger.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            if not isinstance(row, dict) or not isinstance(row.get("record_key"), str):
                raise ValueError(f"{ledger.name}: every line must be an object with a record_key")
            rows[row["record_key"]] = row
    document = json.loads((generation / GENERATION_DOCUMENT).read_text(encoding="utf-8"))
    in_flight = document.get(collection, {}) if isinstance(document, dict) else None
    if not isinstance(in_flight, dict):
        raise ValueError(f"{GENERATION_DOCUMENT}: {collection!r} must be an object")
    for key, row in in_flight.items():
        if isinstance(row, dict):
            rows[key] = row
    return rows


def _record(row: dict[str, Any]) -> dict[str, Any]:
    """Return the imported row's native record, or an empty mapping."""
    payload = row.get("payload")
    record = payload.get("record") if isinstance(payload, dict) else None
    return record if isinstance(record, dict) else {}


def _status(row: dict[str, Any], table: dict[str, str]) -> str:
    raw = str(row.get("status", ""))
    return table.get(raw, raw.lower())


def _single(keys: list[str]) -> str | None:
    return keys[0] if len(keys) == 1 else None


def generation_lifecycle_view(generation: Path) -> dict[str, Any]:
    """Project a generation's imported lifecycle rows onto the epoch-1 maps.

    Args:
        generation: The selected generation directory.

    Returns:
        ``phases``, ``iters`` and ``waves`` keyed by epoch-1 id, each carrying
        its epoch-1 status and both hierarchy directions, plus ``current``
        naming the one ACTIVE Milestone and the one ACTIVE Batch under it
        (``None`` when there is not exactly one).

    Raises:
        OSError: The generation document cannot be read.
        ValueError: The document or a ledger is not the expected JSON shape.
    """
    milestones = {
        key: row
        for key, row in _collection_rows(generation, "milestone").items()
        if _PHASE_KEY_RE.match(key)
    }
    batches = {
        key: row
        for key, row in _collection_rows(generation, "batch").items()
        if _ITER_KEY_RE.match(key)
    }
    tasks = {
        key: row
        for key, row in _collection_rows(generation, "task").items()
        if _WAVE_KEY_RE.match(key)
    }
    phases: dict[str, dict[str, Any]] = {
        key: {
            "status": _status(row, _MILESTONE_STATUSES),
            "iter_ids": list(_record(row).get("required_batch_refs") or []),
        }
        for key, row in milestones.items()
    }
    iters: dict[str, dict[str, Any]] = {
        key: {
            "status": _status(row, _BATCH_STATUSES),
            "phase_id": _record(row).get("milestone_ref"),
            "wave_ids": list(_record(row).get("task_refs") or []),
        }
        for key, row in batches.items()
    }
    waves: dict[str, dict[str, Any]] = {
        key: {"status": _status(row, _TASK_STATUSES), "iter_id": _record(row).get("batch_ref")}
        for key, row in tasks.items()
    }
    # A child names its parent on every imported row, while the parent's
    # child list is carried only where the source recorded one, so the
    # downward edge is completed from the upward one.
    for key, iteration in sorted(iters.items()):
        phase = phases.get(str(iteration["phase_id"]))
        if phase is not None and key not in phase["iter_ids"]:
            phase["iter_ids"].append(key)
    for key, wave in sorted(waves.items()):
        parent = iters.get(str(wave["iter_id"]))
        if parent is not None and key not in parent["wave_ids"]:
            parent["wave_ids"].append(key)
    current_phase = _single(sorted(k for k, p in phases.items() if p["status"] == "active"))
    current_iter = _single(
        sorted(
            key
            for key, iteration in iters.items()
            if iteration["status"] == "active" and iteration["phase_id"] == current_phase
        )
    )
    return {
        "current": {"phase_id": current_phase, "iter_id": current_iter},
        "phases": phases,
        "iters": iters,
        "waves": waves,
    }


def native_task_status(generation: Path, key: str) -> str | None:
    """Return the native status of Task *key* in *generation*.

    Args:
        generation: The selected generation directory.
        key: A native Task key such as ``EAWF-0137``.

    Returns:
        The row's status as the generation spells it (``CLAIMED``,
        ``RUNNING``, ...), or ``None`` when no Task carries *key*.

    Raises:
        OSError: The generation document cannot be read.
        ValueError: The document or the task ledger is not the expected
            JSON shape.
    """
    row = _collection_rows(generation, "task").get(key)
    return None if row is None else str(row.get("status", ""))
