"""One render reads each generation document once.

The ``scope``, ``budget`` and ``mcp_health`` segments read the same selected
generation document. Inside :func:`one_read_per_render` the first read serves
the rest; outside it every read goes to disk, so a segment built on its own
always sees the file as it is.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from eawf.runtime.runtimes.claude.statusline_modules._spine import (
    one_read_per_render,
    read_generation,
)


def _document(path: Path, rows: dict[str, object]) -> Path:
    path.write_text(json.dumps({"capability": rows}), encoding="utf-8")
    return path


def test_outside_a_render_every_read_goes_to_disk(tmp_path: Path) -> None:
    path = _document(tmp_path / "state.json", {})
    first = read_generation(path)
    _document(path, {"a": {}})
    second = read_generation(path)
    assert first == {"capability": {}}
    assert second == {"capability": {"a": {}}}


def test_inside_a_render_the_first_read_serves_the_rest(tmp_path: Path) -> None:
    path = _document(tmp_path / "state.json", {})
    with one_read_per_render():
        first = read_generation(path)
        _document(path, {"a": {}})
        second = read_generation(path)
    assert second is first
    assert second == {"capability": {}}


def test_a_render_ending_drops_what_it_read(tmp_path: Path) -> None:
    path = _document(tmp_path / "state.json", {})
    with one_read_per_render():
        read_generation(path)
    _document(path, {"a": {}})
    assert read_generation(path) == {"capability": {"a": {}}}


def test_each_document_is_read_once_under_its_own_path(tmp_path: Path) -> None:
    one = _document(tmp_path / "one.json", {"a": {}})
    two = _document(tmp_path / "two.json", {"b": {}})
    with one_read_per_render():
        assert read_generation(one) == {"capability": {"a": {}}}
        assert read_generation(two) == {"capability": {"b": {}}}


def test_a_failed_read_is_not_kept_for_the_rest_of_the_render(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    with one_read_per_render():
        with pytest.raises(FileNotFoundError):
            read_generation(path)
        _document(path, {})
        assert read_generation(path) == {"capability": {}}


def test_a_document_that_is_not_an_object_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text("[]", encoding="utf-8")
    with one_read_per_render(), pytest.raises(ValueError):
        read_generation(path)


def test_a_render_that_raises_still_drops_what_it_read(tmp_path: Path) -> None:
    path = _document(tmp_path / "state.json", {})
    with pytest.raises(RuntimeError), one_read_per_render():
        read_generation(path)
        raise RuntimeError("segment failed")
    _document(path, {"a": {}})
    assert read_generation(path) == {"capability": {"a": {}}}
