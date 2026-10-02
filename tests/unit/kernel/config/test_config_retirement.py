"""The config catalog holds only leaves some code reads, and every retired leaf is stripped.

The operator ruling behind this module: a catalogued leaf nothing reads is retired from
the catalog, the built-in defaults, the section models and the console chrome; the config
migration strips it from every layer file and the doctor repair names any a layer still
states. The three leaves whose behaviour already existed behind a constant are wired, and
every catalog default equals the value the built-in layer ships.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

from eawf.kernel.config.defaults import BUILT_IN_DEFAULTS
from eawf.kernel.config.layered import _set_dotted as set_dotted
from eawf.kernel.config.layered import get_dotted
from eawf.kernel.config.migration import migrate_config_payload
from eawf.kernel.config.registry.config_keys import CONFIG_REGISTRY
from eawf.kernel.config.registry.leaf_catalog import DEPRECATED_LEAF_KEYS, LEAF_KEY_REGISTRY
from eawf.kernel.projection.settings import build_settings_view
from eawf.observability.doctor.repair import _config_actions
from eawf.runtime.daemon import main as daemon_main
from eawf.runtime.daemon.limits import configured_daemon_seconds
from eawf.runtime.daemon.methods import MethodContext
from eawf.surfaces.tui.launch import persisted_toast_verbosity

#: Keys no code reads any more, and no longer in the catalog. The legacy auto-accept
#: gates are renamed onto ``flow.advance_after`` rather than stripped.
RETIRED = sorted(
    key
    for key in DEPRECATED_LEAF_KEYS - set(LEAF_KEY_REGISTRY)
    if not key.startswith("flow.auto_accept.")
)


#: Leaves only a retired skill read: the ship gauntlet and its acceptance commands, the
#: audit and review levels, the flow transitions and repair ceiling, the prep resume and
#: the pull-request merge policy. No live command reads any of them.
SKILL_ONLY = (
    "acceptance.commands.build",
    "acceptance.commands.lint",
    "acceptance.commands.tests",
    "acceptance.commands.typecheck",
    "acceptance.required_before_ship",
    "audit.default_level",
    "flow.advance_after.audit",
    "flow.advance_after.polish",
    "flow.advance_after.prep",
    "flow.advance_after.research",
    "flow.max_repair_cycles",
    "prep.auto_resume",
    "review.default_level",
    "ship.gauntlet",
    "vcs.pr_merge_method",
    "vcs.squash_allowed",
)


def _nested(key: str, value: Any) -> dict[str, Any]:
    """Return a layer body stating ``value`` at dotted ``key``."""
    body: dict[str, Any] = value
    for part in reversed(key.split(".")):
        body = {part: body}
    return body


def _write(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Return a repository root whose layers the test owns, the home redirected beside it."""
    home = tmp_path / "home"
    (home / ".config" / "eawf").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    root = tmp_path / "repo"
    (root / ".ea").mkdir(parents=True)
    return root


def _ctx(repo: Path | None) -> MethodContext:
    """Return a daemon context bound to ``repo``'s tree, or to none."""
    return MethodContext(
        started_at="2026-09-30T00:00:00+00:00",
        pid=1,
        protocol_version="1",
        version="0",
        state_path=None if repo is None else repo / ".ea" / "state.json",
    )


# ---------- every catalog default is the built-in value ----------


@pytest.mark.parametrize("key", sorted(k for k, e in LEAF_KEY_REGISTRY.items() if not e.reserved))
def test_every_catalog_default_equals_the_built_in_value(key: str) -> None:
    """A leaf the built-in layer does not state defaults to ``None`` and nothing else."""
    default = LEAF_KEY_REGISTRY[key].default
    expected = list(default) if isinstance(default, tuple) else default
    try:
        shipped = get_dotted(BUILT_IN_DEFAULTS, key)
    except KeyError:
        shipped = None
    assert shipped == expected


def test_the_drifted_defaults_now_agree() -> None:
    """The theme and the toast level were live leaves whose defaults disagreed."""
    assert get_dotted(BUILT_IN_DEFAULTS, "ui.theme") == LEAF_KEY_REGISTRY["ui.theme"].default
    assert get_dotted(BUILT_IN_DEFAULTS, "ui.toasts") == "important"


# ---------- a leaf only a retired skill read is retired ----------


@pytest.mark.parametrize("key", SKILL_ONLY)
def test_a_skill_only_leaf_is_retired_everywhere_it_was_offered(key: str) -> None:
    assert key in DEPRECATED_LEAF_KEYS
    assert key not in LEAF_KEY_REGISTRY
    assert key not in {entry.key for entry in CONFIG_REGISTRY}
    with pytest.raises(KeyError):
        get_dotted(BUILT_IN_DEFAULTS, key)


def test_the_settings_route_offers_no_skill_only_leaf_a_layer_still_states(repo: Path) -> None:
    body: dict[str, Any] = {"schema_version": "1.0"}
    for key in SKILL_ONLY:
        set_dotted(body, key, 1)
    _write(repo / ".ea" / "config.yaml", yaml.safe_dump(body))
    assert migrate_config_payload(body)[0] == {"schema_version": "1.0"}
    view = build_settings_view(
        workspace=repo,
        repo=repo,
        scope_id="P01",
        cursor=0,
        generated_at=datetime(2026, 10, 2, tzinfo=UTC),
        env={},
        branch=None,
    )
    offered = {leaf.key for leaf in view.leaves}
    assert offered.isdisjoint(SKILL_ONLY)
    # the doctor repair is the notice: it names every straggler the layer still states
    (action,) = _config_actions(repo)
    assert action.detail.endswith(": " + ", ".join(sorted(SKILL_ONLY)))


# ---------- the policy tables are catalogued with their consumers ----------


@pytest.mark.parametrize(
    "key", ["economics.prompt_budget", "economics.governor", "economics.notice_policy"]
)
def test_an_economics_table_is_a_mapping_leaf_the_policy_reader_consumes(key: str) -> None:
    entry = LEAF_KEY_REGISTRY[key]
    assert entry.type == "mapping"
    assert entry.default is None
    assert entry.consumer == "eawf.kernel.economics.governor.economics_policy_from"
    assert entry.writable_layers == ("global", "workspace", "repo")


def test_the_eu_view_leaves_carry_their_model_choices() -> None:
    density = LEAF_KEY_REGISTRY["tui.eu_view.density"]
    fields = LEAF_KEY_REGISTRY["tui.eu_view.fields"]
    assert (density.type, density.choices) == ("literal", ("full", "compact"))
    assert fields.type == "list_str"
    assert fields.choices == ("work_sum", "critical_path", "queue", "realistic")


# ---------- the migration strips every retired leaf ----------


@pytest.mark.parametrize("key", RETIRED)
def test_the_migration_strips_a_retired_leaf_and_the_section_it_empties(key: str) -> None:
    upgraded, changed = migrate_config_payload({"schema_version": "1.0", **_nested(key, 1)})
    assert changed is True
    assert upgraded == {"schema_version": "1.0"}


def test_the_migration_keeps_a_live_sibling_of_a_retired_leaf() -> None:
    body = {"schema_version": "1.0", "vcs": {"force_push": "never", "task_reference": "trailer"}}
    upgraded, changed = migrate_config_payload(body)
    assert changed is True
    assert upgraded == {"schema_version": "1.0", "vcs": {"task_reference": "trailer"}}


def test_the_migration_leaves_a_canonical_body_alone() -> None:
    body = {"schema_version": "1.0", "ui": {"toasts": "off"}, "daemon": {"proxy_enabled": False}}
    assert migrate_config_payload(body) == (body, False)


# ---------- doctor names the stragglers a layer still states ----------


def test_the_doctor_repair_names_each_retired_key_a_layer_still_states(repo: Path) -> None:
    _write(
        repo / ".ea" / "config.yaml",
        "schema_version: '1.0'\nmcp:\n  enabled: []\nui:\n  bare_command: tui\n  toasts: 'off'\n",
    )
    actions = _config_actions(repo)
    assert [action.scope for action in actions] == ["workspace"]
    assert actions[0].detail == (
        "normalize deprecated configuration leaves in workspace layer: mcp.enabled, ui.bare_command"
    )


def test_the_doctor_repair_offers_nothing_for_a_canonical_layer(repo: Path) -> None:
    _write(repo / ".ea" / "config.yaml", "schema_version: '1.0'\nui:\n  toasts: 'off'\n")
    assert _config_actions(repo) == []


# ---------- the daemon reads its two timing leaves ----------


@pytest.mark.parametrize(
    ("leaf", "built_in"),
    [("idle_timeout_seconds", 300), ("session_handle_ttl_seconds", 86400)],
)
def test_a_daemon_leaf_resolves_to_the_built_in_value_when_no_layer_states_it(
    repo: Path, leaf: str, built_in: int
) -> None:
    assert configured_daemon_seconds(repo, leaf) == built_in
    assert configured_daemon_seconds(None, leaf) == built_in


def test_a_daemon_leaf_the_repo_states_is_the_value_in_force(repo: Path) -> None:
    _write(repo / ".ea" / "config.yaml", "daemon:\n  idle_timeout_seconds: 60\n")
    assert configured_daemon_seconds(repo, "idle_timeout_seconds") == 60


@pytest.mark.parametrize("raw", ["0", "-5", "'soon'", "true", "1.5", "null"])
def test_an_unusable_daemon_leaf_leaves_the_daemon_on_its_default(repo: Path, raw: str) -> None:
    _write(repo / ".ea" / "config.yaml", f"daemon:\n  idle_timeout_seconds: {raw}\n")
    assert configured_daemon_seconds(repo, "idle_timeout_seconds") is None
    assert daemon_main._resolve_idle_timeout(_ctx(repo)) == daemon_main.DEFAULT_IDLE_TIMEOUT_SECONDS


def test_an_unreadable_layer_leaves_the_daemon_on_its_default(repo: Path) -> None:
    _write(repo / ".ea" / "config.yaml", "daemon: [unclosed\n")
    assert configured_daemon_seconds(repo, "idle_timeout_seconds") is None


def test_the_idle_timeout_env_override_wins_over_the_config(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(repo / ".ea" / "config.yaml", "daemon:\n  idle_timeout_seconds: 60\n")
    monkeypatch.setenv("EAWF_DAEMON_IDLE_TIMEOUT", "7.5")
    assert daemon_main._resolve_idle_timeout(_ctx(repo)) == pytest.approx(7.5)


@pytest.mark.parametrize("raw", ["not-a-number", "0", "-3"])
def test_an_unusable_idle_env_override_falls_back_to_the_config(
    repo: Path, monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    _write(repo / ".ea" / "config.yaml", "daemon:\n  idle_timeout_seconds: 60\n")
    monkeypatch.setenv("EAWF_DAEMON_IDLE_TIMEOUT", raw)
    assert daemon_main._resolve_idle_timeout(_ctx(repo)) == pytest.approx(60.0)


def test_the_session_ttl_reads_the_config_when_no_env_override_is_set(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("EAWF_DAEMON_SESSION_TTL", raising=False)
    _write(repo / ".ea" / "config.yaml", "daemon:\n  session_handle_ttl_seconds: 3600\n")
    assert daemon_main._resolve_session_ttl_seconds(_ctx(repo)) == 3600
    monkeypatch.setenv("EAWF_DAEMON_SESSION_TTL", "120")
    assert daemon_main._resolve_session_ttl_seconds(_ctx(repo)) == 120


# ---------- the toast level reaches both consoles ----------


@pytest.mark.parametrize("level", ["off", "important", "all"])
def test_the_toast_level_a_layer_states_is_read(repo: Path, level: str) -> None:
    _write(repo / ".ea" / "config.yaml", f"ui:\n  toasts: '{level}'\n")
    assert persisted_toast_verbosity(repo) == level


def test_the_toast_level_defaults_to_important(repo: Path) -> None:
    assert persisted_toast_verbosity(repo) == "important"


@pytest.mark.parametrize("body", ["ui:\n  toasts: loud\n", "ui:\n  toasts: 3\n", "ui: [unclosed\n"])
def test_an_unusable_toast_level_keeps_the_default(repo: Path, body: str) -> None:
    _write(repo / ".ea" / "config.yaml", body)
    assert persisted_toast_verbosity(repo) == "important"
