"""The deterministic worker the Milestone smoke's stub launcher starts in a leased workspace.

It reads its prompt on stdin, writes the one note the prompt names, then files the
Run's report through the real per-Run MCP server (``eawf mcp serve``), speaking the
same newline-framed JSON-RPC a provider process speaks. It never commits: the
daemon's supervisor commits the edit with the Task's trailer once the report is
accepted, as it does for a real provider.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Final

#: How long the worker holds its turn before reporting, so the RUNNING state is
#: on screen long enough for a console frame to show it.
TURN_SECONDS: Final = 1.5

_NOTE: Final = re.compile(r"notes/(\d+)\.txt")


def _exchange(server: subprocess.Popen[str], frame: dict[str, Any]) -> dict[str, Any]:
    """Send one JSON-RPC frame and return the answer."""
    assert server.stdin is not None and server.stdout is not None
    server.stdin.write(json.dumps(frame) + "\n")
    server.stdin.flush()
    line = server.stdout.readline()
    if not line:
        raise SystemExit("the MCP server closed before answering")
    answer: dict[str, Any] = json.loads(line)
    return answer


def main() -> int:
    """Write the note, report through MCP, and exit 0 on an accepted report."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--binding", required=True)
    args = parser.parse_args()
    prompt = sys.stdin.read()
    match = _NOTE.search(prompt)
    if match is None:
        raise SystemExit("the prompt names no notes/<n>.txt")
    note = Path("notes") / f"{match.group(1)}.txt"
    note.parent.mkdir(exist_ok=True)
    note.write_text(f"note {match.group(1)}\n", encoding="utf-8")
    time.sleep(TURN_SECONDS)
    server = subprocess.Popen(
        [sys.executable, "-I", "-m", "eawf", "mcp", "serve", "--binding", args.binding],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        _exchange(server, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        answer = _exchange(
            server,
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "submit_report",
                    "arguments": {"verdict": "pass", "summary": f"wrote {note.as_posix()}"},
                },
            },
        )
    finally:
        assert server.stdin is not None
        server.stdin.close()
        server.wait(timeout=30)
    result = answer.get("result") or {}
    if result.get("isError"):
        raise SystemExit(f"submit_report refused: {json.dumps(result)[:400]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
