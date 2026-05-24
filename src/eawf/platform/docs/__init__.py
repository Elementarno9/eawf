"""Documentation generation surface for the Eä reference site.

The package houses the introspection-driven reference generator
(:mod:`eawf.platform.docs.autogen`) that the ``eawf schema dump`` and
``eawf doc verify --strict`` commands drive. Every page under
``docs/reference/autogen/`` is regenerated from the live source tree
(CLI command inventory, skill registry, Pydantic JSON Schema, the state
enum catalog, the cause-level ``ErrorCode`` vocabulary, and the exit-code
buckets) so a hand edit can never silently drift from the implementation.
"""

from __future__ import annotations
