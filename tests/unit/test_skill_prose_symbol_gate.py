"""Unit tests for ``tools/skill_prose_symbol_gate.py``.

Covers the prose-vs-symbol drift gate over the frozen skill registry:

- the gate PASSES on the reconciled live registry (every dotted symbol /
  slash-form module / source path the prose names resolves);
- a fixture body naming the unresolvable ``kernel.spec.research.IntentBrief``
  FAILS, naming the offending token plus the carrying skill;
- the conservative matcher produces no false positives on bare prose nouns
  (``IntentBrief`` / ``--depth`` / ``gate``), slash-joined option phrases
  (``approve/edit/cancel``), and runtime / config dotted access
  (``header.skill``);
- boundary inputs (empty prose, code-fence-only prose) yield no findings.

The gate module is loaded via :mod:`importlib` because ``tools/`` is
excluded from the package and so is not importable by name (mirrors
``tests/unit/test_idle_contract_gate.py``). The matcher is exercised
through :func:`scan_body` over injected ``(skill_name, body)`` pairs so
the failure cases never edit shipped prose.
"""

from __future__ import annotations

import importlib.util
import sys
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_GATE_PATH = _REPO_ROOT / "tools" / "skill_prose_symbol_gate.py"
_TOOL_DIR = _GATE_PATH.parent


def _load_module() -> ModuleType:
    if str(_TOOL_DIR) not in sys.path:
        sys.path.insert(0, str(_TOOL_DIR))
    spec = importlib.util.spec_from_file_location("skill_prose_symbol_gate", _GATE_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["skill_prose_symbol_gate"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def mod() -> ModuleType:
    return _load_module()


@dataclass(frozen=True)
class _FakeSpec:
    """A minimal stand-in for ``SkillSpec`` exposing only what the gate reads."""

    skill_name: str
    body: str


def test_scan_registry_live_tree_has_no_findings(mod: ModuleType) -> None:
    """The shipped registry's prose resolves: every symbol / path is real."""
    assert mod.scan_registry() == []


def test_main_live_tree_exits_zero(mod: ModuleType) -> None:
    """The CLI entrypoint exits 0 over the reconciled live registry."""
    assert mod.main(["skill_prose_symbol_gate.py"]) == 0


def test_scan_body_unresolvable_symbol_fails_naming_token_and_skill(
    mod: ModuleType,
) -> None:
    """A body naming the unresolvable ``kernel.spec.research.IntentBrief`` fails.

    ``IntentBrief`` lives in ``eawf.kernel.spec.intent``, not
    ``eawf.kernel.spec.research`` -- so the dotted reference resolves to a
    real module that lacks the attribute, and the finding names both the
    offending token and the carrying skill.
    """
    body = "The brief conforms to `kernel.spec.research.IntentBrief` typed claims.\n"
    findings = mod.scan_body("research-fixture", body)
    assert len(findings) == 1
    finding = findings[0]
    assert finding.token == "kernel.spec.research.IntentBrief"
    assert finding.skill_name == "research-fixture"
    assert finding.kind == mod.ReferenceKind.SYMBOL
    assert "IntentBrief" in finding.reason


def test_scan_registry_injected_drifted_spec_fails(mod: ModuleType) -> None:
    """An injected spec whose prose drifts fails through the registry walker."""
    drifted = _FakeSpec(
        skill_name="drifted-skill",
        body="See `workflow/verify/oracle.no_such_runner` for the contract.\n",
    )
    findings = mod.scan_registry([drifted])
    assert len(findings) == 1
    assert findings[0].token == "workflow/verify/oracle.no_such_runner"
    assert findings[0].skill_name == "drifted-skill"
    assert findings[0].kind == mod.ReferenceKind.SYMBOL


def test_scan_body_unresolvable_slash_module_fails(mod: ModuleType) -> None:
    """A slash-form module that does not import fails (path-form rule id)."""
    body = "Driven by `platform/lint/eawf999_does_not_exist`.\n"
    findings = mod.scan_body("lint-fixture", body)
    assert len(findings) == 1
    assert findings[0].token == "platform/lint/eawf999_does_not_exist"
    assert "does not import" in findings[0].reason


def test_scan_body_bare_nouns_produce_no_findings(mod: ModuleType) -> None:
    """Bare prose nouns / flags / single words are skipped -- no false positive."""
    body = (
        "Body conforms to `IntentBrief`; the `--depth` flag controls budget; "
        "a `gate` runs; `MockupBody`; `OutputEnvelope`; `CriterionSpec`; `RoleSpec`.\n"
    )
    assert mod.scan_body("bare-fixture", body) == []


def test_scan_body_slash_joined_prose_phrase_skipped(mod: ModuleType) -> None:
    """A slash-joined option phrase (``approve/edit/cancel``) is not a module."""
    body = (
        "Surface options `approve/edit/cancel` and "
        "`use-as-is/revise/replace/cancel` to the operator.\n"
    )
    assert mod.scan_body("phrase-fixture", body) == []


def test_scan_body_runtime_and_config_dotted_access_skipped(mod: ModuleType) -> None:
    """Dotted access on a non-package root (header / config) is prose, skipped."""
    body = (
        "Envelope with `header.skill = research`; pin via "
        "`research.default_depth` in the layered config.\n"
    )
    assert mod.scan_body("dotted-prose", body) == []


def test_scan_body_resolving_references_produce_no_findings(mod: ModuleType) -> None:
    """Real slash / dotted / module-file references all resolve cleanly."""
    body = (
        "See `kernel/spec/intent.IntentBrief`, `workflow/verify/oracle.run_oracle`, "
        "`kernel/spec/math.py`, `eawf.surfaces.render.skills.render`, and "
        "`platform/lint/eawf022_propose_coverage`.\n"
    )
    assert mod.scan_body("real-refs", body) == []


def test_scan_body_source_path_existence(mod: ModuleType) -> None:
    """A real ``src/eawf`` path resolves; a fabricated one under it fails."""
    assert mod.scan_body("path-ok", "Default scope is `src/eawf/`.\n") == []
    findings = mod.scan_body("path-bad", "Look under `src/eawf/no_such_dir_here/`.\n")
    assert len(findings) == 1
    assert findings[0].token == "src/eawf/no_such_dir_here/"
    assert findings[0].kind == mod.ReferenceKind.PATH


def test_scan_body_gitignored_convention_paths_skipped(mod: ModuleType) -> None:
    """Gitignored / generated / templated trees are not existence-checked.

    ``.ea/profile.yaml`` is legitimately absent in a fresh worktree,
    ``.ea/local/...`` is gitignored, ``build/...`` is a generated plugin
    tree, and templated paths carry ``<placeholder>`` spans -- none is a
    reliable existence target, so each is skipped (no false positive).
    """
    body = (
        "Persist `.ea/profile.yaml`; drafts under `.ea/local/`; "
        "PoCs under `.ea/local/poc/<slug>/`; the planner at "
        "`build/eawf-plugin/agents/planner.md`.\n"
    )
    assert mod.scan_body("convention-paths", body) == []


def test_scan_body_empty_prose_has_no_findings(mod: ModuleType) -> None:
    """Boundary: empty prose yields no findings."""
    assert mod.scan_body("empty", "") == []


def test_scan_body_code_fence_only_prose_has_no_findings(mod: ModuleType) -> None:
    """Boundary: prose that is only a fenced code block yields no findings.

    The matcher keys on inline single-backtick spans; a triple-backtick
    fence is not a single-backtick token span and carries no inline
    reference, so the fence body is not parsed as a symbol.
    """
    body = "```\nsome arbitrary code line\nanother line\n```\n"
    assert mod.scan_body("fence-only", body) == []


def test_scan_body_dedupes_repeated_token(mod: ModuleType) -> None:
    """A token repeated in one body is reported once, not per occurrence."""
    body = (
        "First `kernel.spec.research.IntentBrief`, again "
        "`kernel.spec.research.IntentBrief` later.\n"
    )
    findings = mod.scan_body("dup-fixture", body)
    assert len(findings) == 1
