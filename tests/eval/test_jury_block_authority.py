"""The jury-authority config leaf refuses floors outside its range at load."""

from __future__ import annotations

import pytest

from eawf.observability.eval.jury_validation import JuryAuthorityConfig

# --- config-leaf boundary --------------------------------------------------


def test_authority_config_rejects_zero_min_labeled_waves() -> None:
    """A zero labelled-wave floor fails validation at the load boundary.

    ``min_labeled_waves`` is ``Field(ge=1)`` -- a zero floor would defeat the
    earned-authority guarantee, so it is rejected as a ValidationError.
    """
    with pytest.raises(ValueError, match="min_labeled_waves"):
        JuryAuthorityConfig(min_labeled_waves=0)


def test_authority_config_rejects_out_of_range_catch_floor() -> None:
    """A catch-rate floor above 1.0 fails validation at the load boundary."""
    with pytest.raises(ValueError, match="known_bad_catch_lb_floor"):
        JuryAuthorityConfig(known_bad_catch_lb_floor=1.5)
