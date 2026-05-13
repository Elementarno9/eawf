"""Unit tests for ``tools/insert_coauthor.py``."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_TOOL_PATH = _REPO_ROOT / "tools" / "insert_coauthor.py"
_TOOL_DIR = _TOOL_PATH.parent

_CLAUDE_TRAILER = "Co-Authored-By: Claude <noreply@anthropic.com>"
_CODEX_TRAILER = "Co-Authored-By: Codex <noreply@openai.com>"


def _load_module():
    if str(_TOOL_DIR) not in sys.path:
        sys.path.insert(0, str(_TOOL_DIR))
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


def test_appends_claude_trailer_when_override_selects_claude(tmp_path: Path, mod) -> None:
    msg = _write(tmp_path, "[P14-W02] feat: add thing\n\nbody\n")
    changed = mod.append_trailer(msg, {"EAWF_COAUTHOR_HARNESS": "claude"})
    assert changed is True
    text = msg.read_text(encoding="utf-8")
    assert text.endswith(_CLAUDE_TRAILER + "\n")
    assert text.count(_CLAUDE_TRAILER) == 1


def test_appends_codex_trailer_when_override_selects_codex(tmp_path: Path, mod) -> None:
    msg = _write(tmp_path, "[P14-W02] feat: add thing\n\nbody\n")
    changed = mod.append_trailer(msg, {"EAWF_COAUTHOR_HARNESS": "codex"})
    assert changed is True
    text = msg.read_text(encoding="utf-8")
    assert text.endswith(_CODEX_TRAILER + "\n")
    assert text.count(_CODEX_TRAILER) == 1


def test_appends_codex_trailer_when_codex_env_detected(tmp_path: Path, mod) -> None:
    msg = _write(tmp_path, "[P14-W02] feat: add thing\n\nbody\n")
    changed = mod.append_trailer(msg, {"CODEX_SHELL": "1"})
    assert changed is True
    assert _CODEX_TRAILER in msg.read_text(encoding="utf-8")


def test_appends_claude_trailer_when_claude_env_detected(tmp_path: Path, mod) -> None:
    msg = _write(tmp_path, "[P14-W02] feat: add thing\n\nbody\n")
    changed = mod.append_trailer(msg, {"CLAUDECODE": "1"})
    assert changed is True
    assert _CLAUDE_TRAILER in msg.read_text(encoding="utf-8")


def test_invalid_override_falls_back_to_harness_detection(tmp_path: Path, mod) -> None:
    msg = _write(tmp_path, "[P14-W02] feat: add thing\n\nbody\n")
    changed = mod.append_trailer(
        msg,
        {"EAWF_COAUTHOR_HARNESS": "invalid", "CODEX_SHELL": "1"},
    )
    assert changed is True
    assert _CODEX_TRAILER in msg.read_text(encoding="utf-8")


def test_noop_when_harness_not_detected(tmp_path: Path, mod) -> None:
    body = "[P14-W02] feat: add thing\n\nbody\n"
    msg = _write(tmp_path, body)
    changed = mod.append_trailer(msg, {})
    assert changed is False
    assert msg.read_text(encoding="utf-8") == body


def test_noop_when_trailer_present(tmp_path: Path, mod) -> None:
    body = f"[P14-W02] feat: add thing\n\nbody\n\n{_CLAUDE_TRAILER}\n"
    msg = _write(tmp_path, body)
    changed = mod.append_trailer(msg, {"EAWF_COAUTHOR_HARNESS": "codex"})
    assert changed is False
    assert msg.read_text(encoding="utf-8") == body


def test_noop_when_codex_trailer_present(tmp_path: Path, mod) -> None:
    body = f"[P14-W02] feat: add thing\n\nbody\n\n{_CODEX_TRAILER}\n"
    msg = _write(tmp_path, body)
    changed = mod.append_trailer(msg, {"EAWF_COAUTHOR_HARNESS": "claude"})
    assert changed is False
    assert msg.read_text(encoding="utf-8") == body


def test_strips_comment_lines_before_appending(tmp_path: Path, mod) -> None:
    msg = _write(
        tmp_path,
        "[P14-W02] feat: x\n# please enter\n# another\n",
    )
    mod.append_trailer(msg, {"EAWF_COAUTHOR_HARNESS": "claude"})
    text = msg.read_text(encoding="utf-8")
    assert "# please enter" not in text
    assert text.endswith(_CLAUDE_TRAILER + "\n")


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
    assert _CLAUDE_TRAILER not in msg.read_text(encoding="utf-8")


def test_main_appends_on_normal_commit(
    tmp_path: Path, mod, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EAWF_COAUTHOR_HARNESS", "claude")
    msg = _write(tmp_path, "[P14-W02] feat: thing\n\nbody\n")
    rc = mod.main(["prog", str(msg)])
    assert rc == 0
    assert _CLAUDE_TRAILER in msg.read_text(encoding="utf-8")


def test_main_missing_file_exits_zero(tmp_path: Path, mod) -> None:
    rc = mod.main(["prog", str(tmp_path / "missing")])
    assert rc == 0
