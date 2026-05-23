"""Unit tests for :mod:`eawf.config.loader`.

Contracts under test:

- Missing file → empty dict (not an error). Layered merge can compose absent
  layers transparently.
- Empty / whitespace-only file → empty dict.
- Valid YAML mapping → parsed dict.
- Malformed YAML → :class:`eawf.cli.errors.ValidationFailed`.
- Top-level non-mapping (list, scalar) → :class:`ValidationFailed`.
- Non-string top-level keys → :class:`ValidationFailed`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from eawf.cli.errors import ValidationError
from eawf.config.loader import load_yaml_layer


def test_missing_file_returns_empty_dict(tmp_path: Path) -> None:
    target = tmp_path / "nope.yaml"
    assert load_yaml_layer(target) == {}


def test_empty_file_returns_empty_dict(tmp_path: Path) -> None:
    target = tmp_path / "empty.yaml"
    target.write_text("", encoding="utf-8")
    assert load_yaml_layer(target) == {}


def test_whitespace_only_file_returns_empty_dict(tmp_path: Path) -> None:
    target = tmp_path / "blank.yaml"
    target.write_text("   \n\n   \n", encoding="utf-8")
    assert load_yaml_layer(target) == {}


def test_valid_mapping_round_trips(tmp_path: Path) -> None:
    target = tmp_path / "ok.yaml"
    target.write_text(
        "planning:\n  approval: auto\nestimation:\n  eu_minutes: 45\n",
        encoding="utf-8",
    )
    parsed = load_yaml_layer(target)
    assert parsed == {"planning": {"approval": "auto"}, "estimation": {"eu_minutes": 45}}


def test_malformed_yaml_raises_validation_failed(tmp_path: Path) -> None:
    target = tmp_path / "broken.yaml"
    target.write_text("planning:\n  approval: [unclosed\n", encoding="utf-8")
    with pytest.raises(ValidationError) as excinfo:
        load_yaml_layer(target)
    assert "malformed YAML" in str(excinfo.value)


def test_top_level_list_rejected(tmp_path: Path) -> None:
    target = tmp_path / "list.yaml"
    target.write_text("- one\n- two\n", encoding="utf-8")
    with pytest.raises(ValidationError) as excinfo:
        load_yaml_layer(target)
    assert "mapping" in str(excinfo.value).lower()


def test_top_level_scalar_rejected(tmp_path: Path) -> None:
    target = tmp_path / "scalar.yaml"
    target.write_text("just a string\n", encoding="utf-8")
    with pytest.raises(ValidationError):
        load_yaml_layer(target)


def test_explicit_null_document_returns_empty(tmp_path: Path) -> None:
    target = tmp_path / "null.yaml"
    target.write_text("---\n", encoding="utf-8")
    assert load_yaml_layer(target) == {}


def test_non_string_top_level_key_rejected(tmp_path: Path) -> None:
    target = tmp_path / "intkey.yaml"
    # YAML allows integer keys; we forbid them at the top level.
    target.write_text("42: foo\n", encoding="utf-8")
    with pytest.raises(ValidationError) as excinfo:
        load_yaml_layer(target)
    assert "not a string" in str(excinfo.value)
