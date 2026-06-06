"""``svg_well_formed`` audit-DSL kind (Fidelity Spine FS16, T2 structural).

Shells ``xmllint --noout`` over an SVG target and turns a clean parse
into a ``pass`` and a parser error into a ``fail`` carrying the
``line:col`` diagnostic. The kind is the cheapest structural falsifier
for the SVG visual-fidelity oracle stack: a candidate SVG that is not
well-formed XML can never render, so the T2 well-formedness check is
consulted before the expensive T5 pixel diff. The runner's
ascending-tier escalation guarantees that ordering -- a ``svg_well_formed``
fail at T2 means the SVG never reaches the ``svg_pixel_diff`` gate at
T5.

Target resolution
-----------------

The SVG target is read from ``spec.args``, accepting either:

* ``path`` -- a repo-relative path under ``cwd`` (or an absolute path)
  pointing at an ``.svg`` file. ``xmllint`` runs against the file
  directly.
* ``svg`` -- an inline SVG string. The string is written to a temporary
  file under the system temp dir and ``xmllint`` runs against that.

A common authoring mistake the kind catches: a Markdown-fenced SVG
(a ````svg ... ```` block) is NOT well-formed XML -- the backtick fence
makes ``xmllint`` exit non-zero -- so it degrades to ``status="fail"``
with the parser error in ``details``.

Failure handling
----------------

* A malformed ``args`` (neither ``path`` nor ``svg``, both set, or a
  non-str value) yields ``status="fail"`` -- never a raised exception.
* A missing ``path`` target yields ``status="fail"``.
* A missing ``xmllint`` executable yields ``status="blocked"`` (the
  host cannot run the check; CI has it pinned).
"""

from __future__ import annotations

import logging
import os
import subprocess
import tempfile
from pathlib import Path

from eawf.workflow.audit_dsl.models import CheckResult, CheckSpec

logger = logging.getLogger(__name__)

#: The well-formedness tool. ``--noout`` suppresses the reserialised
#: document so only the parser diagnostics reach stderr.
_XMLLINT: str = "xmllint"


def _resolve_svg_source(spec: CheckSpec, cwd: Path) -> tuple[Path | None, str | None]:
    """Resolve the SVG target to a concrete file path.

    Returns a ``(path, error)`` pair: exactly one is non-``None``.
    ``path`` is the file ``xmllint`` should parse (the resolved ``path``
    arg, or a temp file holding the inline ``svg`` string). ``error`` is
    a one-line note when the args are malformed or the path is missing.

    The temp file (for the inline ``svg`` arg) is intentionally NOT
    cleaned up here; the caller deletes it after ``xmllint`` runs.
    """
    path_arg = spec.args.get("path")
    svg_arg = spec.args.get("svg")
    has_path = path_arg is not None
    has_svg = svg_arg is not None
    if has_path == has_svg:
        return None, "exactly one of args 'path' or 'svg' is required"
    if has_path:
        if not isinstance(path_arg, str):
            return None, f"arg 'path' must be a str, got {type(path_arg).__name__}"
        target = (cwd / path_arg).resolve() if not Path(path_arg).is_absolute() else Path(path_arg)
        if not target.is_file():
            return None, f"path={path_arg} not found"
        return target, None
    if not isinstance(svg_arg, str):
        return None, f"arg 'svg' must be a str, got {type(svg_arg).__name__}"
    fd, tmp_name = tempfile.mkstemp(suffix=".svg")
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(svg_arg)
    return Path(tmp_name), None


def check_svg_well_formed(spec: CheckSpec, cwd: Path) -> CheckResult:
    """Run ``xmllint --noout`` over an SVG target and report well-formedness.

    Args (read from ``spec.args``):
        path: Repo-relative (or absolute) path to an SVG file.
        svg: Inline SVG string (mutually exclusive with ``path``).

    Returns:
        :class:`CheckResult` with ``status="pass"`` when ``xmllint``
        exits 0; ``status="fail"`` (with the ``xmllint`` parser-error
        string in ``details``) when the args are malformed, the path is
        missing, or the SVG is not well-formed XML; ``status="blocked"``
        when ``xmllint`` itself is not installed. Never raises -- a bad
        criterion degrades to a failed (or blocked) check, not an
        aborted run.
    """
    source, error = _resolve_svg_source(spec, cwd)
    if error is not None:
        logger.debug(f"check_svg_well_formed setup-fail name={spec.name!r} reason={error}")
        return CheckResult(
            name=spec.name,
            kind=spec.kind,
            passed=False,
            status="fail",
            details=error,
        )
    assert source is not None  # narrowed: error is None implies source is set
    is_inline = spec.args.get("path") is None
    try:
        try:
            completed = subprocess.run(
                [_XMLLINT, "--noout", str(source)],
                check=False,
                capture_output=True,
                text=True,
                cwd=str(cwd),
            )
        except FileNotFoundError:
            logger.info(f"check_svg_well_formed blocked name={spec.name!r} reason=no-xmllint")
            return CheckResult(
                name=spec.name,
                kind=spec.kind,
                passed=False,
                status="blocked",
                details=f"{_XMLLINT} not installed",
            )
    finally:
        if is_inline:
            source.unlink(missing_ok=True)

    if completed.returncode == 0:
        logger.debug(f"check_svg_well_formed ok name={spec.name!r}")
        return CheckResult(
            name=spec.name,
            kind=spec.kind,
            passed=True,
            status="pass",
            details="well-formed svg",
        )
    diagnostic = completed.stderr.strip() or completed.stdout.strip() or "parse error"
    logger.debug(
        f"check_svg_well_formed not-well-formed name={spec.name!r} rc={completed.returncode}"
    )
    return CheckResult(
        name=spec.name,
        kind=spec.kind,
        passed=False,
        status="fail",
        details=diagnostic,
    )


__all__ = ["check_svg_well_formed"]
