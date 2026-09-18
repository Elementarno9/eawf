"""Every native domain parameter document is strict at its own boundary.

Two families meet here. The create documents -- ``TrackCreateSpec`` and
``MilestoneCreateSpec`` -- are the strict shapes a Track and a Milestone
are described by, and the lifecycle parameter models are the strict shapes
a move of one is asked for by. Both refuse an unknown key outright, so a
misspelled field is a loader rejection rather than a value that vanishes
between the client and the record.

The lifecycle models carry two fields no caller may omit. Without an
expected revision a mutation is a blind overwrite of whatever the record
has become; without an idempotency key a retry is a second mutation. Each
is asserted as a requirement rather than as a default, because a default
for either would be a silently wrong answer instead of a refusal.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from eawf.kernel.state.epoch2.milestone import MilestoneCreateSpec
from eawf.kernel.state.epoch2.track import TrackCreateSpec
from eawf.runtime.daemon.methods.domain import (
    DOMAIN_LIFECYCLE_METHODS,
    DOMAIN_LIFECYCLE_PARAMS,
    LifecycleParams,
    MilestoneAcceptParams,
)

#: Four levels up from this file lands on ``tests/``.
FIXTURES = Path(__file__).resolve().parents[3] / "fixtures" / "epoch2"

MILESTONE_URN = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/milestone/MLS-0030"
APPROVAL_URN = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/pending-action/ACT-0001"

#: The widest idempotency key the parameter models admit.
KEY_WIDTH = 128

PARAMS_MODELS: tuple[type[LifecycleParams], ...] = (LifecycleParams, MilestoneAcceptParams)


def _create_spec(name: str) -> dict[str, Any]:
    """Return one create-document fixture as a mutable payload."""
    return yaml.safe_load((FIXTURES / f"{name}.yaml").read_text(encoding="utf-8"))


def _params(**overrides: Any) -> dict[str, Any]:
    """Return a complete lifecycle request payload with *overrides* applied."""
    payload: dict[str, Any] = {
        "urn": MILESTONE_URN,
        "expected_revision": 1,
        "idempotency_key": "req-0001",
        "actor": "OP-0001",
    }
    payload.update(overrides)
    return payload


def test_track_create_spec_accepts_its_fixture() -> None:
    spec = TrackCreateSpec.model_validate(_create_spec("track_create_spec"))

    assert spec.key == "TRK-RUNTIME"


def test_track_create_spec_rejects_an_unknown_field() -> None:
    payload = _create_spec("track_create_spec")
    payload["status"] = "RETIRED"

    with pytest.raises(ValidationError, match="status"):
        TrackCreateSpec.model_validate(payload)


def test_milestone_create_spec_accepts_its_fixture() -> None:
    spec = MilestoneCreateSpec.model_validate(_create_spec("milestone_create_spec"))

    assert spec.key == "MLS-0030"


def test_milestone_create_spec_rejects_an_unknown_field() -> None:
    payload = _create_spec("milestone_create_spec")
    payload["accepted_binding"] = {}

    with pytest.raises(ValidationError, match="accepted_binding"):
        MilestoneCreateSpec.model_validate(payload)


@pytest.mark.parametrize("model", PARAMS_MODELS, ids=lambda model: model.__name__)
def test_params_reject_an_unknown_field(model: type[LifecycleParams]) -> None:
    with pytest.raises(ValidationError, match="to_status"):
        model.model_validate(_params(to_status="ACTIVE"))


@pytest.mark.parametrize("model", PARAMS_MODELS, ids=lambda model: model.__name__)
@pytest.mark.parametrize("missing", ["expected_revision", "idempotency_key", "urn", "actor"])
def test_params_require_every_unguessable_field(model: type[LifecycleParams], missing: str) -> None:
    payload = _params()
    payload.pop(missing)

    with pytest.raises(ValidationError, match=missing):
        model.model_validate(payload)


@pytest.mark.parametrize("model", PARAMS_MODELS, ids=lambda model: model.__name__)
@pytest.mark.parametrize(
    "revision",
    [
        pytest.param(0, id="zero"),
        pytest.param(-1, id="negative"),
        pytest.param(True, id="bool"),
        pytest.param(1.0, id="float"),
        pytest.param("1", id="string"),
        pytest.param(None, id="null"),
    ],
)
def test_params_reject_a_revision_that_is_not_a_positive_int(
    model: type[LifecycleParams], revision: Any
) -> None:
    with pytest.raises(ValidationError, match="expected_revision"):
        model.model_validate(_params(expected_revision=revision))


@pytest.mark.parametrize("model", PARAMS_MODELS, ids=lambda model: model.__name__)
@pytest.mark.parametrize(
    "key",
    [
        pytest.param("", id="empty"),
        pytest.param("x" * (KEY_WIDTH + 1), id="over-max-length"),
        pytest.param(None, id="null"),
        pytest.param(7, id="non-string"),
    ],
)
def test_params_reject_an_unusable_idempotency_key(model: type[LifecycleParams], key: Any) -> None:
    with pytest.raises(ValidationError, match="idempotency_key"):
        model.model_validate(_params(idempotency_key=key))


@pytest.mark.parametrize("model", PARAMS_MODELS, ids=lambda model: model.__name__)
def test_params_admit_the_boundary_revision_and_key(model: type[LifecycleParams]) -> None:
    """One is the first revision a record ever holds; the key is at its width."""
    parsed = model.model_validate(_params(expected_revision=1, idempotency_key="x" * KEY_WIDTH))

    assert parsed.expected_revision == 1
    assert len(parsed.idempotency_key) == KEY_WIDTH


@pytest.mark.parametrize("model", PARAMS_MODELS, ids=lambda model: model.__name__)
def test_params_reject_an_unparsable_urn(model: type[LifecycleParams]) -> None:
    with pytest.raises(ValidationError, match="urn"):
        model.model_validate(_params(urn="not-a-urn"))


@pytest.mark.parametrize("model", PARAMS_MODELS, ids=lambda model: model.__name__)
def test_params_reject_a_free_text_actor(model: type[LifecycleParams]) -> None:
    with pytest.raises(ValidationError, match="actor"):
        model.model_validate(_params(actor="not a principal"))


@pytest.mark.parametrize("model", PARAMS_MODELS, ids=lambda model: model.__name__)
def test_params_default_the_optional_fields_to_empty(model: type[LifecycleParams]) -> None:
    parsed = model.model_validate(_params())

    assert parsed.observations == ()
    assert parsed.binding_refs == ()
    assert parsed.updates == {}
    assert parsed.reason_code is None
    assert parsed.correlation_id is None


def test_accept_params_admit_a_pending_action_reference() -> None:
    parsed = MilestoneAcceptParams.model_validate(_params(approval_receipt_ref=APPROVAL_URN))

    assert parsed.approval_receipt_ref is not None
    assert parsed.approval_receipt_ref.entity_key == "ACT-0001"


def test_accept_params_default_the_approval_reference_to_absent() -> None:
    assert MilestoneAcceptParams.model_validate(_params()).approval_receipt_ref is None


def test_accept_params_reject_an_unparsable_approval_reference() -> None:
    with pytest.raises(ValidationError, match="approval_receipt_ref"):
        MilestoneAcceptParams.model_validate(_params(approval_receipt_ref="not-a-urn"))


def test_the_base_params_refuse_the_approval_reference() -> None:
    """Only the acceptance verb takes an approval; the others forbid the key."""
    with pytest.raises(ValidationError, match="approval_receipt_ref"):
        LifecycleParams.model_validate(_params(approval_receipt_ref=APPROVAL_URN))


def test_every_registered_verb_declares_a_params_model() -> None:
    assert tuple(DOMAIN_LIFECYCLE_PARAMS) == DOMAIN_LIFECYCLE_METHODS
    assert set(DOMAIN_LIFECYCLE_PARAMS.values()) <= set(PARAMS_MODELS)


def test_only_milestone_accept_parses_through_the_approval_model() -> None:
    approving = [
        method
        for method, model in DOMAIN_LIFECYCLE_PARAMS.items()
        if model is MilestoneAcceptParams
    ]

    assert approving == ["domain.milestone.accept"]
