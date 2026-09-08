"""Epoch-1 to epoch-2 importer rules.

Every disposition, status map, annotated default and classifier arm the
cutover applies is code in this package rather than importer discretion,
and each is published as a :class:`~eawf.kernel.migration.epoch2.rules.MappingRuleVersion`
carrying a digest over its own table. A reader of an imported record can
therefore name the exact rule revision that produced it.
"""

from __future__ import annotations
