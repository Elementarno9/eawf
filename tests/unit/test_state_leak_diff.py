"""Unit tests for the old-versus-new state string leak diff.

Every leak fixture is assembled at runtime from split pieces, so this file
carries no literal the leak lints or detect-secrets would flag.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest

from eawf.observability.logging import state_leak
from eawf.observability.logging.state_leak import (
    StateLeakHit,
    StateLeakKind,
    StateStringLeak,
    default_allowed_emails,
    describe_state_leaks,
    diff_state_leaks,
    state_leak_refusal,
)
from eawf.platform.scrub import scan as scrub_scan

MACOS_HOME = "/" + "Users" + "/" + "alice"
WINDOWS_HOME = "C:" + "\\" + "Users" + "\\" + "alice"
LEAKED_EMAIL = "alice" + "@" + "acmecorp.io"
TOKEN = "gh" + "p_" + "A1" * 20
PLACEHOLDER = "/" + "Users" + "/<name>/repo"

_WAVE = "P01-I01-W01"


def _diff(old: object, new: object) -> list[StateStringLeak]:
    return diff_state_leaks(old, new, allowed_emails=default_allowed_emails())


def _state(**wave_fields: Any) -> dict[str, Any]:
    wave: dict[str, Any] = {"id": _WAVE, "title": "ship it", "status": "claimed"}
    wave.update(wave_fields)
    return {
        "updated_at": "2026-09-01T00:00:00Z",
        "waves": {_WAVE: wave, "P01-I01-W02": {"id": "P01-I01-W02", "title": "sibling"}},
        "backlog": None,
    }


def test_diff_state_leaks_reports_new_string_with_field_path() -> None:
    new = _state(outcome=f"worked in {MACOS_HOME}/repo")

    assert _diff(_state(), new) == [
        StateStringLeak(
            field_path=f"waves.{_WAVE}.outcome",
            hit=StateLeakHit(kind=StateLeakKind.HOME_PATH, snippet=MACOS_HOME),
        )
    ]


def test_diff_state_leaks_reports_changed_string() -> None:
    old = _state(outcome="clean")
    new = _state(outcome=f"mail {LEAKED_EMAIL}")

    leaks = _diff(old, new)

    assert [(leak.field_path, leak.hit.kind) for leak in leaks] == [
        (f"waves.{_WAVE}.outcome", StateLeakKind.EMAIL)
    ]


def test_diff_state_leaks_skips_unchanged_preexisting_leak() -> None:
    old = _state(outcome=f"worked in {MACOS_HOME}/repo")
    new = copy.deepcopy(old)
    new["updated_at"] = "2026-09-02T00:00:00Z"
    new["waves"]["P01-I01-W02"]["title"] = "retitled sibling"

    assert _diff(old, new) == []


def test_diff_state_leaks_never_reports_unchanged_placeholder() -> None:
    old = _state(outcome=f"see {PLACEHOLDER}")
    new = copy.deepcopy(old)
    new["waves"][_WAVE]["status"] = "closed"

    assert _diff(old, new) == []


@pytest.mark.parametrize(
    "text",
    [
        "~/.eawf/registry.json",
        "192.168.0.1",
        "http://localhost:8080/health",
        "/private/tmp/scratch",
        "metadata.google.internal",
    ],
)
def test_diff_state_leaks_accepts_legitimate_new_strings(text: str) -> None:
    # The broad artifact scanner flags every one of these, which is why the
    # state write path must not reuse it.
    assert scrub_scan.scan_text(text)

    assert _diff(_state(), _state(outcome=text)) == []


def test_diff_state_leaks_never_calls_scan_text(monkeypatch: pytest.MonkeyPatch) -> None:
    def _forbidden(*args: object, **kwargs: object) -> list[object]:
        raise AssertionError("diff_state_leaks called scan_text")

    monkeypatch.setattr(scrub_scan, "scan_text", _forbidden)
    new = _state(outcome=f"~/.eawf and {MACOS_HOME}/x", notes=["192.168.0.1", TOKEN])

    leaks = _diff(_state(), new)

    assert {leak.hit.kind for leak in leaks} == {StateLeakKind.HOME_PATH, StateLeakKind.TOKEN}
    assert "scan_text" not in vars(state_leak)
    for func in (state_leak.diff_state_leaks, state_leak._collect_string_leaks):
        assert "scan_text" not in func.__code__.co_names


class _UnwalkableDict(dict[str, Any]):
    """A dict that fails the test if the diff iterates it."""

    def items(self) -> Any:
        raise AssertionError("an equal subtree was walked")


def test_diff_state_leaks_skips_equal_subtrees(monkeypatch: pytest.MonkeyPatch) -> None:
    scanned: list[str] = []
    real_scan = state_leak.scan_state_leaks

    def _spy(text: str, *, allowed_emails: frozenset[str]) -> list[StateLeakHit]:
        scanned.append(text)
        return real_scan(text, allowed_emails=allowed_emails)

    monkeypatch.setattr(state_leak, "scan_state_leaks", _spy)
    shared = _UnwalkableDict(outcome=f"{MACOS_HOME}/old", notes=["a", "b"])
    old = {"waves": {"W1": shared, "W2": {"title": "before"}}, "tags": ["x", "y"]}
    new = {"waves": {"W1": shared, "W2": {"title": "after"}}, "tags": ["x", "y"]}

    assert _diff(old, new) == []
    assert scanned == ["after"]


def test_diff_state_leaks_list_insert_does_not_reflag_shifted_items() -> None:
    old = {"notes": [f"{MACOS_HOME}/a"]}
    new = {"notes": ["fresh clean note", f"{MACOS_HOME}/a"]}

    assert _diff(old, new) == []


def test_diff_state_leaks_reports_appended_list_item_by_index() -> None:
    old = {"criteria": [{"text": "clean"}]}
    new = {"criteria": [{"text": "clean"}, {"text": f"token {TOKEN}"}]}

    assert _diff(old, new) == [
        StateStringLeak(
            field_path="criteria[1].text",
            hit=StateLeakHit(kind=StateLeakKind.TOKEN, snippet=TOKEN),
        )
    ]


def test_diff_state_leaks_scans_decoded_windows_path() -> None:
    leaks = _diff({}, {"outcome": f"ran {WINDOWS_HOME}\\repo"})

    assert [leak.hit.kind for leak in leaks] == [StateLeakKind.HOME_PATH]


def test_diff_state_leaks_new_file_scans_every_string() -> None:
    new = {"a": f"{MACOS_HOME}/x", "b": {"c": [LEAKED_EMAIL]}}

    assert [leak.field_path for leak in _diff(None, new)] == ["a", "b.c[0]"]


def test_diff_state_leaks_empty_payloads_report_nothing() -> None:
    assert _diff({}, {}) == []
    assert _diff(None, None) == []


def test_diff_state_leaks_top_level_string() -> None:
    assert [leak.field_path for leak in _diff("clean", f"{MACOS_HOME}/x")] == [""]
    assert _diff(f"{MACOS_HOME}/x", f"{MACOS_HOME}/x") == []


def test_diff_state_leaks_ignores_non_string_values() -> None:
    new = {"count": 3, "flag": True, "ratio": 0.5, "none": None, "raw": f"{MACOS_HOME}/x".encode()}

    assert _diff({}, new) == []


def test_diff_state_leaks_does_not_mutate_inputs() -> None:
    old = _state(outcome="clean", notes=["a"])
    new = _state(outcome=f"{MACOS_HOME}/x", notes=["b", "a"])
    old_before = copy.deepcopy(old)
    new_before = copy.deepcopy(new)

    assert _diff(old, new)
    assert old == old_before
    assert new == new_before


def test_diff_state_leaks_waives_home_path_in_workspace_repo_checkout() -> None:
    old = {"workspace": {"title": "ws", "repos": {}}}
    new = {
        "workspace": {
            "title": f"ws in {MACOS_HOME}/x",
            "repos": {
                "ABC": {"path": f"{MACOS_HOME}/code/abc", "title": "abc"},
                "DEF": {"path": f"{MACOS_HOME}/code/def {LEAKED_EMAIL}", "title": TOKEN},
            },
        },
        "repos": {"ABC": {"path": f"{MACOS_HOME}/code/abc"}},
    }

    assert [(leak.field_path, leak.hit.kind) for leak in _diff(old, new)] == [
        ("workspace.title", StateLeakKind.HOME_PATH),
        ("workspace.repos.DEF.path", StateLeakKind.EMAIL),
        ("workspace.repos.DEF.title", StateLeakKind.TOKEN),
        ("repos.ABC.path", StateLeakKind.HOME_PATH),
    ]


def test_diff_state_leaks_honours_allowed_emails() -> None:
    new = {"outcome": f"mail {LEAKED_EMAIL}"}

    assert diff_state_leaks({}, new, allowed_emails=frozenset({LEAKED_EMAIL})) == []
    assert len(diff_state_leaks({}, new, allowed_emails=frozenset())) == 1


def test_describe_state_leaks_names_each_field_path_once() -> None:
    leaks = _diff(
        {},
        {
            "outcome": f"{MACOS_HOME}/a {MACOS_HOME}/b {LEAKED_EMAIL}",
            "backlog": {"B1": {"title": TOKEN}},
        },
    )

    assert describe_state_leaks(leaks) == (
        "state_leak_refused: outcome (home_path, email); backlog.B1.title (token)"
    )


def test_describe_state_leaks_omits_matched_text() -> None:
    message = describe_state_leaks(_diff({}, {"outcome": f"{MACOS_HOME}/x {TOKEN}"}))

    assert MACOS_HOME not in message
    assert TOKEN not in message


def test_describe_state_leaks_empty_raises() -> None:
    with pytest.raises(ValueError, match="at least one leak"):
        describe_state_leaks([])


def test_state_leak_refusal_names_new_leak() -> None:
    refusal = state_leak_refusal(_state(), _state(outcome=f"{MACOS_HOME}/x"))

    assert refusal == f"state_leak_refused: waves.{_WAVE}.outcome (home_path)"


def test_state_leak_refusal_clean_write_returns_none() -> None:
    old = _state(outcome=f"{MACOS_HOME}/x")
    new = copy.deepcopy(old)
    new["waves"][_WAVE]["title"] = "retitled"

    assert state_leak_refusal(old, new) is None
    assert state_leak_refusal({}, {}) is None


def test_state_leak_refusal_applies_default_allowlist() -> None:
    allowed = sorted(default_allowed_emails())[0]

    assert state_leak_refusal({}, {"outcome": f"co-author {allowed}"}) is None
    assert state_leak_refusal({}, {"outcome": f"mail {LEAKED_EMAIL}"}) == (
        "state_leak_refused: outcome (email)"
    )
