"""Unit tests for the canonical runtime-label normalizer (P30-I21-W21).

The event/telemetry surface keys a runtime on the short ``RuntimeTriple``
spelling (``"claude"``) while sessions / actuals carry the adapter-manifest id
(``"claude-code"``). :func:`canonical_runtime_label` unifies the two so a
cost-by-runtime rollup does not double-count one runtime as two labels.
"""

from __future__ import annotations

from eawf.kernel.store.kinds.events.base import canonical_runtime_label


def test_canonical_runtime_label_unifies_claude_spellings() -> None:
    """Both the event triple and the adapter id resolve to one canonical label."""
    assert canonical_runtime_label("claude") == "claude-code"
    assert canonical_runtime_label("claude-code") == "claude-code"
    # A dispatch_cost row (triple) and a session row (adapter id) collapse to
    # one rollup key.
    assert canonical_runtime_label("claude") == canonical_runtime_label("claude-code")


def test_canonical_runtime_label_passes_through_codex_and_opencode() -> None:
    """codex / opencode share one spelling across both surfaces (identity)."""
    assert canonical_runtime_label("codex") == "codex"
    assert canonical_runtime_label("opencode") == "opencode"


def test_canonical_runtime_label_unknown_passes_through() -> None:
    """An unknown label is returned unchanged (no silent drop)."""
    assert canonical_runtime_label("gemini-cli") == "gemini-cli"
