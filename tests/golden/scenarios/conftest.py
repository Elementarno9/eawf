"""Pytest fixtures + shared helpers for the end-to-end golden scenarios.

This package exercises three byte-stable scenarios against the eawf
public API:

1. ``fresh_repo`` — :func:`eawf.platform.install.wizard.run_wizard_no_input`
   against an empty target directory.
2. ``enrich_existing`` — ditto, but with arbitrary pre-existing files
   in the target so we can assert nothing outside ``.ea/`` is touched.
3. ``flow_full`` — full ``project init -> phase open -> iter open ->
   wave plan/claim/close -> phase close`` walk via
   :class:`typer.testing.CliRunner`.

state.json is NOT byte-stable on its own — :func:`project_state`
projects it to the subset that is genuinely deterministic across
runs (schema_version, scope_kind, the ids and statuses of phases /
iters / waves, and the keys of ``current``). The committed golden
files under each scenario directory hold exactly that projection,
serialised as canonical JSON (``json.dumps(sort_keys=True,
indent=2)`` + trailing newline). AGENTS.md is byte-stable across
runs (verified by a sibling assertion) but its raw body embeds the
project's canonical rules text — including literal pattern examples
that the user-scope PII guard rejects on commit. We therefore commit
a region-id projection (``find_regions`` over the rendered text)
rather than the raw bytes, and pin byte-stability via a separate
two-run assertion that never persists the bytes to disk.

Regenerating goldens
--------------------

::

    uv run eawf snapshot update --kind scenarios

Under any switch in :data:`_REGEN_ENV_VARS` the helpers here overwrite the
goldens with the live projection instead of asserting. Commit the regenerated
files in the change-set that motivated the regen, so a reviewer sees the drift.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from eawf.surfaces.render.regions import find_regions

# Directory containing the three scenario golden fixtures, alongside this file.
SCENARIOS_DIR: Path = Path(__file__).parent


@pytest.fixture(autouse=True)
def _daemonless_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the V1 daemonless carve-out for every golden-scenario test.

    The scenarios exercise the CLI surface via Typer's ``CliRunner`` in
    process. After P24-W10 the built-in ``daemon.proxy_enabled`` default
    is ``True`` — without an explicit opt-out the wave / phase / iter
    mutators would try to reach the daemon (which does not run in test).
    See :mod:`tests.integration.conftest` for the equivalent fixture on
    the integration tree.
    """
    monkeypatch.setenv("EAWF_DAEMONLESS", "1")


#: Refresh switches this suite honours. The first is suite-specific; the other
#: two are the repo-wide switches ``eawf snapshot update`` sets, so the declared
#: refresh path reaches this suite instead of re-running the failing assertion.
_REGEN_ENV_VARS: tuple[str, ...] = (
    "EAWF_GOLDEN_SCENARIOS_REGEN",
    "EAWF_REFRESH_GOLDEN",
    "EAWF_SNAPSHOT_REGEN",
)


def regen_mode() -> bool:
    """Return True when any refresh switch is ``1`` / ``true`` / ``yes``."""
    return any(
        os.environ.get(name, "").strip().lower() in {"1", "true", "yes"} for name in _REGEN_ENV_VARS
    )


def project_state(state: dict[str, Any]) -> dict[str, Any]:
    """Canonical projection of state.json onto its byte-stable subset.

    Drops timestamps and urns (which embed seeded ids that vary across
    runs only when the seed changes — but updated_at always varies).
    Keeps the structural fields a reviewer cares about: schema version,
    scope kind, current-pointer keys, and the ids / statuses of every
    phase, iter, and wave.

    The projection is intentionally minimal so a state-shape change
    that does NOT affect the lifecycle surface (e.g. a new optional
    field on Phase) does not force a golden regen. A change that DOES
    affect the surface (e.g. a renamed status) is expected to fail
    the assertion, prompting an intentional regen.
    """
    current_block = state.get("current") or {}
    phases_block = state.get("phases") or {}
    iters_block = state.get("iters") or {}
    waves_block = state.get("waves") or {}
    return {
        "schema_version": state.get("schema_version"),
        "scope_kind": state.get("scope_kind"),
        "current_keys": sorted(current_block.keys()),
        "phases_ids": sorted(phases_block.keys()),
        "phases_statuses": {pid: (p or {}).get("status") for pid, p in phases_block.items()},
        "iters_ids": sorted(iters_block.keys()),
        "waves_ids": sorted(waves_block.keys()),
    }


def dump_canonical_json(payload: dict[str, Any]) -> str:
    """Serialise *payload* into the canonical golden form.

    ``sort_keys=True`` makes the file deterministic regardless of dict
    insertion order; ``indent=2`` keeps diffs readable; the trailing
    newline keeps pre-commit's end-of-file-fixer a no-op.
    """
    return json.dumps(payload, sort_keys=True, indent=2) + "\n"


def _regen_destination(golden_path: Path) -> Path:
    """Return where a regen write lands, honouring ``EAWF_SNAPSHOT_OUT``.

    ``eawf snapshot update --out`` promises the rewritten bytes go to that
    directory instead of the committed tree; without this the command reported
    the redirect while writing in place. The path under the scenarios root is
    preserved so per-scenario subdirectories do not collide on basename.
    """
    out_dir = os.environ.get("EAWF_SNAPSHOT_OUT")
    if not out_dir:
        return golden_path
    return Path(out_dir) / golden_path.relative_to(SCENARIOS_DIR)


def assert_or_regen_json(golden_path: Path, live_payload: dict[str, Any]) -> None:
    """Compare *live_payload* against the JSON at *golden_path*.

    Under a refresh switch this writes the canonical form to
    :func:`_regen_destination` and returns without asserting; otherwise it
    asserts byte-equality so the golden's whitespace matches the live output.
    """
    canonical = dump_canonical_json(live_payload)
    if regen_mode():
        destination = _regen_destination(golden_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(canonical, encoding="utf-8")
        return
    assert golden_path.exists(), (
        f"golden file missing: {golden_path}; "
        f"regenerate with `uv run eawf snapshot update --kind scenarios`"
    )
    on_disk = golden_path.read_text(encoding="utf-8")
    assert on_disk == canonical, (
        f"golden projection drifted at {golden_path}. "
        f"If intentional, regenerate with `uv run eawf snapshot update --kind scenarios`."
    )


def project_agents_md(text: str) -> dict[str, Any]:
    """Canonical projection of a rendered AGENTS.md onto its region surface.

    Captures the ordered list of managed-region ids and the count of
    bytes in each region's rendered body. We deliberately avoid
    committing the raw bytes (some bodies contain literal pattern
    examples that trip the user-scope PII guard on commit; the byte
    content itself is already pinned by the sibling
    ``tests/golden/agents_md/`` fixtures and by the two-run
    byte-stability assertion in :mod:`.test_scenarios`).

    The projection captures enough surface to flag a structural
    regression (a missing region, a renamed region, a body that
    suddenly doubles in size) without naming the bytes.
    """
    regions = find_regions(text)
    return {
        "region_ids": [r.id for r in regions],
        "region_byte_lengths": {r.id: len(r.body.encode("utf-8")) for r in regions},
    }


@pytest.fixture
def scenarios_dir() -> Path:
    """Return the directory holding scenario golden fixtures (same dir as this file)."""
    return SCENARIOS_DIR


@pytest.fixture
def fresh_target(tmp_path: Path) -> Path:
    """Empty target directory for the ``fresh_repo`` scenario."""
    target = tmp_path / "fresh"
    target.mkdir()
    return target


@pytest.fixture
def enriched_target(tmp_path: Path) -> Path:
    """Pre-populated target directory for the ``enrich_existing`` scenario.

    Lays down a fake ``AGENTS.md`` (user content), a ``README.md``, and a
    ``.git/HEAD`` placeholder. The scenario then runs the wizard and
    asserts the wizard touched only the canonical surfaces (``.ea/``,
    ``AGENTS.md``, ``CLAUDE.md``) while leaving the rest of the
    pre-existing tree intact.
    """
    target = tmp_path / "enriched"
    target.mkdir()
    (target / "README.md").write_text("# Existing project\n", encoding="utf-8")
    (target / "user_notes.txt").write_text("untouched user note\n", encoding="utf-8")
    git_dir = target / ".git"
    git_dir.mkdir()
    (git_dir / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    return target


@pytest.fixture
def flow_target(tmp_path: Path) -> Path:
    """Empty target directory for the ``flow_full`` scenario."""
    target = tmp_path / "flow"
    target.mkdir()
    return target
