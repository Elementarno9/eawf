"""Managed certification: which enriched profiles may drive an unattended Run.

An enriched profile (:attr:`~eawf.platform.profiles.models.ProfileBody.is_enriched`)
injects its role-tier blocks into a dispatched agent's system prompt. It is
interactive-only until certified as managed, because an unattended Run has no
operator watching what those blocks tell the agent. A body is certified when
all three agree on one digest:

- the digest of the body's canonical content, recomputed at use;
- the body's own ``certification.digest``;
- the repository's committed ``profiles.certified`` ledger entry for the
  profile id, so certifying a body is a reviewed change to the repository.

A plain profile needs no certification.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping

from eawf.platform.profiles.models import ProfileBody


def profile_digest(body: ProfileBody) -> str:
    """Return the ``sha256:<hex>`` digest of *body* without its certification.

    Args:
        body: The validated profile body.

    Returns:
        The digest of the body's canonical JSON, keys sorted, with the
        ``certification`` block left out so the digest can be written into it.
    """
    canonical = json.dumps(
        body.model_dump(mode="json", exclude={"certification"}),
        sort_keys=True,
        separators=(",", ":"),
    )
    return f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"


def uncertified_enriched(
    bodies: Mapping[str, ProfileBody], ledger: Mapping[str, object]
) -> tuple[str, ...]:
    """Return the ids of the enriched bodies that are not certified as managed.

    Args:
        bodies: Profile id to its validated body.
        ledger: The ``profiles.certified`` ledger, profile id to pinned digest.

    Returns:
        The sorted ids of every enriched body whose declared digest is
        missing, stale against its content, or not the one the ledger pins.
    """
    refused: list[str] = []
    for profile_id, body in bodies.items():
        if not body.is_enriched:
            continue
        digest = profile_digest(body)
        declared = body.certification.digest if body.certification is not None else None
        if declared != digest or ledger.get(profile_id) != digest:
            refused.append(profile_id)
    return tuple(sorted(refused))


__all__ = ["profile_digest", "uncertified_enriched"]
