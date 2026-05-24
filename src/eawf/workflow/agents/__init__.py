"""Subagent spec + role library for Eä dispatch (P27-I03-W14).

The :mod:`eawf.workflow.agents.specs` subpackage owns the typed
:class:`~eawf.workflow.agents.specs.models.SubagentSpec` model and the role
registry. The dispatch renderer (:mod:`eawf.workflow.dispatch.renderer`) builds a
``SubagentSpec`` from a validated :class:`~eawf.kernel.state.models.State`
snapshot and renders it, so the wave prompt is produced from a typed
spec rather than an ad-hoc string concatenation.

The package itself carries no business logic — importing
``eawf.workflow.agents`` is cheap and side-effect-free.
"""

from __future__ import annotations
