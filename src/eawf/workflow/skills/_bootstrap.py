"""Import-side-effect bootstrap for the catalog-backed Skill subclasses.

Importing this module is the canonical way to ensure every ``Skill`` subclass
that backs a presented catalog skill is registered with
:mod:`eawf.workflow.skills.registry`. Each skill module is imported for its
registration decorator (``@register``); the imports are otherwise unused.

Only catalog skills are imported. A retired skill's module is not bootstrapped:
its class may still register when another module imports it internally, but
:func:`eawf.workflow.skills.registry.lookup` refuses a retired name and names
its successor, so no operator surface can dispatch it.

The CLI surface (`eawf.surfaces.cli.commands.skill`) imports this module so
``eawf skill list`` reports each catalog skill with a class as ``installed``.

A dedicated module avoids two failure modes:

1. Putting the imports in :mod:`eawf.workflow.skills.__init__` would force every
   importer of ``eawf.workflow.skills`` to pay the registration cost — including
   tests that explicitly want the registry empty.
2. Putting the imports inside the CLI handler at call time would force
   the import on every command (slows ``--help``).

Importing this module is idempotent: the registration decorator
short-circuits when the same class re-registers, and re-importing a
Python module is a no-op.
"""

from __future__ import annotations

# Each skill subclass registers itself via the ``@register`` decorator at
# import time. ``noqa: F401`` keeps them in the import graph despite the
# unused-name lint.
from eawf.workflow.skills import dispatch as _dispatch  # noqa: F401
from eawf.workflow.skills import integrate as _integrate  # noqa: F401
from eawf.workflow.skills import memory as _memory  # noqa: F401
from eawf.workflow.skills import research as _research  # noqa: F401
from eawf.workflow.skills import verify as _verify  # noqa: F401

__all__: list[str] = []
