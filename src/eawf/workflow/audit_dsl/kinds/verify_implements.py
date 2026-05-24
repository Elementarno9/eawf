"""``verify_implements`` audit-DSL kind (C03 W02).

Closes the RC-1 loop named in the C03 brief [3]: at the configured
cadence (per :class:`~eawf.kernel.spec.audit.AuditSpec.cadence`), walks every
closed-wave :class:`~eawf.kernel.spec.wave.WaveSpec` under a phase and greps
the ``git diff <diff_base>...HEAD`` output for verdict-id markers
restricted to each wave's :attr:`~eawf.kernel.spec.wave.WaveSpec.file_scopes`.
A missing marker fails the gate with a clear ``unmet
verify-implements`` diagnostic.

Marker grammar
--------------

Verdict markers are comment-style annotations carrying a
:data:`~eawf.kernel.spec.common.VerdictIdStr` identifier — the same regex
shape :class:`~eawf.kernel.spec.common.VerdictCitation` rejects malformed
ids with. Accepted host-comment prefixes:

* ``# IMPLEMENTS: V12``
* ``// IMPLEMENTS: V12-RC3``
* ``<!-- IMPLEMENTS: H03-12 -->``

The leading comment marker (``#``, ``//``, ``<!--``) is matched before
the verdict-id capture; trailing brief / line annotations are
optional and skipped here (the V/D/R/H id alone is the load-bearing
signal — the citation lives in the spec).

Spec loading
------------

The W03 daemon-mediated spec writer + loader is not yet shipped, so
this kind ships a self-contained frontmatter parser: it reads each
``.ea/specs/<phase_id>/**/*.md`` file, splits on the first / second
``---`` separators, and validates the YAML head as
:class:`~eawf.kernel.spec.wave.WaveSpec`. Files without a top-level
``kind: WaveSpec`` frontmatter row are skipped (PhaseSpec / IterSpec
files live in the same directory). When W03 lands its loader this
helper migrates to the canonical surface.

Cadence
-------

Per the C03 D10 lock (operator override 2026-05-16 /blitz) the
:class:`~eawf.kernel.spec.audit.AuditSpec.cadence` field configures *when*
this kind fires. The kind itself reads two args — ``cadence`` (the
AuditSpec value) and ``current_trigger`` (the close event firing the
audit) — and short-circuits with a pass + ``details="skipped"`` when
they don't match. Operator-run audits set ``current_trigger=manual``
to force the kind to evaluate.
"""

from __future__ import annotations

import logging
import re
import subprocess
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from eawf.kernel.spec.wave import WaveSpec
from eawf.workflow.audit_dsl.models import CheckResult, CheckSpec

logger = logging.getLogger(__name__)


# Verdict-id grammar borrowed from ``eawf.kernel.spec.common.VerdictIdStr``.
# The leading comment marker (``#``, ``//``, ``<!--``) is matched
# permissively so per-language comment prefixes are accepted without
# extra wiring.
VERDICT_MARKER_RE = re.compile(r"(?:#|//|<!--)\s*IMPLEMENTS:\s*([VDRH]\d+(?:-[A-Z0-9]+)?)")


# The four cadence values the AuditSpec.cadence Literal accepts. Mirror
# of :data:`eawf.kernel.spec.audit.AUDIT_CADENCE_VALUES` (declared here too so
# this module has no eager dependency on :mod:`eawf.kernel.spec.audit` for the
# cadence short-circuit — the spec model owns the contract, the kind
# applies it).
_VALID_CADENCES = {"every-wave", "every-iter", "every-phase", "manual"}


def _require_str(args: dict[str, Any], key: str, *, name: str, kind: str) -> str:
    value = args.get(key)
    if not isinstance(value, str):
        raise ValueError(f"check {name!r} kind={kind}: missing or non-str arg {key!r}")
    return value


def _parse_frontmatter(body: str) -> dict[str, Any] | None:
    """Extract the YAML frontmatter dict from a markdown spec body.

    Frontmatter convention: file opens with ``---\\n``, then YAML
    headers, then ``---\\n``, then markdown body. Returns ``None``
    when the file does not match the convention.

    Raises:
        ValueError: When the frontmatter region is not a YAML mapping.
    """
    if not body.startswith("---\n"):
        return None
    rest = body[4:]
    close_idx = rest.find("\n---\n")
    if close_idx == -1:
        close_idx = rest.find("\n---") if rest.endswith("\n---") else -1
    if close_idx == -1:
        return None
    head = rest[:close_idx]
    parsed = yaml.safe_load(head)
    if parsed is None:
        return None
    if not isinstance(parsed, dict):
        raise ValueError(f"frontmatter is not a mapping: {type(parsed).__name__}")
    return parsed


def _load_wave_specs(phase_dir: Path) -> list[WaveSpec]:
    """Walk ``phase_dir`` for spec files whose frontmatter is a WaveSpec.

    PhaseSpec / IterSpec files in the same tree are skipped (their
    frontmatter ``kind`` does not validate against :class:`WaveSpec`).
    """
    out: list[WaveSpec] = []
    for spec_path in sorted(phase_dir.rglob("*.md")):
        try:
            body = spec_path.read_text(encoding="utf-8")
        except OSError as exc:
            logger.debug(f"_load_wave_specs skip path={spec_path} reason={exc!r}")
            continue
        try:
            frontmatter = _parse_frontmatter(body)
        except ValueError as exc:
            logger.debug(f"_load_wave_specs skip path={spec_path} reason={exc!s}")
            continue
        if frontmatter is None:
            continue
        if frontmatter.get("kind") != "WaveSpec":
            continue
        try:
            out.append(WaveSpec.model_validate(frontmatter))
        except ValidationError as exc:
            # Malformed WaveSpec is an authoring bug surfaced elsewhere
            # (``eawf wave spec validate``); the audit kind logs + skips
            # so the rest of the phase's specs still get walked.
            logger.warning(f"_load_wave_specs invalid path={spec_path} errors={exc.error_count()}")
            continue
    return out


def _git_diff_files(cwd: Path, diff_base: str) -> set[str]:
    """Return the set of repo-relative paths changed between ``diff_base`` and ``HEAD``.

    Empty set when the git invocation fails (e.g. ``diff_base`` is
    unreachable, the repo is shallow, the directory is not a git tree).
    The caller surfaces the empty-diff case as a failed audit because
    no wave can be verified against an empty diff.
    """
    proc = subprocess.run(
        ["git", "diff", "--name-only", f"{diff_base}...HEAD"],
        check=False,
        capture_output=True,
        text=True,
        cwd=str(cwd),
    )
    if proc.returncode != 0:
        logger.debug(
            f"_git_diff_files non-zero rc={proc.returncode} stderr={proc.stderr.strip()!r}"
        )
        return set()
    return {line.strip() for line in proc.stdout.splitlines() if line.strip()}


def _verdict_ids_in_file(path: Path) -> set[str]:
    """Return the set of verdict ids appearing as ``IMPLEMENTS:`` markers in ``path``."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return set()
    return {match.group(1) for match in VERDICT_MARKER_RE.finditer(text)}


def _cadence_matches(cadence: str, current_trigger: str) -> bool:
    """Return True when the configured cadence should fire for the current trigger.

    The cadence values are exactly the four AuditSpec.cadence enum
    members: ``every-wave``, ``every-iter``, ``every-phase``,
    ``manual``. ``manual`` cadence only fires on a ``manual`` trigger;
    the three ``every-*`` cadences fire on their named trigger.
    """
    if cadence not in _VALID_CADENCES:
        raise ValueError(f"unknown cadence: {cadence!r}")
    if current_trigger not in _VALID_CADENCES:
        raise ValueError(f"unknown current_trigger: {current_trigger!r}")
    return cadence == current_trigger


def check_verify_implements(spec: CheckSpec, cwd: Path) -> CheckResult:
    """Walk closed-wave WaveSpecs + grep file_scopes for verdict markers.

    Args (read from ``spec.args``):
        phase_id: ``P##`` of the phase whose WaveSpecs are walked.
        diff_base: Git ref to diff ``HEAD`` against. Default ``main``.
        cadence: AuditSpec.cadence value — one of ``every-wave``,
            ``every-iter``, ``every-phase``, ``manual``. Default
            ``every-phase``.
        current_trigger: Close event firing the audit — same enum as
            ``cadence``. Default ``every-phase``.

    Returns:
        :class:`CheckResult` — ``passed=True`` when (a) cadence
        short-circuits, or (b) every closed WaveSpec under
        ``.ea/specs/<phase_id>/`` carries every verdict id from its
        ``implements`` list as a marker under at least one of its
        ``file_scopes`` in the diff. ``passed=False`` with
        ``details="unmet verify-implements: ..."`` otherwise.

    Raises:
        ValueError: When ``phase_id`` is missing / non-str, or when
            ``cadence`` / ``current_trigger`` are not one of the four
            enum values.
    """
    phase_id = _require_str(spec.args, "phase_id", name=spec.name, kind=spec.kind)
    diff_base = spec.args.get("diff_base", "main")
    if not isinstance(diff_base, str) or not diff_base:
        raise ValueError(
            f"check {spec.name!r} kind={spec.kind}: arg 'diff_base' must be a non-empty str"
        )
    cadence = spec.args.get("cadence", "every-phase")
    if not isinstance(cadence, str):
        raise ValueError(f"check {spec.name!r} kind={spec.kind}: arg 'cadence' must be a str")
    current_trigger = spec.args.get("current_trigger", "every-phase")
    if not isinstance(current_trigger, str):
        raise ValueError(
            f"check {spec.name!r} kind={spec.kind}: arg 'current_trigger' must be a str"
        )

    if not _cadence_matches(cadence, current_trigger):
        logger.debug(
            f"check_verify_implements skip cadence={cadence!r} current_trigger={current_trigger!r}"
        )
        return CheckResult(
            name=spec.name,
            kind=spec.kind,
            passed=True,
            details=f"skipped: cadence={cadence} trigger={current_trigger}",
        )

    phase_dir = cwd / ".ea" / "specs" / phase_id
    if not phase_dir.is_dir():
        return CheckResult(
            name=spec.name,
            kind=spec.kind,
            passed=False,
            details=f"no spec dir at .ea/specs/{phase_id}",
        )

    wave_specs = _load_wave_specs(phase_dir)
    if not wave_specs:
        return CheckResult(
            name=spec.name,
            kind=spec.kind,
            passed=False,
            details=f"no WaveSpec files under .ea/specs/{phase_id}",
        )

    changed = _git_diff_files(cwd, diff_base)
    logger.debug(
        f"check_verify_implements phase={phase_id} diff_base={diff_base!r} "
        f"changed_count={len(changed)} wave_count={len(wave_specs)}"
    )

    missing: list[str] = []
    for ws in wave_specs:
        verdict_ids = {citation.verdict_id for citation in ws.implements}
        scope_changed = sorted(p for p in ws.file_scopes if p in changed)
        if not scope_changed:
            for marker in sorted(verdict_ids):
                missing.append(
                    f"unmet verify-implements: wave={ws.id!r} expected_marker={marker} "
                    "(no file_scopes in diff)"
                )
            continue
        seen: set[str] = set()
        for path in scope_changed:
            seen |= _verdict_ids_in_file(cwd / path)
        unsatisfied = verdict_ids - seen
        for marker in sorted(unsatisfied):
            missing.append(f"unmet verify-implements: wave={ws.id!r} expected_marker={marker}")

    if missing:
        return CheckResult(
            name=spec.name,
            kind=spec.kind,
            passed=False,
            details="; ".join(missing),
        )
    return CheckResult(
        name=spec.name,
        kind=spec.kind,
        passed=True,
        details=(
            f"phase={phase_id} waves={len(wave_specs)} all WaveSpec.implements markers satisfied"
        ),
    )


__all__ = [
    "VERDICT_MARKER_RE",
    "check_verify_implements",
]
