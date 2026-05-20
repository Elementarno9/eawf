"""Import-side-effect bootstrap for the canonical Skill subclasses.

Importing this module is the canonical way to ensure every builtin
``Skill`` subclass is registered with :mod:`eawf.skills.registry`. Each
skill module is imported for its registration decorator (``@register``);
the imports are otherwise unused. The catalog spans the six core + four
meta workflow skills, ``/blitz`` (P15), and the six C04b skills
(``/coauthor``, ``/memory``, ``/agent-dispatch``, ``/compress``,
``/wave-spec``, ``/security-review``).

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

# Each skill subclass registers itself via the ``@register`` decorator at
# import time. ``noqa: F401`` keeps them in the import graph despite the
# unused-name lint. The block is isort-sorted; import order is irrelevant
# to registration because ``flow`` imports the six core skills at its own
# module level (so they register regardless of the line order here).
from eawf.skills import agent_dispatch as _agent_dispatch  # noqa: F401
from eawf.skills import audit as _audit  # noqa: F401
from eawf.skills import blitz as _blitz  # noqa: F401
from eawf.skills import coauthor as _coauthor  # noqa: F401
from eawf.skills import compress as _compress  # noqa: F401
from eawf.skills import differentiate as _differentiate  # noqa: F401
from eawf.skills import flow as _flow  # noqa: F401
from eawf.skills import init as _init  # noqa: F401
from eawf.skills import memory as _memory  # noqa: F401
from eawf.skills import polish as _polish  # noqa: F401
from eawf.skills import prep as _prep  # noqa: F401
from eawf.skills import research as _research  # noqa: F401
from eawf.skills import review as _review  # noqa: F401
from eawf.skills import roadmap as _roadmap  # noqa: F401
from eawf.skills import security_review as _security_review  # noqa: F401
from eawf.skills import ship as _ship  # noqa: F401
from eawf.skills import wave_spec as _wave_spec  # noqa: F401

__all__: list[str] = []
