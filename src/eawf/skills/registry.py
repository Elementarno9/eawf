"""Lightweight in-process registry mapping :data:`SkillName` to concrete classes.

W01 froze the :class:`~eawf.skills.engine.Skill` ABC and the
:func:`~eawf.skills.engine.run_skill` orchestrator. W02/W03 will land the
six core + four meta concrete subclasses; until then the registry is
empty and ``eawf skill list`` reports every name as ``missing``.

Contract:

- :func:`register` is the canonical writer. Concrete skills decorate
  themselves at import time so a single ``import eawf.skills.research``
  is enough to make them visible.
- :func:`list_registered` returns a snapshot mapping. Callers that
  iterate must either materialise the result or hold the GIL (the dict
  is not protected by a lock — registration only happens at import).
- The registry refuses to register two classes for the same name; a
  collision raises :class:`ValueError` so a duplicate ``register``
  decorator does not silently shadow an earlier registration.

This module is intentionally small. It is not meant to evolve into a
plugin loader; the runtime adapter (W05) handles loading by importing
the concrete-skill modules explicitly.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping

from eawf.render.envelope import SkillName
from eawf.skills.engine import Skill

logger = logging.getLogger(__name__)


# Module-level dict; never reassign — only mutate via :func:`register`.
_REGISTRY: dict[SkillName, type[Skill]] = {}


def register(cls: type[Skill]) -> type[Skill]:
    """Register *cls* as the concrete implementation for ``cls.name``.

    Used as a decorator on concrete :class:`Skill` subclasses::

        @register
        class ResearchSkill(Skill):
            name: SkillName = "/research"
            ...

    Returns *cls* unchanged so the decorator is transparent.

    Raises:
        ValueError: ``cls.name`` is already bound to a different class.
            Re-registering the same class is a no-op.
    """
    name = cls.name
    existing = _REGISTRY.get(name)
    if existing is None:
        _REGISTRY[name] = cls
        return cls
    if existing is cls:
        return cls
    raise ValueError(
        f"skill name {name!r} already registered to {existing!r}; refusing to shadow with {cls!r}"
    )


def unregister(name: SkillName) -> None:
    """Drop *name* from the registry if present.

    Test-only helper: lets a test that registers a stub class clean up
    after itself so the registry is not polluted across tests.
    """
    _REGISTRY.pop(name, None)


def list_registered() -> Mapping[SkillName, type[Skill]]:
    """Return a snapshot mapping ``skill_name → registered class``.

    The returned mapping is a fresh ``dict`` so callers can iterate
    safely without observing concurrent registrations.
    """
    return dict(_REGISTRY)


def lookup(name: SkillName) -> type[Skill] | None:
    """Return the registered class for *name*, or ``None`` if missing."""
    return _REGISTRY.get(name)


__all__ = [
    "list_registered",
    "lookup",
    "register",
    "unregister",
]
