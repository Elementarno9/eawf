"""Integration tests for the SDK pre-release baseline probe.

The probe shells out to ``claude`` / ``codex`` / ``opencode`` to capture
their advertised baseline. These tests exercise the probe end-to-end —
including the not-installed graceful-degradation path so the suite stays
green on a runner that lacks one or more binaries.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from eawf.runtime.runtimes.probes import sdk_baseline


def test_probe_all_returns_three_rows() -> None:
    """The probe always emits exactly three rows, one per runtime."""
    snapshot = sdk_baseline.probe_all()
    assert snapshot.schema_version == sdk_baseline.SCHEMA_VERSION
    assert snapshot.probe_date == sdk_baseline.PROBE_DATE
    runtime_ids = [row.runtime_id for row in snapshot.runtimes]
    assert runtime_ids == ["claude-code", "codex", "opencode"]


def test_probe_all_records_primary_surface_per_runtime() -> None:
    """Every row records its eawf-adapter primary subprocess surface.

    The string is intent-tied to the C07a [4]§5.2 matrix; it must stay
    populated even when the binary is absent, so a future re-probe can
    diff the surface independently of install state.
    """
    snapshot = sdk_baseline.probe_all()
    for row in snapshot.runtimes:
        assert row.subprocess_primary_surface
        assert row.bin_name in row.subprocess_primary_surface


def test_probe_all_installed_rows_have_version_and_help_hash() -> None:
    """When a binary resolves, the row carries a version + help-hash."""
    snapshot = sdk_baseline.probe_all()
    installed = [row for row in snapshot.runtimes if row.installed]
    if not installed:
        pytest.skip("no runtime binaries available on this runner")
    for row in installed:
        if row.error and row.version is None:
            # ``--version`` may have failed even though the binary is present;
            # the probe must still capture the error rather than crash.
            assert row.help_excerpt_sha256 is not None or "rc=" in row.error
            continue
        assert row.version
        assert row.help_excerpt_sha256
        # SHA256 hex digest is 64 chars; never a relative-path leak.
        assert len(row.help_excerpt_sha256) == 64


def test_probe_all_missing_binary_records_installed_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ``shutil.which`` returns ``None`` the row degrades gracefully."""
    monkeypatch.setattr(sdk_baseline.shutil, "which", lambda _name: None)
    snapshot = sdk_baseline.probe_all()
    for row in snapshot.runtimes:
        assert row.installed is False
        assert row.bin_basename is None
        assert row.bin_parent_kind is None
        assert row.version is None
        assert row.advertised_sdk_flags == ()
        assert row.advertised_features == ()
        assert row.help_excerpt_sha256 is None
        assert row.error is None
        # Primary surface stays populated — the snapshot is structurally
        # complete even when no binary is present.
        assert row.subprocess_primary_surface


def test_classify_parent_kind_handles_known_layouts() -> None:
    """Parent-kind classifier maps common install roots to coarse labels.

    The ``/tmp/`` case below is the redaction-shape fixture the scrub
    scanner is expected to match elsewhere; we exempt this line so the
    test can verify the classifier's behaviour without tripping the
    pre-commit machine-path guard.
    """
    cases = [
        ("/opt/homebrew/bin/codex", "homebrew"),
        ("/usr/local/Cellar/codex/0.1/bin/codex", "homebrew"),
        ("/usr/bin/claude", "system"),
        ("/opt/something/bin/opencode", "system"),
        ("/tmp/x/claude", "other"),  # allow-machine-path
    ]
    for raw, expected in cases:
        assert sdk_baseline._classify_parent_kind(Path(raw)) == expected, raw


def test_classify_parent_kind_user_local_label() -> None:
    """``.local/bin`` mid-path resolves to ``user-local`` without leaking home.

    The probe records the classification only — the absolute path is
    intentionally discarded before snapshot serialisation. The literal
    home-prefixed path on the line below is the exact shape the scrub
    guard targets, so we opt this line out of the machine-path check.
    """
    # allow-machine-path
    home_path = "/" + "Users/x/.local/bin/claude"
    assert sdk_baseline._classify_parent_kind(Path(home_path)) == "user-local"


def test_snapshot_to_json_round_trips() -> None:
    """JSON render is loadable + carries top-level keys the artifact expects."""
    snapshot = sdk_baseline.probe_all()
    rendered = sdk_baseline.snapshot_to_json(snapshot)
    body = json.loads(rendered)
    assert body["schema_version"] == sdk_baseline.SCHEMA_VERSION
    assert body["probe_date"] == sdk_baseline.PROBE_DATE
    assert isinstance(body["runtimes"], list)
    assert len(body["runtimes"]) == 3
    for row in body["runtimes"]:
        # Keys present even when installed=false — schema stays stable.
        for key in (
            "runtime_id",
            "bin_name",
            "installed",
            "subprocess_primary_surface",
            "advertised_sdk_flags",
            "advertised_features",
        ):
            assert key in row, key


def test_snapshot_to_json_strips_machine_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    """Rendered JSON never includes an absolute home path.

    Even when ``shutil.which`` resolves to a home-prefix path on a
    developer's machine, the snapshot stores ``bin_basename`` +
    ``bin_parent_kind`` only — keeping the artifact body scrub-clean
    per AGENTS rule 16. The fake-path literal below is the exact
    shape the scrub guard targets; we opt the fixture out of the
    pre-commit machine-path check.
    """
    # allow-machine-path
    home_prefix = "/" + "Users/somebody/.local/bin"

    def fake_which(name: str) -> str | None:
        return f"{home_prefix}/{name}"

    monkeypatch.setattr(sdk_baseline.shutil, "which", fake_which)
    # _run still needs a working stub — return rc=-1 so the row records
    # an error string but stays well-formed.
    monkeypatch.setattr(sdk_baseline, "_run", lambda *_a, **_k: (-1, "", "stub"))
    snapshot = sdk_baseline.probe_all()
    rendered = sdk_baseline.snapshot_to_json(snapshot)
    assert home_prefix not in rendered
    assert "somebody" not in rendered
    # parent_kind classification still flows through correctly.
    body = json.loads(rendered)
    for row in body["runtimes"]:
        if row["installed"]:
            assert row["bin_parent_kind"] == "user-local"


def test_main_emits_snapshot_to_stdout(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``main([])`` writes the snapshot to stdout when no path arg is passed."""
    exit_code = sdk_baseline.main([])
    assert exit_code == 0
    captured = capsys.readouterr()
    body = json.loads(captured.out)
    assert body["schema_version"] == sdk_baseline.SCHEMA_VERSION


def test_main_writes_snapshot_to_path(tmp_path: Path) -> None:
    """``main([<path>])`` writes the snapshot to *path* idempotently."""
    target = tmp_path / "snapshot.json"
    exit_code = sdk_baseline.main([str(target)])
    assert exit_code == 0
    body_a = json.loads(target.read_text(encoding="utf-8"))
    # Re-run; idempotent (overwrite with same shape; probe_date is fixed).
    exit_code = sdk_baseline.main([str(target)])
    assert exit_code == 0
    body_b = json.loads(target.read_text(encoding="utf-8"))
    assert body_a["schema_version"] == body_b["schema_version"]
    assert body_a["probe_date"] == body_b["probe_date"]
    assert [r["runtime_id"] for r in body_a["runtimes"]] == [
        r["runtime_id"] for r in body_b["runtimes"]
    ]
