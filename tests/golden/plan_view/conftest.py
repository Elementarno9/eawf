"""Pin :func:`derive_wave_sha` to a deterministic stub for golden tests.

The plan_view fixtures use ``P05-I01-W##`` wave ids that collide with real
``[P05-W##]`` commits in this repo's git history. Walking ``git log`` from
the live tree would leak full 40-char SHAs into the golden JSON (which
``detect-secrets`` flags as high-entropy strings) and couple fixture
bytes to branch history. The stub returns ``None`` for every wave so the
renderer degrades to ``closed`` without a SHA suffix — exercising the
empty-SHA branch that production hits when a wave has not yet committed.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest


@pytest.fixture(autouse=True)
def _stub_derive_wave_sha(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(
        "eawf.render.plan_view.derive_wave_sha",
        lambda wave_id, *, repo_root=None: None,
    )
    yield
