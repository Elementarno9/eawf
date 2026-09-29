"""Host configuration keys: every key eawf writes into a host's configuration.

A host's own configuration check is not evidence that a key does anything:
a runtime parses invented keys and wrong value types cleanly and ignores
what it does not know. So each key eawf writes carries the requirement it
serves and the probe that watched the installed host behave differently
because of it. A key whose effect no probe has observed is recorded, so its
absence is a known state, and every writer refuses it.

Records name a key as a dotted path into one host document; ``*`` stands
for the one segment the operator names, such as an MCP server id. The
probes live in :data:`PROBE_MODULE` and run against the installed host
binaries.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Final, Literal

#: A host configuration document eawf writes into.
HostDocument = Literal[
    "codex_config",
    "claude_settings",
    "claude_mcp",
    "opencode_config",
]

#: What a probe established about a key.
#:
#: - ``honoured``: the host behaved differently with the key than without it.
#: - ``ignored``: an eawf ownership marker the host does not read; the probe
#:   ran the entry carrying it and the entry worked.
#: - ``no_effect``: the host behaved the same with and without the key.
#: - ``unprobed``: no probe has run against the host.
ProbeEffect = Literal["honoured", "ignored", "no_effect", "unprobed"]

#: The effects a writer may emit a key under.
WRITABLE_EFFECTS: Final[frozenset[ProbeEffect]] = frozenset({"honoured", "ignored"})

#: The test module holding every probe a record cites.
PROBE_MODULE: Final = "tests/integration/runtime/harness/test_host_key_probes.py"

_REQUIREMENT_ID = re.compile(r"^[A-Z]+-\d{3}$")


class UnrecordedHostKeyError(ValueError):
    """A writer was about to emit a host key with no writable record."""


@dataclass(frozen=True, slots=True, kw_only=True)
class HostKeyRecord:
    """One host configuration key, why eawf writes it, and what a probe saw.

    Attributes:
        document: The host document the key lives in.
        path: The dotted key path; ``*`` stands for an operator-named segment.
        source: The requirement id the key serves.
        effect: What the probe established.
        evidence: The ``PROBE_MODULE::test`` node that observed the effect,
            or, for an unprobed key, why no probe exists.
    """

    document: HostDocument
    path: str
    source: str
    effect: ProbeEffect
    evidence: str

    def __post_init__(self) -> None:
        """Refuse a record without a named source or with mismatched evidence.

        Raises:
            ValueError: ``source`` is not a requirement id, a probed effect
                cites no probe node, or an unprobed one cites a node.
        """
        if _REQUIREMENT_ID.fullmatch(self.source) is None:
            raise ValueError(f"{self.path}: source {self.source!r} is not a requirement id")
        cites_probe = self.evidence.startswith(f"{PROBE_MODULE}::test_")
        if cites_probe == (self.effect == "unprobed"):
            raise ValueError(
                f"{self.path}: a {self.effect} key cites "
                f"{'no probe node' if self.effect != 'unprobed' else 'a probe node'}"
            )


def _probe(name: str) -> str:
    return f"{PROBE_MODULE}::{name}"


_NO_FACT = "no fact sets its value, so the concurrency plan leaves it unwritten"
_NO_OPENCODE = "no opencode binary has been available to probe, so the key is not written"

#: The MCP tables eawf marks its own entries in, with the probe that ran one.
_MCP_TABLES: Final[tuple[tuple[HostDocument, str, str], ...]] = (
    ("codex_config", "mcp_servers", "test_surf_168_codex_mcp_table_spawns_its_server"),
    ("claude_mcp", "mcpServers", "test_surf_168_claude_mcp_entry_spawns_its_server"),
)

#: Every host configuration key eawf writes, or has decided not to write.
HOST_KEYS: Final[tuple[HostKeyRecord, ...]] = (
    HostKeyRecord(
        document="codex_config",
        path="agents.max_concurrent_threads_per_session",
        source="SURF-150",
        effect="honoured",
        evidence=_probe("test_surf_168_codex_thread_count_reaches_the_session"),
    ),
    HostKeyRecord(
        document="codex_config",
        path="agents.max_depth",
        source="SURF-150",
        effect="honoured",
        evidence=_probe("test_surf_168_codex_max_depth_refuses_a_grandchild"),
    ),
    HostKeyRecord(
        document="codex_config",
        path="agents.default_subagent_reasoning_effort",
        source="SURF-150",
        effect="honoured",
        evidence=_probe("test_surf_168_codex_subagent_effort_reaches_the_child"),
    ),
    HostKeyRecord(
        document="codex_config",
        path="agents.default_subagent_model",
        source="SURF-150",
        effect="unprobed",
        evidence=_NO_FACT,
    ),
    HostKeyRecord(
        document="codex_config",
        path="agents.job_max_runtime_seconds",
        source="SURF-150",
        effect="unprobed",
        evidence=_NO_FACT,
    ),
    HostKeyRecord(
        document="codex_config",
        path="plugins.eawf.enabled",
        source="SURF-169",
        effect="no_effect",
        evidence=_probe("test_surf_168_codex_bare_plugin_key_loads_nothing"),
    ),
    *(
        HostKeyRecord(
            document="codex_config",
            path=f"mcp_servers.*.{key}",
            source="SURF-042",
            effect="honoured",
            evidence=_probe("test_surf_168_codex_mcp_table_spawns_its_server"),
        )
        for key in ("command", "args", "env")
    ),
    *(
        HostKeyRecord(
            document=document,
            path=f"{table}.*.{marker}",
            source="SURF-042",
            effect="ignored",
            evidence=_probe(probe),
        )
        for document, table, probe in _MCP_TABLES
        for marker in ("__eawf_owner", "__eawf_managed_at")
    ),
    HostKeyRecord(
        document="claude_mcp",
        path="mcpServers",
        source="SURF-042",
        effect="honoured",
        evidence=_probe("test_surf_168_claude_mcp_entry_spawns_its_server"),
    ),
    *(
        HostKeyRecord(
            document="claude_mcp",
            path=f"mcpServers.*.{key}",
            source="SURF-042",
            effect="honoured",
            evidence=_probe("test_surf_168_claude_mcp_entry_spawns_its_server"),
        )
        for key in ("command", "args", "env")
    ),
    HostKeyRecord(
        document="claude_mcp",
        path="mcpServers.*.transport",
        source="SURF-042",
        effect="no_effect",
        evidence=_probe("test_surf_168_claude_mcp_entry_spawns_its_server"),
    ),
    HostKeyRecord(
        document="claude_settings",
        path="__eawf_managed",
        source="SURF-169",
        effect="ignored",
        evidence=_probe("test_surf_168_claude_session_hooks_fire"),
    ),
    HostKeyRecord(
        document="claude_settings",
        path="hooks.SessionStart",
        source="RULE-130",
        effect="honoured",
        evidence=_probe("test_surf_168_claude_session_hooks_fire"),
    ),
    HostKeyRecord(
        document="claude_settings",
        path="hooks.Stop",
        source="REL-009",
        effect="honoured",
        evidence=_probe("test_surf_168_claude_session_hooks_fire"),
    ),
    *(
        HostKeyRecord(
            document="claude_settings",
            path=f"hooks.{event}",
            source="SURF-054",
            effect="honoured",
            evidence=_probe("test_surf_168_claude_subagent_hooks_fire"),
        )
        for event in ("SubagentStart", "SubagentStop")
    ),
    HostKeyRecord(
        document="claude_settings",
        path="hooks.PermissionRequest",
        source="RUN-051",
        effect="honoured",
        evidence=_probe("test_surf_168_claude_permission_request_hook_fires"),
    ),
    HostKeyRecord(
        document="claude_settings",
        path="statusLine",
        source="SURF-120",
        effect="honoured",
        evidence=_probe("test_surf_168_claude_status_line_runs"),
    ),
    HostKeyRecord(
        document="claude_settings",
        path="env.CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS",
        source="SURF-150",
        effect="honoured",
        evidence=_probe("test_surf_168_claude_concurrency_cap_limits_children"),
    ),
    HostKeyRecord(
        document="claude_settings",
        path="env.CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH",
        source="SURF-150",
        effect="honoured",
        evidence=_probe("test_surf_168_claude_spawn_depth_strips_the_child_agent_tool"),
    ),
    HostKeyRecord(
        document="claude_settings",
        path="env.CLAUDE_CODE_SUBAGENT_MODEL",
        source="SURF-150",
        effect="unprobed",
        evidence=_NO_FACT,
    ),
    HostKeyRecord(
        document="opencode_config",
        path="mcp",
        source="SURF-042",
        effect="unprobed",
        evidence=_NO_OPENCODE,
    ),
    *(
        HostKeyRecord(
            document="opencode_config",
            path=f"mcp.*.{key}",
            source="SURF-042",
            effect="unprobed",
            evidence=_NO_OPENCODE,
        )
        for key in ("command", "args", "env", "__eawf_owner", "__eawf_managed_at")
    ),
)

_BY_KEY: Final[dict[tuple[HostDocument, str], HostKeyRecord]] = {
    (record.document, record.path): record for record in HOST_KEYS
}


def require_recorded(document: HostDocument, paths: Iterable[str]) -> None:
    """Refuse to write any key in *paths* that has no writable record.

    Args:
        document: The host document about to be written.
        paths: The dotted key paths the writer emits, with ``*`` in place of
            each operator-named segment.

    Raises:
        UnrecordedHostKeyError: A path has no record, or its record's effect
            is not one a writer may emit; every such path is named.
    """
    refused: list[str] = []
    for path in paths:
        record = _BY_KEY.get((document, path))
        if record is None:
            refused.append(f"{path} (no record)")
        elif record.effect not in WRITABLE_EFFECTS:
            refused.append(f"{path} ({record.effect}: {record.evidence})")
    if refused:
        raise UnrecordedHostKeyError(
            f"refusing to write {document} keys without an observed host effect: "
            f"{'; '.join(refused)}"
        )


__all__ = [
    "HOST_KEYS",
    "PROBE_MODULE",
    "WRITABLE_EFFECTS",
    "HostDocument",
    "HostKeyRecord",
    "ProbeEffect",
    "UnrecordedHostKeyError",
    "require_recorded",
]
