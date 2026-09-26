"""The attention reducer derives every count at render time.

Nothing about attention is stored: the header's ``!N`` and the Attention route's bucket
strip are read out of the action register while the frame is composing, so an action the
register states as resolved moves both on the very next render with no cache to
invalidate. Writing that state is the daemon's, never the console's.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from eawf.surfaces.tui.console.app import compose_frame
from eawf.surfaces.tui.console.attention import (
    NEEDS,
    OPEN,
    VERB,
    attn_list,
    attn_row,
    bucket_count,
    bucket_keys,
    bucket_label,
    is_notice,
    open_actions,
    open_count,
    ordered_actions,
    question_row,
    top_bucket,
    verbs_for,
)
from eawf.surfaces.tui.console.fixture import Action, Detail, Fixture, load_fixture
from eawf.surfaces.tui.console.frame import View
from eawf.surfaces.tui.console.session import SIZES, Session, SessionSetup

GOLDEN_ROOT = Path(__file__).resolve().parents[4] / "fixtures" / "console" / "golden"
FIXTURE_DIR = GOLDEN_ROOT / "fixture"
ATTENTION_ROUTE = "attention"


@pytest.fixture
def fixture() -> Fixture:
    """Return a freshly loaded fixture; each test mutates its own register."""
    return load_fixture(FIXTURE_DIR)


def _with_register(fixture: Fixture, actions: tuple[Action, ...]) -> Fixture:
    """Return a fixture whose attention register is ``actions`` and nothing else changed."""
    return Fixture(
        fixture.proto.model_copy(update={"attention": actions}),
        Detail.model_validate(dict(fixture.detail)),
        fixture.registers,
        fixture.settings,
    )


def _render(fixture: Fixture, route: str) -> list[str]:
    """Return the composed frame of ``route`` at the narrowest size."""
    session = Session()
    session.reset(
        SessionSetup(route=route),
        settings_section_order=fixture.settings.section_order,
        now=0.0,
    )
    w, h = SIZES[session.size]
    return compose_frame(View(session=session, fixture=fixture, w=w, h=h, held=True))


def _needs_in_header(rows: list[str]) -> int:
    """Return the ``!N`` the header row published, or zero when it published none."""
    head = rows[0]
    if "!" not in head:
        return 0
    return int(head.split("!", 1)[1].split(" ", 1)[0])


def _first_open(fixture: Fixture) -> Action:
    return open_actions(fixture)[0]


def test_open_count_is_derived_from_the_register_at_render_time(fixture: Fixture) -> None:
    before = open_count(fixture)
    assert _needs_in_header(_render(fixture, "scope.home")) == before
    _first_open(fixture).state = VERB["a"].state
    assert open_count(fixture) == before - 1
    assert _needs_in_header(_render(fixture, "scope.home")) == before - 1


def test_bucket_count_is_derived_from_the_register_at_render_time(fixture: Fixture) -> None:
    action = _first_open(fixture)
    key = action.bucket
    before = bucket_count(fixture, key)
    assert f"{before}" in "".join(_render(fixture, ATTENTION_ROUTE))
    action.state = VERB["x"].state
    assert bucket_count(fixture, key) == before - 1


def test_bucket_count_rolls_sub_buckets_into_their_parent(fixture: Fixture) -> None:
    subs = [key for key in bucket_keys(fixture) if top_bucket(key) == NEEDS and key != NEEDS]
    assert subs, "the tracked register carries no needs sub-bucket"
    assert bucket_count(fixture, NEEDS) == sum(bucket_count(fixture, key) for key in subs)


def test_bucket_count_of_an_unknown_key_is_zero(fixture: Fixture) -> None:
    assert bucket_count(fixture, "no-such-bucket") == 0


def test_open_count_of_an_empty_register_is_zero(fixture: Fixture) -> None:
    empty = _with_register(fixture, ())
    assert open_count(empty) == 0
    assert open_actions(empty) == []
    assert ordered_actions(empty) == []
    assert _needs_in_header(_render(empty, "scope.home")) == 0


def test_open_count_of_a_single_open_action_is_one(fixture: Fixture) -> None:
    single = _with_register(fixture, (_first_open(fixture),))
    assert open_count(single) == 1
    assert _needs_in_header(_render(single, "scope.home")) == 1


def test_open_count_leaves_out_a_notice(fixture: Fixture) -> None:
    notices = [a for a in fixture.proto.attention if is_notice(a) and a.state == OPEN]
    assert notices, "the tracked register carries no open notice"
    assert all(notice not in open_actions(fixture) for notice in notices)
    assert open_count(fixture) == len(
        [a for a in fixture.proto.attention if a.state == OPEN and not is_notice(a)]
    )


def test_verbs_for_a_notice_offer_no_answer_and_no_deny(fixture: Fixture) -> None:
    notice = next(a for a in fixture.proto.attention if is_notice(a))
    assert verbs_for(notice) == ["z", "v"]
    assert verbs_for(_first_open(fixture)) == ["a", "x", "z", "v"]
    assert verbs_for(None) == ["a", "x", "z", "v"]


def test_top_bucket_of_a_missing_key_is_empty() -> None:
    assert top_bucket(None) == ""
    assert top_bucket("") == ""
    assert top_bucket("needs.permission") == NEEDS
    assert top_bucket(NEEDS) == NEEDS


def test_bucket_label_names_the_parent_of_a_sub_bucket(fixture: Fixture) -> None:
    sub = next(key for key in bucket_keys(fixture) if "." in key)
    assert "↳" in bucket_label(fixture, sub)
    assert bucket_label(fixture, "no-such-bucket") == "no-such-bucket"


def test_attn_row_past_the_end_falls_back_to_the_first_row(fixture: Fixture) -> None:
    session = Session()
    rows = attn_list(session, fixture)
    session.sel = len(rows) + 10
    assert attn_row(session, fixture) is rows[0]


def test_attn_row_of_an_empty_register_is_none(fixture: Fixture) -> None:
    empty = _with_register(fixture, ())
    assert attn_row(Session(), empty) is None


def test_attn_list_filters_to_the_selected_bucket(fixture: Fixture) -> None:
    session = Session()
    session.bucket = NEEDS
    rows = attn_list(session, fixture)
    assert rows
    assert all(top_bucket(row.bucket) == NEEDS for row in rows)


def test_question_row_raises_on_an_empty_register(fixture: Fixture) -> None:
    empty = _with_register(fixture, ())
    with pytest.raises(LookupError, match="holds no action"):
        question_row(Session(), empty)


def test_attention_register_rejects_an_unknown_field(fixture: Fixture) -> None:
    row = fixture.proto.attention[0].model_dump(by_alias=True)
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        Action.model_validate({**row, "surprise": 1})


def test_ordered_actions_put_every_open_action_first(fixture: Fixture) -> None:
    states = [row.state == OPEN for row in ordered_actions(fixture)]
    assert states == sorted(states, reverse=True)
