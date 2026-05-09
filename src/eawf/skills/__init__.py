"""Eä skills package.

Phase 4 W01 freezes the skill output envelope (:mod:`eawf.render.envelope`),
the :class:`Skill` ABC and :func:`run_skill` orchestrator
(:mod:`eawf.skills.engine`), and the per-skill body schemas under
:mod:`eawf.skills.bodies`. Phase 4 W02/W03 fill in the production
``probe → action → envelope`` implementations for the ten skills.

This file is intentionally empty of business logic so importing
``eawf.skills`` is cheap and side-effect-free.
"""

from __future__ import annotations
