"""Emit role carriers and report digest staleness at session start."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pydantic
import pytest
import yaml

from eawf.platform.rules.carriers import (
    CARRIER_NAME_PREFIX,
    RenderedCarrier,
    builtin_carrier_roles,
    carrier_name,
    carrier_roles,
    carrier_stamp_line,
    carrier_stamp_matches_body,
    carrier_target,
    render_role_carriers,
)
from eawf.platform.rules.render import (
    CARD_TARGET,
    POLICY_TARGET,
    ProjectionManifest,
    RuleProjectionHandEditError,
    classify_projection,
    plan_rule_projections,
    render_rule_projections,
)
from eawf.platform.rules.staleness import (
    REFRESH_COMMAND,
    REPORTED_SESSIONS_KEPT,
    REPORTED_SESSIONS_PATH,
    StaleProjection,
    projection_staleness,
    report_projection_staleness_once,
    staleness_report,
    stamp_graph_digest,
)
from eawf.runtime.hooks.event import HookEvent, HookEventType
from eawf.runtime.hooks.runner import HookRunner
from eawf.surfaces.cli.commands.hook import (
    _emit_session_context,
    _register_projection_staleness,
)

_OTHER_DIGEST = f"sha256:{'0' * 64}"

_CONSTITUTION: dict[str, Any] = {
    "rule_id": "repo.changelog",
    "obligation_id": "demo.changelog",
    "revision": 1,
    "title": "Keep release notes in the changelog",
    "zone": "constitution",
    "force": "must",
    "effectiveness": "behavioral",
    "instruction": "Keep the release notes in the changelog under the version heading.",
    "verification": {"method": "review"},
}


def _role_rule(
    rule_id: str, *, roles: list[str], force: str = "should", zone: str = "retrievable"
) -> dict[str, Any]:
    return {
        "rule_id": rule_id,
        "obligation_id": rule_id.replace("repo.", "demo."),
        "revision": 1,
        "title": f"Follow {rule_id.removeprefix('repo.')}",
        "zone": zone,
        "force": force,
        "effectiveness": "behavioral",
        "instruction": f"Cite the call site for {rule_id.removeprefix('repo.')} as file:line.",
        "scope": {"roles": roles},
        "verification": {"method": "review"},
    }


def _write_source(root: Path, *extra: dict[str, Any]) -> None:
    source = root / ".ea" / "rules.yaml"
    source.parent.mkdir(parents=True, exist_ok=True)
    document = {"schema_version": 1, "modules": [], "rules": [_CONSTITUTION, *extra]}
    source.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    root = tmp_path / "demo"
    _write_source(
        root,
        _role_rule("repo.wired-citation", roles=["executor"]),
        _role_rule("repo.refute-first", roles=["auditor", "executor"], force="should"),
    )
    (root / "pyproject.toml").write_text('[project]\nname = "demo"\n', encoding="utf-8")
    return root


def _frontmatter(text: str) -> dict[str, Any]:
    head = text.split("---\n")[1]
    loaded = yaml.safe_load(head)
    assert isinstance(loaded, dict)
    return loaded


def _session_start(repo: Path, session_id: str, capsys: pytest.CaptureFixture[str]) -> str:
    """Run the session-start check the way ``eawf hook run`` does; return stdout."""
    runner = HookRunner()
    _register_projection_staleness(runner, repo_root=repo)
    event = HookEvent(
        event_type=HookEventType.SESSION_START,
        scope_id="",
        command="",
        args={},
        runtime="claude",
        occurred_at=datetime.now(UTC),
        payloads={"session_start": {"hook_event_name": "SessionStart", "session_id": session_id}},
    )
    results = runner.run_event(event)
    assert not any(result.raised for result in results), results
    capsys.readouterr()
    _emit_session_context(results)
    return capsys.readouterr().out


def _diverge_card_stamp(repo: Path) -> None:
    card = repo / CARD_TARGET
    text = card.read_text(encoding="utf-8")
    current = stamp_graph_digest(text)
    assert current is not None
    card.write_text(text.replace(current, _OTHER_DIGEST, 1), encoding="utf-8")


# ---- gate-fire proof: the session-start staleness report ---------------------


def test_session_start_hook_reports_a_diverged_stamp_exactly_once(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    render_rule_projections(repo)
    _diverge_card_stamp(repo)
    first = _session_start(repo, "S-1", capsys)
    lines = [line for line in first.splitlines() if line.strip()]
    assert len(lines) == 1
    context = json.loads(lines[0])["hookSpecificOutput"]
    assert context["hookEventName"] == "SessionStart"
    report = context["additionalContext"]
    assert len(report.splitlines()) == 1
    assert CARD_TARGET in report
    assert f"`{REFRESH_COMMAND}`" in report
    assert _session_start(repo, "S-1", capsys).strip() == ""


def test_session_start_hook_is_silent_when_every_stamp_matches(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    render_rule_projections(repo)
    assert _session_start(repo, "S-1", capsys).strip() == ""
    assert not (repo / REPORTED_SESSIONS_PATH).exists()


def test_session_start_hook_reports_again_in_a_new_session(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    render_rule_projections(repo)
    _diverge_card_stamp(repo)
    assert _session_start(repo, "S-1", capsys).strip()
    assert _session_start(repo, "S-2", capsys).strip()


# ---- the rendered skill listing: carriers are name-only ----------------------


def test_render_rule_projections_lists_each_role_carrier_by_name_only(repo: Path) -> None:
    written = render_rule_projections(repo)
    for role in ("auditor", "executor"):
        target = carrier_target(role)
        assert target in written.manifest.generated
        meta = _frontmatter((repo / target).read_text(encoding="utf-8"))
        assert meta == {
            "name": carrier_name(role),
            "description": carrier_name(role),
            "user-invocable": False,
        }


def test_render_role_carriers_declares_no_invocation_grammar(repo: Path) -> None:
    plan = plan_rule_projections(repo)
    carriers = [text for target, text in plan.outputs if target.endswith("/SKILL.md")]
    assert len(carriers) == len({"auditor", "executor", *builtin_carrier_roles()})
    for text in carriers:
        meta = _frontmatter(text)
        assert "disable-model-invocation" not in meta
        assert "argument-hint" not in meta
        assert "allowed-tools" not in meta


def test_render_role_carriers_delivers_every_rule_scoped_to_the_role(repo: Path) -> None:
    plan = plan_rule_projections(repo)
    texts = dict(plan.outputs)
    executor = texts[carrier_target("executor")]
    auditor = texts[carrier_target("auditor")]
    assert "wired-citation" in executor
    assert "refute-first" in executor
    assert "refute-first" in auditor
    assert "wired-citation" not in auditor
    assert "Keep the release notes" not in executor


# ---- carriers: boundaries ----------------------------------------------------


def test_render_role_carriers_emits_only_builtin_roles_without_repository_role_rules(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    root = tmp_path / "plain"
    _write_source(root)
    plan = plan_rule_projections(root)
    assert [target for target in plan.manifest.generated if target.endswith("SKILL.md")] == [
        carrier_target(role) for role in builtin_carrier_roles()
    ]


def test_carrier_roles_single_role(repo: Path) -> None:
    _write_source(repo, _role_rule("repo.only", roles=["reviewer"]))
    plan = plan_rule_projections(repo)
    assert [t for t in plan.manifest.generated if t.endswith("SKILL.md")] == [
        carrier_target(role) for role in sorted({"reviewer", *builtin_carrier_roles()})
    ]


def test_carrier_name_and_target_follow_the_role() -> None:
    assert carrier_name("executor") == f"{CARRIER_NAME_PREFIX}executor"
    assert carrier_target("executor") == ".claude/skills/eawf-rules-executor/SKILL.md"


def test_rendered_carrier_refuses_a_name_not_derived_from_the_role() -> None:
    with pytest.raises(pydantic.ValidationError, match="derive from role"):
        RenderedCarrier(
            role="executor",
            name="executor",
            target=carrier_target("executor"),
            text="",
            rule_ids=(),
        )


def test_rendered_carrier_refuses_an_invalid_role() -> None:
    with pytest.raises(pydantic.ValidationError):
        RenderedCarrier(role="Executor", name="x", target="x", text="", rule_ids=())


def test_carrier_stamp_matches_body_is_none_for_a_non_carrier() -> None:
    assert carrier_stamp_matches_body("") is None
    assert carrier_stamp_matches_body("# plain\n") is None
    assert carrier_stamp_matches_body("---\nname: x\n") is None
    assert carrier_stamp_matches_body("---\nname: x\n---\nno stamp\n") is None
    assert carrier_stamp_line("# plain\n") is None


def test_classify_projection_detects_a_hand_edited_carrier(repo: Path) -> None:
    render_rule_projections(repo)
    path = repo / carrier_target("executor")
    assert classify_projection(path) == "generated"
    path.write_text(path.read_text(encoding="utf-8") + "- extra rule\n", encoding="utf-8")
    assert classify_projection(path) == "hand_edited"
    with pytest.raises(RuleProjectionHandEditError, match="edited by hand"):
        render_rule_projections(repo)


def test_classify_projection_detects_an_edited_carrier_frontmatter(repo: Path) -> None:
    render_rule_projections(repo)
    path = repo / carrier_target("executor")
    text = path.read_text(encoding="utf-8")
    path.write_text(text.replace("user-invocable: false", "user-invocable: true"), "utf-8")
    assert classify_projection(path) == "hand_edited"


def test_render_rule_projections_removes_a_carrier_whose_role_lost_its_rules(
    repo: Path,
) -> None:
    assert "reviewer" not in builtin_carrier_roles()
    _write_source(repo, _role_rule("repo.review-once", roles=["reviewer"]))
    render_rule_projections(repo)
    assert (repo / carrier_target("reviewer")).is_file()
    _write_source(repo, _role_rule("repo.wired-citation", roles=["executor"]))
    written = render_rule_projections(repo)
    assert carrier_target("reviewer") in written.removed
    assert not (repo / carrier_target("reviewer")).exists()
    manifest = ProjectionManifest.model_validate_json(
        (repo / ".ea/indexes/rule-projections.json").read_bytes()
    )
    assert carrier_target("reviewer") not in manifest.targets


def test_carrier_roles_is_empty_for_an_unscoped_graph(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from eawf.platform.rules.compile import compile_rule_graph

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    root = tmp_path / "plain"
    _write_source(root)
    graph = compile_rule_graph(root)
    assert carrier_roles(graph) == ()
    assert render_role_carriers(graph) == ()


# ---- staleness: library boundaries and error paths --------------------------


def test_projection_staleness_is_empty_right_after_a_render(repo: Path) -> None:
    render_rule_projections(repo)
    assert projection_staleness(repo, plan_rule_projections(repo)) == ()


def test_projection_staleness_names_an_absent_projection(repo: Path) -> None:
    render_rule_projections(repo)
    (repo / POLICY_TARGET).unlink()
    stale = projection_staleness(repo, plan_rule_projections(repo))
    assert [item.target for item in stale] == [POLICY_TARGET]
    assert stale[0].stamped_digest is None


def test_projection_staleness_flags_every_output_after_the_graph_changes(repo: Path) -> None:
    render_rule_projections(repo)
    _write_source(
        repo,
        _role_rule("repo.wired-citation", roles=["executor"]),
        _role_rule("repo.refute-first", roles=["auditor", "executor"], force="should"),
        _role_rule("repo.more", roles=["executor"], force="information", zone="steering"),
    )
    stale = {item.target for item in projection_staleness(repo, plan_rule_projections(repo))}
    assert {CARD_TARGET, POLICY_TARGET, carrier_target("executor")} <= stale


def test_stamp_graph_digest_reads_card_and_carrier_stamps(repo: Path) -> None:
    plan = plan_rule_projections(repo)
    texts = dict(plan.outputs)
    assert stamp_graph_digest(texts[CARD_TARGET]) is not None
    assert stamp_graph_digest(texts[carrier_target("executor")]) is not None
    assert stamp_graph_digest("") is None
    assert stamp_graph_digest("@AGENTS.override.md\n") is None


def test_staleness_report_is_none_when_nothing_is_stale() -> None:
    assert staleness_report(()) is None


def test_staleness_report_is_one_line_naming_the_refresh_command() -> None:
    item = StaleProjection(target=CARD_TARGET, stamped_digest=None, current_digest=_OTHER_DIGEST)
    report = staleness_report((item,))
    assert report is not None
    assert len(report.splitlines()) == 1
    assert "1 rendered projection(s)" in report
    assert REFRESH_COMMAND in report


def test_report_projection_staleness_once_is_none_without_a_rule_source(tmp_path: Path) -> None:
    assert report_projection_staleness_once(tmp_path, session_id="S-1") is None


def test_report_projection_staleness_once_repeats_without_a_session_id(repo: Path) -> None:
    render_rule_projections(repo)
    _diverge_card_stamp(repo)
    assert report_projection_staleness_once(repo, session_id=None) is not None
    assert report_projection_staleness_once(repo, session_id=None) is not None


def test_report_projection_staleness_once_tolerates_an_unreadable_record(repo: Path) -> None:
    render_rule_projections(repo)
    _diverge_card_stamp(repo)
    record = repo / REPORTED_SESSIONS_PATH
    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_text("{not json", encoding="utf-8")
    assert report_projection_staleness_once(repo, session_id="S-1") is not None
    assert report_projection_staleness_once(repo, session_id="S-1") is None


def test_report_projection_staleness_once_keeps_a_bounded_session_record(repo: Path) -> None:
    render_rule_projections(repo)
    _diverge_card_stamp(repo)
    for index in range(REPORTED_SESSIONS_KEPT + 1):
        assert report_projection_staleness_once(repo, session_id=f"S-{index}") is not None
    kept = json.loads((repo / REPORTED_SESSIONS_PATH).read_text(encoding="utf-8"))
    assert len(kept["session_ids"]) == REPORTED_SESSIONS_KEPT
    assert "S-0" not in kept["session_ids"]
