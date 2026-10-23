"""Eä skills package.

The package builds on the frozen skill output envelope
(:mod:`eawf.surfaces.render.envelope`) and holds the :class:`Skill` ABC, the
:func:`run_skill` orchestrator (:mod:`eawf.workflow.skills.engine`) and the
per-skill body schemas under :mod:`eawf.workflow.skills.bodies`. Each skill
subclass carries its production ``probe → action → envelope`` implementation.

This file is intentionally empty of business logic so importing
``eawf.workflow.skills`` is cheap and side-effect-free.
"""

from __future__ import annotations
