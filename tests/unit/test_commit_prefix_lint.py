"""Unit tests for ``tools/commit_prefix_lint.py``."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_LINT_PATH = _REPO_ROOT / "tools" / "commit_prefix_lint.py"
_TOOL_DIR = _LINT_PATH.parent


def _load_module():
    if str(_TOOL_DIR) not in sys.path:
        sys.path.insert(0, str(_TOOL_DIR))
    spec = importlib.util.spec_from_file_location("commit_prefix_lint", _LINT_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["commit_prefix_lint"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def mod():
    return _load_module()


_CLAUDE_TRAILER = "\n\nCo-Authored-By: Claude <noreply@anthropic.com>\n"
_CODEX_TRAILER = "\n\nCo-Authored-By: Codex <noreply@openai.com>\n"


def _write_msg(tmp_path: Path, body: str, *, with_trailer: bool = True) -> Path:
    p = tmp_path / "COMMIT_EDITMSG"
    payload = body if not with_trailer else body.rstrip() + _CLAUDE_TRAILER
    p.write_text(payload, encoding="utf-8")
    return p


def test_accepts_well_formed_wave_commit(tmp_path: Path, mod) -> None:
    msg = _write_msg(tmp_path, "[P14-W02] feat: add commit-prefix linter\n\nbody\n")
    code, diag = mod.lint(msg, ["src/eawf/x.py"])
    assert code == 0, diag


def test_accepts_codex_coauthor_trailer(tmp_path: Path, mod) -> None:
    msg = tmp_path / "COMMIT_EDITMSG"
    msg.write_text(
        "[P14-W02] feat: add commit-prefix linter\n\nbody" + _CODEX_TRAILER,
        encoding="utf-8",
    )
    code, diag = mod.lint(msg, ["src/eawf/x.py"])
    assert code == 0, diag


def test_accepts_both_supported_coauthor_trailers(tmp_path: Path, mod) -> None:
    msg = tmp_path / "COMMIT_EDITMSG"
    msg.write_text(
        "[P14-W02] feat: add commit-prefix linter\n\nbody" + _CLAUDE_TRAILER + _CODEX_TRAILER,
        encoding="utf-8",
    )
    code, diag = mod.lint(msg, ["src/eawf/x.py"])
    assert code == 0, diag


def test_rejects_bare_phase_prefix_non_state_non_docs_type(tmp_path: Path, mod) -> None:
    """P19-W05 + P26-W23 + W28: bare ``[P##]`` rejected for types other than
    ``state`` / ``docs``.

    Bare ``[P##]`` is accepted only for ``type == 'state'`` (bookkeeping) or
    ``type == 'docs'`` (phase/iter-scoped artifacts). For ``feat`` / ``fix`` /
    etc. the ``-W##`` or ``-CORE`` suffix remains mandatory.
    """
    msg = _write_msg(tmp_path, "[P14] feat: drive-by feature\n")
    code, diag = mod.lint(msg, ["src/eawf/x.py"])
    assert code == 1
    assert "commit subject rejected" in diag


def test_rejects_bare_phase_iter_prefix_non_state_non_docs_type(tmp_path: Path, mod) -> None:
    """``[P##-I##]`` without -W##/-CORE rejected for types other than state/docs."""
    msg = _write_msg(tmp_path, "[P14-I02] feat: iter-scope feature\n")
    code, _diag = mod.lint(msg, ["src/eawf/x.py"])
    assert code == 1


def test_accepts_core_state_only_paths(tmp_path: Path, mod) -> None:
    """Legacy ``[P##-CORE] state: ...`` form still validates (back-compat)."""
    msg = _write_msg(tmp_path, "[P14-CORE] state: close W01\n")
    code, diag = mod.lint(msg, [".ea/state.json", ".ea/store/event.jsonl"])
    assert code == 0, diag


def test_accepts_bare_state_type_commit(tmp_path: Path, mod) -> None:
    """P26-W23: bare ``[P##] state: ...`` (no -CORE suffix) is the canonical bookkeeping form.

    ``type == 'state'`` is the semantic signal; the bare ``[P##]`` prefix
    is accepted because the legacy ``-CORE`` carrier is now optional.
    """
    msg = _write_msg(tmp_path, "[P26] state: close iter + phase (audit=A31-P26)\n")
    code, diag = mod.lint(msg, [".ea/state.json", ".ea/store/event.jsonl"])
    assert code == 0, diag


def test_accepts_bare_state_type_commit_with_iter_component(tmp_path: Path, mod) -> None:
    """P26-W23: ``[P##-I##] state: ...`` (no -CORE) accepted; iter form valid for type=state."""
    msg = _write_msg(tmp_path, "[P26-I02] state: close W01\n")
    code, diag = mod.lint(msg, [".ea/state.json"])
    assert code == 0, diag


def test_rejects_bare_state_type_touching_src(tmp_path: Path, mod) -> None:
    """P26-W23: bare ``[P##] state:`` triggers the whitelist via type=state.

    The whitelist is bound to ``type == 'state'`` (the semantic signal),
    not the legacy ``-CORE`` suffix. A bare ``[P##] state:`` commit
    touching ``src/`` is rejected for the same reason
    ``[P##-CORE] state:`` was previously rejected.
    """
    msg = _write_msg(tmp_path, "[P26] state: bogus state commit\n")
    code, diag = mod.lint(msg, [".ea/state.json", "src/eawf/foo.py"])
    assert code == 1
    assert "non-state paths" in diag
    assert "state-type" in diag


def test_rejects_bare_state_type_touching_docs(tmp_path: Path, mod) -> None:
    """P26-W23: bare ``[P##] state:`` triggers whitelist — docs are non-state."""
    msg = _write_msg(tmp_path, "[P26] state: bogus state commit\n")
    code, diag = mod.lint(msg, [".ea/state.json", "docs/architecture.md"])
    assert code == 1
    assert "non-state paths" in diag


def test_accepts_bare_phase_docs_artifact(tmp_path: Path, mod) -> None:
    """W28: bare ``[P##] docs:`` accepted for phase-scoped artifacts under .ea/artifacts/**."""
    msg = _write_msg(tmp_path, "[P27] docs: P27 closure audit report (A37, minor)\n")
    code, diag = mod.lint(msg, [".ea/artifacts/audits/2026-05-23-p27-closure.md"])
    assert code == 0, diag


def test_accepts_bare_iter_docs_artifact(tmp_path: Path, mod) -> None:
    """W28: ``[P##-I##] docs:`` accepted for artifacts under .ea/artifacts/**."""
    msg = _write_msg(tmp_path, "[P27-I03] docs: P27-I03 closure audit (A37)\n")
    code, diag = mod.lint(msg, [".ea/artifacts/audits/2026-05-23-p27-i03-closure.md"])
    assert code == 0, diag


def test_rejects_bare_docs_touching_non_artifact_paths(tmp_path: Path, mod) -> None:
    """W28: bare ``[P##] docs:`` is path-gated to .ea/artifacts/**; docs/ is rejected."""
    msg = _write_msg(tmp_path, "[P27] docs: mkdocs reference page\n")
    code, diag = mod.lint(msg, ["docs/reference/x.md"])
    assert code == 1
    assert "non-artifact paths" in diag


def test_rejects_bare_docs_touching_src(tmp_path: Path, mod) -> None:
    """W28: bare ``[P##] docs:`` touching src/ is rejected (not an artifact path)."""
    msg = _write_msg(tmp_path, "[P27] docs: sneaky src docstring change\n")
    code, diag = mod.lint(msg, [".ea/artifacts/x.md", "src/eawf/foo.py"])
    assert code == 1
    assert "non-artifact paths" in diag


def test_accepts_wave_docs_touching_artifact_and_src(tmp_path: Path, mod) -> None:
    """W28: wave-form ``[P##-W##] docs:`` stays unrestricted (any path)."""
    msg = _write_msg(tmp_path, "[P27-I03-W40] docs: closure audit + narrative\n")
    code, diag = mod.lint(msg, [".ea/artifacts/audits/x.md", "docs/reference/y.md"])
    assert code == 0, diag


def test_rejects_missing_prefix(tmp_path: Path, mod) -> None:
    """Bare conventional-commits subject IS legal when no ACTIVE phase
    (per the W66 lint extension landed in the pre-flight v0.4 chore
    commit); inject an ACTIVE-phase state.json so this case stays
    rejected.
    """
    msg = _write_msg(tmp_path, "feat: drive-by change\n")
    state = _write_state(tmp_path, phase_id="P28")
    code, diag = mod.lint(msg, [], state_path=state)
    assert code == 1
    assert "bare conventional-commits subject rejected" in diag


def test_rejects_wrong_wave_id_shape(tmp_path: Path, mod) -> None:
    msg = _write_msg(tmp_path, "[P14-w2] feat: broken\n")
    code, _diag = mod.lint(msg, [])
    assert code == 1


def test_rejects_wave_zero(tmp_path: Path, mod) -> None:
    """``[P##-W00]`` rejected: wave indices are 1-based; reactive waves append the next W##."""
    msg = _write_msg(tmp_path, "[P14-W00] feat: bogus wave index\n")
    code, diag = mod.lint(msg, ["src/eawf/x.py"])
    assert code == 1
    assert "commit subject rejected" in diag


def test_rejects_iter_zero(tmp_path: Path, mod) -> None:
    """``[P##-I00-...]`` rejected: iter indices are 1-based."""
    msg = _write_msg(tmp_path, "[P14-I00-W01] feat: bogus iter index\n")
    code, diag = mod.lint(msg, ["src/eawf/x.py"])
    assert code == 1
    assert "commit subject rejected" in diag


def test_rejects_iter_zero_core(tmp_path: Path, mod) -> None:
    """``[P##-I00-CORE]`` rejected: iter indices are 1-based."""
    msg = _write_msg(tmp_path, "[P14-I00-CORE] state: bogus iter index\n")
    code, diag = mod.lint(msg, [".ea/state.json"])
    assert code == 1
    assert "commit subject rejected" in diag


def test_accepts_wave_one(tmp_path: Path, mod) -> None:
    """``[P##-W01]`` accepted: lowest legal wave index."""
    msg = _write_msg(tmp_path, "[P14-W01] feat: first wave\n")
    code, diag = mod.lint(msg, ["src/eawf/x.py"])
    assert code == 0, diag


def test_accepts_max_two_digit_wave(tmp_path: Path, mod) -> None:
    """``[P##-W99]`` accepted: highest two-digit wave index."""
    msg = _write_msg(tmp_path, "[P14-W99] feat: hypothetical late wave\n")
    code, diag = mod.lint(msg, ["src/eawf/x.py"])
    assert code == 0, diag


def test_rejects_unknown_type(tmp_path: Path, mod) -> None:
    msg = _write_msg(tmp_path, "[P14-W02] gizmo: broken type\n")
    code, _diag = mod.lint(msg, [])
    assert code == 1


def test_rejects_core_touching_src(tmp_path: Path, mod) -> None:
    msg = _write_msg(tmp_path, "[P14-CORE] state: bogus core change\n")
    code, diag = mod.lint(msg, [".ea/state.json", "src/eawf/foo.py"])
    assert code == 1
    assert "non-state paths" in diag


def test_legacy_core_suffix_non_state_type_touching_src_rejected(tmp_path: Path, mod) -> None:
    """P26-W23: legacy ``-CORE`` suffix still triggers whitelist for back-compat.

    Pre-P26-W23 lint enforced the whitelist on the ``-CORE`` subject
    suffix regardless of type. To preserve D16 (CORE restricted to
    state-only commits), the legacy suffix remains a whitelist trigger
    even when the type is not ``state``.
    """
    msg = _write_msg(tmp_path, "[P14-CORE] feat: hypothetical legacy mis-use\n")
    code, diag = mod.lint(msg, [".ea/state.json", "src/eawf/foo.py"])
    assert code == 1
    assert "non-state paths" in diag
    assert "[P##-CORE]" in diag


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
    code, diag = mod.lint(msg, ["src/eawf/runtime/runtimes/codex/plugin_install.py"])
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
    assert "missing recognized co-author trailer" in diag


def test_rejects_comment_only_coauthor_trailer(tmp_path: Path, mod) -> None:
    msg = _write_msg(
        tmp_path,
        (
            "[P14-W02] feat: add thing\n\nbody only, no trailer\n"
            "# Co-Authored-By: Codex <noreply@openai.com>\n"
        ),
        with_trailer=False,
    )
    code, diag = mod.lint(msg, ["src/eawf/x.py"])
    assert code == 1
    assert "missing recognized co-author trailer" in diag


def test_rejects_unrecognized_coauthor_trailer(tmp_path: Path, mod) -> None:
    msg = _write_msg(
        tmp_path,
        "[P14-W02] feat: add thing\n\nbody\n\nCo-Authored-By: Other <noreply@example.com>\n",
        with_trailer=False,
    )
    code, diag = mod.lint(msg, ["src/eawf/x.py"])
    assert code == 1
    assert "missing recognized co-author trailer" in diag


def test_disabled_coauthor_policy_rejects_any_trailer(tmp_path: Path, mod) -> None:
    msg = _write_msg(tmp_path, "[P17-W02] feat: registry coauthor policy\n")
    code, diag = mod.lint(msg, ["src/eawf/x.py"], env={"EAWF_COAUTHOR_MODE": "disabled"})
    assert code == 1
    assert "disabled" in diag


def test_disabled_coauthor_policy_accepts_no_trailer(tmp_path: Path, mod) -> None:
    msg = _write_msg(
        tmp_path,
        "[P17-W02] feat: registry coauthor policy\n\nbody only\n",
        with_trailer=False,
    )
    code, diag = mod.lint(msg, ["src/eawf/x.py"], env={"EAWF_COAUTHOR_MODE": "disabled"})
    assert code == 0, diag


# ---------------------------------------------------------------------------
# Bare conventional-commits form (out-of-phase): ``<type>: <subject>``.
# Accepted only when ``state.current.phase_id is None``. The state path is
# injected via the *state_path* kwarg so tests do not need to touch the
# checkout's own ``.ea/state.json``.
# ---------------------------------------------------------------------------


def _write_state(tmp_path: Path, *, phase_id: str | None) -> Path:
    state = {"current": {"phase_id": phase_id, "iter_id": None}}
    p = tmp_path / "state.json"
    import json as _json

    p.write_text(_json.dumps(state), encoding="utf-8")
    return p


def test_accepts_pre_flight_chore_when_no_active_phase(tmp_path: Path, mod) -> None:
    msg = _write_msg(tmp_path, "chore: pre-flight chore before roadmap\n\nbody\n")
    state = _write_state(tmp_path, phase_id=None)
    code, diag = mod.lint(msg, ["AGENTS.md", "tools/commit_prefix_lint.py"], state_path=state)
    assert code == 0, diag


def test_accepts_trailer_style_wave_commit_when_configured(tmp_path: Path, mod) -> None:
    repo = tmp_path / "repo"
    (repo / ".ea").mkdir(parents=True)
    (repo / ".ea" / "config.yaml").write_text(
        "vcs:\n  conventions:\n    subject_style: trailer\n",
        encoding="utf-8",
    )
    msg = _write_msg(
        tmp_path,
        "feat: add trailer-style wave commit\n\nEawf-Wave: P28-I03-W02\n",
    )
    state = _write_state(tmp_path, phase_id="P28")
    code, diag = mod.lint(msg, ["src/eawf/x.py"], state_path=state, repo_root=repo)
    assert code == 0, diag


def test_trailer_style_rejects_missing_wave_trailer(tmp_path: Path, mod) -> None:
    repo = tmp_path / "repo"
    (repo / ".ea").mkdir(parents=True)
    (repo / ".ea" / "config.yaml").write_text(
        "vcs:\n  conventions:\n    subject_style: trailer\n",
        encoding="utf-8",
    )
    msg = _write_msg(tmp_path, "feat: missing wave trailer\n\nbody\n")
    state = _write_state(tmp_path, phase_id="P28")
    code, diag = mod.lint(msg, ["src/eawf/x.py"], state_path=state, repo_root=repo)
    assert code == 1
    assert "missing Eawf-Wave trailer" in diag


def test_bracket_default_still_rejects_active_bare_conventional(tmp_path: Path, mod) -> None:
    msg = _write_msg(
        tmp_path,
        "feat: bracket mode ignores trailer escape\n\nEawf-Wave: P28-I03-W02\n",
    )
    state = _write_state(tmp_path, phase_id="P28")
    code, diag = mod.lint(msg, ["src/eawf/x.py"], state_path=state)
    assert code == 1
    assert "bare conventional-commits subject rejected" in diag


def test_rejects_bare_conventional_when_active_phase(tmp_path: Path, mod) -> None:
    msg = _write_msg(tmp_path, "chore: pre-flight scrub for v0.4 design\n\nbody\n")
    state = _write_state(tmp_path, phase_id="P28")
    code, diag = mod.lint(msg, ["AGENTS.md", "tools/commit_prefix_lint.py"], state_path=state)
    assert code == 1
    assert "bare conventional-commits subject rejected" in diag
    assert "ACTIVE phase exists" in diag


def test_bare_conventional_no_state_file_treated_as_no_active_phase(tmp_path: Path, mod) -> None:
    msg = _write_msg(tmp_path, "feat: bootstrap module\n\nbody\n")
    missing = tmp_path / "missing-state.json"
    assert not missing.exists()
    code, diag = mod.lint(msg, ["src/x.py"], state_path=missing)
    assert code == 0, diag


def test_bare_conventional_all_supported_types(tmp_path: Path, mod) -> None:
    state = _write_state(tmp_path, phase_id=None)
    for ctype in (
        "feat",
        "fix",
        "chore",
        "docs",
        "refactor",
        "test",
        "build",
        "perf",
        "ci",
        "revert",
        "state",
    ):
        msg = _write_msg(tmp_path, f"{ctype}: something\n\nbody\n")
        code, diag = mod.lint(msg, ["any/path.py"], state_path=state)
        assert code == 0, f"{ctype} rejected: {diag}"


def test_accepts_three_digit_wave_id(tmp_path: Path, mod) -> None:
    msg = _write_msg(tmp_path, "[P28-W100] feat: late wave\n\nbody\n")
    code, diag = mod.lint(msg, ["src/eawf/x.py"])
    assert code == 0, diag


def test_accepts_three_digit_phase_id(tmp_path: Path, mod) -> None:
    msg = _write_msg(tmp_path, "[P100-W01] feat: distant phase\n\nbody\n")
    code, diag = mod.lint(msg, ["src/eawf/x.py"])
    assert code == 0, diag


def test_accepts_release_annotation_on_phase_close_state_commit(tmp_path: Path, mod) -> None:
    msg = _write_msg(
        tmp_path,
        "[P28] state: close iter + phase (audit=A44-P28) (release=v0.4.0)\n\nbody\n",
    )
    code, diag = mod.lint(msg, [".ea/state.json", ".ea/store/event.jsonl"])
    assert code == 0, diag


def test_rejects_malformed_release_annotation(tmp_path: Path, mod) -> None:
    msg = _write_msg(
        tmp_path,
        "[P28] state: close iter + phase (audit=A44-P28) (release=0.4)\n\nbody\n",
    )
    code, diag = mod.lint(msg, [".ea/state.json"])
    assert code == 1
    assert "release annotation rejected" in diag
