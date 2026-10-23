"""``eawf config set`` writes a mapping leaf, and the governor may sit in the local layer.

Every test runs against temp config layers: the global layer is redirected to a
per-test path and the repo layer is anchored on a tmp repo through ``chdir``.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from eawf.kernel.config import layered
from eawf.kernel.config.registry.leaf_catalog import LEAF_KEY_REGISTRY
from eawf.kernel.config.registry.leaf_keys import _WRITABLE_ALL_DURABLE
from eawf.surfaces.cli.app import app

runner = CliRunner()

_GOVERNOR = "{max_concurrent_runs: 8, max_in_flight_tokens: 8000000, admission: queue}"
_REFERENCE = Path(__file__).resolve().parents[4] / "docs" / "reference" / "config.md"


@pytest.fixture
def repo_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Provide an isolated repo root and a sandboxed global-config path."""
    repo = tmp_path / "repo"
    (repo / ".ea").mkdir(parents=True)
    monkeypatch.setattr(layered, "global_config_path", lambda: tmp_path / "global.yaml")
    monkeypatch.setenv("EAWF_DAEMONLESS", "1")
    monkeypatch.chdir(repo)
    yield repo


def _local_layer(repo: Path) -> object:
    return yaml.safe_load((repo / ".ea" / "local" / "config.yaml").read_text(encoding="utf-8"))


def test_set_local_governor_round_trips_with_source_local(repo_root: Path) -> None:
    result = runner.invoke(
        app, ["config", "set", "economics.governor", _GOVERNOR, "--scope", "local"]
    )
    assert result.exit_code == 0, result.output

    expected = {"max_concurrent_runs": 8, "max_in_flight_tokens": 8000000, "admission": "queue"}
    assert _local_layer(repo_root) == {"economics": {"governor": expected}}
    got = runner.invoke(app, ["--json", "config", "get", "economics.governor"])
    assert got.exit_code == 0, got.output
    body = json.loads(got.output)["result"]
    assert body == {"key": "economics.governor", "value": expected, "source": "local"}


def test_set_local_governor_single_field_mapping(repo_root: Path) -> None:
    result = runner.invoke(
        app, ["config", "set", "economics.governor", "{admission: queue}", "--scope", "local"]
    )
    assert result.exit_code == 0, result.output
    assert _local_layer(repo_root) == {"economics": {"governor": {"admission": "queue"}}}


def test_set_local_governor_empty_mapping(repo_root: Path) -> None:
    result = runner.invoke(app, ["config", "set", "economics.governor", "{}", "--scope", "local"])
    assert result.exit_code == 0, result.output
    assert _local_layer(repo_root) == {"economics": {"governor": {}}}


def test_get_local_governor_source_is_the_highest_layer_stating_part_of_it(
    repo_root: Path, tmp_path: Path
) -> None:
    (tmp_path / "global.yaml").write_text(
        "economics:\n  governor:\n    admission: refuse\n", encoding="utf-8"
    )
    only_global = runner.invoke(app, ["--json", "config", "get", "economics.governor"])
    assert json.loads(only_global.output)["result"]["source"] == "global"

    result = runner.invoke(
        app,
        ["config", "set", "economics.governor", "{max_concurrent_runs: 2}", "--scope", "local"],
    )
    assert result.exit_code == 0, result.output
    got = runner.invoke(app, ["--json", "config", "get", "economics.governor"])
    body = json.loads(got.output)["result"]
    assert body["source"] == "local"
    assert body["value"] == {"admission": "refuse", "max_concurrent_runs": 2}


def test_set_local_governor_json_value(repo_root: Path) -> None:
    result = runner.invoke(
        app, ["config", "set", "economics.governor", '{"admission": "queue"}', "--scope", "local"]
    )
    assert result.exit_code == 0, result.output
    assert _local_layer(repo_root) == {"economics": {"governor": {"admission": "queue"}}}


def test_set_mapping_refusal_skips_a_key_the_catalog_does_not_name(repo_root: Path) -> None:
    result = runner.invoke(app, ["config", "set", "foo.bar", "{a: 1}", "--scope", "local"])
    assert result.exit_code == 0, result.output
    assert _local_layer(repo_root) == {"foo": {"bar": "{a: 1}"}}


@pytest.mark.parametrize("raw", ["8", "[1, 2]", "queue", "null", "{admission: [", ""])
def test_set_mapping_refusal_of_a_non_mapping_value(repo_root: Path, raw: str) -> None:
    result = runner.invoke(app, ["config", "set", "economics.governor", raw, "--scope", "local"])
    assert result.exit_code != 0, result.output
    assert "economics.governor" in result.output
    assert not (repo_root / ".ea" / "local" / "config.yaml").exists()


def test_set_mapping_refusal_through_with_names_the_key(repo_root: Path) -> None:
    result = runner.invoke(
        app,
        [
            "config",
            "set",
            "economics.governor",
            "{admission: queue}",
            "--with",
            "economics.notice_policy=8",
            "--scope",
            "local",
        ],
    )
    assert result.exit_code != 0, result.output
    assert "economics.notice_policy" in result.output


def test_set_mapping_refusal_leaves_scalar_coercion_unchanged(repo_root: Path) -> None:
    result = runner.invoke(app, ["config", "set", "estimation.eu_minutes", "45", "--scope", "repo"])
    assert result.exit_code == 0, result.output
    parsed = yaml.safe_load((repo_root / ".ea" / "config.yaml").read_text(encoding="utf-8"))
    assert parsed["estimation"]["eu_minutes"] == 45


def test_governor_layers_are_all_durable_layers() -> None:
    entry = LEAF_KEY_REGISTRY["economics.governor"]
    assert entry.writable_layers == _WRITABLE_ALL_DURABLE
    assert entry.writable_layers == ("global", "workspace", "repo", "branch", "local")


def test_governor_layers_named_in_the_reference_row() -> None:
    text = _REFERENCE.read_text(encoding="utf-8")
    row = re.search(r"^\| `economics\.governor` \|.*$", text, re.MULTILINE)
    assert row is not None
    cells = [cell.strip() for cell in row.group(0).strip("|").split("|")]
    assert cells[3] == "global, workspace, repo, branch, local"
