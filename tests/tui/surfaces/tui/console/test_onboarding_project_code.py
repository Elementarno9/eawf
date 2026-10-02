"""Unit tests for :func:`eawf.surfaces.tui.console.onboarding.project_code`."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from eawf.surfaces.tui.console.onboarding import project_code
from tests.integration._memory_native import QR_DOCUMENT


def _state(tmp_path: Path, document: dict[str, Any]) -> Path:
    path = tmp_path / "state.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def test_the_state_names_its_project_code(tmp_path: Path) -> None:
    assert project_code(_state(tmp_path, QR_DOCUMENT)) == "QR"


def test_a_missing_state_records_no_code(tmp_path: Path) -> None:
    assert project_code(tmp_path / "state.json") is None


def test_a_state_without_a_project_records_no_code(tmp_path: Path) -> None:
    assert project_code(_state(tmp_path, {**QR_DOCUMENT, "project": None})) is None


@pytest.mark.parametrize(
    "document",
    [
        {**QR_DOCUMENT, "project": {**QR_DOCUMENT["project"], "smuggled": "x"}},
        {"project": {"code": "QR"}},
    ],
)
def test_a_state_that_does_not_validate_records_no_code(
    tmp_path: Path, document: dict[str, Any]
) -> None:
    assert project_code(_state(tmp_path, document)) is None


def test_a_state_that_is_not_json_records_no_code(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text("{not json", encoding="utf-8")
    assert project_code(path) is None
