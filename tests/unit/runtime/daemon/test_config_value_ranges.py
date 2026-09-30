"""The daemon's config writes hold a value to the interactive registry's type, range and choices.

Requirement row proved here, by id:

- ``UI-053``: the range the settings stack draws for a key is the range every write of
  that key is held to, the daemon's layered-config write and the daemonless CLI write
  included, so no surface can persist a value the stack would show as out of range.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

import pytest
import yaml

from eawf import __version__
from eawf.kernel.config.registry import leaf_key_lookup, validate_config_value
from eawf.runtime.daemon import PROTOCOL_VERSION
from eawf.runtime.daemon.bus import EventBus
from eawf.runtime.daemon.methods import MethodContext
from eawf.runtime.daemon.methods.config import set_layer_value
from eawf.surfaces.cli.commands.config import _save_value_to_layer
from eawf.surfaces.cli.errors import UserError

pytestmark = pytest.mark.unit

#: An int the registry holds to [1, 16].
RANGED_INT = "planning.max_parallel_waves"
#: A float the registry holds to at least 1.0.
RANGED_FLOAT = "flow.budget.multiplier"
#: A choice among quick, standard and deep.
CHOICE = "audit.default_level"
#: A list whose items must be declared choices.
MULTICHOICE = "tui.eu_view.fields"
#: A bool.
BOOL = "research.auto_save"
#: A leaf the interactive registry does not describe, so no range or choices apply.
UNREGISTERED = "profiles.enabled"


def _ctx(tmp_path: Path) -> tuple[MethodContext, Path]:
    repo = tmp_path / "repo"
    (repo / ".ea").mkdir(parents=True)
    state_path = repo / ".ea" / "state.json"
    state_path.write_text("{}", encoding="utf-8")
    ctx = MethodContext(
        started_at="2026-09-30T00:00:00+00:00",
        pid=os.getpid(),
        protocol_version=PROTOCOL_VERSION,
        version=__version__,
        shutdown_event=asyncio.Event(),
        bus=EventBus(),
        state_path=state_path,
        idempotency_cache={},
    )
    return ctx, repo


def _set(ctx: MethodContext, key: str, value: Any, layer: str = "repo") -> dict[str, Any]:
    return asyncio.run(
        set_layer_value(ctx, {"layer": layer, "key_path": key.split("."), "value": value})
    )


def _written(repo: Path) -> dict[str, Any]:
    path = repo / ".ea" / "config.yaml"
    return yaml.safe_load(path.read_text()) if path.exists() else {}


# ---------- the daemon's layer write ----------


@pytest.mark.parametrize(
    ("key", "value"),
    [
        (RANGED_INT, 8),
        (RANGED_INT, 1),
        (RANGED_INT, 16),
        (RANGED_FLOAT, 1.0),
        (RANGED_FLOAT, 2.5),
        (CHOICE, "deep"),
        (MULTICHOICE, []),
        (MULTICHOICE, ["queue"]),
        (BOOL, False),
    ],
)
def test_ui_053_an_in_range_value_is_written(tmp_path: Path, key: str, value: Any) -> None:
    ctx, repo = _ctx(tmp_path)

    result = _set(ctx, key, value)

    head, _, leaf = key.rpartition(".")
    node: Any = _written(repo)
    for part in head.split("."):
        node = node[part]
    assert node[leaf] == value
    assert result["value"] == value


@pytest.mark.parametrize(
    ("value", "reason"),
    [
        (0, "below minimum 1"),
        (17, "above maximum 16"),
        (-1, "below minimum 1"),
    ],
)
def test_ui_053_an_out_of_range_int_is_refused_and_nothing_is_written(
    tmp_path: Path, value: int, reason: str
) -> None:
    ctx, repo = _ctx(tmp_path)

    with pytest.raises(ValueError, match=f"validation_failed: .*{reason} for {RANGED_INT}"):
        _set(ctx, RANGED_INT, value)
    assert _written(repo) == {}


def test_ui_053_a_float_just_below_its_minimum_is_refused(tmp_path: Path) -> None:
    ctx, _ = _ctx(tmp_path)

    with pytest.raises(ValueError, match="below minimum 1 for"):
        _set(ctx, RANGED_FLOAT, 0.999)


@pytest.mark.parametrize(
    ("key", "value", "reason"),
    [
        (CHOICE, "lax", "not in choices"),
        (MULTICHOICE, ["queue", "nope"], "not in choices"),
        (RANGED_INT, "fast", "cannot coerce 'fast' to int"),
        (RANGED_INT, 2.5, "cannot coerce 2.5 to int"),
        (RANGED_INT, True, "cannot coerce True to int"),
        (RANGED_INT, None, "cannot coerce None to int"),
        (BOOL, 1, "cannot coerce 1 to bool"),
        (BOOL, "maybe", "cannot coerce 'maybe' to bool"),
    ],
)
def test_ui_053_a_wrong_type_or_undeclared_choice_is_refused(
    tmp_path: Path, key: str, value: Any, reason: str
) -> None:
    ctx, repo = _ctx(tmp_path)

    with pytest.raises(ValueError, match=f"validation_failed: .*{reason}"):
        _set(ctx, key, value)
    assert _written(repo) == {}


def test_ui_053_a_value_is_written_in_its_declared_type(tmp_path: Path) -> None:
    """A string a caller sent for an int is stored as the int the registry declares."""
    ctx, repo = _ctx(tmp_path)

    result = _set(ctx, RANGED_INT, "8")

    assert _written(repo) == {"planning": {"max_parallel_waves": 8}}
    assert result["value"] == 8


def test_ui_053_a_refused_value_leaves_the_layer_as_it_was(tmp_path: Path) -> None:
    ctx, repo = _ctx(tmp_path)
    _set(ctx, RANGED_INT, 5)

    with pytest.raises(ValueError, match="above maximum"):
        _set(ctx, RANGED_INT, 20000)
    assert _written(repo) == {"planning": {"max_parallel_waves": 5}}


def test_ui_053_a_leaf_the_registry_does_not_describe_is_written_as_sent(tmp_path: Path) -> None:
    ctx, repo = _ctx(tmp_path)

    _set(ctx, UNREGISTERED, ["a", "b"])

    assert _written(repo) == {"profiles": {"enabled": ["a", "b"]}}


def test_ui_053_the_daemon_holds_the_range_the_stack_draws() -> None:
    """The write gate and the settings stack read one range, not two copies."""
    low, high = leaf_key_lookup(RANGED_INT).value_range or (None, None)
    assert low is not None and high is not None

    assert validate_config_value(RANGED_INT, int(low)) == int(low)
    assert validate_config_value(RANGED_INT, int(high)) == int(high)
    with pytest.raises(UserError):
        validate_config_value(RANGED_INT, int(low) - 1)
    with pytest.raises(UserError):
        validate_config_value(RANGED_INT, int(high) + 1)


# ---------- the daemonless write ----------


def test_ui_053_the_daemonless_write_holds_the_same_range(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The in-process arm a daemonless CLI writes through refuses what the daemon refuses."""
    monkeypatch.setenv("EAWF_DAEMONLESS", "1")
    target = tmp_path / "config.yaml"

    with pytest.raises(UserError, match="above maximum 16"):
        _save_value_to_layer(target_path=target, key=RANGED_INT, value=17)
    assert not target.exists()

    _save_value_to_layer(target_path=target, key=RANGED_INT, value=16)
    assert yaml.safe_load(target.read_text()) == {"planning": {"max_parallel_waves": 16}}
