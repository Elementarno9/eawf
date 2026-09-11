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
from typing import Annotated, Final

import yaml
from pydantic import ConfigDict, Field

from eawf.kernel.release.gate_binding import profile_gates
from eawf.kernel.spec.common import _StrictModel
from eawf.kernel.spec.release import ReleaseCheckpoint

logger = logging.getLogger(__name__)

#: Top-level key every checkpoint configuration document is wrapped in.
CONFIG_ROOT_KEY: Final[str] = "release"


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
        platform_claims: Advertised platform rows, verbatim.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_branch: Annotated[str, Field(min_length=1)]
    require_signed_tag: bool
    require_clean_tree: bool
    require_ancestor_of_remote: bool
    targets: Annotated[tuple[dict[str, object], ...], Field(min_length=1)]
    platform_claims: tuple[dict[str, object], ...] = ()


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
    body: dict[str, object] = {
        "version": rung.version,
        "channel": rung.channel.value,
        "authority_epoch": rung.authority_epoch,
        "source_branch": template.source_branch,
        "require_signed_tag": template.require_signed_tag,
        "require_clean_tree": template.require_clean_tree,
        "require_ancestor_of_remote": template.require_ancestor_of_remote,
        "targets": [dict(target) for target in template.targets],
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
        f"targets={len(template.targets)}"
    )
    rendered: str = yaml.safe_dump(
        {CONFIG_ROOT_KEY: body}, sort_keys=False, default_flow_style=False
    )
    return rendered


__all__ = [
    "CONFIG_ROOT_KEY",
    "CheckpointConfigTemplate",
    "render_checkpoint_config",
]
