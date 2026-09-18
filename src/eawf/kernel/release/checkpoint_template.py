"""Render a rung's checkpoint configuration from the train-wide template.

A checkpoint configuration is mostly the same file every rung: the same
publication targets, the same source branch, the same three "the tree
must be clean, signed and reachable" flags. Only four things move --
the version, the channel, the authority epoch and the gate set -- and
every one of them is already declared on the rung.

That is why the later rungs are *rendered* rather than copied. The
obvious shortcut is to take the previous rung's authored file and
overlay the version and the channel onto it, and it is wrong in a way
that does not announce itself: the copy carries the previous rung's
``gates.profile`` and the previous rung's required gate list, so the new
checkpoint runs the old profile's gates under the new version's name.
The configuration loader catches that particular overlay -- it compares
the declared profile against the train's -- but only because the
mismatch happens to be visible in one field. Rendering removes the class
of mistake instead of the instance: the four moving parts are read off
the :class:`~eawf.kernel.spec.release.ReleaseCheckpoint`, and the
required gate list is read off :func:`profile_gates`, so there is no
second list anywhere that could disagree with the profile.

:class:`CheckpointConfigTemplate` is the part that does *not* move. It
is authored once per train and validated like any other ingested
document, so a malformed template fails here rather than as a confusing
rejection of the rendered configuration downstream.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from typing import Annotated, Final

import yaml
from pydantic import ConfigDict, Field

from eawf.kernel.release.gate_binding import profile_gates
from eawf.kernel.spec.common import _StrictModel
from eawf.kernel.spec.release import ReleaseCheckpoint, ReleaseGateProfile

logger = logging.getLogger(__name__)

#: Top-level key every checkpoint configuration document is wrapped in.
CONFIG_ROOT_KEY: Final[str] = "release"


#: Every gate profile in ladder order, which is the order the enum
#: declares them in and the order a train's rungs climb. The rank of a
#: profile is its position here, and nothing else orders profiles, so a
#: rung reached later can never rank before an earlier one.
_PROFILE_RANK: Final[Mapping[ReleaseGateProfile, int]] = {
    profile: rank for rank, profile in enumerate(ReleaseGateProfile)
}


class DeferredTargets(_StrictModel):
    """Target rows that join the configuration from one rung onward.

    A publication target declared on the train-wide template would land
    on every rung, including rungs already published. Those rungs bake
    on a frozen manifest their approval pinned, and the manifest is
    frozen from the targets their configuration declared -- so adding a
    target to an earlier rung retroactively changes a document a
    reviewer already approved and a record already bound. Deferring the
    row to the rung where the target first exists is what keeps the
    published half of the ladder fixed while the unpublished half grows.

    Attributes:
        from_profile: Earliest gate profile whose rungs carry these
            rows. Every rung at or after it in ladder order gets them;
            every rung before it renders exactly as it did.
        targets: Non-empty publication target rows, verbatim, appended
            after the always-present ones in publication order.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    from_profile: ReleaseGateProfile
    targets: Annotated[tuple[dict[str, object], ...], Field(min_length=1)]

    def applies_to(self, profile: ReleaseGateProfile) -> bool:
        """Return whether a rung running *profile* carries these rows.

        Args:
            profile: Gate profile of the rung being rendered.

        Returns:
            ``True`` when *profile* is at or after
            :attr:`from_profile` in ladder order.
        """
        return _PROFILE_RANK[profile] >= _PROFILE_RANK[self.from_profile]


class CheckpointConfigTemplate(_StrictModel):
    """The train-wide half of a checkpoint configuration.

    Every field here is the same for every rung of one train. The fields
    that differ per rung are deliberately absent: a template that could
    carry a version would be a configuration, and rendering it would be
    the overlay this module exists to avoid.

    Attributes:
        source_branch: Branch the source commit must be reachable from.
        require_signed_tag: Whether the tag must carry a signature.
        require_clean_tree: Whether a dirty worktree blocks the release.
        require_ancestor_of_remote: Whether the source must be reachable
            from the remote branch.
        targets: Publication target rows, verbatim, in publication
            order. Left as decoded mappings rather than typed rows
            because the configuration loader is the one boundary that
            validates them, and typing them twice would give a rendered
            document two chances to disagree with an authored one.
        deferred_targets: Target rows that join from one rung onward,
            or ``None`` when every rung publishes the same set.
        platform_claims: Advertised platform rows, verbatim.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_branch: Annotated[str, Field(min_length=1)]
    require_signed_tag: bool
    require_clean_tree: bool
    require_ancestor_of_remote: bool
    targets: Annotated[tuple[dict[str, object], ...], Field(min_length=1)]
    deferred_targets: DeferredTargets | None = None
    platform_claims: tuple[dict[str, object], ...] = ()

    def targets_for(self, profile: ReleaseGateProfile) -> list[dict[str, object]]:
        """Return the target rows a rung running *profile* publishes to.

        Args:
            profile: Gate profile of the rung being rendered.

        Returns:
            The always-present rows, followed by the deferred ones when
            the rung is at or after the profile they join at.
        """
        rows = [dict(target) for target in self.targets]
        deferred = self.deferred_targets
        if deferred is not None and deferred.applies_to(profile):
            rows.extend(dict(target) for target in deferred.targets)
        return rows


def render_checkpoint_config(
    *,
    rung: ReleaseCheckpoint,
    template: CheckpointConfigTemplate,
) -> str:
    """Return the YAML configuration text for *rung*.

    Args:
        rung: The train rung being configured. Its version, channel,
            authority epoch and gate profile are the four moving parts.
        template: The train-wide half of the configuration.

    Returns:
        A YAML document wrapped in the ``release:`` key, ready for
        :func:`~eawf.kernel.spec.release_config.load_release_config`.
        The gate list is every gate the rung's profile admits, in the
        profile's own declaration order.

    Raises:
        GateBindingError: When no gate set is declared for the rung's
            profile, which means the rung cannot be configured yet.
    """
    gates = profile_gates(rung.gate_profile)
    targets = template.targets_for(rung.gate_profile)
    body: dict[str, object] = {
        "version": rung.version,
        "channel": rung.channel.value,
        "authority_epoch": rung.authority_epoch,
        "source_branch": template.source_branch,
        "require_signed_tag": template.require_signed_tag,
        "require_clean_tree": template.require_clean_tree,
        "require_ancestor_of_remote": template.require_ancestor_of_remote,
        "targets": targets,
        "platform_claims": [dict(claim) for claim in template.platform_claims],
        "gates": {
            "profile": rung.gate_profile.value,
            "required": [gate.value for gate in gates],
        },
    }
    if not template.platform_claims:
        del body["platform_claims"]
    logger.info(
        f"render_checkpoint_config release_key={rung.release_key!r} "
        f"profile={rung.gate_profile.value!r} gates={len(gates)} "
        f"targets={len(targets)}"
    )
    rendered: str = yaml.safe_dump(
        {CONFIG_ROOT_KEY: body}, sort_keys=False, default_flow_style=False
    )
    return rendered


def with_membership_refs(
    source: str,
    *,
    membership_refs: Sequence[str],
) -> dict[str, object]:
    """Return the configuration in *source* carrying *membership_refs*.

    A rendered configuration cannot declare its membership bundles. Which
    acceptance bundles a checkpoint accepts is a fact about the record
    being cut, not about the train, so the template has nowhere to read
    them from and the rendered text leaves the list absent. Overlaying
    them here -- at the one boundary that holds both the rendered
    document and the record -- keeps the rung's own gate set rendered
    while still letting the loader run its membership-cardinality check
    against real refs.

    Args:
        source: Rendered or authored configuration text, wrapped in the
            :data:`CONFIG_ROOT_KEY` key.
        membership_refs: The record's acceptance bundle references. An
            empty sequence leaves the list empty, which the loader
            refuses for a rung that requires membership.

    Returns:
        The decoded document, ready for
        :func:`~eawf.kernel.spec.release_config.load_release_config`.

    Raises:
        ValueError: When *source* is not a mapping carrying a
            :data:`CONFIG_ROOT_KEY` mapping.
    """
    decoded = yaml.safe_load(source)
    if not isinstance(decoded, Mapping):
        raise ValueError(
            f"checkpoint configuration must be a mapping, got {type(decoded).__name__}"
        )
    body = decoded.get(CONFIG_ROOT_KEY)
    if not isinstance(body, Mapping):
        raise ValueError(
            f"checkpoint configuration must carry a {CONFIG_ROOT_KEY!r} mapping, "
            f"got {type(body).__name__}"
        )
    overlaid = dict(body)
    overlaid["membership_refs"] = list(membership_refs)
    return {CONFIG_ROOT_KEY: overlaid}


__all__ = [
    "CONFIG_ROOT_KEY",
    "CheckpointConfigTemplate",
    "DeferredTargets",
    "render_checkpoint_config",
    "with_membership_refs",
]
