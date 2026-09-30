"""SURF-009: the loading model removal is verified against matches what the hosts load.

A render confirms removal by re-probing the files each host loads
(:func:`eawf.platform.rules.host_probe.host_loaded_files`). These probes run the
installed host binary against a rendered repository, with a local stub model API
and a temporary home, and read what the host actually sent the model: a rule the
render removed stops reaching the model exactly when the model says it stops
loading, and a generated file left on disk keeps reaching it exactly as the
model predicts. SURF-004 rides the same probes: the global instruction chain
the budget charges is the one the host really loads from its own home.
A probe skips when the host is not installed.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from eawf.platform.rules.host_facts import load_host_facts
from eawf.platform.rules.host_probe import global_document_chain, host_loaded_files
from eawf.platform.rules.render import CARD_TARGET, POLICY_TARGET, render_rule_projections
from tests.integration.runtime.harness.test_host_key_probes import (
    _claude_stub,
    _ClaudeHost,
    _CodexHost,
    needs_claude,
    needs_codex,
)

pytestmark = [pytest.mark.integration, pytest.mark.slow]

_SENTINEL = "CONSTITUTION_SENTINEL_7F3C"
_STALE_SKILL = "eawf-rules-stale-probe"
_RULE: dict[str, Any] = {
    "rule_id": "repo.sentinel",
    "obligation_id": "demo.sentinel",
    "revision": 1,
    "title": "Quote the sentinel",
    "zone": "constitution",
    "force": "must",
    "effectiveness": "behavioral",
    "instruction": f"Quote {_SENTINEL} before every report.",
    "verification": {"method": "review"},
}


@pytest.fixture
def host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _ClaudeHost:
    root = tmp_path.resolve()
    project = root / "project"
    for directory in (project / ".claude", root / "home", root / "config"):
        directory.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(root / "home"))
    return _ClaudeHost(root=root, project=project)


def _render(project: Path, *rules: dict[str, Any]) -> None:
    source = project / ".ea" / "rules.yaml"
    source.parent.mkdir(parents=True, exist_ok=True)
    document = {"schema_version": 1, "modules": [], "rules": list(rules)}
    source.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    render_rule_projections(project)


def _sent(host: _ClaudeHost) -> str:
    """Return everything the host sent the model for one session."""
    stub = _claude_stub([])
    host.run("hello", stub)
    return "\n".join(json.dumps(e.body) for e in stub.exchanges if e.role == "root")


def _modelled(project: Path) -> str:
    """Return the text of every file the loading model says a host loads."""
    loaded = host_loaded_files(
        project, load_host_facts(), projections={"card": CARD_TARGET, "policy": POLICY_TARGET}
    )
    return "\n".join((project / path).read_text("utf-8") for path in loaded)


@needs_claude
def test_surf_009_a_removed_rule_stops_reaching_claude_when_the_model_says_so(
    host: _ClaudeHost,
) -> None:
    _render(host.project, _RULE)
    assert _SENTINEL in _modelled(host.project)
    assert _SENTINEL in _sent(host)

    _render(host.project)

    assert _SENTINEL not in _modelled(host.project)
    assert _SENTINEL not in _sent(host)


@needs_claude
def test_surf_009_a_generated_file_left_on_disk_still_reaches_claude_as_modelled(
    host: _ClaudeHost,
) -> None:
    _render(host.project, _RULE)
    stale = host.project / ".claude" / "skills" / _STALE_SKILL / "SKILL.md"
    stale.parent.mkdir(parents=True)
    stale.write_text(f"---\nname: {_STALE_SKILL}\ndescription: stale\n---\nstale\n", "utf-8")

    assert _STALE_SKILL in _modelled(host.project)
    assert _STALE_SKILL in _sent(host)


@pytest.fixture
def codex(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _CodexHost:
    root = tmp_path.resolve()
    for directory in (root / "codex-home", root / "home", root / "work"):
        directory.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(root / "home"))
    return _CodexHost(root=root)


@needs_codex
def test_surf_009_a_removed_rule_stops_reaching_codex_when_the_model_says_so(
    codex: _CodexHost,
) -> None:
    work = codex.root / "work"
    _render(work, _RULE)
    assert _SENTINEL in _modelled(work)
    assert _SENTINEL in codex.prompt_input("")

    _render(work)

    assert _SENTINEL not in _modelled(work)
    assert _SENTINEL not in codex.prompt_input("")


@needs_claude
def test_surf_004_the_global_chain_the_budget_charges_is_the_one_claude_loads(
    host: _ClaudeHost,
) -> None:
    config = host.root / "config"
    (config / "CLAUDE.md").write_text("\n".join(["GLOBAL_SENTINEL_A1", "@extra.md", ""]), "utf-8")
    (config / "extra.md").write_text("GLOBAL_IMPORT_SENTINEL_B2\n", "utf-8")
    environ = host.env("http://127.0.0.1:9")

    charged = global_document_chain("claude", environ, host.root / "home")

    assert len(charged) == 2
    sent = _sent(host)
    assert "GLOBAL_SENTINEL_A1" in sent
    assert "GLOBAL_IMPORT_SENTINEL_B2" in sent


@needs_codex
def test_surf_004_the_global_document_the_budget_charges_is_the_one_codex_loads(
    codex: _CodexHost,
) -> None:
    (codex.home / "AGENTS.md").write_text("CODEX_GLOBAL_SENTINEL_C3\n", "utf-8")

    charged = global_document_chain("codex", codex.env(), codex.root / "home")

    assert list(charged.values()) == ["CODEX_GLOBAL_SENTINEL_C3\n"]
    assert "CODEX_GLOBAL_SENTINEL_C3" in codex.prompt_input("")
