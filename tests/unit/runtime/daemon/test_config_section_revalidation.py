"""The daemon's config writes hold every section a written key belongs to against its model.

A leaf is checked alone against the registry's type, range and choices, but a section can
carry a rule no leaf states: a co-author name is only valid beside its email. After the
proposed value is merged into its layer, the config is composed as the readers compose it
and each touched section is validated by its strict model; a write that leaves one
invalid is refused naming the field and the model's message, and no file changes. Two
leaves valid only together are written in one ``config.set_layer_values`` write.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

import pytest
import yaml

from eawf import __version__
from eawf.kernel.config import layered
from eawf.kernel.config.defaults import built_in_defaults
from eawf.kernel.config.layered import merge_config
from eawf.kernel.config.registry.leaf_catalog import pair_siblings
from eawf.kernel.config.sections import (
    SECTION_MODELS,
    ConfigSectionError,
    check_sections,
    section_of,
)
from eawf.runtime.daemon import PROTOCOL_VERSION
from eawf.runtime.daemon.bus import EventBus
from eawf.runtime.daemon.methods import MethodContext
from eawf.runtime.daemon.methods.config import (
    set_layer_value,
    set_layer_values,
    unset_layer_value,
)
from eawf.runtime.vcs.coauthor import VcsConfig, resolve_coauthor_trailer

pytestmark = pytest.mark.unit

NAME = "vcs.coauthor.project.name"
EMAIL = "vcs.coauthor.project.email"


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Return a repo root, with a global layer file of this test's own beside it."""
    global_file = tmp_path / "global.yaml"
    monkeypatch.setattr(layered, "global_config_path", lambda: global_file)
    root = tmp_path / "repo"
    (root / ".ea").mkdir(parents=True)
    (root / ".ea" / "state.json").write_text("{}", encoding="utf-8")
    return root


def _global(body: dict[str, Any]) -> None:
    """State ``body`` in this test's global layer file."""
    layered.global_config_path().write_text(yaml.safe_dump(body), encoding="utf-8")


def _ctx(repo: Path) -> MethodContext:
    return MethodContext(
        started_at="2026-09-30T00:00:00+00:00",
        pid=os.getpid(),
        protocol_version=PROTOCOL_VERSION,
        version=__version__,
        shutdown_event=asyncio.Event(),
        bus=EventBus(),
        state_path=repo / ".ea" / "state.json",
        idempotency_cache={},
    )


def _set(repo: Path, key: str, value: Any, layer: str = "repo") -> dict[str, Any]:
    params = {"layer": layer, "key_path": key.split("."), "value": value}
    return asyncio.run(set_layer_value(_ctx(repo), params))


def _set_many(repo: Path, writes: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
    params = {"layer": "repo", "writes": writes, **extra}
    return asyncio.run(set_layer_values(_ctx(repo), params))


def _unset(repo: Path, key: str) -> dict[str, Any]:
    params = {"layer": "repo", "key_path": key.split(".")}
    return asyncio.run(unset_layer_value(_ctx(repo), params))


def _file(repo: Path) -> Path:
    return repo / ".ea" / "config.yaml"


def _written(repo: Path) -> dict[str, Any]:
    path = _file(repo)
    return yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}


def _vcs(repo: Path) -> VcsConfig:
    merged, _sources = merge_config(workspace=repo, repo=repo, env={})
    return VcsConfig.model_validate(merged["vcs"])


# ---------- the co-author pair ----------


def test_a_coauthor_name_without_its_email_is_refused_and_nothing_is_written(repo: Path) -> None:
    with pytest.raises(ValueError, match="validation_failed: config_section_invalid") as caught:
        _set(repo, NAME, "Jane Doe")

    message = str(caught.value)
    assert f"{EMAIL}: Field required" in message
    assert "(section vcs)" in message
    assert not _file(repo).exists()


def test_the_pair_written_together_succeeds_and_ship_names_the_trailer(repo: Path) -> None:
    result = _set_many(
        repo,
        [
            {"key_path": NAME.split("."), "value": "Jane Doe"},
            {"key_path": EMAIL.split("."), "value": "jane@example.com"},
        ],
    )

    assert _written(repo) == {
        "vcs": {"coauthor": {"project": {"name": "Jane Doe", "email": "jane@example.com"}}}
    }
    assert [write["key_path"][-1] for write in result["writes"]] == ["name", "email"]
    assert len(result["envelopes"]) == 2
    assert result["idempotent_replay"] is False
    _set(repo, "vcs.coauthor.mode", "project")
    trailer = resolve_coauthor_trailer(_vcs(repo).coauthor)
    assert trailer == "Co-Authored-By: Jane Doe <jane@example.com>"


def test_a_single_leaf_of_a_valid_pair_can_then_be_changed_alone(repo: Path) -> None:
    _set_many(
        repo,
        [
            {"key_path": NAME.split("."), "value": "Jane Doe"},
            {"key_path": EMAIL.split("."), "value": "jane@example.com"},
        ],
    )

    _set(repo, EMAIL, "jd@example.com")

    assert _written(repo)["vcs"]["coauthor"]["project"]["email"] == "jd@example.com"


def test_removing_one_leaf_of_the_pair_is_refused_and_both_together_is_not(repo: Path) -> None:
    _set_many(
        repo,
        [
            {"key_path": NAME.split("."), "value": "Jane Doe"},
            {"key_path": EMAIL.split("."), "value": "jane@example.com"},
        ],
    )
    before = _file(repo).read_text(encoding="utf-8")

    with pytest.raises(ValueError, match=f"{EMAIL}: Field required"):
        _unset(repo, EMAIL)
    assert _file(repo).read_text(encoding="utf-8") == before

    result = _set_many(
        repo,
        [
            {"key_path": NAME.split("."), "unset": True},
            {"key_path": EMAIL.split("."), "unset": True},
        ],
    )
    assert _written(repo) == {}
    assert [envelope["payload"]["operation"] for envelope in result["envelopes"]] == [
        "unset",
        "unset",
    ]


def test_a_multi_leaf_write_with_one_bad_leaf_writes_none_of_them(repo: Path) -> None:
    with pytest.raises(ValueError, match="unknown config key"):
        _set_many(
            repo,
            [
                {"key_path": NAME.split("."), "value": "Jane Doe"},
                {"key_path": ["vcs", "coauthor", "project", "phone"], "value": "1"},
            ],
        )
    assert not _file(repo).exists()


def test_a_multi_leaf_write_is_replayed_under_its_idempotency_key(repo: Path) -> None:
    ctx = _ctx(repo)
    params = {
        "layer": "repo",
        "idempotency_key": "CFG-pair-1",
        "writes": [
            {"key_path": NAME.split("."), "value": "Jane Doe"},
            {"key_path": EMAIL.split("."), "value": "jane@example.com"},
        ],
    }

    first = asyncio.run(set_layer_values(ctx, params))
    again = asyncio.run(set_layer_values(ctx, params))

    assert first["idempotent_replay"] is False
    assert again["idempotent_replay"] is True
    assert again["envelopes"] == first["envelopes"]


def test_a_multi_leaf_write_needs_at_least_one_leaf(repo: Path) -> None:
    with pytest.raises(ValueError, match="validation_failed"):
        _set_many(repo, [])


def test_the_project_identity_leaves_are_each_others_pair() -> None:
    assert pair_siblings(NAME) == (EMAIL,)
    assert pair_siblings(EMAIL) == (NAME,)
    assert pair_siblings("vcs.coauthor.mode") == ()
    assert pair_siblings("no.such.key") == ()


# ---------- one breaking write per section model ----------

#: Per section model: a leaf write the leaf check lets through that breaks the section,
#: and the field the refusal names.
BREAKING: tuple[tuple[str, str, Any, str], ...] = (
    ("vcs", NAME, "Jane Doe", EMAIL),
    ("agents", "agents.extra_tools", {"nobody": ["LSP"]}, "agents.extra_tools"),
    ("runtime.models", "runtime.models.codex", ["gpt-5", "gpt-5"], "runtime.models.codex"),
    (
        "runtime.claude",
        "runtime.claude.permission_wait_s",
        51,
        "runtime.claude.permission_wait_s",
    ),
    ("runtime.codex", "runtime.codex.stall_interval_s", -1, "runtime.codex.stall_interval_s"),
    (
        "runtime.opencode",
        "runtime.opencode.stall_interval_s",
        86_401,
        "runtime.opencode.stall_interval_s",
    ),
    (
        "verify",
        "verify.juror_wall_clock_seconds",
        0,
        "verify.juror_wall_clock_seconds",
    ),
)

#: Sections whose every leaf the registry already bounds at least as tightly as the model,
#: so the breaking state is one a file already holds; a write into the section is refused.
#: Per section: the file body, the leaf then written, its value, the field refused.
ALREADY_BROKEN: tuple[tuple[str, dict[str, Any], str, Any, str], ...] = (
    (
        "estimation",
        {"estimation": {"eu_minutes": 0}},
        "estimation.eu_basis",
        "tokens",
        "eu_minutes",
    ),
    ("preferences", {"preferences": {"stray": 1}}, "preferences.auto_choose", "always", "stray"),
)

#: Sections writable only from the global layer, held to the same rule there.
GLOBAL_ONLY_BROKEN: tuple[tuple[str, dict[str, Any], str, Any, str], ...] = (
    ("operator", {"operator": {"stray": 1}}, "operator.principal", "OP-0001", "stray"),
)


def test_every_section_model_has_a_breaking_write_below() -> None:
    covered = (
        {row[0] for row in BREAKING}
        | {row[0] for row in ALREADY_BROKEN}
        | {row[0] for row in GLOBAL_ONLY_BROKEN}
    )
    assert covered == {section for section, _model in SECTION_MODELS}


@pytest.mark.parametrize(("section", "key", "value", "field"), BREAKING)
def test_a_write_breaking_its_section_model_is_refused(
    repo: Path, section: str, key: str, value: Any, field: str
) -> None:
    with pytest.raises(ValueError, match="config_section_invalid") as caught:
        _set(repo, key, value)

    assert f"{field}" in str(caught.value)
    assert f"(section {section})" in str(caught.value)
    assert not _file(repo).exists()


@pytest.mark.parametrize(("section", "body", "key", "value", "field"), ALREADY_BROKEN)
def test_a_write_into_a_section_a_layer_already_breaks_is_refused(
    repo: Path, section: str, body: dict[str, Any], key: str, value: Any, field: str
) -> None:
    _global(body)

    with pytest.raises(ValueError, match=f"{section}.{field}: .*\\(section {section}\\)"):
        _set(repo, key, value)
    assert not _file(repo).exists()


@pytest.mark.parametrize(("section", "body", "key", "value", "field"), GLOBAL_ONLY_BROKEN)
def test_a_global_write_into_a_section_the_global_layer_breaks_is_refused(
    repo: Path, section: str, body: dict[str, Any], key: str, value: Any, field: str
) -> None:
    _global(body)

    with pytest.raises(ValueError, match=f"{section}.{field}: .*\\(section {section}\\)"):
        _set(repo, key, value, layer="global")
    assert yaml.safe_load(layered.global_config_path().read_text(encoding="utf-8")) == body


def test_a_write_to_another_section_is_not_held_to_a_broken_one(repo: Path) -> None:
    _global({"vcs": {"coauthor": {"mode": "bogus"}}})

    _set(repo, "verify.odr_blocking", True)

    assert _written(repo) == {"verify": {"odr_blocking": True}}


def test_a_write_that_repairs_its_broken_section_is_accepted(repo: Path) -> None:
    _global({"vcs": {"coauthor": {"project": {"name": "Jane"}}}})

    _set(repo, EMAIL, "jane@example.com")

    assert _vcs(repo).coauthor.project is not None


# ---------- the check itself ----------


def test_the_built_in_defaults_pass_every_section_model() -> None:
    check_sections(built_in_defaults(), [section for section, _model in SECTION_MODELS])


def test_no_key_checks_nothing_and_an_unowned_key_has_no_section() -> None:
    broken = {"vcs": {"coauthor": {"mode": "bogus"}}}

    check_sections(broken, [])
    check_sections(broken, ["telemetry.enabled"])
    assert section_of("telemetry.enabled") is None


def test_a_leaf_is_held_to_its_innermost_section() -> None:
    found = section_of("runtime.models.codex")
    assert found is not None
    assert found[0] == "runtime.models"
    assert section_of("runtime.preference") is None


def test_a_section_no_layer_states_is_left_to_its_defaults() -> None:
    check_sections({}, ["runtime.models.codex", "vcs.coauthor.mode"])


def test_the_refusal_carries_the_section_field_and_message() -> None:
    with pytest.raises(ConfigSectionError) as caught:
        check_sections({"verify": {"retyped_rule_threshold": 0}}, ["verify.waiver_mode"])

    assert caught.value.section == "verify"
    assert caught.value.field == "verify.retyped_rule_threshold"
    assert "greater than or equal to 1" in caught.value.message


def test_a_staged_body_stands_in_for_its_file(repo: Path) -> None:
    _file(repo).write_text(yaml.safe_dump({"telemetry": {"enabled": True}}), encoding="utf-8")
    staged = {"telemetry": {"enabled": False}}

    merged, sources = merge_config(workspace=repo, repo=repo, env={}, staged={_file(repo): staged})

    assert merged["telemetry"]["enabled"] is False
    assert sources["telemetry.enabled"] == "repo"
    assert staged == {"telemetry": {"enabled": False}}
    assert yaml.safe_load(_file(repo).read_text())["telemetry"]["enabled"] is True
