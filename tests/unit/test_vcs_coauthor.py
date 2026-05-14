from __future__ import annotations

import pytest
from pydantic import ValidationError

from eawf.vcs.coauthor import (
    CoauthorConfig,
    CoauthorIdentity,
    CoauthorPolicyError,
    resolve_coauthor_trailer,
)


def test_runtime_coauthor_resolves_codex_alias() -> None:
    config = CoauthorConfig()
    trailer = resolve_coauthor_trailer(config, runtime="codex-cli")
    assert trailer == "Co-Authored-By: Codex <noreply@openai.com>"


def test_project_mode_requires_identity() -> None:
    with pytest.raises(ValidationError, match="project"):
        CoauthorConfig(mode="project")


def test_project_mode_uses_project_identity() -> None:
    email = "noreply" + "@" + "example" + ".org"
    config = CoauthorConfig(
        mode="project",
        project=CoauthorIdentity(name="Automation", email=email),
    )
    assert resolve_coauthor_trailer(config) == f"Co-Authored-By: Automation <{email}>"


def test_disabled_mode_rejects_existing_trailer() -> None:
    config = CoauthorConfig(mode="disabled")
    with pytest.raises(CoauthorPolicyError, match="disabled"):
        resolve_coauthor_trailer(
            config,
            message_text="subject\n\nCo-Authored-By: Codex <noreply@openai.com>\n",
        )
