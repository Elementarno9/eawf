"""Deterministic evidence-provenance gate for jury-scorable casts.

A jury that scores rendered evidence (an asciinema cast / Pilot-text
frame) can be gamed when the evidence is provenance-free: a cast emitted
with no build/commit binding lets a hand-authored or stale frame pass a
jury that scores text the running code never produced. This gate closes
that hole on the deterministic side, *before* any jury runs.

Given an evidence cast, its committed golden, and the commit the
evidence is expected to have been rendered from, the gate asserts two
independent contracts:

- **provenance** — the ``source_commit`` embedded in the cast header
  (read via :func:`eawf.surfaces.tui.snapshot.asciinema.read_cast_provenance`)
  equals the expected commit. A forged or stale stamp fails here.
- **no-drift** — the evidence bytes equal the committed golden bytes. A
  stale or hand-edited frame fails here even if the stamp is forged to
  match.

The check is a pure function returning a typed :class:`GateResult` so
tests can assert the failure kind without parsing stderr; the CLI is a
thin wrapper that prints the message and maps the result onto an exit
code.

Invocation:

    python3 tools/evidence_provenance_gate.py <evidence> <golden> <expected-commit>

Exit codes:
- ``0`` — provenance matches AND evidence equals the golden.
- ``1`` — at least one contract failed (the failure is named on stderr).
- ``2`` — usage error (wrong argument count).
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from eawf.surfaces.tui.snapshot.asciinema import read_cast_provenance


class GateFailure(StrEnum):
    """The mutually exclusive ways the evidence gate can fail.

    The order encodes precedence: a missing file is reported before a
    provenance mismatch, which is reported before a byte drift, so a
    single run names the most fundamental problem first.
    """

    MISSING_EVIDENCE = "missing_evidence"
    MISSING_GOLDEN = "missing_golden"
    MISSING_PROVENANCE = "missing_provenance"
    PROVENANCE_MISMATCH = "provenance_mismatch"
    BYTE_DRIFT = "byte_drift"


@dataclass(frozen=True, slots=True)
class GateResult:
    """Typed outcome of one evidence-provenance check.

    Attributes:
        passed: Whether both the provenance and no-drift contracts held.
        failure: The failure kind when ``passed`` is ``False``; ``None``
            on a pass.
        message: A human-readable line naming the evidence file and, on
            failure, the mismatch.
    """

    passed: bool
    failure: GateFailure | None
    message: str


def check_evidence_provenance(
    evidence_path: Path,
    golden_path: Path,
    expected_commit: str,
) -> GateResult:
    """Assert *evidence_path* is provenance-pinned to *expected_commit* and undrifted.

    The two contracts are checked in precedence order: existence, then
    provenance (the embedded ``source_commit`` equals *expected_commit*),
    then no-drift (the evidence bytes equal *golden_path*'s bytes). The
    first failed contract determines the result; a clean result requires
    all of them.

    Args:
        evidence_path: The regenerated-from-HEAD evidence cast under test.
        golden_path: The committed golden the evidence must match byte for
            byte.
        expected_commit: The commit SHA the evidence is expected to carry
            in its provenance stamp.

    Returns:
        A :class:`GateResult` whose ``passed`` is ``True`` only when the
        provenance matches and the bytes are identical; otherwise
        ``failure`` names the first violated contract.
    """
    if not evidence_path.is_file():
        return GateResult(
            passed=False,
            failure=GateFailure.MISSING_EVIDENCE,
            message=f"evidence file not found: {str(evidence_path)!r}",
        )
    if not golden_path.is_file():
        return GateResult(
            passed=False,
            failure=GateFailure.MISSING_GOLDEN,
            message=f"golden file not found: {str(golden_path)!r}",
        )

    source_commit, _ = read_cast_provenance(evidence_path)
    if source_commit is None:
        return GateResult(
            passed=False,
            failure=GateFailure.MISSING_PROVENANCE,
            message=(
                f"evidence {str(evidence_path)!r} carries no source_commit stamp; "
                f"expected {expected_commit!r}"
            ),
        )
    if source_commit != expected_commit:
        return GateResult(
            passed=False,
            failure=GateFailure.PROVENANCE_MISMATCH,
            message=(
                f"provenance mismatch for {str(evidence_path)!r}: "
                f"stamped {source_commit!r}, expected {expected_commit!r}"
            ),
        )

    if evidence_path.read_bytes() != golden_path.read_bytes():
        return GateResult(
            passed=False,
            failure=GateFailure.BYTE_DRIFT,
            message=(
                f"evidence drift for {str(evidence_path)!r}: bytes differ from "
                f"committed golden {str(golden_path)!r}"
            ),
        )

    return GateResult(
        passed=True,
        failure=None,
        message=(
            f"evidence gate: ok ({str(evidence_path)!r} pinned to {expected_commit!r}, "
            f"matches golden)"
        ),
    )


def main(argv: list[str]) -> int:
    if len(argv) < 4:
        print(
            "usage: evidence_provenance_gate.py <evidence> <golden> <expected-commit>",
            file=sys.stderr,
        )
        return 2
    result = check_evidence_provenance(
        Path(argv[1]),
        Path(argv[2]),
        argv[3],
    )
    if result.passed:
        print(result.message)
        return 0
    print(result.message, file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
