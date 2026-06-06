"""``svg_pixel_diff`` audit-DSL kind (Fidelity Spine FS16, T5 golden).

Renders an SVG via the ``resvg`` CLI and compares the produced PNG to a
committed golden PNG. This is the most expensive deterministic falsifier
in the SVG visual-fidelity oracle stack (T5), so the runner consults it
only after the cheaper T2 ``svg_well_formed`` check has passed.

Determinism assumption (dependency-free pixel diff)
---------------------------------------------------

``resvg`` is bit-identical across architectures ONCE two host-dependent
inputs are pinned: the font set and the resvg version. We pin both:

* Fonts are VENDORED (``--use-fonts-dir <dir>``) and system-font
  fallback is DISABLED (``--skip-system-fonts``), so the render never
  depends on whatever fonts the host happens to ship.
* The ``resvg`` version is pinned in the CI tool manifest
  (``.github/workflows/ci.yaml``). Any ``resvg`` version bump is a
  GOLDEN-REFRESH event: the rendered bytes can shift between resvg
  releases, so the committed golden must be regenerated (the refresh
  command lands in a sibling wave as ``eawf vfl approve``).

Because the render is bit-identical under those pins, the pixel diff is
realized as the strictest possible comparison: EXACT BYTE EQUALITY of
the rendered PNG against the committed golden (via
:func:`hashlib.sha256`). Byte equality is ratio-0, which strictly
satisfies the ``maxDiffPixelRatio <= 0.001`` criterion while adding NO
runtime dependency (no numpy / Pillow / pixelmatch).

Host without resvg
------------------

``resvg`` is absent on most developer machines. When
:func:`shutil.which` cannot find it, the kind returns
``status="blocked"`` (details ``"resvg not installed"``) so the local
gauntlet records a clean skip rather than a spurious failure; CI has the
pinned binary and runs the real comparison.
"""

from __future__ import annotations

import hashlib
import logging
import shutil
import subprocess
import tempfile
from pathlib import Path

from eawf.workflow.audit_dsl.models import CheckResult, CheckSpec

logger = logging.getLogger(__name__)

#: The renderer. Pinned to a concrete version in the CI tool manifest
#: (``.github/workflows/ci.yaml``); a version bump is a golden-refresh
#: event (see the module docstring).
_RESVG: str = "resvg"


def _require_path_arg(spec: CheckSpec, cwd: Path, key: str) -> tuple[Path | None, str | None]:
    """Resolve a required repo-relative (or absolute) path arg.

    Returns a ``(path, error)`` pair: exactly one is non-``None``.
    """
    value = spec.args.get(key)
    if not isinstance(value, str) or not value:
        return None, f"missing or non-str arg {key!r}"
    target = (cwd / value).resolve() if not Path(value).is_absolute() else Path(value)
    if not target.is_file():
        return None, f"{key}={value} not found"
    return target, None


def _require_path_arg_dir(spec: CheckSpec, cwd: Path, key: str) -> tuple[Path | None, str | None]:
    """Resolve a required repo-relative (or absolute) directory arg.

    Returns a ``(path, error)`` pair: exactly one is non-``None``.
    """
    value = spec.args.get(key)
    if not isinstance(value, str) or not value:
        return None, f"missing or non-str arg {key!r}"
    target = (cwd / value).resolve() if not Path(value).is_absolute() else Path(value)
    if not target.is_dir():
        return None, f"{key}={value} not a directory"
    return target, None


def check_svg_pixel_diff(spec: CheckSpec, cwd: Path) -> CheckResult:
    """Render an SVG via ``resvg`` and byte-compare the PNG to a golden.

    Args (read from ``spec.args``):
        svg: Repo-relative (or absolute) path to the source SVG.
        golden: Repo-relative (or absolute) path to the committed golden
            PNG.
        fonts_dir: Repo-relative (or absolute) path to the vendored
            font directory passed to ``resvg --use-fonts-dir``.

    Returns:
        :class:`CheckResult` with ``status="blocked"`` (details
        ``"resvg not installed"``) when ``resvg`` is absent;
        ``status="fail"`` when the args are malformed, the render fails,
        or the rendered PNG bytes differ from the golden;
        ``status="pass"`` when the rendered PNG is byte-identical to the
        golden. Never raises -- a bad criterion degrades to a failed (or
        blocked) check, not an aborted run.
    """
    if shutil.which(_RESVG) is None:
        logger.info(f"check_svg_pixel_diff blocked name={spec.name!r} reason=no-resvg")
        return CheckResult(
            name=spec.name,
            kind=spec.kind,
            passed=False,
            status="blocked",
            details="resvg not installed",
        )

    svg_path, svg_err = _require_path_arg(spec, cwd, "svg")
    if svg_err is not None:
        return CheckResult(
            name=spec.name, kind=spec.kind, passed=False, status="fail", details=svg_err
        )
    golden_path, golden_err = _require_path_arg(spec, cwd, "golden")
    if golden_err is not None:
        return CheckResult(
            name=spec.name, kind=spec.kind, passed=False, status="fail", details=golden_err
        )
    fonts_path, fonts_err = _require_path_arg_dir(spec, cwd, "fonts_dir")
    if fonts_err is not None:
        return CheckResult(
            name=spec.name, kind=spec.kind, passed=False, status="fail", details=fonts_err
        )
    assert svg_path is not None and golden_path is not None and fonts_path is not None

    with tempfile.TemporaryDirectory() as tmp:
        rendered = Path(tmp) / "rendered.png"
        argv = [
            _RESVG,
            "--use-fonts-dir",
            str(fonts_path),
            "--skip-system-fonts",
            str(svg_path),
            str(rendered),
        ]
        completed = subprocess.run(
            argv,
            check=False,
            capture_output=True,
            text=True,
            cwd=str(cwd),
        )
        if completed.returncode != 0 or not rendered.is_file():
            diagnostic = completed.stderr.strip() or completed.stdout.strip() or "render failed"
            logger.debug(
                f"check_svg_pixel_diff render-fail name={spec.name!r} rc={completed.returncode}"
            )
            return CheckResult(
                name=spec.name,
                kind=spec.kind,
                passed=False,
                status="fail",
                details=f"resvg render failed: {diagnostic}",
            )
        rendered_bytes = rendered.read_bytes()

    golden_bytes = golden_path.read_bytes()
    rendered_digest = hashlib.sha256(rendered_bytes).hexdigest()
    golden_digest = hashlib.sha256(golden_bytes).hexdigest()
    if rendered_digest == golden_digest:
        logger.debug(f"check_svg_pixel_diff ok name={spec.name!r} sha256={rendered_digest}")
        return CheckResult(
            name=spec.name,
            kind=spec.kind,
            passed=True,
            status="pass",
            details=(
                f"render matches golden (sha256={rendered_digest[:12]} bytes={len(golden_bytes)})"
            ),
        )
    logger.debug(
        f"check_svg_pixel_diff mismatch name={spec.name!r} "
        f"rendered_sha={rendered_digest[:12]} golden_sha={golden_digest[:12]}"
    )
    return CheckResult(
        name=spec.name,
        kind=spec.kind,
        passed=False,
        status="fail",
        details=(
            f"render differs from golden: rendered_bytes={len(rendered_bytes)} "
            f"golden_bytes={len(golden_bytes)} rendered_sha256={rendered_digest[:12]} "
            f"golden_sha256={golden_digest[:12]}"
        ),
    )


__all__ = ["check_svg_pixel_diff"]
