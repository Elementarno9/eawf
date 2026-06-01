"""Unit tests for :mod:`eawf.runtime.sandbox.egress` (P29-I03-W02).

Pin the four load-bearing properties of the default-deny egress policy:

- DEFAULT-DENY: an unlisted host is denied on every lane;
- ALLOWLIST: each lane passes only its own exact hostnames (cross-lane
  hosts are denied);
- METADATA-IP DENY: the cloud metadata endpoints are denied on every
  lane with precedence over the allowlist, even under ``gate_phase``;
- CANONICALIZATION GUARD: a null-byte / whitespace / non-DNS host is
  rejected BEFORE any allowlist check, and uppercase + trailing-dot
  hosts normalize and match.

Plus the gate/push set (closed unless ``gate_phase=True``), boundary
cases (empty host), and the unknown-lane error path.
"""

from __future__ import annotations

import pytest

from eawf.runtime.sandbox.egress import (
    EgressDecision,
    classify_egress,
)

_CLAUDE: str = "claude"
_CODEX: str = "codex"
_ALL_LANES: tuple[str, ...] = (_CLAUDE, _CODEX)


# ---------------------------------------------------------------------------
# Default-deny: an unlisted host is denied on every lane
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("lane", _ALL_LANES)
@pytest.mark.parametrize("host", ["example.com", "evil.test", "google.com"])
def test_classify_egress_default_deny_unlisted_host(host: str, lane: str) -> None:
    """An unlisted host is denied on every lane (default-deny)."""
    decision = classify_egress(host, lane=lane)
    assert decision.allowed is False
    assert decision.reason == "default-deny"
    assert decision.host == host


def test_classify_egress_returns_frozen_decision() -> None:
    """The decision is a frozen dataclass (cannot be mutated post-hoc)."""
    decision = classify_egress("example.com", lane=_CLAUDE)
    assert isinstance(decision, EgressDecision)
    with pytest.raises((AttributeError, TypeError)):
        decision.allowed = True  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Allowlist: each lane passes only its own exact hostnames
# ---------------------------------------------------------------------------


def test_classify_egress_claude_lane_allows_anthropic() -> None:
    """The claude lane allows ``api.anthropic.com`` (both auth paths)."""
    decision = classify_egress("api.anthropic.com", lane=_CLAUDE)
    assert decision.allowed is True
    assert decision.reason == "lane-allow"
    assert decision.host == "api.anthropic.com"


@pytest.mark.parametrize("host", ["chatgpt.com", "api.openai.com"])
def test_classify_egress_codex_lane_allows_openai_hosts(host: str) -> None:
    """The codex lane allows the ChatGPT-auth + API-key fallback hosts."""
    decision = classify_egress(host, lane=_CODEX)
    assert decision.allowed is True
    assert decision.reason == "lane-allow"


def test_classify_egress_cross_lane_anthropic_denied_on_codex() -> None:
    """``api.anthropic.com`` is denied on the codex lane (cross-lane)."""
    decision = classify_egress("api.anthropic.com", lane=_CODEX)
    assert decision.allowed is False
    assert decision.reason == "default-deny"


def test_classify_egress_cross_lane_chatgpt_denied_on_claude() -> None:
    """``chatgpt.com`` is denied on the claude lane (cross-lane)."""
    decision = classify_egress("chatgpt.com", lane=_CLAUDE)
    assert decision.allowed is False
    assert decision.reason == "default-deny"


def test_classify_egress_suffix_does_not_pass_exact_allowlist() -> None:
    """A subdomain of an allowed host is denied (exact match, not suffix)."""
    decision = classify_egress("evil.api.anthropic.com", lane=_CLAUDE)
    assert decision.allowed is False
    assert decision.reason == "default-deny"


def test_classify_egress_allow_host_as_suffix_attacker_denied() -> None:
    """``api.anthropic.com.evil.com`` is denied (the allow host is a prefix)."""
    decision = classify_egress("api.anthropic.com.evil.com", lane=_CLAUDE)
    assert decision.allowed is False
    assert decision.reason == "default-deny"


# ---------------------------------------------------------------------------
# Metadata-IP deny: precedence over the allowlist, every lane, even gated
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("lane", _ALL_LANES)
@pytest.mark.parametrize(
    "host",
    ["169.254.169.254", "169.254.170.2", "metadata.google.internal"],
)
def test_classify_egress_metadata_denied_every_lane(host: str, lane: str) -> None:
    """The metadata endpoints are denied on every lane."""
    decision = classify_egress(host, lane=lane)
    assert decision.allowed is False
    assert decision.reason == "metadata-deny"


@pytest.mark.parametrize("lane", _ALL_LANES)
@pytest.mark.parametrize(
    "host",
    ["169.254.169.254", "169.254.170.2", "metadata.google.internal"],
)
def test_classify_egress_metadata_denied_even_under_gate_phase(host: str, lane: str) -> None:
    """Metadata deny has precedence even when ``gate_phase=True``."""
    decision = classify_egress(host, lane=lane, gate_phase=True)
    assert decision.allowed is False
    assert decision.reason == "metadata-deny"


def test_classify_egress_metadata_denied_even_if_in_extra_allow() -> None:
    """Metadata deny wins even if a metadata host is in ``extra_allow``."""
    decision = classify_egress(
        "metadata.google.internal",
        lane=_CLAUDE,
        gate_phase=True,
        extra_allow=frozenset({"metadata.google.internal"}),
    )
    assert decision.allowed is False
    assert decision.reason == "metadata-deny"


def test_classify_egress_metadata_trailing_dot_normalizes_and_denied() -> None:
    """``metadata.google.internal.`` normalizes and is still denied."""
    decision = classify_egress("metadata.google.internal.", lane=_CLAUDE)
    assert decision.allowed is False
    assert decision.reason == "metadata-deny"
    assert decision.host == "metadata.google.internal"


# ---------------------------------------------------------------------------
# Canonicalization guard: runs before the allowlist
# ---------------------------------------------------------------------------


def test_classify_egress_null_byte_rejected_before_allowlist() -> None:
    """A null-byte host is rejected before any allowlist (Penligent bypass).

    ``api.anthropic.com\\x00.evil.com`` passes a naive ``endsWith`` but the
    resolver truncates at the null byte; the guard rejects it outright.
    """
    decision = classify_egress("api.anthropic.com\x00.evil.com", lane=_CLAUDE)
    assert decision.allowed is False
    assert decision.reason == "non-dns-char"
    # The raw host (null byte included) is preserved on the record.
    assert "\x00" in decision.host


def test_classify_egress_null_byte_on_allowed_host_rejected() -> None:
    """An allowed host with a trailing null-byte payload is still rejected."""
    decision = classify_egress("api.anthropic.com\x00", lane=_CLAUDE)
    assert decision.allowed is False
    assert decision.reason == "non-dns-char"


def test_classify_egress_uppercase_and_trailing_dot_normalizes_and_matches() -> None:
    """``API.ANTHROPIC.COM.`` normalizes (lowercase + strip dot) and matches."""
    decision = classify_egress("API.ANTHROPIC.COM.", lane=_CLAUDE)
    assert decision.allowed is True
    assert decision.reason == "lane-allow"
    assert decision.host == "api.anthropic.com"


@pytest.mark.parametrize(
    "host",
    [
        "api.anthropic.com/path",
        "api.anthropic.com ",
        " api.anthropic.com",
        "api anthropic com",
        "api.anthropic.com\t",
        "api.anthropic.com\n",
        "host_with_underscore.com",
        "host:8080",
    ],
)
def test_classify_egress_non_dns_chars_rejected(host: str) -> None:
    """A slash, space, tab, newline, underscore, or colon -> non-dns-char."""
    decision = classify_egress(host, lane=_CLAUDE)
    assert decision.allowed is False
    assert decision.reason == "non-dns-char"


# ---------------------------------------------------------------------------
# Gate/push set: closed unless gate_phase=True
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "host",
    ["pypi.org", "files.pythonhosted.org", "registry.npmjs.org"],
)
def test_classify_egress_gate_set_denied_without_gate_phase(host: str) -> None:
    """The gate/push set is denied on the read-only assist path."""
    decision = classify_egress(host, lane=_CLAUDE)
    assert decision.allowed is False
    assert decision.reason == "default-deny"


@pytest.mark.parametrize(
    "host",
    ["pypi.org", "files.pythonhosted.org", "registry.npmjs.org"],
)
def test_classify_egress_gate_set_allowed_under_gate_phase(host: str) -> None:
    """The gate/push set is allowed exactly when ``gate_phase=True``."""
    decision = classify_egress(host, lane=_CLAUDE, gate_phase=True)
    assert decision.allowed is True
    assert decision.reason == "lane-allow"


def test_classify_egress_extra_allow_git_remote_under_gate_phase() -> None:
    """A caller-threaded git-remote host is allowed under ``gate_phase``."""
    decision = classify_egress(
        "github.com",
        lane=_CODEX,
        gate_phase=True,
        extra_allow=frozenset({"github.com"}),
    )
    assert decision.allowed is True
    assert decision.reason == "lane-allow"


def test_classify_egress_extra_allow_ignored_without_gate_phase() -> None:
    """``extra_allow`` does not open a host outside ``gate_phase``."""
    decision = classify_egress(
        "github.com",
        lane=_CODEX,
        extra_allow=frozenset({"github.com"}),
    )
    assert decision.allowed is False
    assert decision.reason == "default-deny"


# ---------------------------------------------------------------------------
# Boundary + error paths
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("lane", _ALL_LANES)
def test_classify_egress_empty_host_denied_not_crash(lane: str) -> None:
    """An empty host is denied (not a crash)."""
    decision = classify_egress("", lane=lane)
    assert decision.allowed is False
    assert decision.reason == "non-dns-char"


def test_classify_egress_bare_dot_host_denied() -> None:
    """A host of just ``.`` strips to empty and is denied."""
    decision = classify_egress(".", lane=_CLAUDE)
    assert decision.allowed is False
    assert decision.reason == "non-dns-char"


def test_classify_egress_unknown_lane_raises_value_error() -> None:
    """An unknown lane raises ValueError with a lowercase message."""
    with pytest.raises(ValueError, match="unknown egress lane"):
        classify_egress("api.anthropic.com", lane="bogus")
