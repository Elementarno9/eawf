"""The packaged permission-request hook outlasts the longest wait a repository may set.

The host kills a hook at its manifest timeout. The permission hook waits up to
``runtime.claude.permission_wait_s`` for a principal's decision, so a timeout
at or under that wait drops the decision. The manifest is static and cannot
follow a per-repository value, so its timeout must cover the schema's upper
bound plus the hook's own start-up and daemon round trips.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from annotated_types import Le
from pydantic import ValidationError

from eawf.kernel.config.schema import RuntimeLivenessConfig
from eawf.runtime.hooks.runner import HOOK_DAEMON_DEADLINE_S
from eawf.runtime.runtimes.claude.hook_map import build_plugin_hooks_json
from eawf.runtime.runtimes.claude.plugin_package import package_plugin

pytestmark = pytest.mark.unit

#: Seconds the wrapper and ``uv run eawf`` take to reach the handler.
_START_UP_S = 3


def _wait_bound() -> int:
    """Return the ``le`` bound the schema puts on ``permission_wait_s``."""
    field = RuntimeLivenessConfig.model_fields["permission_wait_s"]
    bounds = [item.le for item in field.metadata if isinstance(item, Le)]
    assert len(bounds) == 1, "permission_wait_s must carry exactly one upper bound"
    return int(bounds[0])


def _permission_timeouts(manifest: dict[str, object]) -> list[int]:
    hooks = manifest["hooks"]
    assert isinstance(hooks, dict)
    return [hook["timeout"] for entry in hooks["PermissionRequest"] for hook in entry["hooks"]]


def test_permission_timeout_exceeds_wait(tmp_path: Path) -> None:
    bound = _wait_bound()
    # The record call and the last decision read each run to the client deadline.
    margin = _START_UP_S + 2 * HOOK_DAEMON_DEADLINE_S
    package_plugin(tmp_path / "plugin", include_hooks=True, extra_tools={})
    packaged = json.loads((tmp_path / "plugin" / "hooks" / "hooks.json").read_text("utf-8"))

    timeouts = _permission_timeouts(packaged)

    assert timeouts, "the packaged manifest subscribes no PermissionRequest hook"
    assert all(timeout >= bound + margin for timeout in timeouts), (bound, margin, timeouts)
    with pytest.raises(ValidationError, match=f"less than or equal to {bound}"):
        RuntimeLivenessConfig(permission_wait_s=bound + 1)
    assert RuntimeLivenessConfig(permission_wait_s=bound).permission_wait_s == bound


def test_only_the_permission_hook_carries_the_long_timeout() -> None:
    hooks = build_plugin_hooks_json()["hooks"]
    others = {
        event: [hook["timeout"] for entry in entries for hook in entry["hooks"]]
        for event, entries in hooks.items()
        if event != "PermissionRequest"
    }

    assert all(timeout <= 10 for timeouts in others.values() for timeout in timeouts), others
    assert others["UserPromptSubmit"] == [3]
    assert set(others["PreToolUse"]) == {5}


@pytest.mark.parametrize("wait", [-1, "20", 2.5, None])
def test_a_malformed_wait_is_refused(wait: object) -> None:
    with pytest.raises(ValidationError):
        RuntimeLivenessConfig.model_validate({"permission_wait_s": wait})


def test_an_unknown_liveness_key_is_refused() -> None:
    with pytest.raises(ValidationError, match="extra"):
        RuntimeLivenessConfig.model_validate({"permission_wait": 5})
