"""Unit tests for ``tools/normalize_coauthor.py``."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_TOOL_PATH = _REPO_ROOT / "tools" / "normalize_coauthor.py"
_TOOL_DIR = _TOOL_PATH.parent

_CLAUDE_TRAILER = "Co-Authored-By: Claude <noreply@anthropic.com>"
_CODEX_TRAILER = "Co-Authored-By: Codex <noreply@openai.com>"


def _load_module():
    if str(_TOOL_DIR) not in sys.path:
        sys.path.insert(0, str(_TOOL_DIR))
    spec = importlib.util.spec_from_file_location("normalize_coauthor", _TOOL_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["normalize_coauthor"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def mod():
    return _load_module()


def _write(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "COMMIT_EDITMSG"
    p.write_text(body, encoding="utf-8")
    return p


# --- boundary cases ---------------------------------------------------------


def test_no_trailers_leaves_message_unchanged(mod) -> None:
    body = "[P19-W15] feat: hook\n\nbody paragraph\n"
    assert mod.normalize(body) == body


def test_single_canonical_trailer_is_idempotent(mod) -> None:
    body = f"[P19-W15] feat: hook\n\nbody\n\n{_CLAUDE_TRAILER}\n"
    assert mod.normalize(body) == body


def test_empty_message_no_panic(mod) -> None:
    assert mod.normalize("") == ""


def test_message_with_only_newlines(mod) -> None:
    # A message with only blank lines has no trailer block and no body.
    assert mod.normalize("\n\n\n") == "\n\n\n"


# --- Claude variant collapse ------------------------------------------------


def test_collapses_claude_lowercase_name(mod) -> None:
    body = "[P19-W15] feat: x\n\nbody\n\nCo-Authored-By: claude <noreply@anthropic.com>\n"
    out = mod.normalize(body)
    assert _CLAUDE_TRAILER in out
    assert "claude <noreply" not in out


def test_collapses_claude_with_marketing_suffix(mod) -> None:
    body = (
        "[P19-W15] feat: x\n\nbody\n\n"
        "Co-Authored-By: Claude (claude.ai/code) <noreply@anthropic.com>\n"
    )
    out = mod.normalize(body)
    assert out.rstrip().endswith(_CLAUDE_TRAILER)
    assert "(claude.ai/code)" not in out


def test_collapses_mixed_case_email(mod) -> None:
    body = "[P19-W15] feat: x\n\nbody\n\nCo-Authored-By: Claude <NoReply@Anthropic.COM>\n"
    out = mod.normalize(body)
    assert out.rstrip().endswith(_CLAUDE_TRAILER)


def test_collapses_alt_claude_email_form_keeps_canonical(mod) -> None:
    # ``claude@anthropic.com`` is a *different* email — not deduped, but
    # the unknown-email branch lowercases the host for hygiene.
    body = "[P19-W15] feat: x\n\nbody\n\nCo-Authored-By: claude <claude@anthropic.com>\n"
    out = mod.normalize(body)
    assert "Co-Authored-By: claude <claude@anthropic.com>" in out


# --- Codex variant collapse -------------------------------------------------


def test_collapses_codex_lowercase_name(mod) -> None:
    body = "[P19-W15] feat: x\n\nbody\n\nCo-Authored-By: codex <noreply@openai.com>\n"
    out = mod.normalize(body)
    assert out.rstrip().endswith(_CODEX_TRAILER)


def test_collapses_codex_with_suffix(mod) -> None:
    body = "[P19-W15] feat: x\n\nbody\n\nCo-Authored-By: Codex CLI <noreply@openai.com>\n"
    out = mod.normalize(body)
    assert out.rstrip().endswith(_CODEX_TRAILER)


# --- dedupe by email --------------------------------------------------------


def test_dedupes_identical_trailer(mod) -> None:
    body = f"[P19-W15] feat: x\n\nbody\n\n{_CLAUDE_TRAILER}\n{_CLAUDE_TRAILER}\n"
    out = mod.normalize(body)
    assert out.count(_CLAUDE_TRAILER) == 1


def test_dedupes_variant_into_canonical(mod) -> None:
    body = (
        "[P19-W15] feat: x\n\nbody\n\n"
        "Co-Authored-By: claude <noreply@anthropic.com>\n"
        f"{_CLAUDE_TRAILER}\n"
    )
    out = mod.normalize(body)
    assert out.count(_CLAUDE_TRAILER) == 1
    assert "claude <noreply" not in out


def test_first_write_wins_dedupe_order(mod) -> None:
    # Two canonical trailers (Claude then Codex) survive dedupe in order;
    # later Claude duplicate is dropped, Codex relative position preserved.
    body = f"[P19-W15] feat: x\n\nbody\n\n{_CLAUDE_TRAILER}\n{_CODEX_TRAILER}\n{_CLAUDE_TRAILER}\n"
    out = mod.normalize(body)
    lines = [line for line in out.splitlines() if line.startswith("Co-Authored-By:")]
    assert lines == [_CLAUDE_TRAILER, _CODEX_TRAILER]


def test_preserves_unknown_third_party_trailer(mod) -> None:
    third = "Co-Authored-By: Reviewer <reviewer@example.com>"
    body = f"[P19-W15] feat: x\n\nbody\n\n{_CLAUDE_TRAILER}\n{third}\n"
    out = mod.normalize(body)
    assert _CLAUDE_TRAILER in out
    assert third in out


def test_unknown_trailer_email_is_lowercased(mod) -> None:
    body = "[P19-W15] feat: x\n\nbody\n\nCo-Authored-By: Reviewer <Reviewer@Example.COM>\n"
    out = mod.normalize(body)
    assert "Co-Authored-By: Reviewer <reviewer@example.com>" in out


# --- idempotency ------------------------------------------------------------


def test_idempotent_on_messy_input(mod) -> None:
    body = (
        "[P19-W15] feat: x\n\nbody\n\n"
        "Co-Authored-By: claude <NoReply@Anthropic.com>\n"
        f"{_CLAUDE_TRAILER}\n"
        "Co-Authored-By: Codex CLI <NOREPLY@OPENAI.COM>\n"
    )
    once = mod.normalize(body)
    twice = mod.normalize(once)
    assert once == twice


def test_idempotent_when_already_canonical(mod) -> None:
    body = f"[P19-W15] feat: x\n\nbody\n\n{_CLAUDE_TRAILER}\n{_CODEX_TRAILER}\n"
    once = mod.normalize(body)
    twice = mod.normalize(once)
    assert once == twice == body


# --- normalize_file + main --------------------------------------------------


def test_normalize_file_returns_true_when_changed(tmp_path: Path, mod) -> None:
    p = _write(
        tmp_path,
        "[P19-W15] feat: x\n\nbody\n\nCo-Authored-By: claude <noreply@anthropic.com>\n",
    )
    assert mod.normalize_file(p) is True
    assert _CLAUDE_TRAILER in p.read_text(encoding="utf-8")


def test_normalize_file_returns_false_when_idempotent(tmp_path: Path, mod) -> None:
    body = f"[P19-W15] feat: x\n\nbody\n\n{_CLAUDE_TRAILER}\n"
    p = _write(tmp_path, body)
    assert mod.normalize_file(p) is False
    assert p.read_text(encoding="utf-8") == body


def test_main_zero_when_missing_file(tmp_path: Path, mod) -> None:
    rc = mod.main(["prog", str(tmp_path / "does-not-exist")])
    assert rc == 0


def test_main_zero_when_no_argv(mod) -> None:
    rc = mod.main(["prog"])
    assert rc == 0


def test_main_rewrites_message_in_place(tmp_path: Path, mod) -> None:
    p = _write(
        tmp_path,
        "[P19-W15] feat: x\n\nbody\n\nCo-Authored-By: claude <noreply@anthropic.com>\n",
    )
    rc = mod.main(["prog", str(p)])
    assert rc == 0
    assert _CLAUDE_TRAILER in p.read_text(encoding="utf-8")


def test_main_zero_on_io_error(tmp_path: Path, mod, monkeypatch: pytest.MonkeyPatch) -> None:
    p = _write(tmp_path, f"[P19-W15] feat: x\n\nbody\n\n{_CLAUDE_TRAILER}\n")

    def boom(*args: object, **kwargs: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(mod, "normalize_file", boom)
    rc = mod.main(["prog", str(p)])
    assert rc == 0


# --- format / hygiene -------------------------------------------------------


def test_blank_separator_between_body_and_trailers(mod) -> None:
    body = "[P19-W15] feat: x\n\nbody paragraph\n\nCo-Authored-By: claude <noreply@anthropic.com>\n"
    out = mod.normalize(body)
    # Body paragraph and trailer block must be separated by exactly one blank.
    assert "body paragraph\n\n" + _CLAUDE_TRAILER in out


def test_preserves_trailing_newline(mod) -> None:
    with_nl = f"[P19-W15] feat: x\n\nbody\n\n{_CLAUDE_TRAILER}\n"
    assert mod.normalize(with_nl).endswith("\n")
    without_nl = f"[P19-W15] feat: x\n\nbody\n\n{_CLAUDE_TRAILER}"
    assert not mod.normalize(without_nl).endswith("\n")


def test_strips_trailing_blank_lines_before_trailer_block(mod) -> None:
    body = f"[P19-W15] feat: x\n\nbody\n\n\n\n{_CLAUDE_TRAILER}\n"
    out = mod.normalize(body)
    # Single blank between body and trailer, no run of three+ newlines.
    assert "\n\n\n" + _CLAUDE_TRAILER not in out
    assert "\n\n" + _CLAUDE_TRAILER in out


# --- many trailers (max-length boundary) ------------------------------------


def test_handles_many_duplicate_trailers(mod) -> None:
    body = "[P19-W15] feat: x\n\nbody\n\n" + ("\n".join([_CLAUDE_TRAILER] * 50) + "\n")
    out = mod.normalize(body)
    assert out.count(_CLAUDE_TRAILER) == 1
