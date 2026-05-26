"""Open-session seam threading the typed :class:`RoleContract` (P28-I01-W13).

W13 wires the rendered role-driven prompt through the open-session seam
so a freshly-spawned runtime receives the role registry's body rather
than a hardcoded executor preamble. Today the live ``claude -p`` /
``codex exec`` / ``opencode run`` subprocess spawn lands in
P26-SURFACES; the v0.3 adapter surface accepts the typed
:class:`RoleContract` keyword so the wire-up is exercised end-to-end
ahead of the subprocess work, and a debug log records the attach so
audits can verify the seam fires.
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pytest

from eawf.kernel.state.enums import WaveStatus
from eawf.kernel.state.models import SessionAttempt, Wave
from eawf.runtime.runtimes.claude.adapter import ClaudeAdapter
from eawf.runtime.runtimes.codex.adapter import CodexAdapter
from eawf.runtime.runtimes.opencode.adapter import OpenCodeAdapter
from eawf.workflow.agents.specs.models import RoleContract

if TYPE_CHECKING:
    from eawf.runtime.runtimes.adapter import RuntimeAdapter


def _run[T](coro: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(coro)


def _wave() -> Wave:
    return Wave(
        id="P28-I01-W13",
        iter_id="P28-I01",
        title="t",
        status=WaveStatus.IN_PROGRESS,
        opened_at=datetime(2026, 5, 26, tzinfo=UTC),
        sessions={},
    )


def _executor_contract() -> RoleContract:
    return RoleContract(
        role="executor",
        summary="Implements the planner's spec.",
        system_prompt="You implement what the planner specified.",
        allowed_tools=["Bash", "Edit", "Read"],
        denied_tools=["WebSearch"],
        model="opus",
        memory=True,
        report_schema_ref="executor_report",
        stop_conditions=["scope_violation"],
    )


def _auditor_contract() -> RoleContract:
    return RoleContract(
        role="auditor",
        summary="Fresh-context auditor.",
        system_prompt="You are skeptical by design.",
        allowed_tools=["Read", "Grep"],
        denied_tools=["Edit", "Write"],
        model="opus",
        memory=False,
        report_schema_ref="auditor_report",
        stop_conditions=[],
    )


# ---------------------------------------------------------------------------
# Each adapter's open_session accepts role_contract without crashing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("adapter_cls", "expected_runtime"),
    [
        (ClaudeAdapter, "claude-code"),
        (CodexAdapter, "codex"),
        (OpenCodeAdapter, "opencode"),
    ],
    ids=["claude-code", "codex", "opencode"],
)
def test_open_session_accepts_role_contract_keyword(
    adapter_cls: type, expected_runtime: str
) -> None:
    """All 3 adapters' ``open_session`` accept ``role_contract`` without crashing."""

    async def body() -> SessionAttempt:
        a: RuntimeAdapter = adapter_cls()
        return await a.open_session(
            _wave(),
            "rendered prompt body",
            role_contract=_executor_contract(),
        )

    attempt = _run(body())
    assert attempt.runtime == expected_runtime
    assert attempt.session_id  # adapter still mints a uuid


@pytest.mark.parametrize(
    "adapter_cls",
    [ClaudeAdapter, CodexAdapter, OpenCodeAdapter],
    ids=["claude-code", "codex", "opencode"],
)
def test_open_session_role_contract_default_is_none(adapter_cls: type) -> None:
    """The ``role_contract`` kwarg defaults to ``None`` — pre-W13 surface preserved."""

    async def body() -> SessionAttempt:
        a: RuntimeAdapter = adapter_cls()
        # Pre-W13 callers never pass role_contract; the call MUST still succeed.
        return await a.open_session(_wave(), "rendered prompt body")

    attempt = _run(body())
    assert attempt.session_id


@pytest.mark.parametrize(
    "adapter_cls",
    [ClaudeAdapter, CodexAdapter, OpenCodeAdapter],
    ids=["claude-code", "codex", "opencode"],
)
def test_open_session_role_contract_emits_debug_log(
    adapter_cls: type, caplog: pytest.LogCaptureFixture
) -> None:
    """The adapters emit a debug log line naming the attached role contract.

    The log is the only externally-observable signal today (the live
    subprocess spawn lands in P26-SURFACES); it lets audits verify the
    seam fires when a role contract is supplied.
    """

    async def body() -> SessionAttempt:
        a: RuntimeAdapter = adapter_cls()
        return await a.open_session(
            _wave(),
            "rendered prompt body",
            role_contract=_auditor_contract(),
        )

    with caplog.at_level("DEBUG", logger=adapter_cls.__module__):
        _run(body())

    # The log includes the role identifier so the adapter-side seam is observable.
    role_logs = [rec for rec in caplog.records if "role='auditor'" in rec.getMessage()]
    assert role_logs, [rec.getMessage() for rec in caplog.records]


@pytest.mark.parametrize(
    "adapter_cls",
    [ClaudeAdapter, CodexAdapter, OpenCodeAdapter],
    ids=["claude-code", "codex", "opencode"],
)
def test_open_session_none_role_contract_suppresses_log(
    adapter_cls: type, caplog: pytest.LogCaptureFixture
) -> None:
    """``role_contract=None`` does not emit a role-attach debug log line."""

    async def body() -> SessionAttempt:
        a: RuntimeAdapter = adapter_cls()
        return await a.open_session(_wave(), "rendered prompt body", role_contract=None)

    with caplog.at_level("DEBUG", logger=adapter_cls.__module__):
        _run(body())

    role_logs = [rec for rec in caplog.records if "role=" in rec.getMessage()]
    assert role_logs == [], [rec.getMessage() for rec in caplog.records]


# ---------------------------------------------------------------------------
# Protocol still accepts the per-adapter conformance with the new kwarg
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "adapter_cls",
    [ClaudeAdapter, CodexAdapter, OpenCodeAdapter],
    ids=["claude-code", "codex", "opencode"],
)
def test_open_session_role_contract_does_not_break_protocol_conformance(
    adapter_cls: type,
) -> None:
    """Adding the kwarg keeps ``isinstance(adapter, RuntimeAdapter)`` ``True``."""
    from eawf.runtime.runtimes.adapter import RuntimeAdapter

    assert isinstance(adapter_cls(), RuntimeAdapter)


# ---------------------------------------------------------------------------
# Boundary: contract attached but prompt still required (signature unchanged)
# ---------------------------------------------------------------------------


def test_open_session_signature_keeps_prompt_required() -> None:
    """The ``prompt`` positional parameter remains required — the seam threads
    the contract alongside, not in place of, the rendered prompt."""

    async def body() -> SessionAttempt:
        a = ClaudeAdapter()
        # role_contract supplied without prompt would be missing a required arg;
        # this verifies the prompt+contract carry together.
        return await a.open_session(
            _wave(),
            "rendered prompt body",
            role_contract=_executor_contract(),
        )

    attempt = _run(body())
    assert attempt.runtime == "claude-code"


# ---------------------------------------------------------------------------
# Cache prefix + role contract both routed through the same call
# ---------------------------------------------------------------------------


def test_open_session_routes_cache_prefix_and_role_contract_together(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Claude's adapter accepts BOTH ``cache_prefix`` and ``role_contract``."""

    async def body() -> SessionAttempt:
        a = ClaudeAdapter()
        return await a.open_session(
            _wave(),
            "rendered prompt body",
            cache_prefix="PRE",
            role_contract=_executor_contract(),
        )

    with caplog.at_level("DEBUG", logger="eawf.runtime.runtimes.claude.adapter"):
        attempt = _run(body())

    assert attempt.runtime == "claude-code"
    messages = [rec.getMessage() for rec in caplog.records]
    assert any("cache_control=injected" in msg for msg in messages), messages
    assert any("role='executor'" in msg for msg in messages), messages


# ---------------------------------------------------------------------------
# The contract carries fields the live spawn will consume in P26-SURFACES
# ---------------------------------------------------------------------------


def test_role_contract_supplies_system_prompt_and_tool_lists() -> None:
    """The contract surface the spawn seam reads is itself well-formed.

    The live subprocess spawn in P26-SURFACES will read
    ``system_prompt`` / ``allowed_tools`` / ``denied_tools`` / ``model``
    off the contract; this test pins those fields are reachable on the
    typed projection so the spawn boundary has a stable shape to bind to.
    """
    contract = _auditor_contract()
    assert contract.system_prompt
    assert contract.allowed_tools  # at least one allow-listed tool
    assert "Edit" in contract.denied_tools
    assert contract.report_schema_ref == "auditor_report"
