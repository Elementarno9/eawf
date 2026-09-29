"""Legacy profile-renderer migration: dispositions, verbatim prose and the switch.

RULE-090: every legacy block and profile field has exactly one typed
disposition, checked by reflection and against a machine-produced inventory.
RULE-091: prose is never split into structured fields; hand-written root
prose is uncertified migration input.
RULE-092: duplicates are settled by obligation identifier; similarity only
lists review candidates.
RULE-094: after the switch, legacy blocks are import-only and an edit to a
generated projection fails validation.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, get_args

import pytest
import yaml
from pydantic import ValidationError

from eawf.platform.install.wizard import WizardAnswers, run_wizard_no_input
from eawf.platform.profiles.models import ProfileBody
from eawf.platform.rules import migration
from eawf.platform.rules.migration import (
    PER_BLOCK,
    PROFILE_FIELD_DISPOSITIONS,
    LegacyItem,
    LegacyMigrationError,
    legacy_inventory,
    plan_legacy_migration,
    profile_field_dispositions,
)
from eawf.platform.rules.records import LegacyDisposition, LegacyDispositionKind
from eawf.platform.rules.render import (
    CARD_TARGET,
    POLICY_TARGET,
    RuleProjectionHandEditError,
    classify_projection,
    plan_rule_projections,
    render_rule_projections,
)
from eawf.surfaces.render import regions

_DUPLICATED = "Prefer short module docstrings that state why the module exists."
_TRIAD_PROSE = "Rationale: speed matters.\nVerification: run the benchmark.\nKeep loops tight."


def _source(root: Path, *, legacy: list[dict[str, Any]] | None = None) -> None:
    document: dict[str, Any] = {
        "schema_version": 1,
        "modules": [],
        "rules": [
            {
                "rule_id": "repo.docstrings",
                "obligation_id": "demo.docstrings",
                "revision": 1,
                "title": "Keep module docstrings short",
                "zone": "steering",
                "force": "should",
                "effectiveness": "behavioral",
                "instruction": _DUPLICATED,
                "verification": {"method": "review"},
            }
        ],
    }
    if legacy is not None:
        document["legacy"] = legacy
    path = root / ".ea" / "rules.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")


def _profile(root: Path, blocks: list[dict[str, Any]]) -> None:
    path = root / ".ea" / "profiles" / "demo.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    body = {"schema_version": "1.0", "name": "demo", "render_blocks": blocks}
    path.write_text(yaml.safe_dump(body, sort_keys=False), encoding="utf-8")


def _enable(root: Path, profiles: list[str]) -> None:
    config = {"schema_version": "1.0", "profiles": {"enabled": profiles}}
    (root / ".ea" / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")


_BLOCKS = [
    {"id": "dup-docstrings", "target": "AGENTS.md", "body_template": _DUPLICATED},
    {"id": "perf-note", "target": "AGENTS.md", "body_template": _TRIAD_PROSE},
]


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    root = tmp_path / "demo"
    _source(root)
    _profile(root, _BLOCKS)
    _enable(root, ["demo"])
    return root


def _all_blocks_disposed() -> list[dict[str, Any]]:
    return [
        {
            "item": "block:dup-docstrings",
            "disposition": "duplicate_of",
            "obligation_id": "demo.docstrings",
        },
        {"item": "block:perf-note", "disposition": "rejected"},
    ]


# ---- RULE-090: exactly one typed disposition per item ----------------------


def test_rule_090_every_profile_field_carries_one_typed_disposition() -> None:
    table = profile_field_dispositions()
    assert set(PROFILE_FIELD_DISPOSITIONS) == set(ProfileBody.model_fields)
    assert set(table) == set(ProfileBody.model_fields) - {"render_blocks"}
    assert PROFILE_FIELD_DISPOSITIONS["render_blocks"] == PER_BLOCK
    assert set(table.values()) <= set(get_args(LegacyDispositionKind))


def test_rule_090_reflection_fails_on_a_field_without_a_disposition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    short = {k: v for k, v in PROFILE_FIELD_DISPOSITIONS.items() if k != "verify"}
    monkeypatch.setattr(migration, "PROFILE_FIELD_DISPOSITIONS", short)
    with pytest.raises(LegacyMigrationError, match=r"without a disposition \['verify'\]"):
        profile_field_dispositions()


def test_rule_090_reflection_fails_on_a_disposition_without_a_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    extra = {**PROFILE_FIELD_DISPOSITIONS, "retired_field": "rejected"}
    monkeypatch.setattr(migration, "PROFILE_FIELD_DISPOSITIONS", extra)
    with pytest.raises(LegacyMigrationError, match="retired_field"):
        legacy_inventory(Path("."))


def test_rule_090_inventory_is_machine_produced_from_model_and_profiles(repo: Path) -> None:
    inventory = legacy_inventory(repo)
    fields = [item.key for item in inventory if item.kind == "profile_field"]
    blocks = [item.key for item in inventory if item.kind == "render_block"]
    assert len(fields) == len(ProfileBody.model_fields) - 1
    assert blocks == ["block:dup-docstrings", "block:perf-note"]


def test_rule_090_cardinality_matches_the_inventory_when_complete(repo: Path) -> None:
    _source(repo, legacy=_all_blocks_disposed())
    result = plan_legacy_migration(repo)
    assert result.complete
    assert len(result.entries) == len(legacy_inventory(repo))
    assert all(entry.disposition is not None for entry in result.entries)


def test_rule_090_an_undisposed_block_fails_migration(repo: Path) -> None:
    _source(repo, legacy=_all_blocks_disposed()[:1])
    result = plan_legacy_migration(repo)
    assert not result.complete
    assert result.undisposed == ("block:perf-note",)


def test_rule_090_a_block_disposed_twice_fails_migration(repo: Path) -> None:
    twice = [*_all_blocks_disposed(), {"item": "block:perf-note", "disposition": "operator_owned"}]
    _source(repo, legacy=twice)
    result = plan_legacy_migration(repo)
    assert result.repeated == ("block:perf-note",)
    assert not result.complete


def test_rule_090_a_disposition_for_no_inventory_item_fails_migration(repo: Path) -> None:
    _source(
        repo, legacy=[*_all_blocks_disposed(), {"item": "block:gone", "disposition": "rejected"}]
    )
    result = plan_legacy_migration(repo)
    assert result.unknown == ("block:gone",)
    assert not result.complete


def test_rule_090_an_operator_disposition_of_a_profile_field_is_a_repeat(repo: Path) -> None:
    field = {"item": "field:verify", "disposition": "rejected"}
    _source(repo, legacy=[*_all_blocks_disposed(), field])
    assert plan_legacy_migration(repo).repeated == ("field:verify",)


def test_rule_090_no_enabled_profile_leaves_only_the_fields(repo: Path) -> None:
    _enable(repo, [])
    result = plan_legacy_migration(repo)
    assert {entry.item.kind for entry in result.entries} == {"profile_field"}
    assert result.complete


@pytest.mark.parametrize(
    "payload",
    [
        {"item": "block:a", "disposition": "duplicate_of"},
        {"item": "block:a", "disposition": "steering_rule"},
        {"item": "block:a", "disposition": "rejected", "obligation_id": "demo.a"},
        {"item": "block:a", "disposition": "merged"},
        {"item": "a", "disposition": "rejected"},
        {"item": "block:a", "disposition": "rejected", "note": "extra"},
    ],
)
def test_rule_090_disposition_record_is_typed_and_closed(payload: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        LegacyDisposition.model_validate(payload)


# ---- RULE-091: prose stays verbatim; root prose is uncertified -------------


def test_rule_091_block_prose_is_carried_verbatim_without_fabricated_fields(repo: Path) -> None:
    item = next(i for i in legacy_inventory(repo) if i.key == "block:perf-note")
    assert item.prose == _TRIAD_PROSE
    assert set(LegacyItem.model_fields) == {"kind", "key", "prose", "certified"}
    entry = next(e for e in plan_legacy_migration(repo).entries if e.item.key == item.key)
    assert entry.disposition is None


def test_rule_091_a_triad_block_keeps_its_authored_fields_unsplit(repo: Path) -> None:
    triad = {
        "id": "triad",
        "target": "AGENTS.md",
        "rationale": "Why.",
        "mechanism": "What.",
        "verification": "How.",
    }
    _profile(repo, [triad])
    (item,) = (i for i in legacy_inventory(repo) if i.kind == "render_block")
    assert item.prose == "Why.\n\nWhat.\n\nHow."


def test_rule_091_hand_written_root_prose_is_uncertified_input_not_a_rule(repo: Path) -> None:
    baseline = plan_rule_projections(repo)
    legacy = regions.replace_region("", id="rules", version="1.0", body="managed body")
    card = repo / CARD_TARGET
    card.write_text(f"# House rules\n\nAlways deploy on Fridays.\n\n{legacy}", encoding="utf-8")
    (root,) = (i for i in legacy_inventory(repo) if i.kind == "root_prose")
    assert root.key == f"root:{CARD_TARGET}"
    assert not root.certified
    assert root.prose == "# House rules\n\nAlways deploy on Fridays."
    assert plan_rule_projections(repo) == baseline


def test_rule_091_a_regions_only_or_absent_card_has_no_root_prose(repo: Path) -> None:
    assert not [i for i in legacy_inventory(repo) if i.kind == "root_prose"]
    legacy = regions.replace_region("", id="rules", version="1.0", body="managed body")
    (repo / CARD_TARGET).write_text(legacy, encoding="utf-8")
    assert not [i for i in legacy_inventory(repo) if i.kind == "root_prose"]


# ---- RULE-092: ownership by obligation identifier only ---------------------


def test_rule_092_textual_similarity_lists_a_candidate_and_never_disposes(repo: Path) -> None:
    entry = next(
        e for e in plan_legacy_migration(repo).entries if e.item.key == "block:dup-docstrings"
    )
    assert entry.review_candidates == ("repo.docstrings",)
    assert entry.disposition is None


def test_rule_092_a_duplicate_is_removed_by_obligation_without_text_match(repo: Path) -> None:
    unlike = [
        {
            "item": "block:perf-note",
            "disposition": "duplicate_of",
            "obligation_id": "demo.docstrings",
        },
        {"item": "block:dup-docstrings", "disposition": "rejected"},
    ]
    _source(repo, legacy=unlike)
    result = plan_legacy_migration(repo)
    entry = next(e for e in result.entries if e.item.key == "block:perf-note")
    assert entry.review_candidates == ()
    assert result.complete


def test_rule_092_a_duplicate_of_an_unowned_obligation_is_unresolved(repo: Path) -> None:
    missing = [
        {**_all_blocks_disposed()[0], "obligation_id": "demo.nobody"},
        _all_blocks_disposed()[1],
    ]
    _source(repo, legacy=missing)
    result = plan_legacy_migration(repo)
    assert result.unresolved == ("block:dup-docstrings -> demo.nobody",)
    assert not result.complete


def test_rule_092_a_field_item_has_no_review_candidates(repo: Path) -> None:
    fields = [e for e in plan_legacy_migration(repo).entries if e.item.kind == "profile_field"]
    assert fields
    assert all(entry.review_candidates == () for entry in fields)


# ---- RULE-094: import-only after the switch --------------------------------


def test_rule_094_a_switched_render_never_writes_a_legacy_block(repo: Path) -> None:
    render_rule_projections(repo)
    card = (repo / CARD_TARGET).read_text(encoding="utf-8")
    policy = (repo / POLICY_TARGET).read_text(encoding="utf-8")
    assert _TRIAD_PROSE not in card
    assert _TRIAD_PROSE not in policy
    assert "eawf:managed" not in card
    assert legacy_inventory(repo)[-1].prose == _TRIAD_PROSE


def test_rule_094_init_on_a_switched_repository_renders_the_rule_graph(repo: Path) -> None:
    answers = WizardAnswers(
        state_path=".ea/state.json",
        project_code="DEMO",
        project_title="Demo",
        lifecycle_depth="phase",
        profiles=("core",),
        runtime="claude-code",
        plugins=(),
        mcp=(),
    )
    run_wizard_no_input(answers, repo, force=True)
    assert classify_projection(repo / CARD_TARGET) == "generated"
    assert "What this repo runs on" not in (repo / CARD_TARGET).read_text(encoding="utf-8")


def test_rule_094_an_operator_edit_to_a_generated_projection_fails_validation(
    repo: Path,
) -> None:
    render_rule_projections(repo)
    policy = repo / POLICY_TARGET
    policy.write_text(policy.read_text(encoding="utf-8") + "- operator rule\n", "utf-8")
    assert classify_projection(policy) == "hand_edited"
    with pytest.raises(RuleProjectionHandEditError, match="edited by hand"):
        render_rule_projections(repo)
