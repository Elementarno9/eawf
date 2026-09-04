"""The fleet drive dispatches an unpinned wave via the configured runtime.

Regression for the autopilot-idle bug surfaced by the v0.6.0 codex smoke:
the fleet ``_default_spawner`` issues ``agent.dispatch`` with no ``runtime``
param, and no wave in practice carries ``runtime_preference`` -- so
:func:`~eawf.runtime.daemon.methods.agent._pick_runtime` raised "no runtime
resolved" and the drive never dispatched. The dispatch now falls back to the
project's configured ``runtime.preference`` (the operator's explicit
``runtime.adapters`` choice, which ``eawf init --runtime <id>`` writes), which
is what lets ``eawf init --runtime codex`` drive a codex fleet without
stamping every wave.
"""

from __future__ import annotations

from pathlib import Path

import orjson
import pytest

from eawf.runtime.daemon.methods.agent import _resolve_config_runtime_preference

pytestmark = pytest.mark.unit


def _write_repo(tmp_path: Path, *, config: dict | None) -> Path:
    """Materialise a minimal ``.ea`` repo and return its ``state.json`` path."""
    ea = tmp_path / ".ea"
    ea.mkdir(parents=True)
    state_path = ea / "state.json"
    state_path.write_bytes(orjson.dumps({"schema_version": "1.0", "waves": {}}))
    if config is not None:
        import yaml

        (ea / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    return state_path


def test_resolve_runtime_preference_reads_configured_runtime(tmp_path: Path) -> None:
    # ``eawf init --runtime codex`` writes runtime.preference=[codex]
    # (alongside adapters); the dispatch fallback surfaces that so a fleet
    # drive on an unpinned wave resolves codex instead of failing fast.
    state_path = _write_repo(
        tmp_path, config={"runtime": {"adapters": ["codex"], "preference": ["codex"]}}
    )

    assert _resolve_config_runtime_preference(state_path) == ["codex"]


def test_resolve_runtime_preference_orders_by_preference(tmp_path: Path) -> None:
    # ``runtime.preference`` is the ordered-fallback list and wins over the
    # bare adapters set.
    state_path = _write_repo(
        tmp_path,
        config={"runtime": {"adapters": ["codex"], "preference": ["opencode", "codex"]}},
    )

    assert _resolve_config_runtime_preference(state_path) == ["opencode", "codex"]


def test_resolve_runtime_preference_default_config_resolves_a_runtime(tmp_path: Path) -> None:
    # The point of the fix: even a default config (no runtime block authored)
    # surfaces the merged-default runtime, so the fleet dispatch ALWAYS
    # resolves a runtime rather than raising "no runtime resolved". The exact
    # default id is the layered-config default (claude), not the assertion --
    # the contract is "non-empty so the drive can dispatch".
    state_path = _write_repo(tmp_path, config={})

    resolved = _resolve_config_runtime_preference(state_path)

    assert resolved, "default config must still resolve a runtime so the fleet drive can dispatch"
