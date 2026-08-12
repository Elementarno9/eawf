from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from eawf.kernel.state import models


@pytest.mark.parametrize(
    "bad_urn",
    [
        "not-a-urn",
        "urn:eawf:v1:",
        "urn:eawf:v1:state:",
        "urn:eawf:v2:state:QR",
        "urn:eawf:v1:nonexistent:foo",
        "urn:eawf:v1:STATE:QR",
    ],
)
def test_project_repo_urn_pattern_rejects_malformed(bad_urn: str) -> None:
    with pytest.raises(ValidationError):
        models.Project(
            code="QR",
            slug="quant-research",
            title="Quant Research",
            description="",
            domains=["quant"],
            default_branch="main",
            status="active",
            repo_urn=bad_urn,
        )


def _hypothesis_payload(hid: str) -> dict[str, object]:
    return {
        "id": hid,
        "scope_id": "QR",
        "title": "h",
        "metric": "m",
        "confirm": "x",
        "reject": "y",
        "status": "pending",
        "verdict": None,
        "audit_id": None,
        "source_artifact_id": None,
    }


@pytest.mark.parametrize(
    "bad_id",
    [
        "something_H03-12",
        "H03-12_garbage",
        "H3-12",
        "H03",
    ],
)
def test_hypothesis_id_pattern_rejects_unanchored(bad_id: str) -> None:
    with pytest.raises(ValidationError):
        models.Hypothesis.model_validate(_hypothesis_payload(bad_id))


@pytest.mark.parametrize("good_id", ["H03-12", "QR-H03-12"])
def test_hypothesis_id_pattern_accepts_canonical(good_id: str) -> None:
    h = models.Hypothesis.model_validate(_hypothesis_payload(good_id))
    assert h.id == good_id


def test_project_extra_forbid() -> None:
    with pytest.raises(ValidationError):
        models.Project(
            code="QR",
            slug="quant-research",
            title="Quant Research",
            description="",
            domains=["quant"],
            default_branch="main",
            status="active",
            repo_urn="urn:eawf:v1:repo:QR",
            extra_field="oops",
        )


def test_project_minimal() -> None:
    project = models.Project(
        code="QR",
        slug="quant-research",
        title="Quant Research",
        description="Research repo",
        domains=["quant", "ml"],
        default_branch="main",
        status="active",
        repo_urn="urn:eawf:v1:repo:QR",
    )
    assert project.code == "QR"
    assert project.status == "active"


def test_project_rejects_bad_code() -> None:
    with pytest.raises(ValidationError):
        models.Project(
            code="qr",
            slug="quant-research",
            title="Quant Research",
            description="",
            domains=["quant"],
            default_branch="main",
            status="active",
            repo_urn="urn:eawf:v1:repo:QR",
        )


def test_project_rejects_bad_urn() -> None:
    with pytest.raises(ValidationError):
        models.Project(
            code="QR",
            slug="quant-research",
            title="Quant Research",
            description="",
            domains=["quant"],
            default_branch="main",
            status="active",
            repo_urn="not-a-urn",
        )


def test_phase_round_trip() -> None:
    now = datetime.now(UTC)
    phase = models.Phase(
        id="P01",
        scope_id="QR",
        title="Bootstrap",
        status="planned",
        iter_ids=[],
        outcome_ids=[],
        opened_at=now,
        closed_at=None,
        audit_id=None,
    )
    dumped = phase.model_dump(mode="json")
    restored = models.Phase.model_validate(dumped)
    assert restored == phase


def test_phase_rejects_bad_id() -> None:
    now = datetime.now(UTC)
    with pytest.raises(ValidationError):
        models.Phase(
            id="P1",
            scope_id="QR",
            title="Bootstrap",
            status="planned",
            iter_ids=[],
            outcome_ids=[],
            opened_at=now,
            closed_at=None,
            audit_id=None,
        )


def test_state_root_with_no_optional_keys() -> None:
    state = models.State.model_validate(
        {
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
                "track_id": None,
                "phase_id": None,
                "iter_id": None,
                "active_wave_ids": [],
                "active_session_ids": [],
            },
            "workspace": None,
            "phases": {},
            "iters": {},
            "waves": {},
            "artifacts": {},
            "agent_sessions": {},
            "plugins": {},
            "indexes": {},
        }
    )
    assert state.scope_kind == "repo"
    assert state.tracks is None or state.tracks == {}


def test_wave_deps_validation() -> None:
    now = datetime.now(UTC)
    wave = models.Wave(
        id="P01-I02-W03",
        iter_id="P01-I02",
        title="Build feature",
        status="pending",
        deps=["P01-I02-W01", "P01-I02-W02"],
        file_scopes=["src/foo/"],
        claim_session_id=None,
        worktree_id=None,
        outcome=None,
        opened_at=now,
        closed_at=None,
    )
    assert wave.deps == ["P01-I02-W01", "P01-I02-W02"]


def test_wave_rejects_bad_dep() -> None:
    now = datetime.now(UTC)
    with pytest.raises(ValidationError):
        models.Wave(
            id="P01-I02-W03",
            iter_id="P01-I02",
            title="Build feature",
            status="pending",
            deps=["not-a-wave-id"],
            file_scopes=[],
            claim_session_id=None,
            worktree_id=None,
            outcome=None,
            opened_at=now,
            closed_at=None,
        )


def test_outcome_direction_enum() -> None:
    now = datetime.now(UTC)
    outcome = models.Outcome(
        id="OUT-001",
        scope_id="QR",
        metric="latency_ms",
        threshold=100.0,
        direction="max",
        value=None,
        status="pending",
        audit_id=None,
        updated_at=now,
    )
    assert outcome.direction == "max"


def test_outcome_rejects_bad_direction() -> None:
    now = datetime.now(UTC)
    with pytest.raises(ValidationError):
        models.Outcome(
            id="OUT-001",
            scope_id="QR",
            metric="latency_ms",
            threshold=100.0,
            direction="sideways",
            value=None,
            status="pending",
            audit_id=None,
            updated_at=now,
        )


def test_audit_verdict_optional() -> None:
    now = datetime.now(UTC)
    audit = models.Audit(
        id="AUD-001",
        scope_id="QR",
        kind="evaluation",
        status="pending",
        report_artifact_id=None,
        check_results=[],
        integrity_results=[],
        created_at=now,
        verdict=None,
    )
    assert audit.verdict is None


def test_hypothesis_thresholds_required_when_populated() -> None:
    hypothesis = models.Hypothesis(
        id="H03-12",
        scope_id="QR",
        title="Latency below 100ms improves UX.",
        metric="p99_latency_ms",
        confirm="< 100",
        reject=">= 200",
        status="pending",
        verdict=None,
        audit_id=None,
        source_artifact_id=None,
    )
    assert hypothesis.confirm == "< 100"
    assert hypothesis.reject == ">= 200"


def test_workspace_index_repos_typed() -> None:
    ws = models.WorkspaceIndex(
        code="MAIN",
        title="Main workspace",
        repos={
            "QR": models.WorkspaceRepoRef(
                code="QR",
                path="/abs/path/qr",
                state_urn="urn:eawf:v1:state:QR",
                project_code="QR",
                title="Quant Research",
                status="active",
            )
        },
        current_repo_code="QR",
    )
    assert ws.repos["QR"].path == "/abs/path/qr"
    assert ws.repos["QR"].status == "active"


def test_memory_summary_confidence_enum() -> None:
    summary = models.MemorySummary(
        id="MEM-001",
        scope_id="QR",
        summary="Short summary.",
        confidence="high",
        status="active",
        store_record_id="REC-001",
        review_due=None,
    )
    assert summary.confidence == "high"


def test_memory_summary_rejects_bad_confidence() -> None:
    with pytest.raises(ValidationError):
        models.MemorySummary(
            id="MEM-001",
            scope_id="QR",
            summary="Short summary.",
            confidence="certain",
            status="active",
            store_record_id="REC-001",
            review_due=None,
        )


def test_worktree_record_requires_base_branch_and_branch() -> None:
    now = datetime.now(UTC)
    record = models.WorktreeRecord(
        id="WT-001",
        wave_id="P01-I02-W01",
        branch="feature/p01-i02-w01",
        path="/abs/path/wt",
        base_branch="feature/eawf-v0.1",
        status="active",
        owner_session_id=None,
        created_at=now,
        merged_commit=None,
    )
    assert record.base_branch == "feature/eawf-v0.1"
    assert record.branch == "feature/p01-i02-w01"


def test_worktree_record_missing_base_branch_raises() -> None:
    now = datetime.now(UTC)
    with pytest.raises(ValidationError):
        models.WorktreeRecord(  # type: ignore[call-arg]
            id="WT-001",
            wave_id="P01-I02-W01",
            branch="feature/p01-i02-w01",
            path="/abs/path/wt",
            status="active",
            owner_session_id=None,
            created_at=now,
            merged_commit=None,
        )


def test_state_schema_version_literal() -> None:
    with pytest.raises(ValidationError):
        models.State.model_validate(
            {
                "schema_version": "2.0",
                "scope_kind": "repo",
                "urn": "urn:eawf:v1:state:QR",
                "updated_at": "2026-05-08T00:00:00Z",
                "project": None,
                "current": {
                    "project_code": "QR",
                    "track_id": None,
                    "phase_id": None,
                    "iter_id": None,
                    "active_wave_ids": [],
                    "active_session_ids": [],
                },
                "workspace": None,
                "phases": {},
                "iters": {},
                "waves": {},
                "artifacts": {},
                "agent_sessions": {},
                "plugins": {},
                "indexes": {},
            }
        )


def test_iter_round_trip() -> None:
    now = datetime.now(UTC)
    it = models.Iter(
        id="P01-I02",
        phase_id="P01",
        title="First iter",
        status="planned",
        wave_ids=[],
        estimate_id=None,
        audit_id=None,
        opened_at=now,
        closed_at=None,
    )
    dumped = it.model_dump(mode="json")
    restored = models.Iter.model_validate(dumped)
    assert restored == it


def _iter_kwargs() -> dict[str, object]:
    """Return the minimal keyword set for a valid :class:`Iter`."""
    return {
        "id": "P01-I02",
        "phase_id": "P01",
        "title": "First iter",
        "status": "planned",
        "opened_at": datetime.now(UTC),
    }


def test_iter_candidate_tag_defaults_none() -> None:
    """``candidate_tag`` is additive: an iter without it loads as ``None``."""
    it = models.Iter(**_iter_kwargs())  # type: ignore[arg-type]
    assert it.candidate_tag is None
    # An on-disk row predating the field re-validates under ``extra="forbid"``.
    restored = models.Iter.model_validate(it.model_dump(mode="json"))
    assert restored.candidate_tag is None


def test_iter_candidate_tag_accepts_valid_release_label() -> None:
    """A ``vMAJOR.MINOR.PATCH`` tag is accepted and round-trips."""
    it = models.Iter(candidate_tag="v0.5.0", **_iter_kwargs())  # type: ignore[arg-type]
    assert it.candidate_tag == "v0.5.0"
    restored = models.Iter.model_validate(it.model_dump(mode="json"))
    assert restored.candidate_tag == "v0.5.0"


@pytest.mark.parametrize(
    "bad_tag",
    [
        "0.5",  # missing v prefix + patch segment
        "v0.5",  # missing patch segment
        "foo",  # not a version at all
        "v0.5.0-junk",  # trailing junk past the semver core
        "0.5.0",  # missing v prefix
    ],
)
def test_iter_candidate_tag_rejects_malformed(bad_tag: str) -> None:
    """A tag that violates the ``ReleaseStr`` pattern fails validation."""
    with pytest.raises(ValidationError):
        models.Iter(candidate_tag=bad_tag, **_iter_kwargs())  # type: ignore[arg-type]


def test_artifact_basic() -> None:
    now = datetime.now(UTC)
    art = models.Artifact(
        id="ART-001",
        kind="report",
        uri="repo:reports/report.md",
        urn="urn:eawf:v1:artifact:QR/ART-001",
        sha256="0" * 64,
        size_bytes=1024,
        created_at=now,
        metadata={},
    )
    assert art.id == "ART-001"
    assert len(art.sha256) == 64


def test_actual_summary_status_enum_rejects_unknown() -> None:
    now = datetime.now(UTC)
    base: dict[str, object] = {
        "id": "ACT-001",
        "scope_id": "QR",
        "elapsed_eu": 1.0,
        "current_store_record_id": "REC-001",
        "updated_at": now,
    }
    with pytest.raises(ValidationError):
        models.ActualSummary.model_validate({**base, "status": "weird"})
    summary = models.ActualSummary.model_validate({**base, "status": "active"})
    assert summary.status == "active"


@pytest.mark.parametrize("bad_id", ["sp-x", "1QR", "qr-x"])
def test_track_id_pattern_rejects_lowercase(bad_id: str) -> None:
    with pytest.raises(ValidationError):
        models.Track(
            id=bad_id,
            code="QR",
            slug="quant-research",
            title="Quant Research",
            kind="strategy",
            domains=["quant"],
            status="active",
        )


def test_track_id_accepts_project_code_shape() -> None:
    sp = models.Track(
        id="QR-X",
        code="QR",
        slug="quant-research",
        title="Quant Research",
        kind="strategy",
        domains=["quant"],
        status="active",
    )
    assert sp.id == "QR-X"


def test_track_kind_rejects_unknown_value() -> None:
    """An unknown ``Track.kind`` fails as a ValidationError at the boundary."""
    with pytest.raises(ValidationError):
        models.Track(
            id="QR-X",
            code="QR",
            slug="quant-research",
            title="Quant Research",
            kind="research-line",
            domains=["quant"],
            status="active",
        )


def test_project_track_ids_defaults_empty() -> None:
    """The Project -> Track containment edge defaults to an empty list."""
    project = models.Project(
        code="QR",
        slug="quant-research",
        title="Quant Research",
        description=None,
        domains=["quant"],
        default_branch="main",
        status="active",
        repo_urn="urn:eawf:v1:repo:QR",
    )
    assert project.track_ids == []


def test_project_track_ids_links_track() -> None:
    """A Project may carry the ids of the Tracks it owns."""
    project = models.Project(
        code="QR",
        slug="quant-research",
        title="Quant Research",
        domains=["quant"],
        default_branch="main",
        status="active",
        repo_urn="urn:eawf:v1:repo:QR",
        track_ids=["QR-X", "QR-Y"],
    )
    assert project.track_ids == ["QR-X", "QR-Y"]


def test_track_status_accepts_planned_active_lifecycle() -> None:
    """Track.status mirrors the Phase lifecycle via the TrackStatus enum."""
    planned = models.Track(
        id="QR-P",
        code="QR",
        slug="quant-research",
        title="Quant Research",
        kind="strategy",
        domains=["quant"],
        status="planned",
    )
    active = models.Track(
        id="QR-A",
        code="QR",
        slug="quant-research",
        title="Quant Research",
        kind="strategy",
        domains=["quant"],
        status="active",
    )
    assert planned.status.value == "planned"
    assert active.status.value == "active"


def test_track_status_rejects_unknown_value() -> None:
    """An unknown ``Track.status`` fails as a ValidationError at the boundary."""
    with pytest.raises(ValidationError):
        models.Track(
            id="QR-X",
            code="QR",
            slug="quant-research",
            title="Quant Research",
            kind="strategy",
            domains=["quant"],
            status="halted",
        )


def test_workspace_index_code_pattern() -> None:
    with pytest.raises(ValidationError):
        models.WorkspaceIndex(code="lower-case", title="bad", repos={})
    ws = models.WorkspaceIndex(code="MAIN", title="ok", repos={})
    assert ws.code == "MAIN"


def test_project_description_optional() -> None:
    project = models.Project(
        code="QR",
        slug="quant-research",
        title="Quant Research",
        description=None,
        domains=["quant"],
        default_branch="main",
        status="active",
        repo_urn="urn:eawf:v1:repo:QR",
    )
    assert project.description is None


def test_phase_track_id_optional() -> None:
    now = datetime.now(UTC)
    phase = models.Phase(
        id="P01",
        scope_id="QR",
        track_id=None,
        title="Bootstrap",
        status="planned",
        iter_ids=[],
        outcome_ids=[],
        opened_at=now,
        closed_at=None,
        audit_id=None,
    )
    assert phase.track_id is None


def test_decisions_default_factory_dict() -> None:
    state = models.State.model_validate(
        {
            "schema_version": "1.0",
            "scope_kind": "repo",
            "urn": "urn:eawf:v1:state:QR",
            "updated_at": "2026-05-08T00:00:00Z",
            "project": None,
            "current": {
                "project_code": "QR",
                "track_id": None,
                "phase_id": None,
                "iter_id": None,
                "active_wave_ids": [],
                "active_session_ids": [],
            },
            "workspace": None,
            "phases": {},
            "iters": {},
            "waves": {},
            "artifacts": {},
            "agent_sessions": {},
            "plugins": {},
            "indexes": {},
        }
    )
    assert state.decisions == {}


def test_plugin_install_owner_required() -> None:
    now = datetime.now(UTC)
    with pytest.raises(ValidationError):
        models.PluginInstall.model_validate(
            {
                "id": "PI-001",
                "runtime": "claude",
                "scope_id": "repo",
                "target_path": "/abs/path",
                "status": "installed",
                "managed_files": [],
                "installed_at": now,
                "updated_at": now,
            }
        )


def test_plugin_install_with_owner() -> None:
    now = datetime.now(UTC)
    plugin = models.PluginInstall(
        id="PI-001",
        owner="eawf",
        runtime="claude",
        scope_id="repo",
        target_path="/abs/path",
        status="installed",
        managed_files=[],
        installed_at=now,
        updated_at=now,
    )
    assert plugin.owner == "eawf"


def test_mcp_env_refs_pattern_rejects_plain_strings() -> None:
    with pytest.raises(ValidationError):
        models.McpServer(
            id="MCP-001",
            owner="eawf",
            command="/usr/bin/mcp",
            args=[],
            env_refs=["FOO"],
            risk="read",
            write_capable=False,
            status="installed",
            installed_targets=[],
        )
    server = models.McpServer(
        id="MCP-001",
        owner="eawf",
        command="/usr/bin/mcp",
        args=[],
        env_refs=["${ENV:OPENAI_KEY}"],
        risk="read",
        write_capable=False,
        status="installed",
        installed_targets=[],
    )
    assert server.env_refs == ["${ENV:OPENAI_KEY}"]


@pytest.mark.parametrize(
    "bad_id",
    ["", "   "],
)
def test_id_str_rejects_empty_and_whitespace_goal(bad_id: str) -> None:
    now = datetime.now(UTC)
    with pytest.raises(ValidationError):
        models.Goal(
            id=bad_id,
            scope_id="QR",
            title="t",
            summary="s",
            status="open",
            outcome_ids=[],
            created_at=now,
            closed_at=None,
        )


@pytest.mark.parametrize(
    "bad_id",
    ["", "   "],
)
def test_id_str_rejects_empty_and_whitespace_outcome(bad_id: str) -> None:
    now = datetime.now(UTC)
    with pytest.raises(ValidationError):
        models.Outcome(
            id=bad_id,
            scope_id="QR",
            metric="m",
            threshold=1.0,
            direction="max",
            value=None,
            status="pending",
            audit_id=None,
            updated_at=now,
        )


@pytest.mark.parametrize(
    "bad_id",
    ["", "   "],
)
def test_id_str_rejects_empty_and_whitespace_backlog_item(bad_id: str) -> None:
    now = datetime.now(UTC)
    with pytest.raises(ValidationError):
        models.BacklogItem(
            id=bad_id,
            scope_id="QR",
            title="t",
            priority="P0",
            status="open",
            created_at=now,
            closed_at=None,
            resolution=None,
        )


def test_id_str_accepts_minimum_token() -> None:
    now = datetime.now(UTC)
    goal = models.Goal(
        id="x",
        scope_id="QR",
        title="t",
        summary="s",
        status="open",
        outcome_ids=[],
        created_at=now,
        closed_at=None,
    )
    assert goal.id == "x"


def _minimal_state_payload(updated_at: str) -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "scope_kind": "repo",
        "urn": "urn:eawf:v1:state:QR",
        "updated_at": updated_at,
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
            "track_id": None,
            "phase_id": None,
            "iter_id": None,
            "active_wave_ids": [],
            "active_session_ids": [],
        },
        "workspace": None,
        "phases": {},
        "iters": {},
        "waves": {},
        "artifacts": {},
        "agent_sessions": {},
        "plugins": {},
        "indexes": {},
    }


def test_naive_datetime_rejected_in_state_root() -> None:
    payload = _minimal_state_payload("2026-05-08T00:00:00")  # no tz
    with pytest.raises(ValidationError) as excinfo:
        models.State.model_validate(payload)
    assert "timezone-aware" in str(excinfo.value)


def test_offset_datetime_normalised_to_utc() -> None:
    payload = _minimal_state_payload("2026-01-01T00:00:00+05:00")
    state = models.State.model_validate(payload)
    assert state.updated_at.tzinfo is UTC
    expected = datetime(2025, 12, 31, 19, 0, 0, tzinfo=UTC)
    assert state.updated_at == expected


# ---- Bounded title + description ------------------------------


def _wave_kwargs(**overrides: object) -> dict[str, object]:
    """Return Wave constructor kwargs with sane defaults for cap tests."""
    base: dict[str, object] = {
        "id": "P00-I01-W01",
        "iter_id": "P00-I01",
        "title": "Wave one",
        "status": "pending",
        "opened_at": datetime.now(UTC),
    }
    base.update(overrides)
    return base


def test_wave_title_at_cap_validates() -> None:
    wave = models.Wave(**_wave_kwargs(title="t" * 72))
    assert len(wave.title) == 72


def test_wave_title_over_cap_rejected() -> None:
    with pytest.raises(ValidationError):
        models.Wave(**_wave_kwargs(title="t" * 73))


def test_wave_title_empty_rejected() -> None:
    with pytest.raises(ValidationError):
        models.Wave(**_wave_kwargs(title=""))


def test_wave_description_at_cap_validates() -> None:
    wave = models.Wave(**_wave_kwargs(description="d" * 500))
    assert wave.description is not None
    assert len(wave.description) == 500


def test_wave_description_over_cap_rejected() -> None:
    with pytest.raises(ValidationError):
        models.Wave(**_wave_kwargs(description="d" * 501))


def test_wave_description_defaults_none() -> None:
    wave = models.Wave(**_wave_kwargs())
    assert wave.description is None


def test_decision_title_over_cap_rejected() -> None:
    with pytest.raises(ValidationError):
        models.Decision(
            id="D01",
            scope_id="QR",
            title="t" * 73,
            rationale="r",
            status="active",
            created_at=datetime.now(UTC),
        )


def test_decision_title_and_description_round_trip() -> None:
    decision = models.Decision(
        id="D01",
        scope_id="QR",
        title="Pick rebase merges",
        description="Squash collapses the per-wave commit history.",
        rationale="Keeps the [P-W] audit trail.",
        status="active",
        created_at=datetime.now(UTC),
    )
    reloaded = models.Decision.model_validate(decision.model_dump(mode="json"))
    assert reloaded.title == "Pick rebase merges"
    assert reloaded.description == "Squash collapses the per-wave commit history."
    assert reloaded == decision


def test_decision_summary_key_rejected_after_rename() -> None:
    """The pre-rename ``summary`` key is now an extra field (extra=forbid)."""
    with pytest.raises(ValidationError):
        models.Decision.model_validate(
            {
                "id": "D01",
                "scope_id": "QR",
                "summary": "old field name",
                "rationale": "r",
                "status": "active",
                "created_at": datetime.now(UTC).isoformat(),
            }
        )


def test_hypothesis_title_over_cap_rejected() -> None:
    with pytest.raises(ValidationError):
        models.Hypothesis(
            id="H03-12",
            scope_id="QR",
            title="t" * 73,
            metric="m",
            confirm="x",
            reject="y",
            status="pending",
        )


def test_hypothesis_title_round_trip() -> None:
    hypothesis = models.Hypothesis(
        id="H03-12",
        scope_id="QR",
        title="Render is idempotent",
        metric="drift",
        confirm="drift == 0",
        reject="drift > 0",
        status="pending",
    )
    reloaded = models.Hypothesis.model_validate(hypothesis.model_dump(mode="json"))
    assert reloaded.title == "Render is idempotent"
    assert reloaded == hypothesis


def test_hypothesis_text_key_rejected_after_rename() -> None:
    """The pre-rename ``text`` key is now an extra field (extra=forbid)."""
    with pytest.raises(ValidationError):
        models.Hypothesis.model_validate(
            {
                "id": "H03-12",
                "scope_id": "QR",
                "text": "old field name",
                "metric": "m",
                "confirm": "x",
                "reject": "y",
                "status": "pending",
            }
        )
