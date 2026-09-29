"""The rule graph renders and stays out of Run compilation in a fresh process.

RULE-080: the modules that compile a Run and seal its capsule load no rule
graph module, so no projection can be an input to a compiled capsule.
RULE-093: a shadow generation is computed, validated and selected with no
daemon: the child refuses every socket connection and the render succeeds.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import yaml

from eawf.platform.rules.render import PROJECTION_MANIFEST_PATH

# The modules that turn configuration into a sealed spec and a capsule.
_RUN_COMPILER_MODULES = (
    "eawf.workflow.runtime.compile",
    "eawf.workflow.runtime.merge",
    "eawf.kernel.runtime.compiled",
    "eawf.kernel.runtime.capsule",
    "eawf.kernel.config.providers",
)


def test_rule_080_the_run_compiler_loads_no_rule_graph_module() -> None:
    script = (
        "import importlib, sys\n"
        f"for name in {_RUN_COMPILER_MODULES!r}:\n"
        "    importlib.import_module(name)\n"
        "print(','.join(sorted(m for m in sys.modules if m.startswith('eawf.platform.rules'))))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, check=True
    )
    assert result.stdout.strip() == ""


def test_rule_093_a_shadow_generation_renders_without_the_daemon(tmp_path: Path) -> None:
    repo = tmp_path / "demo"
    source = repo / ".ea" / "rules.yaml"
    source.parent.mkdir(parents=True)
    source.write_text(
        yaml.safe_dump({"schema_version": 1, "modules": ["eawf.craft.python"], "rules": []}),
        encoding="utf-8",
    )
    # Any daemon reach goes through a socket; the child refuses every one.
    script = (
        "import socket\n"
        "def refuse(*args, **kwargs):\n"
        "    raise RuntimeError('the render reached for a socket')\n"
        "socket.socket.connect = refuse\n"
        "socket.socket.connect_ex = refuse\n"
        "from pathlib import Path\n"
        "from eawf.platform.rules.render import render_rule_projections\n"
        f"render_rule_projections(Path({str(repo)!r}))\n"
    )
    subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, check=True)
    assert (repo / PROJECTION_MANIFEST_PATH).is_file()
