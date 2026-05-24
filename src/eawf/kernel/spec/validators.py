"""Loader-side validators for the C03 spec stack.

Three classes of validation:

1. **Model-time** — Pydantic ``model_validator`` hooks live on the
   models themselves (e.g. :class:`~eawf.kernel.spec.wave.WaveSpec._consistent_ids`
   and :class:`~eawf.kernel.spec.wave.WaveSpec._mockup_required`). They run at
   ``model_validate`` time and need no filesystem or state-tree lookup.
2. **Loader-time** — these functions run AFTER ``model_validate`` and
   need a project root or state tree. They enforce WSV-05 / WSV-06 /
   WSV-10 (tests exist on disk, brief paths exist on disk).
3. **Pre-commit** — out of scope for this module; lives in
   ``tools/pre_commit_spec_paths.py``.

The loader-time functions raise :class:`SpecValidationError` with a
multi-line diagnostic so a CLI caller can re-emit it verbatim. Per
AGENTS rule 17, error messages start lowercase, omit class-name
prefixes, and use ``!r`` when interpolating user-supplied paths.
"""

from __future__ import annotations

import logging
from pathlib import Path

from eawf.kernel.spec.heuristics import missing_test_paths
from eawf.kernel.spec.phase import PhaseSpec
from eawf.kernel.spec.wave import WaveSpec

logger = logging.getLogger(__name__)


class SpecValidationError(ValueError):
    """Raised when loader-side spec validation fails.

    Inherits from :class:`ValueError` so the CLI surface can catch
    Pydantic's :class:`~pydantic.ValidationError` and this class with
    one ``except ValueError`` branch (CLI dispatch layer prints the
    diagnostic and exits non-zero).
    """


def validate_wave_spec_tests_exist(
    spec: WaveSpec,
    project_root: Path,
) -> None:
    """Enforce WSV-05 + WSV-06: every test path on disk.

    Walks ``spec.tests`` and every ``behavior.test_refs[*]`` checking
    each path resolves to a regular file under ``project_root``. Missing
    paths are batched into one diagnostic so the operator sees the full
    list, not one path per re-run.

    Args:
        spec: Wave spec to validate.
        project_root: Repo root to resolve repo-relative refs against.

    Raises:
        SpecValidationError: when at least one test ref does not resolve
            to an existing file on disk.
    """
    aggregated: list[str] = list(spec.tests)
    for behavior in spec.behaviors:
        aggregated.extend(behavior.test_refs)
    missing = missing_test_paths(aggregated, project_root)
    if missing:
        joined = ", ".join(repr(ref) for ref in missing)
        raise SpecValidationError(
            f"validate_wave_spec_tests_exist wave={spec.id!r} missing test paths: {joined}"
        )


def validate_wave_spec_brief_paths_exist(
    spec: WaveSpec,
    project_root: Path,
) -> None:
    """Enforce WSV-10: every ``implements[*].brief`` exists on disk.

    Args:
        spec: Wave spec to validate.
        project_root: Repo root to resolve repo-relative brief paths
            against.

    Raises:
        SpecValidationError: when at least one cited brief path does
            not resolve to an existing file on disk.
    """
    missing = [cit.brief for cit in spec.implements if not (project_root / cit.brief).is_file()]
    if missing:
        joined = ", ".join(repr(brief) for brief in missing)
        raise SpecValidationError(
            f"validate_wave_spec_brief_paths_exist wave={spec.id!r} missing brief paths: {joined}"
        )


def validate_phase_spec_has_kpis(spec: PhaseSpec) -> None:
    """Enforce PSV-01 (loader): PhaseSpec carries at least one KPI.

    The Pydantic schema lets ``kpis`` default to the empty list so
    test fixtures stay terse, but the loader rejects empty lists at
    READY graduation per goal G2 [§2 G2]: "Every phase opened after C03
    carries a PhaseSpec with outcome statement + KPI(s) + ship
    criteria".

    Args:
        spec: Phase spec to validate.

    Raises:
        SpecValidationError: when ``spec.kpis`` is empty.
    """
    if not spec.kpis:
        raise SpecValidationError(
            f"validate_phase_spec_has_kpis phase={spec.id!r} kpis=[] "
            "(at least one PhaseKPI required at READY)"
        )


def validate_wave_spec_at_load(
    spec: WaveSpec,
    project_root: Path,
) -> None:
    """Run every loader-side validator on a WaveSpec.

    Aggregates :func:`validate_wave_spec_tests_exist` and
    :func:`validate_wave_spec_brief_paths_exist`. The mockup-required
    heuristic + non-empty list constraints + id-nesting check are
    already enforced by Pydantic at ``model_validate`` time, so this
    helper only adds the disk-lookup checks.

    Args:
        spec: Wave spec to validate.
        project_root: Repo root for disk lookups.

    Raises:
        SpecValidationError: when any loader-side check fails.
    """
    validate_wave_spec_tests_exist(spec, project_root)
    validate_wave_spec_brief_paths_exist(spec, project_root)


def validate_phase_spec_at_load(spec: PhaseSpec) -> None:
    """Run every loader-side validator on a PhaseSpec.

    Aggregates :func:`validate_phase_spec_has_kpis`. PhaseSpec
    non-empty fields (``failure_modes`` + ``ship_criteria``) are
    already enforced by Pydantic; the KPI presence rule lives here
    because the schema default is the empty list.

    Args:
        spec: Phase spec to validate.

    Raises:
        SpecValidationError: when any loader-side check fails.
    """
    validate_phase_spec_has_kpis(spec)


__all__ = [
    "SpecValidationError",
    "validate_phase_spec_at_load",
    "validate_phase_spec_has_kpis",
    "validate_wave_spec_at_load",
    "validate_wave_spec_brief_paths_exist",
    "validate_wave_spec_tests_exist",
]
