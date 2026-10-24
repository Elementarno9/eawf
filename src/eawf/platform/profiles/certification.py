"""The content digest of a profile body.

:func:`profile_digest` is the digest an enriched profile
(:attr:`~eawf.platform.profiles.models.ProfileBody.is_enriched`) records in
its own ``certification.digest`` and ``eawf profile validate`` reports.
"""

from __future__ import annotations

import hashlib
import json

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


__all__ = ["profile_digest"]
