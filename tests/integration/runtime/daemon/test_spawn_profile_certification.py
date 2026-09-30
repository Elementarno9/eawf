"""SURF-012: an uncertified enriched profile never instructs a headless spawn.

A headless wave spawn has no operator watching it, so the live dispatch path
resolves the enabled profiles as unattended: an enriched profile whose digest
is not certified as managed is left out of the spawned agent's prompt, and a
certified one reaches it.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path

import pytest
import yaml

from eawf.platform.profiles import discovery
from eawf.platform.profiles.certification import profile_digest
from eawf.platform.profiles.models import ProfileBody
from eawf.platform.render_block import DISPATCH_SYSTEM_PROMPT_TARGET
from eawf.runtime.daemon.methods.agent import dispatch
from eawf.runtime.runtimes.adapter import SpawnResult
from tests.integration.runtime.daemon.test_spawn_deny_list import (
    _WAVE_ID,
    _ctx,
    _DenyRecordingAdapter,
    _patch_adapter,
    _run,
    _write_state,
)

pytestmark = pytest.mark.integration

_HOUSE_RULE = "Quote the house sentinel 7f3c before every report."


class _PromptRecordingAdapter(_DenyRecordingAdapter):
    """A stub adapter that also keeps every prompt it was asked to spawn."""

    def __init__(self) -> None:
        super().__init__()
        self.prompts: list[str] = []

    async def spawn_session(
        self,
        prompt: str,
        *,
        model: str,
        cwd: str | None = None,
        extra_args: Sequence[str] = (),
        denied_tools: Sequence[str] = (),
        timeout: float | None = None,
        on_spawn: Callable[[int], None] | None = None,
        on_chunk: Callable[[str], Awaitable[None]] | None = None,
    ) -> SpawnResult:
        self.prompts.append(prompt)
        return await super().spawn_session(
            prompt,
            model=model,
            cwd=cwd,
            extra_args=extra_args,
            denied_tools=denied_tools,
            timeout=timeout,
            on_spawn=on_spawn,
            on_chunk=on_chunk,
        )


def _install_profile(root: Path, *, certify: bool) -> None:
    profile: dict[str, object] = {
        "name": "house",
        "render_blocks": [
            {
                "id": "house-rule",
                "target": DISPATCH_SYSTEM_PROMPT_TARGET,
                "agent_role": "executor",
                "body_template": _HOUSE_RULE,
            }
        ],
    }
    certified: dict[str, str] = {}
    if certify:
        digest = profile_digest(ProfileBody.model_validate(profile))
        profile["certification"] = {"digest": digest}
        certified["house"] = digest
    (root / ".ea" / "profiles").mkdir(parents=True, exist_ok=True)
    (root / ".ea" / "profiles" / "house.yaml").write_text(yaml.safe_dump(profile), "utf-8")
    config = {"profiles": {"enabled": ["house"], "certified": certified}}
    (root / ".ea" / "config.yaml").write_text(yaml.safe_dump(config), "utf-8")


def _spawned_prompt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, certify: bool) -> str:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    discovery._clear_cache_for_tests()
    state_path = _write_state(tmp_path)
    _install_profile(tmp_path, certify=certify)
    adapter = _PromptRecordingAdapter()
    _patch_adapter(monkeypatch, adapter)
    ctx = _ctx(state_path, event_path=tmp_path / ".ea" / "store" / "event.jsonl")
    _run(dispatch(ctx, {"wave_id": _WAVE_ID, "spawn": True}))
    assert len(adapter.prompts) == 1
    return adapter.prompts[0]


def test_surf_012_headless_spawn_leaves_out_an_uncertified_enriched_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert _HOUSE_RULE not in _spawned_prompt(tmp_path, monkeypatch, certify=False)


def test_surf_012_headless_spawn_carries_a_certified_enriched_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert _HOUSE_RULE in _spawned_prompt(tmp_path, monkeypatch, certify=True)
