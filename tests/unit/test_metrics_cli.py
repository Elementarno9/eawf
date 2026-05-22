"""CLI dispatch tests for the ``eawf metrics`` telemetry sub-verbs (P27-I01-W16).

Drives the Typer app via :class:`CliRunner` against a temp workspace and
checks the sub-verb dispatch:

- ``metrics show`` with ``telemetry.enabled=false`` prints the one-time
  opt-in nudge and exits cleanly (no projection, no metrics).
- ``metrics show`` with ``telemetry.enabled=true`` renders the rolling
  metrics (Prometheus families) from the local cache.
- ``metrics export --format prom|json|csv`` serialises to stdout and to a
  ``--out`` file.
- ``metrics info`` prints cache stats (db kind, path, pricing version).
- ``metrics rebuild --full`` drives the projector over the (empty) sources.
- An unknown sub-verb fails fast.

The global config layer is monkeypatched to a tmp path so the host's real
``~/.config/eawf/config.yaml`` cannot influence the merge.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

import eawf.config.layered as layered
from eawf.cli.app import app

runner = CliRunner()


@pytest.fixture(autouse=True)
def _isolate_global_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the global config layer at an empty tmp file + clear EA_STATE."""
    fake_global = tmp_path / "global-config.yaml"
    monkeypatch.setattr(layered, "global_config_path", lambda: fake_global)
    monkeypatch.delenv("EA_STATE", raising=False)


def _make_workspace(tmp_path: Path, *, telemetry_enabled: bool) -> Path:
    """Create a workspace with ``.ea/`` and a repo config setting telemetry.enabled."""
    workspace = tmp_path / "ws"
    ea_dir = workspace / ".ea"
    ea_dir.mkdir(parents=True)
    config = {"schema_version": "1.0", "telemetry": {"enabled": telemetry_enabled}}
    (ea_dir / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    return workspace


def test_metrics_show_disabled_prints_opt_in_nudge(tmp_path: Path) -> None:
    """``metrics show`` with telemetry disabled prints the opt-in nudge, exit 0."""
    workspace = _make_workspace(tmp_path, telemetry_enabled=False)
    result = runner.invoke(app, ["-w", str(workspace), "metrics", "show"])
    assert result.exit_code == 0, result.output
    assert "telemetry is disabled" in result.stdout
    assert "telemetry.enabled true" in result.stdout
    # No metric families should render in the disabled path.
    assert "eawf_tokens_total" not in result.stdout


def test_metrics_show_disabled_json_envelope(tmp_path: Path) -> None:
    """``--json metrics show`` (disabled) emits the typed nudge envelope."""
    workspace = _make_workspace(tmp_path, telemetry_enabled=False)
    result = runner.invoke(app, ["--json", "-w", str(workspace), "metrics", "show"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["telemetry_enabled"] is False
    assert "nudge" in payload


def test_metrics_show_enabled_renders_families(tmp_path: Path) -> None:
    """``metrics show`` with telemetry enabled renders Prometheus families."""
    workspace = _make_workspace(tmp_path, telemetry_enabled=True)
    result = runner.invoke(app, ["-w", str(workspace), "metrics", "show"])
    assert result.exit_code == 0, result.output
    assert "# HELP eawf_tokens_total" in result.stdout


@pytest.mark.parametrize("fmt", ["prom", "json", "csv"])
def test_metrics_export_to_stdout(tmp_path: Path, fmt: str) -> None:
    """``metrics export --format`` writes the rendered document to stdout."""
    workspace = _make_workspace(tmp_path, telemetry_enabled=True)
    result = runner.invoke(app, ["-w", str(workspace), "metrics", "export", "--format", fmt])
    assert result.exit_code == 0, result.output
    if fmt == "prom":
        assert "# TYPE eawf_tokens_total counter" in result.stdout
    elif fmt == "json":
        assert json.loads(result.stdout)["schema_version"] == 1
    else:
        assert result.stdout.splitlines()[0] == "metric,type,labels,value"


def test_metrics_export_to_file(tmp_path: Path) -> None:
    """``metrics export --out`` writes the document to a file."""
    workspace = _make_workspace(tmp_path, telemetry_enabled=True)
    out = tmp_path / "out" / "metrics.prom"
    result = runner.invoke(
        app,
        ["-w", str(workspace), "metrics", "export", "--format", "prom", "--out", str(out)],
    )
    assert result.exit_code == 0, result.output
    assert out.exists()
    assert "# HELP eawf_tokens_total" in out.read_text(encoding="utf-8")


def test_metrics_info_prints_cache_stats(tmp_path: Path) -> None:
    """``metrics info`` prints db kind, path, schema + pricing version."""
    workspace = _make_workspace(tmp_path, telemetry_enabled=True)
    result = runner.invoke(app, ["--json", "-w", str(workspace), "metrics", "info"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["db_kind"] == "sqlite"
    assert payload["pricing_version"] == "2026.05.17"
    assert payload["telemetry_enabled"] is True
    assert payload["db_path"].endswith("telemetry.db")


def test_metrics_rebuild_full_on_empty_sources(tmp_path: Path) -> None:
    """``metrics rebuild --full`` drives the projector and reports zero rows."""
    workspace = _make_workspace(tmp_path, telemetry_enabled=True)
    result = runner.invoke(app, ["--json", "-w", str(workspace), "metrics", "rebuild", "--full"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["mode"] == "full"
    assert payload["sessions"] == 0


def _db_path(workspace: Path) -> Path:
    """Return the telemetry cache path that a collecting sub-verb would create."""
    return workspace / ".ea" / "telemetry.db"


def test_metrics_export_disabled_creates_no_db(tmp_path: Path) -> None:
    """``metrics export`` with telemetry off must not project or create the DB."""
    workspace = _make_workspace(tmp_path, telemetry_enabled=False)
    result = runner.invoke(app, ["-w", str(workspace), "metrics", "export", "--format", "prom"])
    assert result.exit_code == 0, result.output
    assert "telemetry is disabled" in result.stdout
    assert "# HELP eawf_tokens_total" not in result.stdout
    assert not _db_path(workspace).exists()


def test_metrics_export_disabled_out_file_not_written(tmp_path: Path) -> None:
    """``metrics export --out`` with telemetry off writes neither DB nor target."""
    workspace = _make_workspace(tmp_path, telemetry_enabled=False)
    out = tmp_path / "out" / "metrics.prom"
    result = runner.invoke(
        app,
        ["-w", str(workspace), "metrics", "export", "--format", "prom", "--out", str(out)],
    )
    assert result.exit_code == 0, result.output
    assert "telemetry is disabled" in result.stdout
    assert not out.exists()
    assert not _db_path(workspace).exists()


def test_metrics_rebuild_disabled_creates_no_db(tmp_path: Path) -> None:
    """``metrics rebuild`` with telemetry off must not project or create the DB."""
    workspace = _make_workspace(tmp_path, telemetry_enabled=False)
    result = runner.invoke(app, ["-w", str(workspace), "metrics", "rebuild", "--full"])
    assert result.exit_code == 0, result.output
    assert "telemetry is disabled" in result.stdout
    assert not _db_path(workspace).exists()


def test_metrics_info_disabled_creates_no_db(tmp_path: Path) -> None:
    """``metrics info`` (a read) with telemetry off must not create the DB."""
    workspace = _make_workspace(tmp_path, telemetry_enabled=False)
    result = runner.invoke(app, ["-w", str(workspace), "metrics", "info"])
    assert result.exit_code == 0, result.output
    assert "telemetry is disabled" in result.stdout
    assert not _db_path(workspace).exists()


def test_metrics_info_disabled_json_envelope(tmp_path: Path) -> None:
    """``--json metrics info`` (disabled) emits the typed nudge envelope, no DB."""
    workspace = _make_workspace(tmp_path, telemetry_enabled=False)
    result = runner.invoke(app, ["--json", "-w", str(workspace), "metrics", "info"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["telemetry_enabled"] is False
    assert "nudge" in payload
    assert not _db_path(workspace).exists()


def test_metrics_export_enabled_creates_db(tmp_path: Path) -> None:
    """Boundary: with telemetry enabled the export path still projects (no regress)."""
    workspace = _make_workspace(tmp_path, telemetry_enabled=True)
    result = runner.invoke(app, ["-w", str(workspace), "metrics", "export", "--format", "prom"])
    assert result.exit_code == 0, result.output
    assert "# HELP eawf_tokens_total" in result.stdout
    assert _db_path(workspace).exists()


def test_metrics_info_enabled_creates_db(tmp_path: Path) -> None:
    """Boundary: with telemetry enabled, ``info`` opens the cache (no regress)."""
    workspace = _make_workspace(tmp_path, telemetry_enabled=True)
    result = runner.invoke(app, ["-w", str(workspace), "metrics", "info"])
    assert result.exit_code == 0, result.output
    assert _db_path(workspace).exists()


def test_metrics_unknown_subverb_fails(tmp_path: Path) -> None:
    """An unknown sub-verb is rejected with a non-zero exit."""
    workspace = _make_workspace(tmp_path, telemetry_enabled=False)
    result = runner.invoke(app, ["-w", str(workspace), "metrics", "bogus"])
    assert result.exit_code != 0


def test_metrics_bare_view_still_works(tmp_path: Path) -> None:
    """The bare ``eawf metrics`` invocation still renders the estimation view.

    The telemetry sub-verbs ride the same command; the bare call (no
    sub-verb) must keep the P20-W08 estimation behaviour. With no
    ``state.json`` present the estimation view fails closed with NotFound
    (exit 1), proving the bare path routes to the workflow-metrics handler
    rather than a telemetry sub-verb.
    """
    workspace = tmp_path / "no-state"
    workspace.mkdir()
    result = runner.invoke(app, ["-w", str(workspace), "metrics"])
    assert result.exit_code == 1
