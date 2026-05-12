"""Unit tests for ``tools/insert_coauthor.py``."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_TOOL_PATH = _REPO_ROOT / "tools" / "insert_coauthor.py"

_TRAILER = "Co-Authored-By: Claude <noreply@anthropic.com>"


def _load_module():
    spec = importlib.util.spec_from_file_location("insert_coauthor", _TOOL_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["insert_coauthor"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def mod():
    return _load_module()


def _write(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "COMMIT_EDITMSG"
    p.write_text(body, encoding="utf-8")
    return p


def test_appends_trailer_when_absent(tmp_path: Path, mod) -> None:
    msg = _write(tmp_path, "[P14-W02] feat: add thing\n\nbody\n")
    changed = mod.append_trailer(msg)
    assert changed is True
    text = msg.read_text(encoding="utf-8")
    assert text.endswith(_TRAILER + "\n")
    assert text.count(_TRAILER) == 1


def test_noop_when_trailer_present(tmp_path: Path, mod) -> None:
    body = f"[P14-W02] feat: add thing\n\nbody\n\n{_TRAILER}\n"
    msg = _write(tmp_path, body)
    changed = mod.append_trailer(msg)
    assert changed is False
    assert msg.read_text(encoding="utf-8") == body


def test_strips_comment_lines_before_appending(tmp_path: Path, mod) -> None:
    msg = _write(
        tmp_path,
        "[P14-W02] feat: x\n# please enter\n# another\n",
    )
    mod.append_trailer(msg)
    text = msg.read_text(encoding="utf-8")
    assert "# please enter" not in text
    assert text.endswith(_TRAILER + "\n")


def test_empty_message_left_alone(tmp_path: Path, mod) -> None:
    msg = _write(tmp_path, "# only comments\n# nothing else\n")
    changed = mod.append_trailer(msg)
    assert changed is False
    assert "Co-Authored-By" not in msg.read_text(encoding="utf-8")


def test_main_skips_on_merge_source(tmp_path: Path, mod) -> None:
    msg = _write(tmp_path, "Merge branch 'x'\n")
    rc = mod.main(["prog", str(msg), "merge"])
    assert rc == 0
    assert "Co-Authored-By" not in msg.read_text(encoding="utf-8")


def test_main_skips_on_squash_source(tmp_path: Path, mod) -> None:
    msg = _write(tmp_path, "[P14-W02] feat: thing\n\nsquashed\n")
    rc = mod.main(["prog", str(msg), "squash"])
    assert rc == 0
    assert _TRAILER not in msg.read_text(encoding="utf-8")


def test_main_appends_on_normal_commit(tmp_path: Path, mod) -> None:
    msg = _write(tmp_path, "[P14-W02] feat: thing\n\nbody\n")
    rc = mod.main(["prog", str(msg)])
    assert rc == 0
    assert _TRAILER in msg.read_text(encoding="utf-8")


def test_main_missing_file_exits_zero(tmp_path: Path, mod) -> None:
    rc = mod.main(["prog", str(tmp_path / "missing")])
    assert rc == 0
