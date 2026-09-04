"""Tests for the canonical skill body-model registry.

The 17-entry skill-name -> body-model map lives in the library at
:data:`eawf.workflow.skills.bodies.SKILL_BODY_MODELS` so the engine can
bind it without an upward import into the CLI (CLI-is-dispatch rule).
The CLI ``_skill_body_models`` re-exports the same object, which keeps
the ``skill list`` fingerprint column byte-stable.
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from eawf.surfaces.render.envelope import CANONICAL_SKILL_NAMES
from eawf.workflow.skills.bodies import SKILL_BODY_MODELS, body_model_for


def test_body_model_for_resolves_every_canonical_name() -> None:
    """Every key in the registry resolves to a ``BaseModel`` subclass."""
    assert SKILL_BODY_MODELS, "registry must not be empty"
    for name in SKILL_BODY_MODELS:
        model = body_model_for(name)
        assert isinstance(model, type)
        assert issubclass(model, BaseModel), name


def test_body_model_for_covers_all_canonical_skill_names() -> None:
    """The registry key set equals the frozen canonical skill-name set."""
    assert set(SKILL_BODY_MODELS) == set(CANONICAL_SKILL_NAMES)


def test_body_model_for_unknown_name_raises_keyerror() -> None:
    """An unknown skill name raises ``KeyError`` (never returns ``None``)."""
    with pytest.raises(KeyError, match="unknown skill: '/not-a-skill'"):
        body_model_for("/not-a-skill")


def test_body_model_for_bare_name_raises_keyerror() -> None:
    """A bare (unslashed) name is not canonical and raises ``KeyError``."""
    with pytest.raises(KeyError, match="research"):
        body_model_for("research")


def test_body_model_for_empty_name_raises_keyerror() -> None:
    """The empty string is not a canonical name and raises ``KeyError``."""
    with pytest.raises(KeyError, match="unknown skill: ''"):
        body_model_for("")


def test_cli_reexport_is_the_same_object() -> None:
    """The CLI ``_skill_body_models`` returns the library map identity.

    Identity (not just equality) is the contract: the ``skill list``
    fingerprint column derives ``f"{cls.__module__}.{cls.__qualname__}"``
    from these classes, so sharing the one object keeps the rendered
    column byte-stable across the engine and CLI surfaces.
    """
    from eawf.surfaces.cli.commands.skill import _skill_body_models

    assert _skill_body_models() is SKILL_BODY_MODELS


def test_cli_reexport_fingerprints_are_byte_stable() -> None:
    """Each re-exported model yields its library-module fingerprint.

    Moving the map into ``bodies/__init__`` must not change any class's
    ``__module__`` (the classes still live in their per-skill modules),
    so the dotted fingerprint stays anchored under
    ``eawf.workflow.skills.bodies.<skill>``.
    """
    from eawf.surfaces.cli.commands.skill import _skill_body_models

    models = _skill_body_models()
    for name, cls in models.items():
        fingerprint = f"{cls.__module__}.{cls.__qualname__}"
        assert fingerprint.startswith("eawf.workflow.skills.bodies."), (name, fingerprint)
