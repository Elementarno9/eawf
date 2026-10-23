"""The epoch-1 commit gates and config leaves stay retired.

The phase, iter and wave lifecycle is gone, so a hook that guards a wave
trailer, a wave spec under ``.ea/specs`` or a title in the frozen
``.ea/state.json`` can no longer fire, and a config leaf that capped waves
or gated an iter close has no reader. These checks red when one of them
returns.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Final

import pytest
import yaml

from eawf.kernel.config.defaults import BUILT_IN_DEFAULTS
from eawf.kernel.config.registry.leaf_catalog import DEPRECATED_LEAF_KEYS, LEAF_KEY_REGISTRY
from eawf.observability.doctor import checks

pytestmark = pytest.mark.unit

REPO: Final = Path(__file__).resolve().parents[2]

#: The pre-commit hooks that guarded epoch-1 lifecycle records.
RETIRED_HOOK_IDS: Final = frozenset({"wave-trailer-guard", "spec-paths", "eawf016-title-clarity"})
#: The config leaves only the epoch-1 lifecycle read.
RETIRED_LEAVES: Final = ("planning.max_parallel_waves", "verify.require_iter_audit_accepted")
#: The jury calibration ceilings the native verdict observations still read.
KEPT_JURY_LEAVES: Final = ("verify.jury_max_brier", "verify.jury_max_co_error")
#: The CLI module the roadmap retirement deleted.
ROADMAP_MODULE: Final = "src/eawf/surfaces/cli/commands/roadmap.py"


def _hook_ids() -> set[str]:
    config = yaml.safe_load((REPO / ".pre-commit-config.yaml").read_text(encoding="utf-8"))
    return {hook["id"] for repo in config["repos"] for hook in repo["hooks"]}


def _sets_leaf(mapping: object, dotted: str) -> bool:
    for part in dotted.split("."):
        if not isinstance(mapping, dict) or part not in mapping:
            return False
        mapping = mapping[part]
    return True


def test_retired_epoch1_hooks_are_gone() -> None:
    hook_ids = _hook_ids()
    assert "commit-prefix-lint" in hook_ids, "the hook inventory did not parse"
    assert not hook_ids & RETIRED_HOOK_IDS, sorted(hook_ids & RETIRED_HOOK_IDS)
    assert not (REPO / "tools" / "wave_trailer_guard.py").exists()


def test_no_epoch1_config_key_or_roadmap_exemption() -> None:
    config = yaml.safe_load((REPO / ".ea" / "config.yaml").read_text(encoding="utf-8"))
    assert [leaf for leaf in RETIRED_LEAVES if _sets_leaf(config, leaf)] == []
    pyproject = (REPO / "pyproject.toml").read_text(encoding="utf-8")
    tomllib.loads(pyproject)
    assert ROADMAP_MODULE not in pyproject


def test_epoch1_config_leaves_are_retired() -> None:
    assert not (REPO / "tools" / "pre_commit_spec_paths.py").exists()
    for leaf in RETIRED_LEAVES:
        assert leaf in DEPRECATED_LEAF_KEYS, leaf
        assert leaf not in LEAF_KEY_REGISTRY, leaf
        assert not _sets_leaf(BUILT_IN_DEFAULTS, leaf), leaf
    templates = sorted((REPO / "src" / "eawf" / "platform" / "templates" / "init").glob("*.yaml"))
    assert templates, "no init template found"
    assert [
        path.name for path in templates if "max_parallel_waves" in path.read_text(encoding="utf-8")
    ] == []
    assert not hasattr(checks, "resolve_max_parallel_waves")
    for leaf in KEPT_JURY_LEAVES:
        assert leaf in LEAF_KEY_REGISTRY, leaf
