"""Backfill CLI over the legacy-to-typed criterion converter.

The converter logic lives in :mod:`eawf.kernel.spec.common` (so it has a
production home beside :class:`~eawf.kernel.spec.common.CriterionSpec` /
:class:`~eawf.kernel.spec.common.GateSpec` and is unit-testable by name); the
legacy-count surface lives in :mod:`eawf.workflow.verify.readiness`. This module
is the standalone backfill shim: it reads a SAMPLE of grandfathered
success-criterion strings off a JSON fixture, drives
:func:`~eawf.kernel.spec.common.backfill_legacy_criteria` over them, and asserts
the active-criteria legacy count drops to ZERO over the converted sample.

It deliberately operates on a fixture sample, never on live ``state.json``:
mass-migrating the ~1392 on-disk rows is a follow-up daemon mutation, not this
tool's job. The tool proves the converter falsifies a representative sample.

Sample JSON shape (``{file_scopes: [...], criteria: [...]}``):

    {
      "file_scopes": ["src/eawf/kernel/spec/common.py"],
      "criteria": [
        "validates the schema; pytest tests/unit/spec/test_x.py",
        "the converter exists and the legacy count drops to zero"
      ]
    }

Invocation:

    python3 tools/eawf021_measurable_criterion.py <sample.json>

Exit codes:
- ``0`` -- the converter built a typed + gated row for every sample string and
  the post-conversion legacy count is ``0``.
- ``1`` -- the sample was malformed, conversion raised, or the post-conversion
  legacy count was non-zero (the reason is named on stderr).
"""

from __future__ import annotations

import sys
from pathlib import Path

import orjson

from eawf.kernel.spec.common import backfill_legacy_criteria
from eawf.workflow.verify.readiness import legacy_criterion_count


def _load_sample(path: Path) -> tuple[list[str], list[str]]:
    """Decode the ``{file_scopes, criteria}`` sample JSON at *path*.

    Args:
        path: Path to the sample JSON fixture.

    Returns:
        A ``(legacy_texts, file_scopes)`` tuple.

    Raises:
        ValueError: When the file is missing, not a JSON object, or lacks a
            non-empty ``criteria`` / ``file_scopes`` list.
    """
    if not path.is_file():
        raise ValueError(f"sample not found: {path!s}")
    data = orjson.loads(path.read_bytes())
    if not isinstance(data, dict):
        raise ValueError(f"sample must be a json object: {path!s}")
    criteria = data.get("criteria")
    file_scopes = data.get("file_scopes")
    if not isinstance(criteria, list) or not criteria:
        raise ValueError("sample 'criteria' must be a non-empty list")
    if not isinstance(file_scopes, list) or not file_scopes:
        raise ValueError("sample 'file_scopes' must be a non-empty list")
    return [str(c) for c in criteria], [str(s) for s in file_scopes]


def run_backfill(path: Path) -> int:
    """Convert the sample at *path* and report the legacy-count drop.

    Args:
        path: Path to the sample JSON fixture.

    Returns:
        ``0`` when every string converted and the post-conversion legacy count
        is ``0``; ``1`` otherwise (the reason is printed to stderr).
    """
    try:
        legacy_texts, file_scopes = _load_sample(path)
    except (ValueError, orjson.JSONDecodeError) as exc:
        print(f"eawf021-backfill: bad sample: {exc}", file=sys.stderr)
        return 1
    before = len(legacy_texts)
    try:
        criteria, gates = backfill_legacy_criteria(legacy_texts, file_scopes=file_scopes)
    except ValueError as exc:
        print(f"eawf021-backfill: conversion failed: {exc}", file=sys.stderr)
        return 1
    after = legacy_criterion_count(criteria)
    if after != 0:
        print(
            f"eawf021-backfill: legacy count not drained: before={before} after={after}",
            file=sys.stderr,
        )
        return 1
    print(
        f"eawf021-backfill ok: converted={len(criteria)} gates={len(gates)} "
        f"legacy_before={before} legacy_after={after}"
    )
    for criterion in criteria:
        tier = criterion.oracle_tier.name if criterion.oracle_tier is not None else "?"
        print(f"  {criterion.id} kind={criterion.kind} tier={tier} gates={criterion.gate_ids}")
    return 0


def main(argv: list[str]) -> int:
    """Parse argv and run the backfill over the named sample.

    Args:
        argv: CLI args after the program name; ``argv[0]`` is the sample path.

    Returns:
        ``0`` on a fully-drained conversion, ``1`` otherwise.
    """
    if len(argv) != 1:
        print(
            "usage: python3 tools/eawf021_measurable_criterion.py <sample.json>",
            file=sys.stderr,
        )
        return 1
    return run_backfill(Path(argv[0]))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
