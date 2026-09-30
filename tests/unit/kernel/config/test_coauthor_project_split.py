"""CON-123: the project co-author identity is two text leaves, and old files migrate to them.

``vcs.coauthor.project`` holds a ``{name, email}`` record, but it was catalogued as one
string, so the console's text editor wrote ``Name <email>`` where the record belongs and
``VcsConfig`` then refused it. The catalog now names the two leaves the record is made
of, and the config migration rewrites a one-string identity into them.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from eawf.kernel.config import layered
from eawf.kernel.config.layered import merge_config
from eawf.kernel.config.migration import migrate_config_payload
from eawf.kernel.config.registry.leaf_catalog import LEAF_KEY_REGISTRY
from eawf.runtime.vcs.coauthor import CoauthorConfig


def test_con123_the_project_identity_is_catalogued_as_two_text_leaves() -> None:
    assert "vcs.coauthor.project" not in LEAF_KEY_REGISTRY
    for key in ("vcs.coauthor.project.name", "vcs.coauthor.project.email"):
        entry = LEAF_KEY_REGISTRY[key]
        assert entry.type == "str"
        assert entry.default is None
        assert entry.consumer == "eawf.runtime.vcs.coauthor.resolve_coauthor_trailer"


def test_con123_the_migration_splits_a_one_string_identity() -> None:
    written = {
        "vcs": {"coauthor": {"mode": "project", "project": "Ada Lovelace <ada@example.com>"}}
    }

    upgraded, changed = migrate_config_payload({"schema_version": "1.0", **written})

    assert changed is True
    identity = upgraded["vcs"]["coauthor"]["project"]
    assert identity == {"name": "Ada Lovelace", "email": "ada@example.com"}
    assert CoauthorConfig.model_validate(upgraded["vcs"]["coauthor"]).project is not None


@pytest.mark.parametrize("text", ["Ada Lovelace", "", "<ada@example.com>"])
def test_con123_the_migration_removes_a_string_that_names_no_identity(text: str) -> None:
    upgraded, changed = migrate_config_payload(
        {"schema_version": "1.0", "vcs": {"coauthor": {"project": text, "mode": "runtime"}}}
    )

    assert changed is True
    assert upgraded["vcs"]["coauthor"] == {"mode": "runtime"}


def test_con123_the_migration_leaves_a_record_and_an_absent_identity_alone() -> None:
    record = {"name": "Ada Lovelace", "email": "ada@example.com"}
    for body in ({"vcs": {"coauthor": {"project": record}}}, {"vcs": {"coauthor": {}}}, {}):
        upgraded, changed = migrate_config_payload({"schema_version": "1.0", **body})
        assert changed is False
        assert upgraded == {"schema_version": "1.0", **body}


def test_con123_the_two_leaves_merge_into_the_record_coauthor_validates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(layered, "global_config_path", lambda: tmp_path / "global.yaml")
    repo = tmp_path / "repo"
    (repo / ".ea").mkdir(parents=True)
    (repo / ".ea" / "config.yaml").write_text(
        "vcs:\n  coauthor:\n    mode: project\n    project:\n"
        "      name: Ada Lovelace\n      email: ada@example.com\n",
        encoding="utf-8",
    )

    merged, sources = merge_config(workspace=repo, repo=repo, env={}, branch="main")

    config = CoauthorConfig.model_validate(merged["vcs"]["coauthor"])
    assert config.project is not None
    assert config.project.trailer() == "Co-Authored-By: Ada Lovelace <ada@example.com>"
    assert sources["vcs.coauthor.project.name"] == "repo"
