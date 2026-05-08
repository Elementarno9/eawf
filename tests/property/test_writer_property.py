from __future__ import annotations

import json
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from eawf.state import writer

# orjson serialises integers as 64-bit; constrain to avoid out-of-range TypeError.
_INT64_MIN = -(2**63)
_INT64_MAX = 2**63 - 1

json_value = st.recursive(
    st.one_of(
        st.none(),
        st.booleans(),
        st.integers(min_value=_INT64_MIN, max_value=_INT64_MAX),
        st.text(max_size=10),
    ),
    lambda children: (
        st.lists(children, max_size=4)
        | st.dictionaries(st.text(min_size=1, max_size=4), children, max_size=4)
    ),
    max_leaves=8,
)


@given(payload=st.dictionaries(st.text(min_size=1, max_size=8), json_value, max_size=4))
@settings(max_examples=300, deadline=None)
def test_round_trip(tmp_path_factory: pytest.TempPathFactory, payload: dict) -> None:  # type: ignore[type-arg]
    tmp_path = tmp_path_factory.mktemp("writer")
    target: Path = tmp_path / "state.json"
    writer.atomic_write_json(target, payload)
    assert json.loads(target.read_text()) == payload
    assert not any(p.name.startswith("state.json.tmp.") for p in tmp_path.iterdir())
