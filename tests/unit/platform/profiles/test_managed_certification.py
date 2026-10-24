"""SURF-012: an enriched profile records the digest of its own content.

A profile is enriched when it carries a role-tier block. Its
``certification.digest`` is the digest of the body without that block,
so any edit to the body changes the digest it must record.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from eawf.platform.profiles.certification import profile_digest
from eawf.platform.profiles.models import ProfileBody, ProfileCertification
from eawf.platform.render_block import DISPATCH_SYSTEM_PROMPT_TARGET

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
