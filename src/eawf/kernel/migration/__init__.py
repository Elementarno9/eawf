"""Epoch-to-epoch corpus migration.

Distinct from :mod:`eawf.kernel.migrations`, which steps the epoch-1
state document forward one schema version at a time. This package
carries the one-shot epoch-1 to epoch-2 cutover instead: a whole-corpus
import that mints a new tree rather than editing the old one in place.
"""

from __future__ import annotations
