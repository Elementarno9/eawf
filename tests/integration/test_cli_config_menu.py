"""Integration tests for ``eawf config menu`` (P20-W10).

The menu drives :mod:`questionary` for tab → field → value selection, then
flushes the coerced value through :func:`eawf.cli.commands.config._save_value_to_layer`.
These tests stub the questionary widgets (the project does not exercise
real TTY interactions in CI) and patch the save helper so the cases are
small, deterministic, and cover both the happy path and the cancellation /
invalid-input branches.

Covers:

- ``eawf config --help`` advertises the ``menu`` subcommand.
- ``menu`` accepts the same ``--scope`` writable layers as ``config set``.
- Built-in / non-writable scopes reject with exit code ``3``.
- A happy-path menu run calls ``_save_value_to_layer`` exactly once with
  the typed value matching the registry entry.
- Operator cancellation (questionary returns ``None``) maps to exit code
  ``USER_DECLINED`` per the canonical CLI taxonomy.
- An invalid out-of-range answer surfaces ``InvalidInput`` (exit 3) and
  the save helper is never called.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from eawf.cli.app import app
from eawf.cli.commands import config as config_cmd
from eawf.cli.exit_codes import INVALID_INPUT, USER_DECLINED
from eawf.kernel.config import layered, registry

runner = CliRunner()


@pytest.fixture
def repo_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Isolated repo root + sandboxed global-config path."""
    repo = tmp_path / "repo"
    (repo / ".ea").mkdir(parents=True)
    fake_global = tmp_path / "global.yaml"
    monkeypatch.setattr(layered, "global_config_path", lambda: fake_global)
    monkeypatch.chdir(repo)
    yield repo


# --- CLI surface checks ------------------------------------------------------


def test_config_help_advertises_menu_subcommand(repo_root: Path) -> None:
    """``eawf config --help`` must list the ``menu`` verb (success criterion)."""
    result = runner.invoke(app, ["config", "--help"])
    assert result.exit_code == 0, result.output
    assert "menu" in result.output


def test_menu_help_text_mentions_interactive_questionary(repo_root: Path) -> None:
    """The menu's help text surfaces its interactive nature so operators know."""
    result = runner.invoke(app, ["config", "menu", "--help"])
    assert result.exit_code == 0, result.output
    assert "questionary" in result.output.lower() or "interactive" in result.output.lower()


def test_menu_rejects_built_in_scope(repo_root: Path) -> None:
    """Built-in is read-only; the menu must mirror ``config set``'s rejection."""
    result = runner.invoke(app, ["config", "menu", "--scope", "built-in"])
    assert result.exit_code == INVALID_INPUT, result.output


def test_menu_rejects_unknown_scope(repo_root: Path) -> None:
    result = runner.invoke(app, ["config", "menu", "--scope", "moonbase"])
    assert result.exit_code == INVALID_INPUT, result.output


# --- happy-path menu run with questionary stubs ------------------------------


class _FakeQuestion:
    """Minimal stand-in for the ``questionary.<widget>(...).ask()`` chain."""

    def __init__(self, answer: Any) -> None:
        self._answer = answer

    def ask(self) -> Any:
        return self._answer


def _patch_questionary(
    monkeypatch: pytest.MonkeyPatch,
    *,
    select_answers: list[Any],
    confirm_answer: Any = None,
    text_answer: Any = None,
    checkbox_answer: Any = None,
) -> dict[str, list[Any]]:
    """Replace each questionary widget so the menu reads stubbed answers.

    Returns a dict of recorded call args so the test can introspect what the
    menu actually asked for (which choices were rendered, in what order).
    """
    calls: dict[str, list[Any]] = {
        "select": [],
        "confirm": [],
        "text": [],
        "checkbox": [],
    }
    select_iter = iter(select_answers)

    def fake_select(*args: Any, **kwargs: Any) -> _FakeQuestion:
        calls["select"].append({"args": args, "kwargs": kwargs})
        return _FakeQuestion(next(select_iter))

    def fake_confirm(*args: Any, **kwargs: Any) -> _FakeQuestion:
        calls["confirm"].append({"args": args, "kwargs": kwargs})
        return _FakeQuestion(confirm_answer)

    def fake_text(*args: Any, **kwargs: Any) -> _FakeQuestion:
        calls["text"].append({"args": args, "kwargs": kwargs})
        return _FakeQuestion(text_answer)

    def fake_checkbox(*args: Any, **kwargs: Any) -> _FakeQuestion:
        calls["checkbox"].append({"args": args, "kwargs": kwargs})
        return _FakeQuestion(checkbox_answer)

    import questionary

    monkeypatch.setattr(questionary, "select", fake_select)
    monkeypatch.setattr(questionary, "confirm", fake_confirm)
    monkeypatch.setattr(questionary, "text", fake_text)
    monkeypatch.setattr(questionary, "checkbox", fake_checkbox)
    return calls


def _capture_save_calls(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Patch the layered-config save helper and record every invocation."""
    recorded: list[dict[str, Any]] = []

    def fake_save(
        *,
        target_path: Path,
        key: str,
        value: Any,
        repo_root: Path | None = None,
    ) -> None:
        recorded.append(
            {
                "target_path": target_path,
                "key": key,
                "value": value,
                "repo_root": repo_root,
            }
        )

    monkeypatch.setattr(config_cmd, "_save_value_to_layer", fake_save)
    return recorded


def test_menu_happy_path_saves_via_layer_helper(
    repo_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end: pick a choice key, accept the offered choice, save lands.

    Verifies the save flushes via ``_save_value_to_layer`` — the shared
    mutator path also used by ``eawf config set``. The test stubs that
    helper so the assertion is on contract, not on disk.
    """
    # The first registered choice-typed entry — picked so the test does not
    # bind to a specific tab/key.
    choice_entry = next(e for e in registry.CONFIG_REGISTRY if e.type == "choice")
    assert choice_entry.choices is not None
    answer = choice_entry.choices[-1]

    _patch_questionary(
        monkeypatch,
        select_answers=[choice_entry.tab, f"{choice_entry.key} — {choice_entry.label}", answer],
    )
    recorded = _capture_save_calls(monkeypatch)

    result = runner.invoke(app, ["config", "menu"])
    assert result.exit_code == 0, result.output
    assert len(recorded) == 1
    assert recorded[0]["key"] == choice_entry.key
    assert recorded[0]["value"] == answer


def test_menu_happy_path_bool_uses_confirm_widget(
    repo_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bool entry routes through ``questionary.confirm``."""
    bool_entry = next(e for e in registry.CONFIG_REGISTRY if e.type == "bool")
    _patch_questionary(
        monkeypatch,
        select_answers=[bool_entry.tab, f"{bool_entry.key} — {bool_entry.label}"],
        confirm_answer=True,
    )
    recorded = _capture_save_calls(monkeypatch)

    result = runner.invoke(app, ["config", "menu"])
    assert result.exit_code == 0, result.output
    assert recorded[0]["key"] == bool_entry.key
    assert recorded[0]["value"] is True


def test_menu_happy_path_int_coerces_text_answer(
    repo_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An int entry pulls a text widget; the coerced value is the stored int."""
    int_entry = next(
        e
        for e in registry.CONFIG_REGISTRY
        if e.type == "int" and e.min_value is not None and e.max_value is not None
    )
    # Pick a midpoint that satisfies the range so coercion succeeds.
    midpoint = int((int_entry.min_value + int_entry.max_value) / 2)  # type: ignore[operator]
    _patch_questionary(
        monkeypatch,
        select_answers=[int_entry.tab, f"{int_entry.key} — {int_entry.label}"],
        text_answer=str(midpoint),
    )
    recorded = _capture_save_calls(monkeypatch)

    result = runner.invoke(app, ["config", "menu"])
    assert result.exit_code == 0, result.output
    assert recorded[0]["key"] == int_entry.key
    assert recorded[0]["value"] == midpoint
    assert isinstance(recorded[0]["value"], int)


# --- order invariants surfaced in the questionary widgets ---------------------


def test_menu_tabs_widget_lists_tabs_alphabetical(
    repo_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The first ``select`` widget exposes tabs in alphabetical order."""
    bool_entry = next(e for e in registry.CONFIG_REGISTRY if e.type == "bool")
    calls = _patch_questionary(
        monkeypatch,
        select_answers=[bool_entry.tab, f"{bool_entry.key} — {bool_entry.label}"],
        confirm_answer=False,
    )
    _capture_save_calls(monkeypatch)

    result = runner.invoke(app, ["config", "menu"])
    assert result.exit_code == 0, result.output

    first_select_kwargs = calls["select"][0]["kwargs"]
    tabs_rendered = list(first_select_kwargs["choices"])
    assert tabs_rendered == sorted(tabs_rendered)


def test_menu_fields_widget_lists_keys_alphabetical(
    repo_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The second ``select`` widget exposes keys alphabetical within the tab."""
    bool_entry = next(e for e in registry.CONFIG_REGISTRY if e.type == "bool")
    calls = _patch_questionary(
        monkeypatch,
        select_answers=[bool_entry.tab, f"{bool_entry.key} — {bool_entry.label}"],
        confirm_answer=False,
    )
    _capture_save_calls(monkeypatch)

    result = runner.invoke(app, ["config", "menu"])
    assert result.exit_code == 0, result.output

    # The second select widget receives the keys-for-tab.
    second_select_kwargs = calls["select"][1]["kwargs"]
    rendered_choices = list(second_select_kwargs["choices"])
    # Each rendered choice is "<key> — <label>"; sort key is the prefix
    # before " — ", i.e. the dotted key itself.
    rendered_keys = [choice.split(" — ", 1)[0] for choice in rendered_choices]
    assert rendered_keys == sorted(rendered_keys)


# --- failure / cancellation branches ----------------------------------------


def test_menu_cancellation_at_tab_step_returns_user_declined(
    repo_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ctrl-C / Esc at the tab picker maps to ``USER_DECLINED``."""
    _patch_questionary(monkeypatch, select_answers=[None])
    recorded = _capture_save_calls(monkeypatch)

    result = runner.invoke(app, ["config", "menu"])
    assert result.exit_code == USER_DECLINED, result.output
    assert recorded == []  # no save side-effect on cancellation


def test_menu_cancellation_at_value_step_returns_user_declined(
    repo_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ctrl-C at the value prompt is also ``USER_DECLINED``."""
    bool_entry = next(e for e in registry.CONFIG_REGISTRY if e.type == "bool")
    _patch_questionary(
        monkeypatch,
        select_answers=[bool_entry.tab, f"{bool_entry.key} — {bool_entry.label}"],
        confirm_answer=None,
    )
    recorded = _capture_save_calls(monkeypatch)

    result = runner.invoke(app, ["config", "menu"])
    assert result.exit_code == USER_DECLINED, result.output
    assert recorded == []


def test_menu_invalid_int_input_rejects_with_exit_3(
    repo_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An int that violates the registered range fails with InvalidInput."""
    int_entry = next(
        e for e in registry.CONFIG_REGISTRY if e.type == "int" and e.max_value is not None
    )
    out_of_range = str(int(int_entry.max_value) + 100)  # type: ignore[arg-type]
    _patch_questionary(
        monkeypatch,
        select_answers=[int_entry.tab, f"{int_entry.key} — {int_entry.label}"],
        text_answer=out_of_range,
    )
    recorded = _capture_save_calls(monkeypatch)

    result = runner.invoke(app, ["config", "menu"])
    assert result.exit_code == INVALID_INPUT, result.output
    assert recorded == []
