"""SURF-012: an enriched profile is interactive-only until certified as managed.

A profile is enriched when it carries a role-tier block, which reaches a
dispatched agent's system prompt. Unattended resolution drops every
enriched profile whose declared digest, recomputed digest and committed
``profiles.certified`` ledger entry do not all agree.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from eawf.platform.profiles import discovery
from eawf.platform.profiles.certification import profile_digest, uncertified_enriched
from eawf.platform.profiles.models import ProfileBody, ProfileCertification
from eawf.platform.render_block import DISPATCH_SYSTEM_PROMPT_TARGET
from eawf.workflow.dispatch.renderer import resolve_role_blocks

_HOUSE_RULE = "Run the targeted tests before reporting."


def _body(name: str = "house", *, enriched: bool = True, **extra: object) -> dict[str, object]:
    block: dict[str, object] = (
        {
            "id": "house-rule",
            "target": DISPATCH_SYSTEM_PROMPT_TARGET,
            "agent_role": "executor",
            "body_template": _HOUSE_RULE,
        }
        if enriched
        else {"id": "house-note", "target": "AGENTS.md", "body_template": "A note."}
    )
    return {"name": name, "render_blocks": [block], **extra}


def _certified(payload: dict[str, object]) -> dict[str, object]:
    digest = profile_digest(ProfileBody.model_validate(payload))
    return {**payload, "certification": {"digest": digest}}


# ---- what makes a profile enriched -------------------------------------------------------


def test_surf_012_role_tier_block_makes_a_profile_enriched() -> None:
    assert ProfileBody.model_validate(_body()).is_enriched


def test_surf_012_managed_file_blocks_only_is_plain() -> None:
    assert not ProfileBody.model_validate(_body(enriched=False)).is_enriched


def test_surf_012_empty_profile_is_plain() -> None:
    assert not ProfileBody(name="empty").is_enriched


# ---- the certification field -------------------------------------------------------------


def test_surf_012_profile_written_before_certification_existed_loads() -> None:
    body = ProfileBody.model_validate({"name": "legacy", "description": "no certification key"})
    assert body.certification is None


@pytest.mark.parametrize(
    "digest",
    ["", "sha256:", "sha256:" + "a" * 63, "sha256:" + "A" * 64, "md5:" + "a" * 64],
)
def test_surf_012_certification_refuses_a_malformed_digest(digest: str) -> None:
    with pytest.raises(ValidationError):
        ProfileCertification(digest=digest)


def test_surf_012_certification_refuses_unknown_keys() -> None:
    with pytest.raises(ValidationError):
        ProfileCertification.model_validate({"digest": "sha256:" + "a" * 64, "by": "me"})


def test_surf_012_digest_leaves_out_the_certification_block() -> None:
    plain = ProfileBody.model_validate(_body())
    certified = ProfileBody.model_validate(_certified(_body()))
    assert profile_digest(plain) == profile_digest(certified)


def test_surf_012_digest_follows_the_content() -> None:
    first = ProfileBody.model_validate(_body())
    edited = ProfileBody.model_validate(_body(description="edited"))
    assert profile_digest(first) != profile_digest(edited)


# ---- uncertified_enriched -----------------------------------------------------------------


def test_surf_012_no_profiles_refuses_nothing() -> None:
    assert uncertified_enriched({}, {}) == ()


def test_surf_012_plain_profile_needs_no_certification() -> None:
    body = ProfileBody.model_validate(_body(enriched=False))
    assert uncertified_enriched({"house": body}, {}) == ()


def test_surf_012_certified_enriched_profile_is_admitted() -> None:
    body = ProfileBody.model_validate(_certified(_body()))
    assert uncertified_enriched({"house": body}, {"house": profile_digest(body)}) == ()


@pytest.mark.parametrize("case", ["undeclared", "unpinned", "pinned-elsewhere", "stale"])
def test_surf_012_enriched_profile_without_agreeing_digests_is_refused(case: str) -> None:
    payload = _body() if case == "undeclared" else _certified(_body())
    if case == "stale":
        payload = {**payload, "description": "edited after certification"}
    body = ProfileBody.model_validate(payload)
    ledger = {
        "undeclared": {"house": profile_digest(body)},
        "unpinned": {},
        "pinned-elsewhere": {"other": profile_digest(body)},
        "stale": {"house": profile_digest(body)},
    }[case]
    assert uncertified_enriched({"house": body}, ledger) == ("house",)


def test_surf_012_refusals_are_sorted() -> None:
    bodies = {name: ProfileBody.model_validate(_body(name)) for name in ("zeta", "alpha")}
    assert uncertified_enriched(bodies, {}) == ("alpha", "zeta")


# ---- resolve_role_blocks ----------------------------------------------------------------


@pytest.fixture()
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    discovery._clear_cache_for_tests()
    root = tmp_path / "repo"
    (root / ".ea" / "profiles").mkdir(parents=True)
    return root


def _install(repo: Path, profile: dict[str, object], *, certified: dict[str, str]) -> None:
    (repo / ".ea" / "profiles" / "house.yaml").write_text(yaml.safe_dump(profile), "utf-8")
    config = {"profiles": {"enabled": ["house"], "certified": certified}}
    (repo / ".ea" / "config.yaml").write_text(yaml.safe_dump(config), "utf-8")


def test_surf_012_interactive_resolution_keeps_an_uncertified_enriched_profile(
    repo: Path,
) -> None:
    _install(repo, _body(), certified={})
    assert resolve_role_blocks(repo).role_blocks == {"executor": _HOUSE_RULE}


def test_surf_012_unattended_resolution_refuses_an_uncertified_enriched_profile(
    repo: Path, caplog: pytest.LogCaptureFixture
) -> None:
    _install(repo, _body(), certified={})
    assert resolve_role_blocks(repo, unattended=True).role_blocks == {}
    assert "profile=house uncertified" in caplog.text


def test_surf_012_unattended_resolution_admits_a_certified_enriched_profile(repo: Path) -> None:
    profile = _certified(_body())
    digest = str(profile["certification"]["digest"])  # type: ignore[index]
    _install(repo, profile, certified={"house": digest})
    assert resolve_role_blocks(repo, unattended=True).role_blocks == {"executor": _HOUSE_RULE}
