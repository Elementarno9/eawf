"""Unit tests for ``tools/commit_prefix_lint.py``."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_LINT_PATH = _REPO_ROOT / "tools" / "commit_prefix_lint.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("commit_prefix_lint", _LINT_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["commit_prefix_lint"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def mod():
    return _load_module()


_TRAILER = "\n\nCo-Authored-By: Claude <noreply@anthropic.com>\n"


def _write_msg(tmp_path: Path, body: str, *, with_trailer: bool = True) -> Path:
    p = tmp_path / "COMMIT_EDITMSG"
    payload = body if not with_trailer else body.rstrip() + _TRAILER
    p.write_text(payload, encoding="utf-8")
    return p


def test_accepts_well_formed_wave_commit(tmp_path: Path, mod) -> None:
    msg = _write_msg(tmp_path, "[P14-W02] feat: add commit-prefix linter\n\nbody\n")
    code, diag = mod.lint(msg, ["src/eawf/x.py"])
    assert code == 0, diag


def test_accepts_phase_commit_without_wave(tmp_path: Path, mod) -> None:
    msg = _write_msg(tmp_path, "[P14] docs: update phase narrative\n")
    code, diag = mod.lint(msg, ["docs/x.md"])
    assert code == 0, diag


def test_accepts_core_state_only_paths(tmp_path: Path, mod) -> None:
    msg = _write_msg(tmp_path, "[P14-CORE] state: close W01\n")
    code, diag = mod.lint(msg, [".ea/state.json", ".ea/store/event.jsonl"])
    assert code == 0, diag


def test_rejects_missing_prefix(tmp_path: Path, mod) -> None:
    msg = _write_msg(tmp_path, "feat: drive-by change\n")
    code, diag = mod.lint(msg, [])
    assert code == 1
    assert "commit subject rejected" in diag


def test_rejects_wrong_wave_id_shape(tmp_path: Path, mod) -> None:
    msg = _write_msg(tmp_path, "[P14-w2] feat: broken\n")
    code, _diag = mod.lint(msg, [])
    assert code == 1


def test_rejects_unknown_type(tmp_path: Path, mod) -> None:
    msg = _write_msg(tmp_path, "[P14-W02] gizmo: broken type\n")
    code, _diag = mod.lint(msg, [])
    assert code == 1


def test_rejects_core_touching_src(tmp_path: Path, mod) -> None:
    msg = _write_msg(tmp_path, "[P14-CORE] state: bogus core change\n")
    code, diag = mod.lint(msg, [".ea/state.json", "src/eawf/foo.py"])
    assert code == 1
    assert "non-state paths" in diag


def test_accepts_core_touching_specs(tmp_path: Path, mod) -> None:
    msg = _write_msg(tmp_path, "[P14-CORE] state: wave spec\n")
    code, diag = mod.lint(msg, [".ea/specs/P14-I01-W01.md"])
    assert code == 0, diag


def test_accepts_core_touching_secrets_baseline(tmp_path: Path, mod) -> None:
    msg = _write_msg(tmp_path, "[P14-CORE] state: reopen P14\n")
    code, diag = mod.lint(
        msg,
        [".ea/state.json", ".ea/store/event.jsonl", ".secrets.baseline"],
    )
    assert code == 0, diag


def test_accepts_wave_commit_with_iter_component(tmp_path: Path, mod) -> None:
    """Iter component is mandatory for I02+ (single-iter phases stay short)."""
    msg = _write_msg(tmp_path, "[P14-I02-W01] feat: native plugin layout\n")
    code, diag = mod.lint(msg, ["src/eawf/runtimes/codex/plugin_install.py"])
    assert code == 0, diag


def test_accepts_core_commit_with_iter_component(tmp_path: Path, mod) -> None:
    msg = _write_msg(tmp_path, "[P14-I02-CORE] state: close W01\n")
    code, diag = mod.lint(msg, [".ea/state.json"])
    assert code == 0, diag


def test_accepts_core_touching_audit_store(tmp_path: Path, mod) -> None:
    msg = _write_msg(tmp_path, "[P14-CORE] state: register audit\n")
    code, diag = mod.lint(
        msg,
        [".ea/state.json", ".ea/store/audit.jsonl", ".ea/store/event.jsonl"],
    )
    assert code == 0, diag


def test_empty_subject_rejected(tmp_path: Path, mod) -> None:
    msg = _write_msg(tmp_path, "\n# just a comment\n", with_trailer=False)
    code, _diag = mod.lint(msg, [])
    assert code == 1


def test_skip_comment_lines_then_real_subject(tmp_path: Path, mod) -> None:
    msg = _write_msg(
        tmp_path,
        "# please enter the commit message\n# another comment\n[P14-W02] fix: real subject\n",
    )
    code, diag = mod.lint(msg, [])
    assert code == 0, diag


def test_rejects_missing_coauthor_trailer(tmp_path: Path, mod) -> None:
    msg = _write_msg(
        tmp_path,
        "[P14-W02] feat: add thing\n\nbody only, no trailer\n",
        with_trailer=False,
    )
    code, diag = mod.lint(msg, ["src/eawf/x.py"])
    assert code == 1
    assert "Co-Authored-By: Claude" in diag
