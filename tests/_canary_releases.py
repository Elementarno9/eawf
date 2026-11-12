"""The releases the canary evidence map names ahead of their canary walk.

Shared by the suites that read the map, so a release with no committed
export yet is exempted by name in one place rather than by a file
existence check that would also skip a deleted export.
"""

from __future__ import annotations

from typing import Final

#: Releases mapped in ``CANARY_EVIDENCE_DIRS`` whose canary export, commit
#: policy rows and ignore lines land in a later commit. Remove a key once
#: its export is committed.
UNRECORDED_RELEASES: Final[frozenset[str]] = frozenset()
