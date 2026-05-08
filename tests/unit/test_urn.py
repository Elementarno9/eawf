from __future__ import annotations

import pytest

from eawf.state import urn


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
        "urn:eawf:v1:audit:QR/A1",
        "urn:eawf:v1:hypothesis:QR/H03-12",
        "urn:eawf:v1:session:QR/S1",
        "urn:eawf:v1:incident:QR/I1",
        "urn:eawf:v1:decision:QR/D1",
        "urn:eawf:v1:goal:QR/G1",
        "urn:eawf:v1:outcome:QR/O1",
    ],
)
def test_parse_rejects_dropped_kinds(raw: str) -> None:
    with pytest.raises(ValueError):
        urn.parse(raw)


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
