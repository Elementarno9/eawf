"""Unit tests for ``tools/commit_prefix_lint.py``."""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path
from types import SimpleNamespace

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
    etc. the ``-W##`` suffix remains mandatory (the legacy ``-CORE`` alias
    is retired).
    """
    msg = _write_msg(tmp_path, "[P14] feat: drive-by feature\n")
    code, diag = mod.lint(msg, ["src/eawf/x.py"])
    assert code == 1
    assert "commit subject rejected" in diag


def test_rejects_bare_phase_iter_prefix_non_state_non_docs_type(tmp_path: Path, mod) -> None:
    """``[P##-I##]`` without -W## rejected for types other than state/docs."""
    msg = _write_msg(tmp_path, "[P14-I02] feat: iter-scope feature\n")
    code, _diag = mod.lint(msg, ["src/eawf/x.py"])
    assert code == 1


def test_core_suffix_rejected(tmp_path: Path, mod) -> None:
    """The retired ``-CORE`` suffix no longer matches any subject grammar.

    ``[P##] state: ...`` is now the sole canonical bookkeeping form; a
    ``-CORE``-suffixed subject falls through to the generic mismatch
    diagnostic regardless of how whitelist-friendly the staged paths are.
    """
    msg = _write_msg(tmp_path, "[P14-CORE] state: close W01\n")
    code, diag = mod.lint(msg, [".ea/state.json", ".ea/store/event.jsonl"])
    assert code == 1
    assert "commit subject rejected" in diag


def test_accepts_bare_state_type_commit(tmp_path: Path, mod) -> None:
    """P26-W23: bare ``[P##] state: ...`` is the sole canonical bookkeeping form.

    ``type == 'state'`` is the semantic signal; the legacy ``-CORE`` carrier
    is retired, so the bare ``[P##]`` prefix is the only accepted spelling.
    """
    msg = _write_msg(tmp_path, "[P26] state: close iter + phase (audit=A31-P26)\n")
    code, diag = mod.lint(msg, [".ea/state.json", ".ea/store/event.jsonl"])
    assert code == 0, diag


def test_accepts_bare_state_type_commit_with_iter_component(tmp_path: Path, mod) -> None:
    """P26-W23: ``[P##-I##] state: ...`` accepted (the -CORE suffix is retired);
    iter form valid for type=state."""
    msg = _write_msg(tmp_path, "[P26-I02] state: close W01\n")
    code, diag = mod.lint(msg, [".ea/state.json"])
    assert code == 0, diag


def test_rejects_bare_state_type_touching_src(tmp_path: Path, mod) -> None:
    """P26-W23: bare ``[P##] state:`` triggers the whitelist via type=state.

    The whitelist is bound to ``type == 'state'`` (the semantic signal);
    the legacy ``-CORE`` suffix is retired and no longer part of the
    grammar at all. A bare ``[P##] state:`` commit touching ``src/`` is
    rejected the same way the retired ``[P##-CORE] state:`` form used to be.
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
    """``[P##-I00-W##]`` rejected: iter indices are 1-based.

    Rewritten from the retired ``[P##-I00-CORE]`` legacy spelling onto the
    surviving wave form -- the ``-CORE`` suffix no longer parses at all, so
    this isolates the I00 rejection on the form that still exists.
    """
    msg = _write_msg(tmp_path, "[P14-I00-W02] state: legacy iter-zero shape\n")
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


def test_accepts_state_touching_specs(tmp_path: Path, mod) -> None:
    """Rewritten from the retired ``[P##-CORE] state:`` form.

    Bare ``[P##] state:`` accepts ``.ea/specs/**`` wave-spec paths.
    """
    msg = _write_msg(tmp_path, "[P14] state: wave spec\n")
    code, diag = mod.lint(msg, [".ea/specs/P14-I01-W01.md"])
    assert code == 0, diag


def test_accepts_state_touching_secrets_baseline(tmp_path: Path, mod) -> None:
    """Rewritten from the retired ``[P##-CORE] state:`` form.

    Bare ``[P##] state:`` accepts ``.secrets.baseline`` alongside
    ``state.json`` and ``event.jsonl``.
    """
    msg = _write_msg(tmp_path, "[P14] state: reopen P14\n")
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


def test_accepts_state_touching_audit_store(tmp_path: Path, mod) -> None:
    """Rewritten from the retired ``[P##-CORE] state:`` form.

    Bare ``[P##] state:`` accepts the typed audit store alongside
    ``event.jsonl``.
    """
    msg = _write_msg(tmp_path, "[P14] state: register audit\n")
    code, diag = mod.lint(
        msg,
        [".ea/state.json", ".ea/store/audit.jsonl", ".ea/store/event.jsonl"],
    )
    assert code == 0, diag


def test_accepts_state_touching_evidence_store(tmp_path: Path, mod) -> None:
    # The deterministic close gate appends evidence.jsonl rows as a wave
    # closes; every per-kind JSONL under .ea/store/ is a committed daemon
    # store and rides the state-bookkeeping surface.
    msg = _write_msg(tmp_path, "[P29-I13] state: close wave\n")
    code, diag = mod.lint(
        msg,
        [".ea/state.json", ".ea/store/event.jsonl", ".ea/store/evidence.jsonl"],
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
    state = {
        "current": {"phase_id": phase_id, "iter_id": None},
        "phases": {},
        "iters": {},
        "waves": {},
    }
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
    state = _write_hierarchy_state(tmp_path, wave_status="claimed", iter_token="I03")
    code, diag = mod.lint(
        msg,
        ["src/eawf/x.py"],
        state_path=state,
        repo_root=repo,
        canonical_state_path=state,
    )
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


def _write_hierarchy_state(
    tmp_path: Path,
    *,
    wave_status: str,
    iter_token: str = "I01",
    phase_status: str = "active",
    iter_status: str = "active",
    current_phase_id: str | None = "P28",
    current_iter_id: str | None = None,
    filename: str = "hierarchy-state.json",
) -> Path:
    """Write one bidirectionally-linked phase/iter/wave lint fixture."""
    phase_id = "P28"
    iter_id = f"{phase_id}-{iter_token}"
    wave_id = f"{iter_id}-W02"
    current_iter = iter_id if current_iter_id is None else current_iter_id
    payload = {
        "current": {
            "phase_id": current_phase_id,
            "iter_id": current_iter,
        },
        "phases": {
            phase_id: {
                "id": phase_id,
                "status": phase_status,
                "iter_ids": [iter_id],
            }
        },
        "iters": {
            iter_id: {
                "id": iter_id,
                "phase_id": phase_id,
                "status": iter_status,
                "wave_ids": [wave_id],
            }
        },
        "waves": {
            wave_id: {
                "id": wave_id,
                "iter_id": iter_id,
                "status": wave_status,
            }
        },
    }
    path = tmp_path / filename
    import json as _json

    path.write_text(_json.dumps(payload), encoding="utf-8")
    return path


def test_managed_state_decode_failure_rejects_bare_commit(tmp_path: Path, mod) -> None:
    """A present-but-corrupt managed state never fails open."""
    state = tmp_path / "state.json"
    state.write_text("{not-json", encoding="utf-8")
    msg = _write_msg(tmp_path, "fix: cannot authorize against corrupt state\n")

    code, diag = mod.lint(msg, ["src/eawf/x.py"], state_path=state)

    assert code == 1
    assert "managed state decode failed" in diag


def test_wave_subject_rejects_unknown_hierarchy(tmp_path: Path, mod) -> None:
    state = _write_hierarchy_state(tmp_path, wave_status="claimed")
    msg = _write_msg(tmp_path, "[P28-I01-W03] fix: unknown wave\n")

    code, diag = mod.lint(
        msg,
        ["src/eawf/x.py"],
        state_path=state,
        canonical_state_path=state,
    )

    assert code == 1
    assert "unknown wave reference" in diag


def test_wave_subject_rejects_wrong_bidirectional_parent(tmp_path: Path, mod) -> None:
    state = _write_hierarchy_state(tmp_path, wave_status="claimed")
    import json as _json

    payload = _json.loads(state.read_text(encoding="utf-8"))
    payload["iters"]["P28-I01"]["wave_ids"] = []
    state.write_text(_json.dumps(payload), encoding="utf-8")
    msg = _write_msg(tmp_path, "[P28-W02] fix: detached wave\n")

    code, diag = mod.lint(
        msg,
        ["src/eawf/x.py"],
        state_path=state,
        canonical_state_path=state,
    )

    assert code == 1
    assert "does not contain wave" in diag


def test_short_wave_subject_normalizes_to_i01(tmp_path: Path, mod) -> None:
    state = _write_hierarchy_state(tmp_path, wave_status="claimed")
    msg = _write_msg(tmp_path, "[P28-W02] fix: valid short wave\n")

    code, diag = mod.lint(
        msg,
        ["src/eawf/x.py"],
        state_path=state,
        canonical_state_path=state,
    )

    assert code == 0, diag


def test_source_commit_requires_current_active_phase_and_iter(tmp_path: Path, mod) -> None:
    state = _write_hierarchy_state(
        tmp_path,
        wave_status="claimed",
        phase_status="closed",
        iter_status="closed",
    )
    msg = _write_msg(tmp_path, "[P28-W02] fix: stale source scope\n")

    code, diag = mod.lint(
        msg,
        ["src/eawf/x.py"],
        state_path=state,
        canonical_state_path=state,
    )

    assert code == 1
    assert "not current ACTIVE phase" in diag


def test_trailer_reference_rejects_unknown_hierarchy(tmp_path: Path, mod) -> None:
    state = _write_hierarchy_state(tmp_path, wave_status="claimed", iter_token="I03")
    msg = _write_msg(
        tmp_path,
        "fix: bad trailer scope\n\nEawf-Wave: P28-I03-W03\n",
    )

    code, diag = mod.lint(
        msg,
        ["src/eawf/x.py"],
        state_path=state,
        subject_style="trailer",
        canonical_state_path=state,
    )

    assert code == 1
    assert "Eawf-Wave trailer rejected" in diag
    assert "unknown wave reference" in diag


def test_claimed_proof_uses_canonical_state_not_stale_child(tmp_path: Path, mod) -> None:
    child = _write_hierarchy_state(
        tmp_path,
        wave_status="pending",
        filename="child-state.json",
    )
    canonical = _write_hierarchy_state(
        tmp_path,
        wave_status="claimed",
        filename="canonical-state.json",
    )
    msg = _write_msg(tmp_path, "[P28-W02] fix: canonical claim proof\n")

    code, diag = mod.lint(
        msg,
        ["src/eawf/x.py"],
        state_path=child,
        canonical_state_path=canonical,
    )

    assert code == 0, diag


@pytest.mark.parametrize("status", ["claimed", "in_progress"])
def test_claimed_proof_accepts_live_canonical_status(tmp_path: Path, mod, status: str) -> None:
    state = _write_hierarchy_state(tmp_path, wave_status=status)
    msg = _write_msg(tmp_path, "[P28-W02] fix: live proof\n")

    code, diag = mod.lint(
        msg,
        ["src/eawf/x.py"],
        state_path=state,
        canonical_state_path=state,
    )

    assert code == 0, diag


def test_claimed_proof_rejects_pending_canonical_state(tmp_path: Path, mod) -> None:
    child = _write_hierarchy_state(
        tmp_path,
        wave_status="claimed",
        filename="child-state.json",
    )
    canonical = _write_hierarchy_state(
        tmp_path,
        wave_status="pending",
        filename="canonical-state.json",
    )
    msg = _write_msg(tmp_path, "[P28-W02] fix: pending canonical proof\n")

    code, diag = mod.lint(
        msg,
        ["src/eawf/x.py"],
        state_path=child,
        canonical_state_path=canonical,
    )

    assert code == 1
    assert "canonical status 'pending'" in diag


def test_claimed_proof_rejects_missing_canonical_state(tmp_path: Path, mod) -> None:
    state = _write_hierarchy_state(tmp_path, wave_status="claimed")
    msg = _write_msg(tmp_path, "[P28-W02] fix: missing canonical proof\n")

    code, diag = mod.lint(
        msg,
        ["src/eawf/x.py"],
        state_path=state,
        canonical_state_path=tmp_path / "missing-canonical.json",
    )

    assert code == 1
    assert "claimed proof unavailable" in diag


def test_canonical_state_path_uses_supported_git_common_dir(
    tmp_path: Path, mod, monkeypatch: pytest.MonkeyPatch
) -> None:
    common_dir = tmp_path / "main" / ".git"
    seen: dict[str, object] = {}

    def _run(args: list[str], **kwargs: object) -> object:
        seen["args"] = args
        seen["cwd"] = kwargs.get("cwd")
        return SimpleNamespace(returncode=0, stdout=f"{common_dir}\n", stderr="")

    monkeypatch.setattr(mod.subprocess, "run", _run)

    assert mod._canonical_state_path(tmp_path) == tmp_path / "main" / ".ea" / "state.json"
    assert seen["args"] == [
        "git",
        "rev-parse",
        "--path-format=absolute",
        "--git-common-dir",
    ]
    assert seen["cwd"] == tmp_path


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


def test_check_release_annotation_accepts_v060_phase_close_subject(mod) -> None:
    """P30-I15-W09: the v0.6.0 phase-close annotation is accepted (returns None).

    The deterministic close commit that ends P30 carries
    ``(release=v0.6.0)``; ``_check_release_annotation`` returns ``None`` (no
    rejection) so the annotation rides the ``[P30] state:`` close subject.
    """
    subject = "[P30] state: close iter + phase (audit=A50-P30) (release=v0.6.0)"
    assert mod._check_release_annotation(subject) is None


@pytest.mark.parametrize(
    "annotation",
    [
        "(release=0.6.0)",  # missing leading 'v'
        "(release=v0.6)",  # not MAJOR.MINOR.PATCH
        "(release=v0.6.0.0)",  # too many segments
        "(release=v0.6.0-rc1)",  # hyphenated pre-release not accepted
    ],
)
def test_check_release_annotation_rejects_malformed_v060_forms(mod, annotation: str) -> None:
    """P30-I15-W09: a malformed v0.6.0-shaped annotation is rejected.

    The prefix ``(release=`` is present but the version body does not match
    ``v<MAJOR>.<MINOR>.<PATCH>[aN|bN|rcN]``, so the linter rejects with the
    ``release annotation rejected`` diagnostic.
    """
    subject = f"[P30] state: close iter + phase (audit=A50-P30) {annotation}"
    result = mod._check_release_annotation(subject)
    assert result is not None
    code, diag = result
    assert code == 1
    assert "release annotation rejected" in diag


def test_phase_release_workflow_regex_captures_v060_tag_and_version() -> None:
    """P30-I15-W09: the phase-release.yaml annotation regex captures v0.6.0.

    Pins the workflow's annotation-parse contract: the ``(release=v...)``
    regex embedded in ``.github/workflows/phase-release.yaml`` captures
    ``tag=v0.6.0`` and ``version=0.6.0`` from the close-commit subject. The
    test reads the regex out of the YAML so the workflow and this assertion
    stay coupled.
    """
    import re

    workflow = (_REPO_ROOT / ".github" / "workflows" / "phase-release.yaml").read_text(
        encoding="utf-8"
    )
    pattern_match = re.search(r'match = re\.search\(r"([^"]+)", subject\)', workflow)
    assert pattern_match is not None, "annotation regex literal not found in phase-release.yaml"
    annotation_re = re.compile(pattern_match.group(1))

    subject = "[P30] state: close iter + phase (audit=A50-P30) (release=v0.6.0)"
    captured = annotation_re.search(subject)
    assert captured is not None
    tag = captured.group(1)
    assert tag == "v0.6.0"
    assert tag[1:] == "0.6.0"


# ---------------------------------------------------------------------------
# P30-I23-W32: harden the release-annotation lint against the BL-1 fused shape
# ``(audit=..., release=v0.6.0)`` (the one-character malformation that would
# silently zero tag + PyPI + npm), and dry-run the drafted W22 phase-close
# subject against the phase-release.yaml:46 extraction regex.
# ---------------------------------------------------------------------------

# The drafted W22 phase-close subject that re-closes P30 as v0.6.0. Bare
# ``[P30] state:`` is the canonical phase-close form; a W-suffixed variant is
# exercised too since the lint accepts the annotation on any state-type subject.
_W22_SUBJECT = "[P30] state: close iter + phase (audit=A-P30-I22-ship) (release=v0.6.0)"
_W22_SUBJECT_WAVE = (
    "[P30-I21-W22] state: close iter + phase (audit=A-P30-I22-ship) (release=v0.6.0)"
)
# BL-1 fused shape: the ``release=`` annotation is welded into the audit paren
# group instead of standing alone, so the workflow regex never matches it.
_W22_SUBJECT_FUSED = "[P30] state: close iter + phase (audit=A-P30-I22-ship, release=v0.6.0)"


def _workflow_release_regex() -> re.Pattern[str]:
    """Return the phase-release.yaml:46 extraction regex, read from the YAML.

    Reads the literal out of the workflow so the dry-run assertions stay
    coupled to the exact regex the release automation runs.
    """
    workflow = (_REPO_ROOT / ".github" / "workflows" / "phase-release.yaml").read_text(
        encoding="utf-8"
    )
    pattern_match = re.search(r'match = re\.search\(r"([^"]+)", subject\)', workflow)
    assert pattern_match is not None, "annotation regex literal not found in phase-release.yaml"
    return re.compile(pattern_match.group(1))


def test_check_release_annotation_rejects_fused_shape(mod) -> None:
    """CR-01: the fused ``(audit=A-x, release=v0.6.0)`` shape is a hard reject.

    ``release=`` is present, but the annotation is welded into the audit paren
    group rather than standing alone, so ``_check_release_annotation`` rejects
    with a diagnostic naming the phase-release.yaml workflow regex.
    """
    subject = "[P30] state: close iter + phase (audit=A-x, release=v0.6.0)"
    result = mod._check_release_annotation(subject)
    assert result is not None
    code, diag = result
    assert code == 1
    assert "release annotation rejected" in diag
    assert "phase-release.yaml" in diag


def test_check_release_annotation_accepts_standalone_paren_group(mod) -> None:
    """CR-01: the standalone ``(audit=A-x) (release=v0.6.0)`` group is accepted."""
    subject = "[P30] state: close iter + phase (audit=A-x) (release=v0.6.0)"
    assert mod._check_release_annotation(subject) is None


def test_lint_rejects_fused_release_annotation(tmp_path: Path, mod) -> None:
    """CR-01: end-to-end lint rejects the fused shape (exit 1, workflow message)."""
    msg = _write_msg(
        tmp_path,
        "[P30] state: close iter + phase (audit=A-x, release=v0.6.0)\n\nbody\n",
    )
    code, diag = mod.lint(msg, [".ea/state.json", ".ea/store/event.jsonl"])
    assert code == 1
    assert "release annotation rejected" in diag
    assert "phase-release.yaml" in diag


def test_lint_accepts_standalone_release_annotation(tmp_path: Path, mod) -> None:
    """CR-01: end-to-end lint accepts the standalone paren group (exit 0)."""
    msg = _write_msg(
        tmp_path,
        "[P30] state: close iter + phase (audit=A-x) (release=v0.6.0)\n\nbody\n",
    )
    code, diag = mod.lint(msg, [".ea/state.json", ".ea/store/event.jsonl"])
    assert code == 0, diag


def test_dry_run_w22_subject_passes_lint(tmp_path: Path, mod) -> None:
    """CR-02: the drafted W22 phase-close subject passes the lint (exit 0)."""
    msg = _write_msg(tmp_path, f"{_W22_SUBJECT}\n\nbody\n")
    code, diag = mod.lint(msg, [".ea/state.json", ".ea/store/event.jsonl"])
    assert code == 0, diag


def test_dry_run_w22_wave_suffixed_subject_passes_lint(tmp_path: Path, mod) -> None:
    """CR-02: the W-suffixed W22 variant also passes the lint (exit 0)."""
    msg = _write_msg(tmp_path, f"{_W22_SUBJECT_WAVE}\n\nbody\n")
    code, diag = mod.lint(msg, [".ea/state.json", ".ea/store/event.jsonl"])
    assert code == 0, diag


def test_dry_run_w22_subject_matches_workflow_regex_byte_for_byte(mod) -> None:
    """CR-02: the drafted W22 subject matches the phase-release.yaml:46 regex.

    The workflow's extraction regex, read out of the YAML, captures
    ``tag=v0.6.0`` and ``version=0.6.0`` from the drafted subject — so the
    release automation would tag + publish exactly as intended.
    """
    annotation_re = _workflow_release_regex()
    for subject in (_W22_SUBJECT, _W22_SUBJECT_WAVE):
        captured = annotation_re.search(subject)
        assert captured is not None, subject
        assert captured.group(1) == "v0.6.0"
        assert captured.group(1)[1:] == "0.6.0"


def test_dry_run_fused_w22_subject_fails_lint_and_workflow_regex(tmp_path: Path, mod) -> None:
    """CR-02: the fused BL-1 shape fails the lint AND the workflow regex.

    Confirms the lint catches exactly what the workflow would silently miss:
    the fused ``(audit=..., release=v0.6.0)`` shape does not match the
    phase-release.yaml:46 regex (so the workflow would skip the release), and
    the hardened lint rejects it up front.
    """
    annotation_re = _workflow_release_regex()
    assert annotation_re.search(_W22_SUBJECT_FUSED) is None

    msg = _write_msg(tmp_path, f"{_W22_SUBJECT_FUSED}\n\nbody\n")
    code, diag = mod.lint(msg, [".ea/state.json", ".ea/store/event.jsonl"])
    assert code == 1
    assert "release annotation rejected" in diag
    assert "phase-release.yaml" in diag


def test_phase_release_workflow_version_source_regex_reads_060() -> None:
    """P30-I15-W09: the workflow version-source regex reads ``__version__``.

    Pins the version-source-match guard: the regex that reads
    ``src/eawf/_version.py`` in ``phase-release.yaml`` extracts the version
    string the step compares against the annotation (``0.6.0`` post-W07).
    """
    import re

    workflow = (_REPO_ROOT / ".github" / "workflows" / "phase-release.yaml").read_text(
        encoding="utf-8"
    )
    version_match = re.search(r"match = re\.search\(\s*r'(\^__version__[^']+)'", workflow)
    assert version_match is not None, "version-source regex literal not found in phase-release.yaml"
    version_re = re.compile(version_match.group(1), flags=re.MULTILINE)
    captured = version_re.search('__version__ = "0.6.0"\n')
    assert captured is not None
    assert captured.group(1) == "0.6.0"
