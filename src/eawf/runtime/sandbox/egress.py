"""Default-deny egress policy: the pure decision layer for the UDS proxy.

The spawned agent (``claude -p`` / codex) reaches the network ONLY via a
Unix-domain-socket proxy that lives OUTSIDE the sandbox (see
:mod:`eawf.runtime.sandbox.egress_proxy`). This module is the pure policy
core that proxy enforces: given a requested target host, it returns a
typed :class:`EgressDecision` -- no sockets, no I/O, no resolution -- so
the decision is independently testable and auditable.

The policy is DEFAULT-DENY. Only an exact-hostname allowlist passes:

- claude lane -> ``api.anthropic.com`` (both auth paths use this host);
- codex lane -> ``chatgpt.com`` (ChatGPT-auth primary) plus
  ``api.openai.com`` (API-key fallback).

A SEPARATE gate/push set (``pypi.org`` / ``files.pythonhosted.org`` /
``registry.npmjs.org``, plus an optional caller-threaded git-remote host)
opens ONLY when ``gate_phase=True`` -- never on the read-only assist path.

Two ordering rules are load-bearing, both run BEFORE the allowlist:

1. **Canonicalization guard.** Penligent's real SOCKS5 bypass was a
   null-byte host ``attacker.example\x00.google.com`` that passes a naive
   ``endsWith(".google.com")`` but ``getaddrinfo()`` truncates at the
   null byte, so the resolver targets ``attacker.example``. We validate
   the hostname the RESOLVER will use: reject an embedded null byte,
   whitespace, or any character outside the DNS set ``[a-z0-9.-]`` (after
   lowercasing and stripping one trailing dot) FIRST, then exact-match.
2. **Metadata deny precedence.** The cloud metadata endpoints
   (``169.254.169.254`` / ``169.254.170.2`` / ``metadata.google.internal``)
   are denied at the IP/host layer BEFORE the allowlist, so even an
   allowlist that would otherwise pass them cannot -- deny wins.

Authoritative spec: ``.ea/local/research/2026-05-30-safety-floor.md``
(section "The floor: env-scrub + egress proxy", the Egress allowlist
table + the Canonicalization hazard paragraph).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

#: The auth lane an egress request belongs to. Mirrors the env-scrub lane
#: split but keyed on the short lane label rather than the runtime id, so
#: the proxy can classify without importing the adapter registry.
_CLAUDE_LANE: str = "claude"
_CODEX_LANE: str = "codex"

#: claude-lane allowlist: both auth paths (subscription OAuth + API key)
#: reach the same host.
_CLAUDE_ALLOW: frozenset[str] = frozenset({"api.anthropic.com"})

#: codex-lane allowlist: the ChatGPT-auth primary plus the API-key
#: fallback host.
_CODEX_ALLOW: frozenset[str] = frozenset({"chatgpt.com", "api.openai.com"})

#: Per-lane base allowlist (read-only assist path). The gate/push set is
#: added on top only when ``gate_phase=True``.
_LANE_ALLOW: dict[str, frozenset[str]] = {
    _CLAUDE_LANE: _CLAUDE_ALLOW,
    _CODEX_LANE: _CODEX_ALLOW,
}

#: Gate/push-only allowlist: package indexes the gate/push waves need.
#: Opened ONLY under ``gate_phase=True`` (exact-hostname), never on the
#: read-only assist path. The configured git-remote host is threaded in
#: per call via ``extra_allow`` rather than hardcoded here.
_GATE_ALLOW: frozenset[str] = frozenset(
    {
        "pypi.org",
        "files.pythonhosted.org",
        "registry.npmjs.org",
    }
)

#: Cloud metadata endpoints denied at the IP/host layer (SSRF -> cred
#: theft). Denied with precedence over EVERY allowlist, on every lane,
#: even under ``gate_phase=True``.
_METADATA_DENY: frozenset[str] = frozenset(
    {
        "169.254.169.254",
        "169.254.170.2",
        "metadata.google.internal",
    }
)

#: The DNS character set a normalized hostname may contain. Anything else
#: (null byte, whitespace, slash, underscore, ...) fails the
#: canonicalization guard before any allowlist check.
_DNS_CHARS: frozenset[str] = frozenset("abcdefghijklmnopqrstuvwxyz0123456789.-")


@dataclass(frozen=True, slots=True)
class EgressDecision:
    """The verdict for one egress request.

    Attributes:
        allowed: ``True`` only when *host* passed the canonicalization
            guard, was not a metadata endpoint, and matched the lane
            (or gate/push) allowlist.
        host: The NORMALIZED host the decision was made on (lowercased,
            one trailing dot stripped) when normalization succeeded; the
            raw input host when the canonicalization guard rejected it
            before normalization could complete.
        reason: A short lowercase tag naming why the request was allowed
            or denied -- e.g. ``"lane-allow"``, ``"metadata-deny"``,
            ``"non-dns-char"``, ``"default-deny"``.
    """

    allowed: bool
    host: str
    reason: str


def _normalize_host(host: str) -> str | None:
    """Return the normalized host, or ``None`` when it fails the guard.

    Normalization lowercases and strips exactly one trailing dot (the
    DNS root label), then validates that every remaining character is in
    the DNS set. An embedded null byte, whitespace, slash, or any other
    non-DNS character -- the canonicalization-bypass vector -- yields
    ``None`` so the caller denies BEFORE any allowlist comparison. An
    empty host (raw or post-strip) also yields ``None``.

    Args:
        host: The raw requested target host.

    Returns:
        The normalized host string, or ``None`` when the host is empty or
        carries a non-DNS character.
    """
    if not host:
        return None
    normalized = host.lower()
    if normalized.endswith("."):
        normalized = normalized[:-1]
    if not normalized:
        return None
    if any(char not in _DNS_CHARS for char in normalized):
        return None
    return normalized


def classify_egress(
    host: str,
    *,
    lane: str,
    gate_phase: bool = False,
    extra_allow: frozenset[str] = frozenset(),
) -> EgressDecision:
    """Classify one egress request against the default-deny policy.

    Decision order (each step short-circuits):

    1. **Canonicalization guard** -- reject an empty host, an embedded
       null byte / whitespace / non-DNS character. Runs FIRST so a
       null-byte host can never reach an allowlist comparison.
    2. **Metadata deny** -- the cloud metadata endpoints are denied with
       precedence over every allowlist, even under ``gate_phase``.
    3. **Allowlist** -- the lane's exact-hostname set, plus the gate/push
       set (and *extra_allow*) when ``gate_phase=True``.
    4. **Default-deny** -- anything else.

    Args:
        host: The requested target host (the resolver target, e.g. the
            host portion of a ``host:port`` request from the child).
        lane: The auth lane the spawning runtime belongs to -- ``"claude"``
            or ``"codex"``. Selects the base allowlist.
        gate_phase: When ``True``, additionally allow the gate/push set
            (package indexes) plus *extra_allow*. Defaults to ``False``
            (the read-only assist path).
        extra_allow: Additional exact hostnames to allow under
            ``gate_phase`` -- typically the single configured git-remote
            host threaded in by the caller. Ignored when
            ``gate_phase=False``.

    Returns:
        The :class:`EgressDecision` for the request.

    Raises:
        ValueError: When *lane* is not a known auth lane.
    """
    if lane not in _LANE_ALLOW:
        raise ValueError(f"unknown egress lane: {lane!r}")

    normalized = _normalize_host(host)
    if normalized is None:
        # Guard failed: keep the raw host on the record so the refusal log
        # shows exactly what the child sent (null bytes included via !r).
        decision = EgressDecision(allowed=False, host=host, reason="non-dns-char")
        logger.warning(
            f"classify_egress host={host!r} lane={lane!r} "
            f"allowed={decision.allowed} reason={decision.reason}"
        )
        return decision

    if normalized in _METADATA_DENY:
        decision = EgressDecision(allowed=False, host=normalized, reason="metadata-deny")
        logger.warning(
            f"classify_egress host={normalized!r} lane={lane!r} "
            f"allowed={decision.allowed} reason={decision.reason}"
        )
        return decision

    allow = _LANE_ALLOW[lane]
    if gate_phase:
        allow = allow | _GATE_ALLOW | extra_allow

    if normalized in allow:
        decision = EgressDecision(allowed=True, host=normalized, reason="lane-allow")
    else:
        decision = EgressDecision(allowed=False, host=normalized, reason="default-deny")

    logger.info(
        f"classify_egress host={normalized!r} lane={lane!r} "
        f"gate_phase={gate_phase} allowed={decision.allowed} reason={decision.reason}"
    )
    return decision


__all__ = [
    "EgressDecision",
    "classify_egress",
]
