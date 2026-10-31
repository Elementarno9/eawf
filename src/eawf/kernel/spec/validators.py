"""Loader-side validators for the C03 spec stack.

Model-time checks live on the Pydantic models themselves and run at
``model_validate`` time. The loader-time functions here run after
``model_validate`` and enforce rules the schema defaults leave open.

The loader-time functions raise :class:`SpecValidationError` with a
diagnostic a CLI caller can re-emit verbatim. Per AGENTS rule 17, error
messages start lowercase, omit class-name prefixes, and use ``!r`` when
interpolating user-supplied values.
"""

from __future__ import annotations

import logging

from eawf.kernel.spec.phase import PhaseSpec

logger = logging.getLogger(__name__)


class SpecValidationError(ValueError):
    """Raised when loader-side spec validation fails.

    Inherits from :class:`ValueError` so the CLI surface can catch
    Pydantic's :class:`~pydantic.ValidationError` and this class with
    one ``except ValueError`` branch (CLI dispatch layer prints the
    diagnostic and exits non-zero).
    """


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
]
