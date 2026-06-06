"""Test helpers for building typed wave success criteria.

The ``1.6 -> 1.7`` state migration retyped
:attr:`eawf.kernel.state.models.Wave.success_criteria` from ``list[str]`` into
``list[CriterionSpec]``. Tests that previously fed bare strings build
grandfathered rows through :func:`legacy_criteria` to keep churn low.
"""

from __future__ import annotations

from eawf.kernel.spec.common import CriterionSpec, grandfather_criterion


def legacy_criteria(*texts: str) -> list[CriterionSpec]:
    """Return grandfathered :class:`CriterionSpec` rows for *texts*.

    Mirrors the production grandfather path so a test row and a migrated
    on-disk row are indistinguishable. Each text becomes one row with a
    1-based ``CR-<index>`` id.

    Args:
        *texts: Legacy success-criterion strings.

    Returns:
        One grandfathered :class:`CriterionSpec` per text, in order.
    """
    return [grandfather_criterion(text, index=idx) for idx, text in enumerate(texts, start=1)]
