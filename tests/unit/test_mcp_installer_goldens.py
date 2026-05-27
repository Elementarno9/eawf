"""Idempotent re-install goldens for :mod:`eawf.runtime.mcp.installer` (B062).

The MCP config writers are byte-deterministic: a fresh install of a fixed
server (with the epoch timestamp sentinel) produces exactly the checked-in
golden, and a second install of the same server re-reads ``unchanged`` and
leaves the file byte-identical. The goldens cover the Claude ``.mcp.json``
writer, Codex TOML writer, and OpenCode ``opencode.json`` writer.

Regenerate the goldens (only on an intentional format change) with::

    uv run python - <<'PY'
    ... install_runtime_entry into a temp dir, copy the file into
    tests/golden/mcp/ ...
    PY
"""

from __future__ import annotations

from pathlib import Path

import pytest

from eawf.kernel.state.enums import McpRisk, McpStatus
from eawf.kernel.state.models import McpServer
from eawf.runtime.mcp.installer import install_runtime_entry

pytestmark = pytest.mark.unit

_GOLDEN_DIR = Path(__file__).parent.parent / "golden" / "mcp"


def _golden_server() -> McpServer:
    """The canonical server the goldens are rendered from."""
    return McpServer(
        id="eawf-mcp",
        owner="eawf",
        command="eawf-mcp",
        args=["--port", "8080"],
        env_refs=["${ENV:DEMO_KEY}"],
        risk=McpRisk.READ,
        write_capable=False,
        status=McpStatus.CONFIGURED,
        installed_targets=[],
    )


@pytest.mark.parametrize(
    ("runtime", "rel_path", "golden_name"),
    [
        ("claude", ".mcp.json", "claude_mcp.json"),
        ("codex", ".codex/config.toml", "codex_config.toml"),
        ("opencode", "opencode.json", "opencode_config.json"),
    ],
)
def test_install_matches_golden_and_is_idempotent(
    tmp_path: Path, runtime: str, rel_path: str, golden_name: str
) -> None:
    golden = (_GOLDEN_DIR / golden_name).read_bytes()

    first = install_runtime_entry(
        server=_golden_server(), runtime=runtime, target_dir=tmp_path, force=False
    )
    assert first.action == "created"
    written = tmp_path / rel_path
    assert written.read_bytes() == golden, f"{runtime} install diverged from golden"

    # Re-install the identical server: no bytes change, action is unchanged.
    second = install_runtime_entry(
        server=_golden_server(), runtime=runtime, target_dir=tmp_path, force=False
    )
    assert second.action == "unchanged"
    assert written.read_bytes() == golden, f"{runtime} re-install was not byte-stable"
