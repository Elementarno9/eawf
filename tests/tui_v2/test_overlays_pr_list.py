"""Tests for the C06 ``PrListModal`` ``/pr`` overlay (P26-W21 + P26-W44).

Three layers: the pure ``gh pr list --json`` parser (:func:`parse_pr_rows`,
including the tolerant author-login extraction + partial-record skip)
without Textual; the read-only ``gh`` shell-out + 60 s cache
(:func:`fetch_open_prs`, fully mocked at the ``subprocess`` boundary so no
test ever invokes real ``gh`` or hits the network); and Pilot-driven
mounting + row-selection movement of the overlay through the ``/pr``
palette verb + the modal-stack cap.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from rich.text import Text
from textual.widgets import Static

from eawf.tui_v2.app import EaApp
from eawf.tui_v2.screens.overlays import pr_list as pr_list_mod
from eawf.tui_v2.screens.overlays.pr_list import (
    GH_PR_FIELDS,
    GH_TIMEOUT_S,
    PR_CACHE_TTL_S,
    PrFetch,
    PrFetchStatus,
    PrListModal,
    PrRow,
    fetch_open_prs,
    parse_pr_rows,
    reset_pr_cache,
)

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "states" / "valid"
_PHASE_ITER_WAVE = _FIXTURES / "03-phase-iter-wave-active.json"


def _completed(stdout: str = "", returncode: int = 0, stderr: str = "") -> SimpleNamespace:
    """Build a stand-in ``subprocess.run`` result."""
    return SimpleNamespace(stdout=stdout, returncode=returncode, stderr=stderr)


_GH_JSON = json.dumps(
    [
        {
            "number": 12,
            "title": "fix one",
            "author": {"login": "alice"},
            "state": "OPEN",
            "url": "https://example.test/12",
        },
        {
            "number": 13,
            "title": "fix two",
            "author": {"login": "bob"},
            "state": "OPEN",
            "url": "https://example.test/13",
        },
    ]
)


@pytest.fixture(autouse=True)
def _clear_pr_cache() -> Any:
    """Reset the open-PR TTL cache around every test (xdist hygiene)."""
    reset_pr_cache()
    yield
    reset_pr_cache()


def _text(widget: Static) -> str:
    rendered = widget.render()
    return rendered.plain if isinstance(rendered, Text) else str(rendered)


# --------------------------------------------------------------------------
# parse_pr_rows — gh pr list --json decode
# --------------------------------------------------------------------------


def test_parse_pr_rows_decodes_full_record() -> None:
    rows = parse_pr_rows(
        [
            {
                "number": 42,
                "title": "fix the thing",
                "author": {"login": "alice"},
                "state": "OPEN",
                "url": "https://example.test/pr/42",
            }
        ]
    )
    assert rows == (
        PrRow(
            number=42,
            title="fix the thing",
            author="alice",
            state="OPEN",
            url="https://example.test/pr/42",
        ),
    )


def test_parse_pr_rows_empty_input_is_empty() -> None:
    assert parse_pr_rows([]) == ()


def test_parse_pr_rows_skips_record_without_number() -> None:
    rows = parse_pr_rows([{"title": "no number"}, {"number": 7, "title": "ok"}])
    assert [r.number for r in rows] == [7]


def test_parse_pr_rows_skips_non_int_number() -> None:
    rows = parse_pr_rows([{"number": "abc", "title": "bad"}, {"number": 9}])
    assert [r.number for r in rows] == [9]


def test_parse_pr_rows_author_bare_string() -> None:
    rows = parse_pr_rows([{"number": 1, "author": "bob"}])
    assert rows[0].author == "bob"


def test_parse_pr_rows_author_missing_is_empty() -> None:
    rows = parse_pr_rows([{"number": 1, "title": "t"}])
    assert rows[0].author == ""


def test_parse_pr_rows_preserves_input_order() -> None:
    rows = parse_pr_rows([{"number": 3}, {"number": 1}, {"number": 2}])
    assert [r.number for r in rows] == [3, 1, 2]


def test_gh_pr_fields_include_number_and_url() -> None:
    assert "number" in GH_PR_FIELDS
    assert "url" in GH_PR_FIELDS


def test_pr_cache_ttl_is_sixty_seconds() -> None:
    assert PR_CACHE_TTL_S == 60.0


# --------------------------------------------------------------------------
# fetch_open_prs — read-only gh shell-out (subprocess fully mocked)
# --------------------------------------------------------------------------


def test_fetch_open_prs_ok_decodes_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(*args: Any, **kwargs: Any) -> SimpleNamespace:
        return _completed(stdout=_GH_JSON)

    monkeypatch.setattr(subprocess, "run", fake_run)
    fetch = fetch_open_prs(Path("/tmp"), force=True)
    assert fetch.status is PrFetchStatus.OK
    assert [r.number for r in fetch.rows] == [12, 13]
    assert fetch.rows[0].author == "alice"


def test_fetch_open_prs_invokes_expected_argv(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def fake_run(argv: list[str], **kwargs: Any) -> SimpleNamespace:
        seen["argv"] = argv
        seen["timeout"] = kwargs.get("timeout")
        seen["cwd"] = kwargs.get("cwd")
        return _completed(stdout="[]")

    monkeypatch.setattr(subprocess, "run", fake_run)
    fetch_open_prs(Path("/tmp"), force=True)
    assert seen["argv"][:3] == ["gh", "pr", "list"]
    assert "--state" in seen["argv"] and "open" in seen["argv"]
    assert "--json" in seen["argv"]
    json_value = seen["argv"][seen["argv"].index("--json") + 1]
    assert json_value == ",".join(GH_PR_FIELDS)
    assert "--limit" in seen["argv"]
    assert seen["timeout"] == GH_TIMEOUT_S


def test_fetch_open_prs_genuine_empty_is_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _completed(stdout="[]"))
    fetch = fetch_open_prs(Path("/tmp"), force=True)
    assert fetch.status is PrFetchStatus.OK
    assert fetch.rows == ()


def test_fetch_open_prs_gh_missing_is_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(*args: Any, **kwargs: Any) -> SimpleNamespace:
        raise FileNotFoundError("gh")

    monkeypatch.setattr(subprocess, "run", fake_run)
    fetch = fetch_open_prs(Path("/tmp"), force=True)
    assert fetch.status is PrFetchStatus.UNAVAILABLE
    assert fetch.rows == ()


def test_fetch_open_prs_nonzero_is_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: _completed(stdout="", returncode=1, stderr="not authed"),
    )
    fetch = fetch_open_prs(Path("/tmp"), force=True)
    assert fetch.status is PrFetchStatus.UNAVAILABLE
    assert fetch.rows == ()


def test_fetch_open_prs_timeout_is_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(*args: Any, **kwargs: Any) -> SimpleNamespace:
        raise subprocess.TimeoutExpired(cmd="gh", timeout=GH_TIMEOUT_S)

    monkeypatch.setattr(subprocess, "run", fake_run)
    fetch = fetch_open_prs(Path("/tmp"), force=True)
    assert fetch.status is PrFetchStatus.UNAVAILABLE


def test_fetch_open_prs_bad_json_is_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _completed(stdout="not json{"))
    fetch = fetch_open_prs(Path("/tmp"), force=True)
    assert fetch.status is PrFetchStatus.UNAVAILABLE


def test_fetch_open_prs_non_list_payload_is_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _completed(stdout='{"x": 1}'))
    fetch = fetch_open_prs(Path("/tmp"), force=True)
    assert fetch.status is PrFetchStatus.UNAVAILABLE


def test_fetch_open_prs_cache_hit_skips_respawn(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}

    def fake_run(*args: Any, **kwargs: Any) -> SimpleNamespace:
        calls["n"] += 1
        return _completed(stdout=_GH_JSON)

    monkeypatch.setattr(subprocess, "run", fake_run)
    first = fetch_open_prs(Path("/tmp"))
    second = fetch_open_prs(Path("/tmp"))  # within TTL — must reuse the cache
    assert calls["n"] == 1
    assert first is second


def test_fetch_open_prs_force_bypasses_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}

    def fake_run(*args: Any, **kwargs: Any) -> SimpleNamespace:
        calls["n"] += 1
        return _completed(stdout="[]")

    monkeypatch.setattr(subprocess, "run", fake_run)
    fetch_open_prs(Path("/tmp"))
    fetch_open_prs(Path("/tmp"), force=True)
    assert calls["n"] == 2


# --------------------------------------------------------------------------
# PrListModal — mounting + selection (Pilot)
# --------------------------------------------------------------------------


_ROWS = (
    PrRow(12, "fix one", "alice", "OPEN", "https://example.test/12"),
    PrRow(13, "fix two", "bob", "OPEN", "https://example.test/13"),
    PrRow(14, "fix three", "carol", "OPEN", "https://example.test/14"),
)


def test_pr_modal_mounts_rows() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(140, 48)) as pilot:
            await pilot.pause()
            app.push_modal(PrListModal(_ROWS))
            await pilot.pause()
            assert isinstance(app.screen, PrListModal)
            rows = app.screen.query_one("#pr-list").query(".pr-row")
            assert len(rows) == 3
            assert "#12" in _text(app.screen.query_one("#pr-row-0", Static))

    asyncio.run(body())


def test_pr_modal_selection_starts_at_first_row() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(140, 48)) as pilot:
            await pilot.pause()
            app.push_modal(PrListModal(_ROWS))
            await pilot.pause()
            assert app.screen.selected == 0
            assert app.screen.query_one("#pr-row-0", Static).has_class("-selected")

    asyncio.run(body())


def test_pr_modal_down_moves_selection() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(140, 48)) as pilot:
            await pilot.pause()
            app.push_modal(PrListModal(_ROWS))
            await pilot.pause()
            await pilot.press("down")
            await pilot.pause()
            assert app.screen.selected == 1
            assert app.screen.query_one("#pr-row-1", Static).has_class("-selected")

    asyncio.run(body())


def test_pr_modal_selection_clamps_at_bottom() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(140, 48)) as pilot:
            await pilot.pause()
            app.push_modal(PrListModal(_ROWS))
            await pilot.pause()
            for _ in range(5):  # over-scroll past the last row
                await pilot.press("down")
                await pilot.pause()
            assert app.screen.selected == 2

    asyncio.run(body())


def test_pr_modal_empty_ok_shows_no_open_prs() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(140, 48)) as pilot:
            await pilot.pause()
            app.push_modal(PrListModal((), PrFetchStatus.OK))
            await pilot.pause()
            empty = app.screen.query_one("#pr-list").query(".pr-empty")
            assert empty
            assert _text(empty.first(Static)) == "no open pull requests"
            assert app.screen.selected == -1

    asyncio.run(body())


def test_pr_modal_empty_unavailable_shows_gh_hint() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(140, 48)) as pilot:
            await pilot.pause()
            app.push_modal(PrListModal((), PrFetchStatus.UNAVAILABLE))
            await pilot.pause()
            empty = app.screen.query_one("#pr-list").query(".pr-empty")
            assert "gh unavailable" in _text(empty.first(Static))

    asyncio.run(body())


def test_pr_verb_opens_modal_through_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    async def body() -> None:
        from eawf.tui_v2.palette.verbs import VERBS

        # Mock the gh shell-out so the verb path never invokes real gh.
        monkeypatch.setattr(subprocess, "run", lambda *a, **k: _completed(stdout=_GH_JSON))
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(140, 48)) as pilot:
            await pilot.pause()
            handler = next(v.handler for v in VERBS if v.name == "/pr")
            handler(app, "")
            await app.workers.wait_for_complete()  # /pr fetch runs in a worker
            await pilot.pause()
            assert isinstance(app.screen, PrListModal)
            assert app.modal_depth() == 1
            assert len(app.screen.query_one("#pr-list").query(".pr-row")) == 2

    asyncio.run(body())


def test_pr_verb_gh_unavailable_opens_with_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    async def body() -> None:
        from eawf.tui_v2.palette.verbs import VERBS

        def fake_run(*args: Any, **kwargs: Any) -> SimpleNamespace:
            raise FileNotFoundError("gh")

        monkeypatch.setattr(subprocess, "run", fake_run)
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(140, 48)) as pilot:
            await pilot.pause()
            handler = next(v.handler for v in VERBS if v.name == "/pr")
            handler(app, "")
            await app.workers.wait_for_complete()  # /pr fetch runs in a worker
            await pilot.pause()
            assert isinstance(app.screen, PrListModal)
            empty = app.screen.query_one("#pr-list").query(".pr-empty")
            assert "gh unavailable" in _text(empty.first(Static))

    asyncio.run(body())


def test_pr_modal_esc_closes() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(140, 48)) as pilot:
            await pilot.pause()
            app.push_modal(PrListModal(_ROWS))
            await pilot.pause()
            assert app.modal_depth() == 1
            await pilot.press("escape")
            await pilot.pause()
            assert app.modal_depth() == 0

    asyncio.run(body())


def test_pr_modal_open_web_noop_on_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    async def body() -> None:
        gh_calls = {"n": 0}

        def fake_run(argv: list[str], **kwargs: Any) -> SimpleNamespace:
            # Count only gh invocations; GitPane's git calls share this stub.
            if argv and argv[0] == "gh":
                gh_calls["n"] += 1
            return _completed()

        monkeypatch.setattr(subprocess, "run", fake_run)
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(140, 48)) as pilot:
            await pilot.pause()
            app.push_modal(PrListModal((), PrFetchStatus.OK))
            await pilot.pause()
            # Enter on an empty list is a no-op (no crash, no gh spawn).
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, PrListModal)
            assert gh_calls["n"] == 0

    asyncio.run(body())


def test_pr_modal_open_web_spawns_gh_view(monkeypatch: pytest.MonkeyPatch) -> None:
    async def body() -> None:
        gh_argvs: list[list[str]] = []

        def fake_run(argv: list[str], **kwargs: Any) -> SimpleNamespace:
            # Record only gh argv; GitPane's git calls share this stub.
            if argv and argv[0] == "gh":
                gh_argvs.append(argv)
            return _completed()

        monkeypatch.setattr(subprocess, "run", fake_run)
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(140, 48)) as pilot:
            await pilot.pause()
            app.push_modal(PrListModal(_ROWS, PrFetchStatus.OK))
            await pilot.pause()
            await pilot.press("down")  # highlight the second row (#13)
            await pilot.pause()
            await pilot.press("enter")
            await app.workers.wait_for_complete()  # gh pr view --web runs in a worker
            await pilot.pause()
            assert gh_argvs == [["gh", "pr", "view", "--web", "13"]]

    asyncio.run(body())


def test_pr_modal_open_web_graceful_when_gh_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    async def body() -> None:
        def fake_run(*args: Any, **kwargs: Any) -> SimpleNamespace:
            raise FileNotFoundError("gh")

        monkeypatch.setattr(subprocess, "run", fake_run)
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(140, 48)) as pilot:
            await pilot.pause()
            app.push_modal(PrListModal(_ROWS, PrFetchStatus.OK))
            await pilot.pause()
            # A missing gh must not crash the overlay on Enter.
            await pilot.press("enter")
            await app.workers.wait_for_complete()  # gh pr view --web runs in a worker
            await pilot.pause()
            assert isinstance(app.screen, PrListModal)

    asyncio.run(body())


def test_open_pr_list_threads_status() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(140, 48)) as pilot:
            await pilot.pause()
            assert pr_list_mod.open_pr_list(app, (), status=PrFetchStatus.UNAVAILABLE)
            await pilot.pause()
            empty = app.screen.query_one("#pr-list").query(".pr-empty")
            assert "gh unavailable" in _text(empty.first(Static))

    asyncio.run(body())


def test_pr_fetch_is_frozen() -> None:
    fetch = PrFetch(rows=(), status=PrFetchStatus.OK)
    with pytest.raises((AttributeError, TypeError)):
        fetch.status = PrFetchStatus.UNAVAILABLE  # type: ignore[misc]


def test_pr_hint_has_top_margin() -> None:
    # W15 polish: the close-hint gets a top margin so it no longer sits
    # flush against the PR rows (mirrors the DetailModal hint gap).
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(140, 48)) as pilot:
            await pilot.pause()
            app.push_modal(PrListModal(_ROWS))
            await pilot.pause()
            hint = app.screen.query_one(".pr-hint", Static)
            assert hint.styles.margin.top == 1

    asyncio.run(body())
