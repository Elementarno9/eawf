"""A Batch frame draws the review and checks of the pull request open for its branch.

The Batch states the branch it integrates into; the daemon's repository read looks the pull
request up for that branch, and the frame draws its review and checks. A Batch that names
no branch yet, a pull request the host says is not open, and a read that failed each say
why in words. The branch itself is drawn as the Batch's base.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

from eawf.kernel.projection.compute import RouteProjection, build_route_projection
from eawf.kernel.projection.spine import build_spine_view
from eawf.runtime.vcs.repository_read import PullRequestRead, RepositoryAnswer
from eawf.surfaces.tui.console.chrome import load_chrome
from eawf.surfaces.tui.console.fixture import Fixture
from eawf.surfaces.tui.console.frame import View
from eawf.surfaces.tui.console.live_reads import (
    BRANCH_READS_MAX,
    BRANCH_REVIEWS_READ,
    LIVE_READS,
    REPOSITORY_READ,
    HeldBranchReviews,
)
from eawf.surfaces.tui.console.renderers import render_route
from eawf.surfaces.tui.console.renderers.batch_detail import PAST_CAP, REGISTER_UNREAD
from eawf.surfaces.tui.console.session import SIZES, Session

from . import test_native_route_frames as nrf
from .test_spine_frames import _projection

ROUTE = "batch.detail"
BRANCH = "feature/eawf-v0.7-rc1"


def _document(**batch: Any) -> dict[str, Any]:
    document = {key: dict(rows) for key, rows in nrf.DOCUMENT.items()}
    document["batch"] = {
        "BAT-0101": nrf._row(
            "batch", "BAT-0101", "ACTIVE", milestone_ref=f"{nrf.ROOT}/milestone/MLS-0100", **batch
        )
    }
    return document


def _register_document(branches: list[str | None]) -> dict[str, Any]:
    """Return a tree whose Batches integrate into ``branches``, one Batch per entry."""
    document = {key: dict(rows) for key, rows in nrf.DOCUMENT.items()}
    document["batch"] = {
        f"BAT-{index:04d}": nrf._row(
            "batch",
            f"BAT-{index:04d}",
            "ACTIVE",
            milestone_ref=f"{nrf.ROOT}/milestone/MLS-0100",
            **({} if branch is None else {"target_branch": branch}),
        )
        for index, branch in enumerate(branches, start=1)
    }
    return document


def _frame(repository: RepositoryAnswer | None, **batch: Any) -> list[str]:
    session = Session()
    session.route, session.subj_id = ROUTE, "BAT-0101"
    view = View(
        session=session,
        fixture=Fixture.from_chrome(load_chrome()),
        w=120,
        h=dict(SIZES)[120],
        projection=build_spine_view(_projection(ROUTE, _document(**batch))),
        live={} if repository is None else {REPOSITORY_READ: repository},
    )
    return render_route(view)


def _row(frame: list[str], name: str) -> str:
    return next(row for row in frame if row.startswith(f" {name}"))


OPEN = RepositoryAnswer(
    pull_request=PullRequestRead.model_validate(
        {
            "number": 418,
            "state": "OPEN",
            "url": "https://example.invalid/pr/418",
            "review_decision": "APPROVED",
            "approvals": 1,
            "checks": [
                {"name": "tests", "outcome": "pass"},
                {"name": "lint", "outcome": "fail"},
            ],
        }
    ),
    pull_request_branch=BRANCH,
)


def test_the_review_and_checks_are_the_pull_request_of_the_batch_branch() -> None:
    frame = _frame(OPEN, target_branch=BRANCH)
    assert "PR #418 open · approved · 1 approval" in _row(frame, "REVIEW")
    checks = _row(frame, "CHECKS")
    assert "1 pass" in checks and "1 fail" in checks
    assert BRANCH in _row(frame, "BASE")


def test_a_batch_naming_no_branch_says_why_no_pull_request_is_read() -> None:
    frame = _frame(None)
    assert "names no target branch" in _row(frame, "REVIEW")
    assert "names no target branch" in _row(frame, "CHECKS")
    assert "no target branch is chosen" in _row(frame, "BASE")


def test_no_open_pull_request_and_an_unread_one_say_so_in_words() -> None:
    none = _frame(RepositoryAnswer(pull_request_branch=BRANCH), target_branch=BRANCH)
    assert f"no pull request is open for {BRANCH}" in _row(none, "REVIEW")
    unread = _frame(
        RepositoryAnswer(pull_request_unread="gh is not logged in", pull_request_branch=BRANCH),
        target_branch=BRANCH,
    )
    assert "gh is not logged in" in _row(unread, "REVIEW")


def test_before_the_read_arrives_the_rows_say_it_is_not_read_yet() -> None:
    assert "not read yet" in _row(_frame(None, target_branch=BRANCH), "REVIEW")


class _Host:
    """A live-read host on one route, opened on ``subject``, recording what it asks."""

    operator = None
    history_cursor = None

    def __init__(self, route: str, subject: str | None, projection: RouteProjection) -> None:
        self.route, self.subject, self._projection = route, subject, projection
        self.asked: list[dict[str, Any]] = []

    def projection_for(self, route: str) -> RouteProjection | None:
        return self._projection if route == self.route else None

    def live(self, name: str, *, anywhere: bool = False) -> None:
        return None

    async def call(self, method: str, params: Mapping[str, Any]) -> dict[str, Any]:
        self.asked.append(dict(params))
        return {}

    def now(self) -> None:
        return None


def _host(route: str, subject: str | None, **batch: Any) -> _Host:
    projection = build_route_projection(
        route=route,
        document=_document(**batch),
        cursor=nrf.CURSOR,
        scope_id=nrf.SCOPE,
        generated_at=nrf.AT,
    )
    return _Host(route, subject, projection)


def test_the_repository_read_asks_for_the_batch_branch_on_both_batch_surfaces() -> None:
    read = LIVE_READS[REPOSITORY_READ]
    for route in (ROUTE, "git.pr"):
        host = _host(route, "BAT-0101", target_branch=BRANCH)
        address = read.address(host)  # type: ignore[arg-type]
        assert address is not None and BRANCH in address
        asyncio.run(read.fetch(host, address))  # type: ignore[arg-type]
        assert host.asked == [{"branch": BRANCH}]


def test_a_batch_frame_with_no_branch_or_no_batch_owes_no_repository_read() -> None:
    read = LIVE_READS[REPOSITORY_READ]
    assert read.address(_host(ROUTE, "BAT-0101")) is None  # type: ignore[arg-type]
    assert read.address(_host(ROUTE, None, target_branch=BRANCH)) is None  # type: ignore[arg-type]


# ---------- the Batch register draws each row's review and checks ----------


def _register(live: dict[str, object], branches: list[str | None]) -> list[str]:
    session = Session()
    session.route = ROUTE
    view = View(
        session=session,
        fixture=Fixture.from_chrome(load_chrome()),
        w=120,
        h=dict(SIZES)[120],
        projection=build_spine_view(_projection(ROUTE, _register_document(branches))),
        live=live,
    )
    return render_route(view)


def _register_row(frame: list[str], key: str) -> str:
    return next(row for row in frame if key in row)


def test_the_register_draws_each_batch_review_and_checks_from_its_branch_read() -> None:
    held = HeldBranchReviews(answers={BRANCH: OPEN})
    frame = _register({BRANCH_REVIEWS_READ: held}, [BRANCH, None])
    assert any("REVIEW" in row and "CHECKS" in row for row in frame)
    row = _register_row(frame, "BAT-0001")
    assert "#418 open · approved" in row and "1 pass · 1 fail" in row
    assert "∅ no branch named" in _register_row(frame, "BAT-0002")


def test_the_register_says_in_words_what_it_has_not_read() -> None:
    unread = _register({}, [BRANCH])
    assert REGISTER_UNREAD in _register_row(unread, "BAT-0001")
    past = _register({BRANCH_REVIEWS_READ: HeldBranchReviews()}, [BRANCH])
    assert PAST_CAP in _register_row(past, "BAT-0001")
    failed = HeldBranchReviews(
        answers={BRANCH: RepositoryAnswer(pull_request_unread="gh is not logged in")}
    )
    assert "unavailable" in _register_row(
        _register({BRANCH_REVIEWS_READ: failed}, [BRANCH]), "BAT-0001"
    )
    closed = HeldBranchReviews(answers={BRANCH: RepositoryAnswer(pull_request_branch=BRANCH)})
    assert "no pull request" in _register_row(
        _register({BRANCH_REVIEWS_READ: closed}, [BRANCH]), "BAT-0001"
    )


def _register_host(subject: str | None, branches: list[str | None]) -> _Host:
    projection = build_route_projection(
        route=ROUTE,
        document=_register_document(branches),
        cursor=nrf.CURSOR,
        scope_id=nrf.SCOPE,
        generated_at=nrf.AT,
    )
    return _Host(ROUTE, subject, projection)


def test_the_register_reads_each_distinct_branch_once() -> None:
    read = LIVE_READS[BRANCH_REVIEWS_READ]
    host = _register_host(None, [BRANCH, "main", BRANCH, None])
    address = read.address(host)  # type: ignore[arg-type]
    assert address is not None
    held = asyncio.run(read.fetch(host, address))  # type: ignore[arg-type]
    assert host.asked == [{"branch": BRANCH}, {"branch": "main"}]
    assert set(held.answers) == {BRANCH, "main"}


def test_the_register_reads_at_most_the_cap_of_branches() -> None:
    read = LIVE_READS[BRANCH_REVIEWS_READ]
    branches: list[str | None] = [f"feature/b{n}" for n in range(BRANCH_READS_MAX + 1)]
    host = _register_host(None, branches)
    address = read.address(host)  # type: ignore[arg-type]
    assert address is not None
    asyncio.run(read.fetch(host, address))  # type: ignore[arg-type]
    assert len(host.asked) == BRANCH_READS_MAX
    assert {"branch": f"feature/b{BRANCH_READS_MAX}"} not in host.asked


def test_the_register_owes_no_read_opened_on_a_batch_or_with_no_branch() -> None:
    read = LIVE_READS[BRANCH_REVIEWS_READ]
    assert read.address(_register_host("BAT-0001", [BRANCH])) is None  # type: ignore[arg-type]
    assert read.address(_register_host(None, [None])) is None  # type: ignore[arg-type]
    assert read.address(_register_host(None, [])) is None  # type: ignore[arg-type]
