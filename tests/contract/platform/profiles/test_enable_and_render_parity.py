"""``config profile enable`` and ``sync`` resolve every profile layer identically.

Each case plants the same profile id in a set of layers, every copy
carrying a layer-specific required state key and a layer-specific
AGENTS.md marker. Enable reports the state key it materialised and render
writes the marker it composed, so the pair names the layer each path
resolved; the two must agree, and both must name the highest layer present.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from eawf.platform.profiles import discovery
from eawf.surfaces.cli.app import app
from tests._epoch2_helpers import lay_epoch2_tree

runner = CliRunner()

_PROBE = "parity-probe"
_LAYERS = ("user", "workspace", "repo")


@pytest.fixture(autouse=True)
def _clear_cache() -> None:
    discovery._clear_cache_for_tests()


@pytest.fixture()
def roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """Return ``(repo, workspace)`` with an isolated home and cwd at the repo."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    repo = tmp_path / "repo"
    workspace = tmp_path / "ws"
    (repo / ".ea").mkdir(parents=True)
    workspace.mkdir()
    # Born at epoch 2, as ``eawf init`` leaves a tree after the flag day; the
    # enable reports the probe's required state keys instead of writing them.
    lay_epoch2_tree(repo, state={"schema_version": "1.0"})
    monkeypatch.chdir(repo)
    return repo, workspace


def _layer_dir(layer: str, repo: Path, workspace: Path) -> Path:
    if layer == "user":
        return discovery.user_profiles_dir()
    return discovery.workspace_profiles_dir(repo if layer == "repo" else workspace)


def _plant(layer: str, repo: Path, workspace: Path) -> None:
    root = _layer_dir(layer, repo, workspace)
    root.mkdir(parents=True, exist_ok=True)
    (root / f"{_PROBE}.yaml").write_text(
        f"""\
name: {_PROBE}
state_extensions:
  fields_required: [probe_{layer}]
render_blocks:
  - id: parity-probe
    target: AGENTS.md
    version: "1.0"
    body_template: |
      ## Parity probe
      resolved-from-{layer}
""",
        encoding="utf-8",
    )


def _enable(workspace_flag: list[str]) -> dict[str, object]:
    result = runner.invoke(app, ["--json", *workspace_flag, "config", "profile", "enable", _PROBE])
    assert result.exit_code == 0, result.output
    return json.loads(result.output)["result"]


def _render(repo: Path, workspace_flag: list[str]) -> str:
    # Sync reads state strictly; the bare enable fixture is not a full state.
    (repo / ".ea" / "state.json").unlink(missing_ok=True)
    result = runner.invoke(app, ["--json", *workspace_flag, "sync", "--target", str(repo)])
    assert result.exit_code == 0, result.output
    return (repo / "AGENTS.md").read_text(encoding="utf-8")


def _rendered_layers(agents_md: str) -> list[str]:
    return [layer for layer in _LAYERS if f"resolved-from-{layer}" in agents_md]


@pytest.mark.parametrize(
    "present",
    [
        ("user",),
        ("workspace",),
        ("repo",),
        ("user", "workspace"),
        ("workspace", "repo"),
        ("user", "workspace", "repo"),
    ],
)
def test_surf_070_enable_and_render_resolve_the_same_layer(
    roots: tuple[Path, Path], present: tuple[str, ...]
) -> None:
    repo, workspace = roots
    for layer in present:
        _plant(layer, repo, workspace)
    flag = ["--workspace", str(workspace)]
    expected = present[-1]

    enabled = _enable(flag)
    agents_md = _render(repo, flag)

    assert enabled["state_keys_required"] == [f"probe_{expected}"]
    assert enabled["state_keys_materialised"] == []
    assert _rendered_layers(agents_md) == [expected]


def test_surf_070_repository_layer_resolves_for_both_without_a_workspace(
    roots: tuple[Path, Path],
) -> None:
    repo, workspace = roots
    _plant("repo", repo, workspace)

    enabled = _enable([])
    agents_md = _render(repo, [])

    assert enabled["state_keys_required"] == ["probe_repo"]
    assert _rendered_layers(agents_md) == ["repo"]


def test_surf_070_builtin_layer_resolves_for_both(roots: tuple[Path, Path]) -> None:
    repo, _workspace = roots
    result = runner.invoke(app, ["--json", "config", "profile", "enable", "python"])
    assert result.exit_code == 0, result.output
    assert "Python style (python profile)" in _render(repo, [])


def test_surf_070_id_in_no_layer_is_refused_by_both(roots: tuple[Path, Path]) -> None:
    repo, _workspace = roots
    result = runner.invoke(app, ["config", "profile", "enable", _PROBE])
    assert result.exit_code != 0
    assert "unknown profile" in result.output
    (repo / ".ea" / "config.yaml").write_text(
        f"profiles:\n  enabled: [{_PROBE}]\n", encoding="utf-8"
    )
    rendered = runner.invoke(app, ["sync", "--target", str(repo)])
    assert rendered.exit_code != 0
    assert "unknown enabled profiles" in rendered.output
