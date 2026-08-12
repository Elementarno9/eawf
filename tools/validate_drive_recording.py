"""Validate a committed live-drive recording.

Eight assertions over a recording directory produced by a capped
``fleet.drive`` run — the machine-checkable half of the phase's
"the autopilot ran live and honestly" claim:

1. ``cost_usd > 0`` — the run was priced, not a stub.
2. ``elapsed_eu > 0`` — at least one actual captured runtime.
3. model set — every cost row names the billed model.
4. canonical runtime labels — every cost row's ``runtime`` is a canonical
   triple label, never a harness alias.
5. readable tail — the watch tail carries non-envelope prose lines.
6. campaign terminal — the recorded research campaign reached a terminal
   status (``converged`` / ``cancelled``), never left mid-flight.
7. jail smoke — the seatbelt spawn markers are present.
8. gate-executing closes — one passing ``run_close_gates`` line per
   claimed close in ``close_gates.log``.

Usage::

    uv run python tools/validate_drive_recording.py <recording-dir>

Exits 0 when all eight hold; prints one line per assertion.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

#: Canonical runtime triple labels: the short vendor-family names the
#: dispatch-cost writer emits. Harness aliases like ``claude-code`` are
#: telemetry-facing only and never appear on priced rows.
CANONICAL_RUNTIME_LABELS: frozenset[str] = frozenset({"claude", "codex", "opencode"})

#: Terminal campaign statuses — a recording with an ``active`` campaign was
#: cut mid-flight and proves nothing about convergence robustness.
TERMINAL_CAMPAIGN_STATUSES: frozenset[str] = frozenset({"converged", "cancelled"})


class RecordingInvalidError(ValueError):
    """Raised when one of the eight assertions fails."""


def _load_summary(recording_dir: Path) -> dict:
    summary_path = recording_dir / "summary.json"
    if not summary_path.is_file():
        raise RecordingInvalidError(f"missing summary.json under {recording_dir}")
    return json.loads(summary_path.read_text(encoding="utf-8"))


def _cost_rows(recording_dir: Path) -> list[dict]:
    path = recording_dir / "dispatch_cost.jsonl"
    if not path.is_file():
        raise RecordingInvalidError("missing dispatch_cost.jsonl")
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def validate_recording(recording_dir: Path) -> list[str]:
    """Run the eight assertions; return one confirmation line per check.

    Raises:
        RecordingInvalidError: On the first failing assertion, naming it.
    """
    summary = _load_summary(recording_dir)
    rows = _cost_rows(recording_dir)
    confirmations: list[str] = []

    total = float(summary.get("total_cost_usd") or 0)
    if total <= 0:
        raise RecordingInvalidError(f"assertion 1 failed: total_cost_usd={total} is not > 0")
    confirmations.append(f"1 cost_usd>0: {total}")

    actuals = summary.get("actuals") or {}
    captured = [a for a in actuals.values() if float(a.get("elapsed_eu") or 0) > 0]
    if not captured:
        raise RecordingInvalidError("assertion 2 failed: no actual carries elapsed_eu > 0")
    confirmations.append(f"2 elapsed_eu>0: {len(captured)} actual(s)")

    unmodeled = [r for r in rows if not r.get("model")]
    if unmodeled:
        raise RecordingInvalidError(
            f"assertion 3 failed: {len(unmodeled)} cost row(s) without model"
        )
    confirmations.append(f"3 model set on all {len(rows)} cost rows")

    bad_labels = sorted({r.get("runtime") for r in rows} - CANONICAL_RUNTIME_LABELS - {None})
    if bad_labels:
        raise RecordingInvalidError(
            f"assertion 4 failed: non-canonical runtime labels {bad_labels}"
        )
    confirmations.append("4 canonical runtime labels")

    tail = (recording_dir / "watch_tail.txt").read_text(encoding="utf-8").splitlines()
    prose = [line for line in tail if line.strip() and not line.lstrip().startswith(("{", "["))]
    if not prose:
        raise RecordingInvalidError("assertion 5 failed: watch tail has no readable prose lines")
    confirmations.append(f"5 readable tail: {len(prose)} prose line(s)")

    status = str(summary.get("campaign_terminal_status") or "")
    if status not in TERMINAL_CAMPAIGN_STATUSES:
        raise RecordingInvalidError(f"assertion 6 failed: campaign status {status!r} not terminal")
    confirmations.append(f"6 campaign terminal: {status}")

    jail = (recording_dir / "jail_smoke.log").read_text(encoding="utf-8")
    if "jail=on" not in jail or "sandbox-exec" not in jail:
        raise RecordingInvalidError("assertion 7 failed: jail smoke markers absent")
    confirmations.append("7 jail smoke markers present")

    # Tightened per the W35 review: the gate-executing-close claim is
    # machine-verified, not attested — one passing run_close_gates line
    # per claimed close.
    claimed = list(summary.get("gate_executing_closes") or [])
    gates_log = (recording_dir / "close_gates.log").read_text(encoding="utf-8")
    passing = gates_log.count("run_close_gates") and sum(
        1 for line in gates_log.splitlines() if "run_close_gates" in line and "passed=True" in line
    )
    if claimed and (not passing or passing < len(claimed)):
        raise RecordingInvalidError(
            f"gate-executing claim unverified: {len(claimed)} close(s) claimed, "
            f"{passing or 0} passing run_close_gates line(s)"
        )
    confirmations.append(f"8 gate-executing closes verified: {len(claimed)}")

    return confirmations


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: validate_drive_recording.py <recording-dir>", file=sys.stderr)
        return 2
    try:
        for line in validate_recording(Path(argv[1])):
            print(f"ok {line}")
    except RecordingInvalidError as exc:
        print(f"invalid: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
