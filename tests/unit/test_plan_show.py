"""Unit tests for :mod:`eawf.surfaces.render.plan_view`.

Bypasses the CLI; exercises ``build_view``, ``render_markdown``, and
``render_json`` directly. State instances are constructed inline via
``State.model_validate`` against minimal payloads.
"""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from eawf.kernel.state.models import State
from eawf.surfaces.render.plan_view import (
    PlanSection,
    PlanViewNotFound,
    build_view,
    render_json,
    render_markdown,
)

_GOLDEN_STATE_PATH = (
    Path(__file__).resolve().parents[1]
    / "golden"
    / "plan_view"
    / "core_python_research"
    / "state.json"
)


def _golden_state() -> State:
    """Load the committed plan-view golden fixture as a typed :class:`State`."""
    return State.model_validate(json.loads(_GOLDEN_STATE_PATH.read_text(encoding="utf-8")))


def _base_state() -> dict[str, Any]:
    """Return a minimal, schema-valid state with one phase and one iter."""
    return {
        "schema_version": "1.0",
        "scope_kind": "repo",
        "urn": "urn:eawf:v1:state:QR",
        "updated_at": "2026-05-08T00:00:00Z",
        "project": {
            "code": "QR",
            "slug": "quant-research",
            "title": "Quant Research",
            "description": "",
            "domains": ["quant"],
            "default_branch": "main",
            "status": "active",
            "repo_urn": "urn:eawf:v1:repo:QR",
        },
        "current": {
            "project_code": "QR",
            "subproject_id": None,
            "phase_id": "P05",
            "iter_id": "P05-I01",
            "active_wave_ids": [],
            "active_session_ids": [],
        },
        "workspace": None,
        "phases": {
            "P05": {
                "id": "P05",
                "scope_id": "QR",
                "title": "Phase Five",
                "status": "active",
                "iter_ids": ["P05-I01"],
                "outcome_ids": [],
                "opened_at": "2026-05-08T00:00:00Z",
                "closed_at": None,
                "audit_id": None,
            }
        },
        "iters": {
            "P05-I01": {
                "id": "P05-I01",
                "phase_id": "P05",
                "title": "Iter One",
                "status": "active",
                "wave_ids": [],
                "estimate_id": None,
                "audit_id": None,
                "opened_at": "2026-05-08T00:00:00Z",
                "closed_at": None,
            }
        },
        "waves": {},
        "artifacts": {},
        "agent_sessions": {},
        "plugins": {},
        "indexes": {},
    }


def _wave(
    wave_id: str,
    *,
    iter_id: str = "P05-I01",
    status: str = "pending",
    deps: list[str] | None = None,
    title: str | None = None,
    description: str | None = None,
    claim_session_id: str | None = None,
    outcome: str | None = None,
    closed_at: str | None = None,
    effort_bucket: str | None = None,
) -> dict[str, Any]:
    return {
        "id": wave_id,
        "iter_id": iter_id,
        "title": title or f"Wave {wave_id}",
        "description": description,
        "status": status,
        "deps": deps or [],
        "file_scopes": [],
        "claim_session_id": claim_session_id,
        "worktree_id": None,
        "effort_bucket": effort_bucket,
        "outcome": outcome,
        "opened_at": "2026-05-08T00:00:00Z",
        "closed_at": closed_at,
    }


def _add_wave(s: dict[str, Any], wave: dict[str, Any]) -> None:
    s["waves"][wave["id"]] = wave
    s["iters"]["P05-I01"]["wave_ids"].append(wave["id"])


def test_build_view_resolves_iter_when_id_provided() -> None:
    state = State.model_validate(_base_state())
    view = build_view(state, "P05-I01")
    assert view.iter.id == "P05-I01"
    assert view.phase is not None
    assert view.phase.id == "P05"


def test_build_view_unknown_iter_raises_not_found() -> None:
    state = State.model_validate(_base_state())
    with pytest.raises(PlanViewNotFound):
        build_view(state, "P99-I99")


def test_build_view_empty_iter_yields_empty_collections() -> None:
    state = State.model_validate(_base_state())
    view = build_view(state, "P05-I01")
    assert view.waves == []
    assert view.dag.nodes == []
    assert view.dag.edges == []
    assert view.dag.topo_order == []  # acyclic, just empty
    assert view.dag.cycle is None
    assert view.checks == []
    assert view.risks == []
    assert view.summary.wave_count == 0
    assert view.summary.blocked_waves == []


def test_build_view_topo_orders_dag() -> None:
    s = _base_state()
    _add_wave(s, _wave("P05-I01-W00", status="closed", closed_at="2026-05-08T01:00:00Z"))
    _add_wave(s, _wave("P05-I01-W01", deps=["P05-I01-W00"]))
    _add_wave(s, _wave("P05-I01-W02", deps=["P05-I01-W01"]))
    state = State.model_validate(s)
    view = build_view(state, "P05-I01")
    assert view.dag.cycle is None
    assert view.dag.topo_order == ["P05-I01-W00", "P05-I01-W01", "P05-I01-W02"]


def test_build_view_detects_dag_cycle() -> None:
    s = _base_state()
    _add_wave(s, _wave("P05-I01-W01", deps=["P05-I01-W02"]))
    _add_wave(s, _wave("P05-I01-W02", deps=["P05-I01-W01"]))
    state = State.model_validate(s)
    view = build_view(state, "P05-I01")
    assert view.dag.topo_order is None
    assert view.dag.cycle is not None
    assert set(view.dag.cycle) == {"P05-I01-W01", "P05-I01-W02"}


def test_build_view_collects_iter_audit_checks() -> None:
    s = _base_state()
    _add_wave(s, _wave("P05-I01-W01"))
    s["iters"]["P05-I01"]["audit_id"] = "AU-1"
    s["audits"] = {
        "AU-1": {
            "id": "AU-1",
            "scope_id": "P05-I01",
            "kind": "evaluation",
            "status": "complete",
            "report_artifact_id": None,
            "check_results": [
                {"name": "ruff_clean", "passed": True, "details": None},
                {"name": "mypy_strict", "passed": False, "details": "10 errors"},
            ],
            "integrity_results": [],
            "created_at": "2026-05-08T00:00:00Z",
            "verdict": "minor",
        }
    }
    state = State.model_validate(s)
    view = build_view(state, "P05-I01")
    assert len(view.checks) == 2
    assert all(c.source == "iter_audit" for c in view.checks)
    names = {c.name for c in view.checks}
    assert names == {"ruff_clean", "mypy_strict"}
    failed = next(c for c in view.checks if not c.passed)
    assert failed.details == "10 errors"


def test_build_view_collects_wave_audit_checks() -> None:
    s = _base_state()
    _add_wave(s, _wave("P05-I01-W01"))
    s["audits"] = {
        "AU-W1": {
            "id": "AU-W1",
            "scope_id": "P05-I01-W01",
            "kind": "review",
            "status": "complete",
            "report_artifact_id": None,
            "check_results": [
                {"name": "tests_green", "passed": True, "details": None},
            ],
            "integrity_results": [],
            "created_at": "2026-05-08T00:00:00Z",
            "verdict": "pass",
        }
    }
    state = State.model_validate(s)
    view = build_view(state, "P05-I01")
    assert len(view.checks) == 1
    cv = view.checks[0]
    assert cv.source == "wave_audit"
    assert cv.wave_id == "P05-I01-W01"
    assert cv.audit_id == "AU-W1"


def test_build_view_synthesises_wave_outcome_check() -> None:
    s = _base_state()
    _add_wave(
        s,
        _wave(
            "P05-I01-W01",
            status="closed",
            closed_at="2026-05-08T02:00:00Z",
            outcome="ok",
        ),
    )
    state = State.model_validate(s)
    view = build_view(state, "P05-I01")
    outcome_checks = [c for c in view.checks if c.source == "wave_outcome"]
    assert len(outcome_checks) == 1
    assert outcome_checks[0].name == "ok"
    assert outcome_checks[0].passed is True


def test_collect_risks_p0_backlog() -> None:
    s = _base_state()
    _add_wave(s, _wave("P05-I01-W01"))
    s["backlog"] = {
        "BL-1": {
            "id": "BL-1",
            "scope_id": "P05-I01",
            "title": "fix flake on W01",
            "priority": "P0",
            "status": "open",
            "created_at": "2026-05-08T00:00:00Z",
            "closed_at": None,
            "resolution": None,
        }
    }
    state = State.model_validate(s)
    view = build_view(state, "P05-I01")
    assert len(view.risks) == 1
    r = view.risks[0]
    assert r.kind == "backlog"
    assert r.id == "BL-1"
    assert r.severity == "P0"


def test_collect_risks_open_incident_high() -> None:
    s = _base_state()
    s["incidents"] = {
        "INC-1": {
            "id": "INC-1",
            "scope_id": "P05",
            "severity": "high",
            "title": "lock collision",
            "status": "open",
            "opened_at": "2026-05-08T00:00:00Z",
            "closed_at": None,
            "root_cause": None,
            "corrective_action_ids": [],
            "report_artifact_id": None,
        }
    }
    state = State.model_validate(s)
    view = build_view(state, "P05-I01")
    assert len(view.risks) == 1
    r = view.risks[0]
    assert r.kind == "incident"
    assert r.severity == "high"


def test_collect_risks_rejected_hypothesis() -> None:
    s = _base_state()
    s["hypotheses"] = {
        "H05-01": {
            "id": "H05-01",
            "scope_id": "P05-I01",
            "title": "tried approach X; failed",
            "metric": "throughput",
            "confirm": ">100",
            "reject": "<50",
            "status": "rejected",
            "verdict": "rejected",
            "audit_id": None,
            "source_artifact_id": None,
        }
    }
    state = State.model_validate(s)
    view = build_view(state, "P05-I01")
    assert len(view.risks) == 1
    r = view.risks[0]
    assert r.kind == "hypothesis_rejected"
    assert r.severity == "rejected"


def test_render_markdown_ascii_dag_handles_disjoint_components() -> None:
    s = _base_state()
    # Two trees: W00 -> W01; W02 -> W03 (no shared nodes).
    _add_wave(s, _wave("P05-I01-W00", status="closed", closed_at="2026-05-08T01:00:00Z"))
    _add_wave(s, _wave("P05-I01-W01", deps=["P05-I01-W00"]))
    _add_wave(s, _wave("P05-I01-W02", status="closed", closed_at="2026-05-08T01:00:00Z"))
    _add_wave(s, _wave("P05-I01-W03", deps=["P05-I01-W02"]))
    state = State.model_validate(s)
    view = build_view(state, "P05-I01")
    md = render_markdown(view, ascii_dag=True)
    # Both roots present at level 0 (no leading whitespace).
    lines = md.splitlines()
    assert "P05-I01-W00 (closed)" in lines
    assert "P05-I01-W02 (closed)" in lines
    # Children indented under their parent.
    assert "  -> P05-I01-W01 (pending)" in lines
    assert "  -> P05-I01-W03 (pending)" in lines


def test_render_markdown_empty_iter_uses_friendly_paragraph() -> None:
    state = State.model_validate(_base_state())
    view = build_view(state, "P05-I01")
    md = render_markdown(view)
    assert "# Plan: P05-I01" in md
    assert "No waves planned yet." in md
    assert "## Summary" not in md


def test_render_markdown_show_section_emits_only_named_block() -> None:
    s = _base_state()
    _add_wave(s, _wave("P05-I01-W01"))
    state = State.model_validate(s)
    view = build_view(state, "P05-I01")
    md = render_markdown(view, sections=PlanSection.RISKS)
    assert "## Risks" in md
    assert "## Summary" not in md
    assert "## DAG" not in md


def test_build_view_carries_wave_description() -> None:
    """``build_view`` projects ``Wave.description`` onto ``WaveView``."""
    s = _base_state()
    _add_wave(s, _wave("P05-I01-W01", description="Long-form purpose of the wave."))
    state = State.model_validate(s)
    view = build_view(state, "P05-I01")
    assert view.waves[0].description == "Long-form purpose of the wave."


def test_render_markdown_surfaces_wave_description_in_detail_block() -> None:
    """A wave with a description renders a ``### Wave details`` block."""
    s = _base_state()
    _add_wave(
        s,
        _wave("P05-I01-W01", title="Bound title", description="The longer purpose blurb."),
    )
    state = State.model_validate(s)
    view = build_view(state, "P05-I01")
    md = render_markdown(view)
    assert "### Wave details" in md
    assert "- **P05-I01-W01** — The longer purpose blurb." in md
    # The compact table cell still carries the bounded title.
    assert "**P05-I01-W01** Bound title |" in md


def test_render_markdown_omits_detail_block_without_description() -> None:
    """No wave carries a description ⇒ the ``Wave details`` block is absent."""
    s = _base_state()
    _add_wave(s, _wave("P05-I01-W01"))
    state = State.model_validate(s)
    view = build_view(state, "P05-I01")
    md = render_markdown(view)
    assert "### Wave details" not in md


def test_render_markdown_detail_block_lists_only_described_waves() -> None:
    """Only waves with a description appear in the detail block."""
    s = _base_state()
    _add_wave(s, _wave("P05-I01-W01", description="Has a blurb."))
    _add_wave(s, _wave("P05-I01-W02"))
    state = State.model_validate(s)
    view = build_view(state, "P05-I01")
    md = render_markdown(view)
    assert "- **P05-I01-W01** — Has a blurb." in md
    assert "P05-I01-W02 —" not in md


def test_render_markdown_max_length_title_fits_with_description() -> None:
    """A 72-char (max) title renders verbatim in the table; blurb in details."""
    max_title = "x" * 72
    s = _base_state()
    _add_wave(s, _wave("P05-I01-W01", title=max_title, description="Overflow text."))
    state = State.model_validate(s)
    view = build_view(state, "P05-I01")
    md = render_markdown(view)
    # The model bounds title to 72; the table renders it without truncation.
    assert f"**P05-I01-W01** {max_title} |" in md
    assert "- **P05-I01-W01** — Overflow text." in md


def test_render_json_excludes_wave_description() -> None:
    """The JSON envelope omits ``description`` so the schema stays conformant."""
    s = _base_state()
    _add_wave(s, _wave("P05-I01-W01", description="Should not leak into JSON."))
    state = State.model_validate(s)
    view = build_view(state, "P05-I01")
    env = render_json(view)
    assert env["waves"]
    assert "description" not in env["waves"][0]


def test_render_json_envelope_contains_all_keys() -> None:
    s = _base_state()
    _add_wave(s, _wave("P05-I01-W01"))
    state = State.model_validate(s)
    view = build_view(state, "P05-I01")
    env = render_json(view)
    assert set(env.keys()) == {"iter", "phase", "waves", "dag", "checks", "risks", "summary"}


def test_render_json_section_filter_keeps_envelope_shape() -> None:
    s = _base_state()
    _add_wave(s, _wave("P05-I01-W01"))
    state = State.model_validate(s)
    view = build_view(state, "P05-I01")
    env = render_json(view, sections=PlanSection.WAVES)
    # Shape stable, but body sections empty.
    assert set(env.keys()) == {"iter", "phase", "waves", "dag", "checks", "risks", "summary"}
    assert env["waves"]
    assert env["risks"] == []
    assert env["checks"] == []
    assert env["dag"]["nodes"] == []


def test_render_json_dag_envelope() -> None:
    """``--show dag`` keeps the canonical envelope shape and populates DAG only."""
    state = _golden_state()
    view = build_view(state, "P05-I01")
    env = render_json(view, sections=PlanSection.DAG)
    # Header + summary always present; only DAG body populated.
    assert set(env.keys()) == {"iter", "phase", "waves", "dag", "checks", "risks", "summary"}
    assert env["waves"] == []
    assert env["checks"] == []
    assert env["risks"] == []
    assert env["dag"]["nodes"], "dag.nodes should be populated for the golden fixture"
    # Topo order or cycle is mutually exclusive — exactly one of them is set.
    assert (env["dag"]["topo_order"] is None) != (env["dag"]["cycle"] is None)


def test_render_json_checks_envelope() -> None:
    """``--show checks`` populates ``checks`` from the iter audit + wave outcomes."""
    state = _golden_state()
    view = build_view(state, "P05-I01")
    env = render_json(view, sections=PlanSection.CHECKS)
    assert set(env.keys()) == {"iter", "phase", "waves", "dag", "checks", "risks", "summary"}
    assert env["waves"] == []
    assert env["risks"] == []
    assert env["dag"]["nodes"] == []
    assert env["checks"], "checks should be populated for the golden fixture"
    # Each row carries the documented surface keys.
    for c in env["checks"]:
        assert set(c.keys()) >= {"source", "audit_id", "wave_id", "name", "passed"}
    # Summary still carries the count derived from the unrestricted view.
    assert env["summary"]["check_count"] == len(env["checks"])


def test_render_json_risks_envelope() -> None:
    """``--show risks`` populates ``risks`` from backlog/incidents/hypotheses."""
    state = _golden_state()
    view = build_view(state, "P05-I01")
    env = render_json(view, sections=PlanSection.RISKS)
    assert set(env.keys()) == {"iter", "phase", "waves", "dag", "checks", "risks", "summary"}
    assert env["waves"] == []
    assert env["checks"] == []
    assert env["dag"]["nodes"] == []
    assert env["risks"], "risks should be populated for the golden fixture"
    # Summary risk_count mirrors the populated list length.
    assert env["summary"]["risk_count"] == len(env["risks"])
    # Each row carries the documented surface keys.
    for r in env["risks"]:
        assert set(r.keys()) >= {"kind", "id", "severity", "title", "status"}


def test_render_roadmap_markdown_emits_four_eu_hour_rows() -> None:
    """Default ``roadmap show --md`` includes work, path, queue, and realistic rows."""
    from eawf.surfaces.render.plan_view import render_roadmap_markdown

    s = _base_state()
    _add_wave(s, _wave("P05-I01-W01", effort_bucket="M"))
    _add_wave(s, _wave("P05-I01-W02", effort_bucket="M"))
    _add_wave(s, _wave("P05-I01-W03", effort_bucket="S", deps=["P05-I01-W01"]))
    state = State.model_validate(s)

    md = render_roadmap_markdown(state)

    assert "## EU/hour rollup" in md
    rows = [
        line for line in md.splitlines() if line.startswith("| `P05` | ") and "`active`" not in line
    ]
    assert [row.split("|")[2].strip() for row in rows] == [
        "work-sum",
        "critical-path",
        "queue",
        "realistic",
    ]
    assert len(rows) == 4


def test_render_roadmap_markdown_consumes_eu_view_density_and_fields() -> None:
    """``tui.eu_view`` config can compact and subset the EU/hour table."""
    from eawf.surfaces.render.plan_view import render_roadmap_markdown

    s = _base_state()
    _add_wave(s, _wave("P05-I01-W01", effort_bucket="M"))
    _add_wave(s, _wave("P05-I01-W02", effort_bucket="M"))
    state = State.model_validate(s)

    md = render_roadmap_markdown(
        state,
        config={
            "tui": {"eu_view": {"density": "compact", "fields": ["realistic"]}},
            "planning": {"max_parallel_waves": 1},
            "estimation": {"eu_minutes": 60},
        },
    )

    assert "| Phase | Metric | EU | Hours |" in md
    assert "| Phase | Metric | EU | Hours | Detail |" not in md
    assert "| `P05` | realistic | 2 | 2 |" in md
    assert "work-sum" not in md


# ---- W02: release-banded roadmap render ------------------------------------


def _add_phase(s: dict[str, Any], phase_id: str, *, release: str | None = None) -> None:
    """Attach an extra phase (with its own I01 iter) to a base-state dict."""
    iter_id = f"{phase_id}-I01"
    s["phases"][phase_id] = {
        "id": phase_id,
        "scope_id": "QR",
        "title": f"Phase {phase_id}",
        "status": "planned",
        "iter_ids": [iter_id],
        "outcome_ids": [],
        "opened_at": "2026-05-08T00:00:00Z",
        "closed_at": None,
        "audit_id": None,
        "release": release,
    }
    s["iters"][iter_id] = {
        "id": iter_id,
        "phase_id": phase_id,
        "title": f"Iter {iter_id}",
        "status": "planned",
        "wave_ids": [],
        "estimate_id": None,
        "audit_id": None,
        "opened_at": "2026-05-08T00:00:00Z",
        "closed_at": None,
    }


def _band_header_order(md: str) -> list[str]:
    """Return the ordered ``### <band>`` header labels in *md*."""
    return [line[4:] for line in md.splitlines() if line.startswith("### ")]


def test_render_roadmap_markdown_unbanded_when_no_release() -> None:
    """No phase carries a release -> single unbanded table, legacy layout."""
    from eawf.surfaces.render.plan_view import render_roadmap_markdown

    state = State.model_validate(_base_state())
    md = render_roadmap_markdown(state)
    assert md.startswith("| Phase | Status | Waves | Depends on | Title |\n")
    assert "### " not in md


def test_render_roadmap_markdown_bands_phases_by_release() -> None:
    """Phases group under ``### <version>`` bands, newest first, then Unreleased."""
    from eawf.surfaces.render.plan_view import render_roadmap_markdown

    s = _base_state()  # P05 has no release
    _add_phase(s, "P06", release="v0.5.0")
    _add_phase(s, "P07", release="v0.4.1")
    state = State.model_validate(s)

    md = render_roadmap_markdown(state)

    assert _band_header_order(md) == ["v0.5.0", "v0.4.1", "Unreleased"]
    # Each phase lands in its own band; verify P06 precedes P07 precedes P05.
    assert md.index("`P06`") < md.index("`P07`") < md.index("`P05`")


def test_render_roadmap_markdown_groups_shared_release() -> None:
    """Two phases on the same release share one band."""
    from eawf.surfaces.render.plan_view import render_roadmap_markdown

    s = _base_state()
    s["phases"]["P05"]["release"] = "v0.5.0"
    _add_phase(s, "P06", release="v0.5.0")
    state = State.model_validate(s)

    md = render_roadmap_markdown(state)

    # One v0.5.0 band, no Unreleased band (every phase is banded).
    assert _band_header_order(md) == ["v0.5.0"]
    assert "`P05`" in md
    assert "`P06`" in md


def test_render_roadmap_markdown_all_unreleased_when_filtered() -> None:
    """A filtered single phase without release renders the unbanded table."""
    from eawf.surfaces.render.plan_view import render_roadmap_markdown

    s = _base_state()
    _add_phase(s, "P06", release="v0.5.0")
    state = State.model_validate(s)

    md = render_roadmap_markdown(state, phase_id_filter="P05")

    # Only P05 (no release) survives the filter -> unbanded.
    assert "### " not in md
    assert md.startswith("| Phase | Status | Waves | Depends on | Title |\n")


def test_render_roadmap_markdown_empty_state_literal_unchanged() -> None:
    """The empty-state literal is unaffected by banding."""
    from eawf.surfaces.render.plan_view import render_roadmap_markdown

    state = State.model_validate(_base_state())
    md = render_roadmap_markdown(state, phase_id_filter="P99")
    assert md == "_(no phases in state)_"


def test_render_roadmap_markdown_prerelease_sorts_below_release() -> None:
    """A prerelease band sorts below its final release under newest-first order."""
    from eawf.surfaces.render.plan_view import render_roadmap_markdown

    s = _base_state()
    s["phases"]["P05"]["release"] = "v0.5.0rc1"
    _add_phase(s, "P06", release="v0.5.0")
    state = State.model_validate(s)

    md = render_roadmap_markdown(state)
    # Under newest-first ordering a final release outranks its prerelease of
    # the same semver core, so v0.5.0 precedes v0.5.0rc1 deterministically.
    headers = _band_header_order(md)
    assert headers == ["v0.5.0", "v0.5.0rc1"]


def test_release_sort_key_orders_core_then_prerelease() -> None:
    """``_release_sort_key`` orders by semver core, final above its prereleases."""
    from eawf.surfaces.render.plan_view import _release_sort_key

    labels = ["v0.4.1", "v0.5.0", "v0.5.0rc1", "v0.5.0a1", "v1.0.0"]
    newest_first = sorted(labels, key=_release_sort_key, reverse=True)
    assert newest_first == ["v1.0.0", "v0.5.0", "v0.5.0rc1", "v0.5.0a1", "v0.4.1"]


def test_release_sort_key_nonconforming_sorts_last_newest_first() -> None:
    """A malformed label sorts last under newest-first so it stays visible."""
    from eawf.surfaces.render.plan_view import _release_sort_key

    labels = ["v0.5.0", "garbage"]
    newest_first = sorted(labels, key=_release_sort_key, reverse=True)
    assert newest_first == ["v0.5.0", "garbage"]


def test_render_show_md_bands_dict_rows_by_release() -> None:
    """The CLI dict-row thin wrapper bands rows by their ``release`` key."""
    from eawf.surfaces.cli.commands.roadmap import _render_show_md

    rows = [
        {
            "id": "P06",
            "status": "planned",
            "title": "Six",
            "depends_on": [],
            "wave_count": 0,
            "release": "v0.5.0",
        },
        {
            "id": "P05",
            "status": "active",
            "title": "Five",
            "depends_on": [],
            "wave_count": 0,
            "release": None,
        },
    ]
    md = _render_show_md(rows)
    assert _band_header_order(md) == ["v0.5.0", "Unreleased"]
    assert md.index("`P06`") < md.index("`P05`")


def test_render_show_md_unbanded_dict_rows_when_no_release() -> None:
    """Dict rows without any release render the legacy unbanded table."""
    from eawf.surfaces.cli.commands.roadmap import _render_show_md

    rows = [
        {
            "id": "P05",
            "status": "active",
            "title": "Five",
            "depends_on": [],
            "wave_count": 0,
            "release": None,
        },
    ]
    md = _render_show_md(rows)
    assert "### " not in md
    assert md.startswith("| Phase | Status | Waves | Depends on | Title |")


def test_parse_check_result_skips_malformed_row() -> None:
    s = _base_state()
    _add_wave(s, _wave("P05-I01-W01"))
    s["iters"]["P05-I01"]["audit_id"] = "AU-1"
    s["audits"] = {
        "AU-1": {
            "id": "AU-1",
            "scope_id": "P05-I01",
            "kind": "evaluation",
            "status": "complete",
            "report_artifact_id": None,
            "check_results": [
                {"name": "ruff_clean", "passed": True, "details": None},
                {"missing_keys": True},  # malformed: skipped, no raise
                "wholly_unstructured",  # malformed: skipped
            ],
            "integrity_results": [],
            "created_at": "2026-05-08T00:00:00Z",
            "verdict": "pass",
        }
    }
    state = State.model_validate(s)
    view = build_view(state, "P05-I01")
    assert len(view.checks) == 1
    assert view.checks[0].name == "ruff_clean"


def test_blocked_waves_lists_pending_waves_with_open_deps() -> None:
    s = _base_state()
    _add_wave(s, _wave("P05-I01-W00", status="closed", closed_at="2026-05-08T01:00:00Z"))
    _add_wave(s, _wave("P05-I01-W01", deps=["P05-I01-W00"]))  # closed dep — not blocked
    _add_wave(s, _wave("P05-I01-W02", deps=["P05-I01-W01"]))  # pending dep — blocked
    state = State.model_validate(s)
    view = build_view(state, "P05-I01")
    assert view.summary.blocked_waves == ["P05-I01-W02"]


@settings(max_examples=30, deadline=None)
@given(
    wave_count=st.integers(min_value=2, max_value=6),
)
def test_build_view_topo_idempotent(wave_count: int) -> None:
    """Property: same input produces identical topo_order regardless of dict ordering."""
    s = _base_state()
    base_order = [f"P05-I01-W{n:02d}" for n in range(wave_count)]
    # Linear chain.
    _add_wave(s, _wave(base_order[0], status="closed", closed_at="2026-05-08T01:00:00Z"))
    for i in range(1, wave_count):
        _add_wave(s, _wave(base_order[i], deps=[base_order[i - 1]]))

    state_a = State.model_validate(s)
    view_a = build_view(state_a, "P05-I01")

    # Permute dict insertion order: rebuild waves dict in reverse order.
    s2 = deepcopy(s)
    s2["waves"] = {wid: s2["waves"][wid] for wid in reversed(list(s2["waves"].keys()))}
    state_b = State.model_validate(s2)
    view_b = build_view(state_b, "P05-I01")

    assert view_a.dag.topo_order == view_b.dag.topo_order
    assert view_a.dag.cycle == view_b.dag.cycle
