"""A config layer that names an epoch-1 cadence is rewritten to the native value once."""

from __future__ import annotations

from typing import Any

import pytest

from eawf.kernel.config.migration import migrate_config_payload


def _layer(*cadences: Any) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "verify": {
            "floor_checks": [
                {"name": f"check-{index}", "cadence": cadence}
                for index, cadence in enumerate(cadences)
            ],
        },
    }


def _cadences(body: dict[str, Any]) -> list[Any]:
    return [row["cadence"] for row in body["verify"]["floor_checks"]]


def test_legacy_cadence_values_migrate_once() -> None:
    written = _layer("every-wave", "every-iter", "every-phase", "ship", "manual", "every-task")
    written["gate"] = {"cadence": "every-phase", "label": "every-wave"}

    upgraded, changed = migrate_config_payload(written)

    assert changed is True
    assert _cadences(upgraded) == [
        "every-task",
        "every-batch",
        "every-milestone",
        "ship",
        "manual",
        "every-task",
    ]
    assert upgraded["gate"] == {"cadence": "every-milestone", "label": "every-wave"}
    assert _cadences(written)[0] == "every-wave"

    again, changed_again = migrate_config_payload(upgraded)

    assert changed_again is False
    assert again == upgraded


def test_a_legacy_marker_layer_also_migrates_its_cadences() -> None:
    written = {**_layer("every-iter"), "schema_version": "1.1"}

    upgraded, changed = migrate_config_payload(written)

    assert changed is True
    assert _cadences(upgraded) == ["every-batch"]


@pytest.mark.parametrize(
    "body",
    [
        {"schema_version": "1.0"},
        _layer(),
        _layer("every-task"),
        _layer("ship", "manual", "every-milestone"),
        _layer(None, 3, ["every-wave"], "every-run"),
        {"schema_version": "1.0", "notes": ["every-wave"]},
    ],
)
def test_a_layer_without_a_legacy_cadence_is_left_unchanged(body: dict[str, Any]) -> None:
    upgraded, changed = migrate_config_payload(body)

    assert changed is False
    assert upgraded == body
