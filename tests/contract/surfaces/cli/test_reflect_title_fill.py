"""SURF-171: ``eawf reflect run`` fills session titles by default, from a digest only.

- Default on: every uncached session's digest goes to the provider and the answer is
  cached under the digest's SHA-256, never under or beside the digest text.
- ``--local-only``: no provider call, no connection, no spawned session; every title
  resolves through the fallback chain and the report says the fill was disabled.
- A filled key is answered from disk, so a second render is byte-identical.
- ``UNKNOWN`` is left uncached and the title falls back.
- The helper session opens with the reflection marker and carries the digest alone.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from eawf.observability.reflect import titles as tt
from eawf.observability.reflect.report import TITLE_CACHE_FILENAME, reflect_root
from eawf.observability.reflect.runs import read_tree_runs
from eawf.surfaces.cli.app import app
from tests.contract.surfaces.cli._reflect_tree import (
    command,
    native_tree,
    refuse_egress,
    run_row,
    summarized,
)

__all__ = ["refuse_egress"]

runner = CliRunner()

#: A transcript line that must never reach the provider or the cache.
REQUEST_TEXT = "please refactor the loader under the private customer folder"


class StubProvider:
    """A provider answering from a script, recording every digest it was sent."""

    def __init__(self, answer: str | None = "Refactor the loader module") -> None:
        self.answer = answer
        self.sent: list[str] = []

    @property
    def name(self) -> str:
        return "stub"

    def title_for(self, digest_text: str) -> str | None:
        self.sent.append(digest_text)
        return self.answer


@pytest.fixture
def tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Return a canary with two finished Runs, one carrying request-like text."""
    monkeypatch.delenv("EA_STATE", raising=False)
    return native_tree(
        tmp_path,
        [run_row("RUN-00000001"), run_row("RUN-00000002", hours=1)],
        [summarized("RUN-00000001", 1, REQUEST_TEXT), command("RUN-00000001", 2)],
    )


def _run(tree: Path, *args: str) -> str:
    result = runner.invoke(app, ["-w", str(tree.parent), "reflect", "run", *args])
    assert result.exit_code == 0, result.output
    return result.output


def _report(tree: Path) -> bytes:
    return next(reflect_root(tree).glob("*-reflect-report.txt")).read_bytes()


def _cache(tree: Path) -> dict[str, object]:
    raw = json.loads((reflect_root(tree) / TITLE_CACHE_FILENAME).read_text())
    assert isinstance(raw, dict)
    return raw


def test_surf_171_default_on_sends_only_the_digest_and_caches_by_hash(
    tree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without a flag the provider is asked, once per session, with the digest alone."""
    stub = StubProvider()
    monkeypatch.setattr(tt.ClaudeTitleProvider, "discover", classmethod(lambda cls: stub))
    output = _run(tree)
    assert len(stub.sent) == 2
    for text in stub.sent:
        assert REQUEST_TEXT not in text
        assert set(json.loads(text)) == set(tt.SessionDigest.model_fields)
    entries = _cache(tree)["entries"]
    assert isinstance(entries, dict)
    assert set(entries) == {tt.SessionDigest.model_validate_json(t).key() for t in stub.sent}
    cache_text = (reflect_root(tree) / TITLE_CACHE_FILENAME).read_text()
    assert all(text not in cache_text for text in stub.sent)
    assert '"Refactor the loader module" [model_written]' in output
    assert "title fill: enabled" in output


def test_surf_171_local_only_makes_no_egress_and_says_the_fill_was_off(
    tree: Path, refuse_egress: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Under --local-only nothing connects, nothing spawns, and titles fall back."""

    def discover(cls: type) -> None:
        raise AssertionError("--local-only must not look for a provider")

    monkeypatch.setattr(tt.ClaudeTitleProvider, "discover", classmethod(discover))
    output = _run(tree, "--local-only")
    assert refuse_egress == []
    assert "title fill: disabled (--local-only)" in output
    assert "[structural]" in output
    assert "[model_written]" not in output
    manifest = json.loads(next(reflect_root(tree).glob("*-reflect-manifest.json")).read_text())
    assert manifest["local_only"] is True
    assert manifest["digests_sent"] == 0
    assert sum(manifest["titles_by_source"].values()) == manifest["sessions_listed"] == 2


def test_surf_171_a_cache_hit_renders_byte_identically_without_calling_out(
    tree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The second render answers every key from disk and matches the first byte for byte."""
    first_stub = StubProvider()
    monkeypatch.setattr(tt.ClaudeTitleProvider, "discover", classmethod(lambda cls: first_stub))
    _run(tree)
    first = _report(tree)
    second_stub = StubProvider(answer="A different answer")
    monkeypatch.setattr(tt.ClaudeTitleProvider, "discover", classmethod(lambda cls: second_stub))
    _run(tree)
    assert second_stub.sent == []
    assert _report(tree) == first


def test_surf_171_unknown_is_left_uncached_and_the_title_falls_back(
    tree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An UNKNOWN answer stores nothing, counts as unknown and resolves structurally."""
    stub = StubProvider(answer="UNKNOWN\n")
    monkeypatch.setattr(tt.ClaudeTitleProvider, "discover", classmethod(lambda cls: stub))
    output = _run(tree)
    assert _cache(tree)["entries"] == {}
    assert "[structural]" in output
    manifest = json.loads(next(reflect_root(tree).glob("*-reflect-manifest.json")).read_text())
    assert manifest["unknown_uncached"] == 2


def test_surf_171_the_helper_session_is_named_with_the_reflection_marker() -> None:
    """The spawned prompt opens with the marker and carries no text but the digest."""
    digest = tt.SessionDigest(
        project_label="PRJ-RFL",
        runtime="claude-code",
        model_family="unknown",
        terminal_observation="COMPLETED",
        model_steps=1,
        subagents=0,
        tool_work=1,
        wall_hours=None,
    )
    argv = tt.ClaudeTitleProvider(binary="claude").argv(digest.text())
    prompt = argv[argv.index("-p") + 1]
    assert prompt.startswith(tt.REFLECTION_MARKER)
    assert prompt == f"{tt.REFLECTION_MARKER} {tt.TITLE_INSTRUCTION}\n{digest.text()}"


def test_surf_171_an_unavailable_provider_falls_back_per_title(tree: Path) -> None:
    """With the fill on but no provider found, every title still resolves and says how."""
    titles, account, _ = tt.fill_titles(
        read_tree_runs(tree),
        cache=tt.TitleCache(),
        provider=None,
        local_only=False,
        now=datetime(2026, 9, 29, tzinfo=UTC),
    )
    assert account.provider is None
    assert [item.source for item in titles] == [tt.TitleSource.STRUCTURAL] * 2


def test_surf_171_a_run_never_started_has_no_title_it_could_carry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Boundary: a queued Run supports no structural title, so it is unavailable."""
    monkeypatch.delenv("EA_STATE", raising=False)
    tree = native_tree(tmp_path, [run_row("RUN-00000003", status="QUEUED")], [])
    titles, _, _ = tt.fill_titles(
        read_tree_runs(tree),
        cache=tt.TitleCache(),
        provider=None,
        local_only=True,
        now=datetime(2026, 9, 29, tzinfo=UTC),
    )
    assert titles[0].source is tt.TitleSource.UNAVAILABLE
    assert titles[0].title is None


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        (None, None),
        ("", None),
        ("UNKNOWN", None),
        ("Fix the loader.\nmore text", "Fix the loader"),
        ("x" * 72, "x" * 72),
        ("word " * 20, ("word " * 14).strip()),
        ("Tidy ~/secret/place", "Tidy <local-path>"),
    ],
)
def test_surf_171_a_provider_answer_becomes_a_persistable_title(
    answer: str | None, expected: str | None
) -> None:
    """Empty, UNKNOWN, trailing period, the 72-character limit and the scrub."""
    assert tt.model_title(answer) == expected


@pytest.mark.parametrize(
    "entries",
    [
        {"not-a-hash": {"title": "Fix it", "stored_at": "2026-09-29T00:00:00Z"}},
        {"a" * 64: {"title": "UNKNOWN", "stored_at": "2026-09-29T00:00:00Z"}},
        {"a" * 64: {"title": "Ends with a period.", "stored_at": "2026-09-29T00:00:00Z"}},
        {"a" * 64: {"title": "x" * 73, "stored_at": "2026-09-29T00:00:00Z"}},
    ],
)
def test_surf_171_the_cache_refuses_a_keyless_or_placeholder_title(
    tmp_path: Path, entries: dict[str, object]
) -> None:
    """A cached title with no digest key, an UNKNOWN, or a malformed title fails load."""
    path = tmp_path / "cache.json"
    path.write_text(json.dumps({"schema_version": 1, "entries": entries}))
    with pytest.raises(ValidationError):
        tt.load_title_cache(path)
