"""One validator every native verb parses its request through.

Seven daemon verb modules each carried their own copy of the same
validator; they now route through :func:`native_params`. The copies
differed only in the model they named, so this pins the one behaviour
they shared: the fence's repository key is dropped before the closed
model sees the request, and a refusal names field paths, never a value.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict

from eawf.runtime.daemon.methods import DaemonValidationError
from eawf.runtime.daemon.native_guard import REPO_ROOT_PARAM, native_params


class _Params(BaseModel):
    """A closed two-field request, as every native verb's params are."""

    model_config = ConfigDict(extra="forbid")

    urn: str
    count: int


def test_r09_native_params_drops_the_fence_key_before_validating() -> None:
    parsed = native_params(_Params, {REPO_ROOT_PARAM: "/somewhere", "urn": "u", "count": 1})

    assert parsed == _Params(urn="u", count=1)


def test_r09_native_params_admits_a_request_without_the_fence_key() -> None:
    assert native_params(_Params, {"urn": "u", "count": 0}).count == 0


@pytest.mark.parametrize(
    ("params", "fields"),
    [
        pytest.param({}, "count, urn", id="empty"),
        pytest.param({"urn": "u"}, "count", id="missing-key"),
        pytest.param({"urn": "u", "count": "many"}, "count", id="wrong-type"),
        pytest.param({"urn": "u", "count": 1, "extra": 2}, "extra", id="unknown-field"),
    ],
)
def test_r09_native_params_names_the_failing_fields(params: dict[str, Any], fields: str) -> None:
    with pytest.raises(DaemonValidationError) as caught:
        native_params(_Params, params)

    assert str(caught.value) == f"validation_failed: schema_validation_failed: check {fields}"


def test_r09_native_params_never_repeats_a_submitted_value() -> None:
    with pytest.raises(DaemonValidationError) as caught:
        native_params(_Params, {"urn": "u", "count": "a-secret-looking-value"})

    assert "a-secret-looking-value" not in str(caught.value)
