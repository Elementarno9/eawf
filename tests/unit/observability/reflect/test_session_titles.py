"""Titles for history sessions: the structural digest, the contract and the cached fill."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from eawf.observability.reflect import titles as tt
from eawf.observability.reflect.analysis import HistorySession, Metric
from eawf.observability.reflect.generations import refuse_opaque_blob
from eawf.observability.reflect.rates import PricingQuality
from eawf.observability.reflect.session_titles import (
    PROVIDER_ATTEMPTS,
    fill_session_titles,
    history_digest,
    normalize_title,
)
from eawf.observability.reflect.titles import (
    REFLECTION_MARKER,
    TITLE_CALLS_PER_RUN,
    CachedTitle,
    CallBudget,
    ClaudeTitleProvider,
    TitleCache,
    TitleSource,
)

_NOW = datetime(2026, 11, 1, tzinfo=UTC)


def _session(index: int = 1, **fields: Any) -> HistorySession:
    started = datetime(2026, 9, 1, 9, 0, tzinfo=UTC)
    values: dict[str, Any] = {
        "session_ref": f"vsid-{index:032x}",
        "runtime": "claude",
        "project_key": "cwd-a",
        "project_label": "SYN",
        "started_at": started,
        "ended_at": started + timedelta(hours=2),
        "model": "claude-opus-4-7",
        "model_family": "claude-opus",
        "orchestrated": True,
        "metrics": {
            Metric.MODEL_STEPS: 12.0,
            Metric.CHILD_COUNT: 3.0,
            Metric.TOOL_CALLS: 13.0,
            Metric.CALENDAR_MS: 7_200_000.0,
        },
        "cost_usd": Decimal(1),
        "pricing_quality": PricingQuality.VERIFIED,
    }
    values.update(fields)
    return HistorySession.model_validate(values)


class _Provider:
    """A title provider that answers from a script and records what it was asked."""

    name = "scripted"

    def __init__(self, *answers: str | None) -> None:
        self._answers = list(answers)
        self.asked: list[str] = []

    def title_for(self, digest_text: str) -> str | None:
        self.asked.append(digest_text)
        return self._answers.pop(0) if self._answers else "Fallback title"


# ---- the digest -----------------------------------------------------------------------


def test_history_digest_is_stable_for_a_fixed_session() -> None:
    assert history_digest(_session()).key() == history_digest(_session()).key()


def test_history_digest_carries_the_structural_facts() -> None:
    digest = history_digest(_session())

    assert digest.project_label == "SYN"
    assert digest.model_steps == 12
    assert digest.subagents == 3
    assert digest.tool_work == 13
    assert digest.wall_hours == Decimal("2.0")
    assert digest.terminal_observation == "unknown"


def test_history_digest_of_an_unmeasured_session_counts_zero() -> None:
    digest = history_digest(_session(metrics={}))

    assert (digest.model_steps, digest.subagents, digest.tool_work) == (0, 0, 0)
    assert digest.wall_hours is None


def test_a_changed_session_changes_the_digest() -> None:
    moved = _session(metrics={Metric.MODEL_STEPS: 13.0})

    assert history_digest(moved).key() != history_digest(_session()).key()
    assert len(history_digest(moved).key()) == 64


def test_history_digest_never_carries_a_session_ref_or_cost() -> None:
    text = history_digest(_session()).text()

    assert "vsid-" not in text
    assert "cwd-a" not in text
    assert refuse_opaque_blob(text) == text


# ---- the contract ---------------------------------------------------------------------


def test_normalize_title_strips_quotes_and_a_trailing_period() -> None:
    assert normalize_title('"Split the config loader."') == "Split the config loader"


def test_normalize_title_strips_a_title_lead() -> None:
    assert normalize_title("Title: Split the config loader") == "Split the config loader"


def test_normalize_title_keeps_a_title_at_the_limit() -> None:
    assert normalize_title("x" * 72) == "x" * 72


@pytest.mark.parametrize(
    "answer",
    [
        None,
        "",
        "   ",
        "x" * 73,
        "UNKNOWN",
        "unknown",
        "ab",
        "I cannot write a title for this",
        "Sorry",
    ],
)
def test_normalize_title_rejects_what_is_not_a_title(answer: str | None) -> None:
    assert normalize_title(answer) is None


def test_normalize_title_scrubs_an_email_address() -> None:
    title = normalize_title("Mail someone@example.com the summary")

    assert title is not None
    assert "someone@example.com" not in title


# ---- the fill -------------------------------------------------------------------------


def test_fill_answers_a_cached_digest_without_calling_out() -> None:
    session = _session()
    cache = TitleCache(
        entries={history_digest(session).key(): CachedTitle(title="Ship it", stored_at=_NOW)}
    )
    provider = _Provider()

    titles, account, _cache = fill_session_titles(
        [session], cache=cache, provider=provider, local_only=False, now=_NOW
    )

    assert titles[session.session_ref].title == "Ship it"
    assert titles[session.session_ref].source is TitleSource.MODEL_WRITTEN
    assert provider.asked == []
    assert account.cache_hits == 1


def test_fill_stores_a_new_answer_under_its_digest() -> None:
    session = _session()
    provider = _Provider("Split the config loader")

    titles, account, cache = fill_session_titles(
        [session], cache=TitleCache(), provider=provider, local_only=False, now=_NOW
    )

    assert titles[session.session_ref].title == "Split the config loader"
    assert cache.entries[history_digest(session).key()].title == "Split the config loader"
    assert provider.asked == [history_digest(session).text()]
    assert account.digests_sent == 1


def test_fill_asks_once_more_after_a_rejected_answer() -> None:
    provider = _Provider("z" * 200, "Split the config loader")

    titles, account, _cache = fill_session_titles(
        [_session()], cache=TitleCache(), provider=provider, local_only=False, now=_NOW
    )

    assert next(iter(titles.values())).title == "Split the config loader"
    assert account.digests_sent == PROVIDER_ATTEMPTS


def test_fill_leaves_a_twice_rejected_digest_uncached() -> None:
    session = _session()
    provider = _Provider("x" * 200, "y" * 200, "never asked")

    titles, _account, cache = fill_session_titles(
        [session], cache=TitleCache(), provider=provider, local_only=False, now=_NOW
    )

    assert len(provider.asked) == PROVIDER_ATTEMPTS
    assert cache.entries == {}
    assert titles[session.session_ref].source is TitleSource.STRUCTURAL
    assert (
        titles[session.session_ref].title == "Review the SYN claude session of 2026-09-01, 12 steps"
    )


def test_fill_leaves_an_unknown_answer_uncached_without_retrying() -> None:
    provider = _Provider("UNKNOWN")

    _titles, account, cache = fill_session_titles(
        [_session()], cache=TitleCache(), provider=provider, local_only=False, now=_NOW
    )

    assert len(provider.asked) == 1
    assert account.unknown_uncached == 1
    assert cache.entries == {}


def test_fill_local_only_falls_back_to_the_structural_title() -> None:
    titles, account, cache = fill_session_titles(
        [_session()], cache=TitleCache(), provider=None, local_only=True, now=_NOW
    )

    (title,) = titles.values()
    assert title.source is TitleSource.STRUCTURAL
    assert account.local_only is True
    assert account.digests_sent == 0
    assert cache.entries == {}


def test_fill_without_a_start_time_is_unavailable() -> None:
    session = _session(started_at=None, ended_at=None)

    titles, account, _cache = fill_session_titles(
        [session], cache=TitleCache(), provider=None, local_only=True, now=_NOW
    )

    assert titles[session.session_ref].title is None
    assert titles[session.session_ref].source is TitleSource.UNAVAILABLE
    assert account.by_source[TitleSource.UNAVAILABLE.value] == 1


def test_fill_of_nothing_is_empty() -> None:
    titles, account, cache = fill_session_titles(
        [], cache=TitleCache(), provider=None, local_only=True, now=_NOW
    )

    assert titles == {}
    assert account.digests_sent == 0
    assert cache.entries == {}


# ---- the run's call budget -------------------------------------------------------------


def _distinct(count: int) -> list[HistorySession]:
    return [
        _session(index, metrics={Metric.MODEL_STEPS: float(index)}) for index in range(1, count + 1)
    ]


def test_fill_stops_calling_out_once_the_run_budget_is_spent() -> None:
    sessions = _distinct(5)
    budget = CallBudget(limit=3)
    provider = _Provider()

    titles, account, cache = fill_session_titles(
        sessions, cache=TitleCache(), provider=provider, local_only=False, now=_NOW, budget=budget
    )

    assert len(provider.asked) == account.digests_sent == budget.used == 3
    assert account.digests_skipped == 2
    assert len(cache.entries) == 3
    sources = [titles[session.session_ref].source for session in sessions]
    assert sources == [TitleSource.MODEL_WRITTEN] * 3 + [TitleSource.STRUCTURAL] * 2


def test_fill_budget_is_shared_across_fills_of_one_run() -> None:
    budget = CallBudget(limit=4)
    first, second = _distinct(6)[:3], _distinct(6)[3:]

    _titles, before, _cache = fill_session_titles(
        first, cache=TitleCache(), provider=_Provider(), local_only=False, now=_NOW, budget=budget
    )
    _titles, after, _cache = fill_session_titles(
        second, cache=TitleCache(), provider=_Provider(), local_only=False, now=_NOW, budget=budget
    )

    assert (before.digests_sent, before.digests_skipped) == (3, 0)
    assert (after.digests_sent, after.digests_skipped) == (1, 2)


def test_fill_with_an_empty_budget_calls_nothing() -> None:
    provider = _Provider()

    _titles, account, _cache = fill_session_titles(
        _distinct(2),
        cache=TitleCache(),
        provider=provider,
        local_only=False,
        now=_NOW,
        budget=CallBudget(limit=0),
    )

    assert provider.asked == []
    assert (account.digests_sent, account.digests_skipped) == (0, 2)


def test_fill_retry_spends_the_budget_and_stops_when_it_runs_out() -> None:
    provider = _Provider("z" * 200, "Split the config loader")

    titles, account, _cache = fill_session_titles(
        [_session()],
        cache=TitleCache(),
        provider=provider,
        local_only=False,
        now=_NOW,
        budget=CallBudget(limit=1),
    )

    assert len(provider.asked) == 1
    assert (account.digests_sent, account.digests_skipped) == (1, 0)
    assert next(iter(titles.values())).source is TitleSource.STRUCTURAL


def test_call_budget_defaults_to_the_stated_cap_and_refuses_a_negative_one() -> None:
    assert CallBudget().limit == TITLE_CALLS_PER_RUN == 25
    with pytest.raises(ValueError, match="negative"):
        CallBudget(limit=-1)


def test_claude_provider_runs_in_a_private_directory_outside_every_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "repo"
    (project / ".ea").mkdir(parents=True)
    monkeypatch.chdir(project)
    seen: dict[str, Any] = {}

    def run(argv: list[str], **kwargs: Any) -> SimpleNamespace:
        workdir = Path(kwargs["cwd"])
        seen["cwd"] = workdir
        seen["is_dir"] = workdir.is_dir()
        seen["mode"] = workdir.stat().st_mode & 0o777
        seen["prompt"] = argv[argv.index("-p") + 1]
        return SimpleNamespace(returncode=0, stdout="Tidy the loader\n", stderr="")

    monkeypatch.setattr(tt.subprocess, "run", run)

    answer = ClaudeTitleProvider(binary="claude").title_for("facts")

    assert answer == "Tidy the loader\n"
    assert seen["is_dir"] and seen["mode"] == 0o700
    assert not seen["cwd"].is_relative_to(project)
    assert not any((parent / ".ea").exists() for parent in (seen["cwd"], *seen["cwd"].parents))
    assert not seen["cwd"].exists()
    assert seen["prompt"].startswith(REFLECTION_MARKER)
