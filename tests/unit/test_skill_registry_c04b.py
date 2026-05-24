"""C04b (P26-W11) acceptance: the 6 missing skills + 17-skill catalog.

Pins the C04b §5 contract for the six skills landed inline per D-b4
(``/coauthor``, ``/memory``, ``/agent-dispatch``, ``/compress``,
``/wave-spec``, ``/security-review``):

- After import-side-effect bootstrap, the skill registry holds all 17
  canonical skills (11 original + 6 new) — closing the C00 17-skill
  catalog drift.
- Each new skill registers under its canonical slashed name.
- Each new skill declares a :class:`SkillManifest` (typed, ``runtime``
  non-empty, ``output_envelope_kind`` set).
- Each new skill emits a typed envelope (no placeholder ``pass`` body):
  the happy path returns ``ok``/``partial`` with a populated dict body,
  and the missing-required-arg path degrades to ``needs_user``.
"""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest

from eawf.runtimes.plugin_manifest import SkillManifest
from eawf.workflow.skills import (
    _bootstrap as _skills_bootstrap,  # noqa: F401 — registers all skills
)
from eawf.workflow.skills import registry
from eawf.workflow.skills.agent_dispatch import AgentDispatchSkill
from eawf.workflow.skills.coauthor import CoauthorSkill
from eawf.workflow.skills.compress import CompressSkill
from eawf.workflow.skills.engine import Skill, SkillContext, run_skill
from eawf.workflow.skills.memory import MemorySkill
from eawf.workflow.skills.security_review import SecurityReviewSkill
from eawf.workflow.skills.wave_spec import WaveSpecSkill

# The six C04b skills landed by P26-W11, keyed by canonical name.
_C04B_SKILLS: dict[str, type[Skill]] = {
    "/coauthor": CoauthorSkill,
    "/memory": MemorySkill,
    "/agent-dispatch": AgentDispatchSkill,
    "/compress": CompressSkill,
    "/wave-spec": WaveSpecSkill,
    "/security-review": SecurityReviewSkill,
}

# The 11 skills that predate C04b (six core + four meta + /blitz).
_ORIGINAL_SKILLS: frozenset[str] = frozenset(
    {
        "/research",
        "/prep",
        "/audit",
        "/ship",
        "/review",
        "/polish",
        "/init",
        "/roadmap",
        "/differentiate",
        "/flow",
        "/blitz",
    }
)


@pytest.fixture
def state_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    state_dir = tmp_path / ".ea"
    state_dir.mkdir(parents=True, exist_ok=True)
    state_path = state_dir / "state.json"
    monkeypatch.setenv("EA_STATE", str(state_path))
    monkeypatch.setenv("EA_INSTRUMENT_PROBE", str(state_dir / "instrument-probe.json"))
    return state_dir


def _ctx() -> SkillContext:
    return SkillContext(
        scope="urn:eawf:v1:state:QR/P00",
        session="urn:eawf:v1:store:QR/sessions/SES-1",
    )


def test_registry_holds_all_seventeen_skills() -> None:
    """C00 17-skill catalog drift closed (D-b4): 11 original + 6 new."""
    registered = set(registry.list_registered())
    expected = _ORIGINAL_SKILLS | set(_C04B_SKILLS)
    assert len(expected) == 17
    assert expected <= registered, f"missing from registry: {expected - registered}"


@pytest.mark.parametrize("name", sorted(_C04B_SKILLS))
def test_c04b_skill_registered_under_canonical_name(name: str) -> None:
    assert registry.lookup(name) is _C04B_SKILLS[name]


@pytest.mark.parametrize("name", sorted(_C04B_SKILLS))
def test_c04b_skill_declares_manifest(name: str) -> None:
    import importlib

    module = importlib.import_module(_C04B_SKILLS[name].__module__)
    manifest = module.MANIFEST
    assert isinstance(manifest, SkillManifest)
    assert manifest.name == name
    assert manifest.runtime, "manifest runtime list must be non-empty"
    assert manifest.output_envelope_kind


def test_coauthor_happy_path(state_dir: Path) -> None:
    env = run_skill(CoauthorSkill(), _ctx())
    assert env.header.status == "ok"
    body = cast(dict, env.body)
    assert body["kind"] == "coauthor_resolution"
    # Default runtime mode resolves the Claude trailer.
    assert body["trailer"] == "Co-Authored-By: Claude <noreply@anthropic.com>"


def test_memory_save_requires_name(state_dir: Path) -> None:
    ctx = _ctx()
    ctx.args = {"verb": "save"}
    env = run_skill(MemorySkill(), ctx)
    assert env.header.status == "needs_user"


def test_memory_list_happy_path(state_dir: Path) -> None:
    ctx = _ctx()
    ctx.args = {"verb": "list"}
    env = run_skill(MemorySkill(), ctx)
    assert env.header.status == "ok"
    body = cast(dict, env.body)
    assert body["verb"] == "list"
    assert body["tier"] == "working"
    assert len(env.footer.persisted_store_records) == 1


def test_agent_dispatch_resolves_ladder_head(state_dir: Path) -> None:
    ctx = _ctx()
    ctx.args = {"wave_id": "P26-I01-W11", "runtime_preference": ["codex", "claude-code"]}
    env = run_skill(AgentDispatchSkill(), ctx)
    assert env.header.status == "ok"
    body = cast(dict, env.body)
    assert body["resolved_runtime"] == "codex"
    assert body["wave_id"] == "P26-I01-W11"


def test_agent_dispatch_missing_wave_id_needs_user(state_dir: Path) -> None:
    env = run_skill(AgentDispatchSkill(), _ctx())
    assert env.header.status == "needs_user"


def test_agent_dispatch_no_ladder_is_partial(state_dir: Path) -> None:
    ctx = _ctx()
    ctx.args = {"wave_id": "P26-I01-W11"}
    env = run_skill(AgentDispatchSkill(), ctx)
    assert env.header.status == "partial"
    assert cast(dict, env.body)["resolved_runtime"] is None


def test_compress_records_token_ratio(state_dir: Path) -> None:
    ctx = _ctx()
    ctx.args = {"tokens_before": 1000, "tokens_after": 250}
    env = run_skill(CompressSkill(), ctx)
    assert env.header.status == "ok"
    body = cast(dict, env.body)
    assert body["tokens_before"] == 1000
    assert body["tokens_after"] == 250
    assert body["ratio"] == 0.25


def test_compress_requires_tokens_before(state_dir: Path) -> None:
    env = run_skill(CompressSkill(), _ctx())
    assert env.header.status == "needs_user"


def test_compress_clamps_growth_to_no_op(state_dir: Path) -> None:
    """A reported tokens_after above tokens_before clamps to a 1.0 ratio."""
    ctx = _ctx()
    ctx.args = {"tokens_before": 100, "tokens_after": 500}
    env = run_skill(CompressSkill(), ctx)
    body = cast(dict, env.body)
    assert body["tokens_after"] == 100
    assert body["ratio"] == 1.0


def test_wave_spec_init_requires_wave_id(state_dir: Path) -> None:
    env = run_skill(WaveSpecSkill(), _ctx())
    assert env.header.status == "needs_user"


def test_wave_spec_validate_happy_path(state_dir: Path) -> None:
    ctx = _ctx()
    ctx.args = {"verb": "validate", "wave_id": "P26-I01-W11"}
    env = run_skill(WaveSpecSkill(), ctx)
    assert env.header.status == "ok"
    body = cast(dict, env.body)
    assert body["verb"] == "validate"
    assert body["wave_id"] == "P26-I01-W11"
    assert env.footer.next_valid_actions == ["eawf spec validate P26-I01-W11"]


def test_security_review_missing_spec_needs_user(state_dir: Path) -> None:
    env = run_skill(SecurityReviewSkill(), _ctx())
    assert env.header.status == "needs_user"


def test_security_review_runs_passing_spec(state_dir: Path, tmp_path: Path) -> None:
    spec = tmp_path / "audit.yaml"
    spec.write_text(
        "schema_version: '1.0'\n"
        "checks:\n"
        "  - kind: file_exists\n"
        "    name: spec-present\n"
        "    args:\n"
        f"      path: {spec.name}\n",
        encoding="utf-8",
    )
    ctx = _ctx()
    ctx.args = {"spec_path": str(spec), "cwd": str(tmp_path)}
    env = run_skill(SecurityReviewSkill(), ctx)
    assert env.header.status == "ok"
    body = cast(dict, env.body)
    assert body["checks_run"] == 1
    assert body["findings"][0]["passed"] is True


def test_security_review_failing_spec_is_failed(state_dir: Path, tmp_path: Path) -> None:
    spec = tmp_path / "audit.yaml"
    spec.write_text(
        "schema_version: '1.0'\n"
        "checks:\n"
        "  - kind: file_exists\n"
        "    name: missing-file\n"
        "    args:\n"
        "      path: does-not-exist.txt\n",
        encoding="utf-8",
    )
    ctx = _ctx()
    ctx.args = {"spec_path": str(spec), "cwd": str(tmp_path)}
    env = run_skill(SecurityReviewSkill(), ctx)
    assert env.header.status == "failed"
    assert env.footer.repair_commands  # failing checks surface repair commands
