"""Runtime-label folding at the dispatch-cost telemetry ingest (P30-I23-W29).

One runtime, two spellings: the event surface writes the short
``RuntimeTriple`` (``claude``) while sessions / actuals carry the
adapter-manifest id (``claude-code``). The telemetry ingest folds both
onto one closed ``RuntimeName`` key via ``runtime_triple_label`` so a
cost-by-runtime rollup never double-counts one runtime as two — and
never silently drops the adapter-id spelling. The write site keeps the
closed ``RuntimeTriple`` literal (a write-site ``claude-code`` wrap is
rejected by ``DispatchCostPayload.runtime``).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from eawf.kernel.store.kinds.events.base import (
    canonical_runtime_label,
    runtime_triple_label,
)
from eawf.kernel.store.kinds.events.dispatch_cost import DispatchCostPayload
from eawf.observability.telemetry.sources.dispatch_cost import _runtime_or_none

# --- runtime_triple_label (the un-idled inverse helper) ----------------------


def test_runtime_triple_label_folds_adapter_id_to_triple() -> None:
    assert runtime_triple_label("claude-code") == "claude"


def test_runtime_triple_label_passes_triple_and_unknown_through() -> None:
    assert runtime_triple_label("claude") == "claude"
    assert runtime_triple_label("codex") == "codex"
    assert runtime_triple_label("opencode") == "opencode"
    assert runtime_triple_label("mystery") == "mystery"


def test_triple_and_canonical_are_inverse_on_the_mapped_pair() -> None:
    assert runtime_triple_label(canonical_runtime_label("claude")) == "claude"
    assert canonical_runtime_label(runtime_triple_label("claude-code")) == "claude-code"


# --- the ingest seam folds both spellings onto one closed key ----------------


def test_runtime_or_none_folds_both_spellings_to_one_key() -> None:
    assert _runtime_or_none("claude") == "claude"
    assert _runtime_or_none("claude-code") == "claude"
    assert _runtime_or_none("claude") == _runtime_or_none("claude-code")


def test_runtime_or_none_passes_codex_and_opencode_through() -> None:
    assert _runtime_or_none("codex") == "codex"
    assert _runtime_or_none("opencode") == "opencode"


def test_runtime_or_none_coerces_junk_to_none() -> None:
    assert _runtime_or_none("mystery-runtime") is None
    assert _runtime_or_none("") is None
    assert _runtime_or_none(None) is None
    assert _runtime_or_none(42) is None


# --- the write site keeps the closed RuntimeTriple literal -------------------


def test_dispatch_cost_payload_rejects_adapter_id_at_write_site() -> None:
    """The blocked write-site wrap: DispatchCostPayload refuses claude-code.

    Pins WHY the fold lives at the read seam — constructing the payload
    with the adapter id raises, so canonicalizing at the writer would
    crash every claude dispatch at the cost-emit step.
    """
    with pytest.raises(ValidationError):
        DispatchCostPayload(
            runtime="claude-code",  # type: ignore[arg-type]
            model="claude-sonnet-5",
            input_tokens=1,
            output_tokens=1,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=0,
            cost_usd="0.01",
            trace_id="tr-1",
            wave_id="P01-I01-W01",
            session_id="SES-1",
        )
