"""Import-side-effect bootstrap for the ten Skill subclasses.

Importing this module is the canonical way to ensure all ten ``Skill``
subclasses (six core + four meta) are registered with
:mod:`eawf.skills.registry`. Each skill module is imported for its
registration decorator (``@register``); the imports are otherwise
unused.

The CLI surface (`eawf.cli.commands.skill`) imports this module so
``eawf skill list`` reports every skill as ``installed`` once W02 + W03
have landed.

A dedicated module avoids two failure modes:

1. Putting the imports in :mod:`eawf.skills.__init__` would force every
   importer of ``eawf.skills`` to pay the registration cost — including
   tests that explicitly want the registry empty (e.g. the W07
   ``test_skill_list_shows_all_ten_names_missing_by_default`` predates
   W02 and unregisters first).
2. Putting the imports inside the CLI handler at call time would force
   the import on every command (slows ``--help``).

Importing this module is idempotent: the registration decorator
short-circuits when the same class re-registers, and re-importing a
Python module is a no-op.
"""

from __future__ import annotations

# The ten skill subclasses each register themselves via the ``@register``
# decorator at import time. ``noqa: F401`` keeps them in the import graph
# despite the unused-name lint. Order: six core (W02) then four meta
# (W03); ``flow`` imports the six core skills, so it must come last.
from eawf.skills import audit as _audit  # noqa: F401
from eawf.skills import blitz as _blitz  # noqa: F401
from eawf.skills import differentiate as _differentiate  # noqa: F401
from eawf.skills import flow as _flow  # noqa: F401
from eawf.skills import init as _init  # noqa: F401
from eawf.skills import polish as _polish  # noqa: F401
from eawf.skills import prep as _prep  # noqa: F401
from eawf.skills import research as _research  # noqa: F401
from eawf.skills import review as _review  # noqa: F401
from eawf.skills import roadmap as _roadmap  # noqa: F401
from eawf.skills import ship as _ship  # noqa: F401

__all__: list[str] = []
