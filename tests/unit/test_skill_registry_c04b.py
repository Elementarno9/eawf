"""The ``/memory`` skill registers, declares its manifest and emits a typed envelope.

- After import-side-effect bootstrap the registry binds ``/memory`` to
  :class:`MemorySkill`.
- The module declares a :class:`SkillManifest` (typed, ``runtime``
  non-empty, ``output_envelope_kind`` set).
- The happy path returns ``ok`` with a populated dict body, and the
  missing-required-arg path degrades to ``needs_user``.
"""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest

from eawf.runtime.runtimes.plugin_manifest import SkillManifest
from eawf.workflow.skills import (
    _bootstrap as _skills_bootstrap,  # noqa: F401 — registers all skills
)
from eawf.workflow.skills import memory as memory_module
from eawf.workflow.skills import registry
from eawf.workflow.skills.engine import SkillContext, run_skill
from eawf.workflow.skills.memory import MemorySkill


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


def test_memory_skill_registered_under_canonical_name() -> None:
    assert registry.list_registered()["/memory"] is MemorySkill


def test_memory_skill_declares_manifest() -> None:
    manifest = memory_module.MANIFEST
    assert isinstance(manifest, SkillManifest)
    assert manifest.name == "/memory"
    assert manifest.runtime, "manifest runtime list must be non-empty"
    assert manifest.output_envelope_kind


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
