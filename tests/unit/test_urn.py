from __future__ import annotations

import pytest

from eawf.kernel.state import urn


def test_parse_repo_urn() -> None:
    parsed = urn.parse("urn:eawf:v1:repo:QR")
    assert parsed.kind == "repo"
    assert parsed.owner == "QR"
    assert parsed.id is None


def test_parse_state_urn_with_path() -> None:
    parsed = urn.parse("urn:eawf:v1:state:QR/P13-I04-W01")
    assert parsed.kind == "state"
    assert parsed.owner == "QR"
    assert parsed.id == "P13-I04-W01"


def test_parse_query_and_fragment_ignored_for_identity() -> None:
    a = urn.parse("urn:eawf:v1:state:QR/P13-I04?=view=dashboard#summary")
    b = urn.parse("urn:eawf:v1:state:QR/P13-I04")
    assert a.identity() == b.identity()
    assert a.query == {"view": "dashboard"}
    assert a.fragment == "summary"


def test_build_state_urn() -> None:
    built = urn.build("state", owner="QR", id="P13-I04")
    assert built == "urn:eawf:v1:state:QR/P13-I04"


def test_build_repo_urn_no_id() -> None:
    built = urn.build("repo", owner="QR")
    assert built == "urn:eawf:v1:repo:QR"


def test_round_trip_artifact_urn() -> None:
    raw = "urn:eawf:v1:artifact:QR/ART-20260506-p13-i04-audit"
    assert urn.build_from(urn.parse(raw)) == raw


def test_build_rejects_unknown_kind() -> None:
    with pytest.raises(ValueError):
        urn.build("nope", owner="QR")


def test_build_rejects_empty_owner() -> None:
    with pytest.raises(ValueError):
        urn.build("repo", owner="")


@pytest.mark.parametrize(
    "raw",
    [
        "not-a-urn",
        "urn:eawf:v2:repo:QR",
        "urn:eawf:v1::QR",
        "urn:eawf:v1:state:",
        "urn:eawf:v1:UNKNOWN:QR",
    ],
)
def test_parse_rejects_malformed(raw: str) -> None:
    with pytest.raises(ValueError):
        urn.parse(raw)


@pytest.mark.parametrize(
    "raw",
    [
        "urn:eawf:v1:incident:QR/I1",
        "urn:eawf:v1:goal:QR/G1",
        "urn:eawf:v1:outcome:QR/O1",
        "urn:eawf:v1:backlog:QR/B1",
        "urn:eawf:v1:flow:QR/F1",
        "urn:eawf:v1:subproject:QR/SP1",
        "urn:eawf:v1:agent_report:QR/executor/W01",
    ],
)
def test_parse_rejects_non_urn_kinds(raw: str) -> None:
    """Kinds that stay rejected per C01 §5.2.2 §258.

    The 10 supplementary entities (Incident, Goal, Outcome, BacklogItem,
    EstimateSummary, ActualSummary, WorktreeRecord, SandboxPolicy, Flow,
    Subproject) are addressable via composite key, not via dedicated URN.
    The underscored ``agent_report`` token is also rejected — canonical
    form is single-word ``report`` per operator D1 2026-05-16.
    """
    with pytest.raises(ValueError):
        urn.parse(raw)


@pytest.mark.parametrize(
    "kind",
    sorted(urn.URN_KINDS),
)
def test_urn_kinds_catalog_count(kind: str) -> None:
    """Every catalogued URN kind round-trips through parse(build())."""
    built = urn.build(kind, owner="ABC", id="x1")
    parsed = urn.parse(built)
    assert parsed.kind == kind
    assert parsed.owner == "ABC"
    assert parsed.id == "x1"


def test_urn_kinds_total_is_26() -> None:
    """C01-IMPL W01 expansion target: URN_KINDS has exactly 26 kinds."""
    assert len(urn.URN_KINDS) == 26


def test_urn_kinds_catalog_membership() -> None:
    """Catalogued kinds per c01-foundations §5.2.2 (alphabetical)."""
    expected = frozenset(
        {
            "artifact",
            "audit",
            "blob",
            "branch",
            "commit",
            "decision",
            "event",
            "hypothesis",
            "iter",
            "mcp",
            "memory",
            "phase",
            "plugin",
            "pr",
            "principal",
            "profile",
            "repo",
            "report",
            "runtime",
            "secret",
            "session",
            "spec",
            "state",
            "store",
            "wave",
            "workspace",
        }
    )
    assert expected == urn.URN_KINDS


@pytest.mark.parametrize(
    "kind",
    ["spec", "report", "event", "memory", "session", "plugin", "mcp"],
)
def test_new_slash_kinds_accept_slash_id(kind: str) -> None:
    """C01-IMPL W01 extends _SLASH_KINDS to cover new slash-friendly kinds."""
    built = urn.build(kind, owner="ABC", id="part/subpart")
    parsed = urn.parse(built)
    assert parsed.kind == kind
    assert parsed.id == "part/subpart"


def test_spec_urn_tier_aware_path() -> None:
    """Spec URN id may carry phase[/iter[/wave]] per c01-foundations §5.2.2."""
    built = urn.build("spec", owner="ABC", id="p20/i03/w01")
    assert built == "urn:eawf:v1:spec:ABC/p20/i03/w01"


def test_report_urn_role_base_attempt() -> None:
    """Report URN id format: <role>/<base_id>-<attempt> per c01-foundations §5.2.2."""
    built = urn.build("report", owner="ABC", id="executor/p20-i03-w01-01")
    assert built == "urn:eawf:v1:report:ABC/executor/p20-i03-w01-01"


def test_principal_urn_reserved_parses() -> None:
    """Principal URN kind reserved per c01-foundations D4; parse succeeds."""
    parsed = urn.parse("urn:eawf:v1:principal:user/u-abc123")
    assert parsed.kind == "principal"
    assert parsed.owner == "user"
    assert parsed.id == "u-abc123"


def test_build_from_round_trips_percent_encoded_query() -> None:
    parsed = urn.parse("urn:eawf:v1:state:QR?=k=v%20with%20space")
    assert parsed.query == {"k": "v with space"}
    rebuilt = urn.build_from(parsed)
    re_parsed = urn.parse(rebuilt)
    assert re_parsed.query == {"k": "v with space"}


def test_build_state_id_with_slash_rejected() -> None:
    with pytest.raises(ValueError):
        urn.build("state", owner="QR", id="a/b")


def test_build_repo_id_with_slash_allowed() -> None:
    built = urn.build("repo", owner="QR", id="a/b")
    assert built == "urn:eawf:v1:repo:QR/a/b"


def test_build_artifact_id_with_slash_allowed() -> None:
    built = urn.build("artifact", owner="QR", id="dir/sub")
    assert built == "urn:eawf:v1:artifact:QR/dir/sub"


def test_build_store_id_with_slash_allowed() -> None:
    built = urn.build("store", owner="QR", id="executor_report/AR-001")
    assert built == "urn:eawf:v1:store:QR/executor_report/AR-001"
