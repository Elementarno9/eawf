"""Unit tests for the per-profile TrackKindSpec defaults.

Each of the six per-domain profiles declares exactly one
:class:`~eawf.platform.profiles.models.TrackKind` under ``track.kinds`` — its
default Track kind. A ``track add`` under a project on that profile picks the
profile's sole declared kind when the operator names no explicit ``--kind``.

The contracts under test:

- The quant profile resolves ``strategy`` as its default (sole) Track kind
  through profile composition (``load_composed_profile``).
- Each per-domain profile names its expected default kind: quant=strategy,
  ml=model, re=target, game=feature, apps=service, infra=service.
- A profile naming a kind with no registered TrackKindSpec fails composition —
  the closed ``TrackKind`` enum key type rejects a typo'd kind as a
  :class:`pydantic.ValidationError` at the load boundary, so a typo cannot
  silently resolve.
- Each declared TrackKindSpec carries a non-empty noun matching its kind.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from eawf.kernel.state.enums import TrackKind
from eawf.platform.profiles.loader import load_composed_profile, load_profile
from eawf.platform.profiles.models import ProfileBody, TrackProfileBlock

# Each per-domain profile maps to its single default Track kind.
_PROFILE_DEFAULT_KIND: dict[str, TrackKind] = {
    "quant": TrackKind.STRATEGY,
    "ml": TrackKind.MODEL,
    "re": TrackKind.TARGET,
    "game": TrackKind.FEATURE,
    "apps": TrackKind.SERVICE,
    "infra": TrackKind.SERVICE,
}


def _default_kind(block: TrackProfileBlock) -> TrackKind:
    """Return the sole declared kind of a single-kind profile track block.

    Raises:
        AssertionError: when the block declares zero or more than one kind, so
            the single-kind default invariant the profiles ship is enforced.
    """
    assert len(block.kinds) == 1, f"expected exactly one declared kind, got {list(block.kinds)}"
    return next(iter(block.kinds))


def test_quant_default_kind_resolves_strategy_through_composition() -> None:
    """The quant profile resolves ``strategy`` as its default Track kind."""
    composed = load_composed_profile(["quant"])
    assert composed.name == "quant"
    # The composed view validated the quant body through the closed schema, so
    # re-loading the contributing body surfaces the same validated track block.
    body = load_profile("quant")
    assert body.track is not None
    assert _default_kind(body.track) is TrackKind.STRATEGY
    spec = body.track.kinds[TrackKind.STRATEGY]
    assert spec.noun == "strategy"
    assert spec.outcome_template == "sharpe"
    assert spec.status_lifecycle[0] == "research"
    assert spec.status_lifecycle[-1] == "retired"


@pytest.mark.parametrize(("profile_id", "expected"), sorted(_PROFILE_DEFAULT_KIND.items()))
def test_per_profile_default_kind(profile_id: str, expected: TrackKind) -> None:
    """Each per-domain profile names its expected default Track kind."""
    body = load_profile(profile_id)
    assert body.track is not None, f"{profile_id} declares no track block"
    assert _default_kind(body.track) is expected
    spec = body.track.kinds[expected]
    # The noun is the operator-facing singular of the kind value.
    assert spec.noun == expected.value
    assert spec.status_lifecycle, f"{profile_id} declares an empty status lifecycle"
    assert spec.overview_view == "leaderboard"


def test_unregistered_kind_fails_composition() -> None:
    """A profile naming a kind with no registered TrackKindSpec fails to load.

    The closed ``TrackKind`` enum key type rejects a typo'd kind, so a typo
    cannot silently resolve to a default.
    """
    with pytest.raises(ValidationError):
        ProfileBody.model_validate(
            {
                "name": "typo-kind",
                "track": {
                    "kinds": {
                        # ``stratagem`` is not a member of the closed TrackKind enum.
                        "stratagem": {
                            "noun": "strategy",
                            "status_lifecycle": ["research", "live"],
                            "outcome_template": "sharpe",
                            "overview_view": "leaderboard",
                        }
                    }
                },
            }
        )


def test_known_kind_round_trips_through_track_block() -> None:
    """A registered kind validates and keys the spec under the enum member."""
    block = TrackProfileBlock.model_validate(
        {
            "kinds": {
                "strategy": {
                    "noun": "strategy",
                    "status_lifecycle": ["research", "live"],
                    "outcome_template": "sharpe",
                    "overview_view": "leaderboard",
                }
            }
        }
    )
    assert _default_kind(block) is TrackKind.STRATEGY
    assert block.kinds[TrackKind.STRATEGY].noun == "strategy"
