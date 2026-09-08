"""Strictness census over the whole epoch-2 entity package.

Strictness is a property of the package, not of the models somebody
remembered to check. One model that omits ``extra="forbid"`` is enough to
let a misspelled field through a create document, and the record it
produces reads plausibly everywhere downstream. So the census walks every
module in the package, collects every model defined there, and asserts
the setting on each -- which is what makes a model added next year
inherit the guarantee without an author opting in.

The census also asserts it found something. A walk that silently collects
nothing would pass forever, which is the failure mode an architecture
test has to rule out about itself.
"""

from __future__ import annotations

import importlib
import inspect
import pkgutil
from types import ModuleType

import pytest
from pydantic import BaseModel, ValidationError

from eawf.kernel.state import epoch2

pytestmark = pytest.mark.contract

#: Models the census must find. A rename that drops one of these from the
#: package is a contract change, not a refactor.
REQUIRED_MODELS = frozenset(
    {
        "AcceptanceStep",
        "CampaignTemplateRef",
        "DeliveryBatch",
        "DurationBudget",
        "EntityOrigin",
        "EntityRef",
        "Epoch2Model",
        "Epoch2Record",
        "ExactRevisionBinding",
        "Hold",
        "MetricSpec",
        "Milestone",
        "MilestoneCreateSpec",
        "OwnerPrincipal",
        "PromotionRule",
        "RepoTrackScope",
        "Task",
        "Track",
        "TrackCreateSpec",
        "TrackPolicy",
        "TrackPresentationDefaults",
        "TransitionReason",
        "WipPolicy",
        "WorkspaceTrackScope",
    }
)


def _package_modules() -> list[ModuleType]:
    """Import and return every module of the epoch-2 package."""
    modules = [epoch2]
    for info in pkgutil.walk_packages(epoch2.__path__, prefix=f"{epoch2.__name__}."):
        modules.append(importlib.import_module(info.name))
    return modules


def _package_models() -> dict[str, type[BaseModel]]:
    """Return every Pydantic model defined inside the epoch-2 package."""
    found: dict[str, type[BaseModel]] = {}
    for module in _package_modules():
        for name, obj in inspect.getmembers(module, inspect.isclass):
            if not issubclass(obj, BaseModel):
                continue
            if not obj.__module__.startswith(epoch2.__name__):
                continue
            found[name] = obj
    return found


def test_package_model_census_is_not_empty() -> None:
    assert len(_package_models()) >= len(REQUIRED_MODELS)


def test_package_model_census_covers_every_required_model() -> None:
    assert set(_package_models()) >= REQUIRED_MODELS


@pytest.mark.parametrize("name", sorted(_package_models()))
def test_every_epoch2_model_forbids_extra_keys(name: str) -> None:
    model = _package_models()[name]
    assert model.model_config.get("extra") == "forbid", (
        f"{model.__module__}.{name} does not declare extra='forbid'"
    )


def test_every_epoch2_module_is_importable() -> None:
    names = {module.__name__ for module in _package_modules()}
    assert f"{epoch2.__name__}.base" in names
    assert f"{epoch2.__name__}.urns" in names


def test_a_sampled_model_actually_refuses_an_unknown_key() -> None:
    with pytest.raises(ValidationError, match=r"extra_forbidden|Extra inputs"):
        epoch2.OwnerPrincipal.model_validate(
            {"principal_kind": "operator", "principal_id": "OP-0001", "email": "x"}
        )


def test_the_frozen_value_object_refuses_mutation() -> None:
    binding = epoch2.ExactRevisionBinding(
        head_sha="a" * 40,
        tree_sha="b" * 40,
        contract_digest="sha256:" + "c" * 64,
        policy_revision=1,
        evidence_digest="sha256:" + "d" * 64,
    )
    with pytest.raises(ValidationError, match="frozen"):
        binding.head_sha = "e" * 40
