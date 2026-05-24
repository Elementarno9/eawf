from __future__ import annotations

import contextlib
import json
import threading
from pathlib import Path

import pytest

from eawf.kernel.state import writer


def test_atomic_write_creates_file(tmp_path: Path) -> None:
    target = tmp_path / "state.json"
    writer.atomic_write_json(target, {"hello": "world"})
    assert json.loads(target.read_text()) == {"hello": "world"}


def test_atomic_write_overwrites(tmp_path: Path) -> None:
    target = tmp_path / "state.json"
    writer.atomic_write_json(target, {"a": 1})
    writer.atomic_write_json(target, {"a": 2})
    assert json.loads(target.read_text()) == {"a": 2}


def test_atomic_write_leaves_no_tempfiles(tmp_path: Path) -> None:
    target = tmp_path / "state.json"
    writer.atomic_write_json(target, {"k": "v"})
    leftovers = [p for p in tmp_path.iterdir() if p.name.startswith("state.json.tmp.")]
    assert leftovers == []


def test_atomic_write_failure_preserves_original(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "state.json"
    writer.atomic_write_json(target, {"a": 1})
    original = target.read_text()

    def boom(*args: object, **kwargs: object) -> None:
        raise OSError("simulated failure")

    monkeypatch.setattr(writer.os, "replace", boom)
    with contextlib.suppress(OSError):
        writer.atomic_write_json(target, {"a": 2})
    assert target.read_text() == original


def test_atomic_write_creates_parent_dirs(tmp_path: Path) -> None:
    """Parent directories that do not exist must be created automatically."""
    target = tmp_path / "nested" / "deep" / "state.json"
    assert not target.parent.exists()
    writer.atomic_write_json(target, {"nested": True})
    assert json.loads(target.read_text()) == {"nested": True}


def test_atomic_write_concurrent_no_corruption(tmp_path: Path) -> None:
    """Two threads writing different payloads must not corrupt the file."""
    target = tmp_path / "concurrent.json"
    errors: list[Exception] = []

    def write_loop(value: int) -> None:
        try:
            for _ in range(10):
                writer.atomic_write_json(target, {"v": value})
        except Exception as exc:
            errors.append(exc)

    t1 = threading.Thread(target=write_loop, args=(1,))
    t2 = threading.Thread(target=write_loop, args=(2,))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert not errors, f"Thread errors: {errors}"
    result = json.loads(target.read_text())
    assert result["v"] in (1, 2)


def test_atomic_write_empty_dict(tmp_path: Path) -> None:
    """An empty mapping must be written and read back correctly."""
    target = tmp_path / "empty.json"
    writer.atomic_write_json(target, {})
    assert json.loads(target.read_text()) == {}


def test_atomic_write_ends_with_newline(tmp_path: Path) -> None:
    """Payload must terminate with a single ``\\n`` so the file passes
    the ``end-of-file-fixer`` pre-commit hook on every state mutation."""
    target = tmp_path / "state.json"
    writer.atomic_write_json(target, {"k": "v"})
    raw = target.read_bytes()
    assert raw.endswith(b"\n")
    assert not raw.endswith(b"\n\n")


def test_atomic_write_tempfile_cleaned_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Temp file must be removed even when os.replace raises."""
    target = tmp_path / "state.json"

    def boom(*args: object, **kwargs: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(writer.os, "replace", boom)
    with pytest.raises(OSError, match="disk full"):
        writer.atomic_write_json(target, {"x": 1})

    leftovers = [p for p in tmp_path.iterdir() if p.name.startswith("state.json.tmp.")]
    assert leftovers == [], f"Unexpected tempfiles: {leftovers}"
