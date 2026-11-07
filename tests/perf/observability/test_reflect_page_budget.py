"""Bound the reflect pages over a synthetic history the size of a busy month.

300 Claude sessions run in the tree's own project, one of them 2,000 requests long
with 3 compactions and 30 subagents. ``reflect run --local-only`` indexes them,
analyses them and writes the overview, the finder and 300 session pages; the run
must finish inside its ceiling and every page must stay inside the page budget.
"""

from __future__ import annotations

import json
import os
import time
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from eawf.surfaces.cli.app import app
from tests.contract.surfaces.cli._reflect_tree import native_tree, run_row

pytestmark = pytest.mark.perf

#: The wall time one reflect run over the corpus must finish under.
REFLECT_CEILING_SECONDS = 60.0

#: The most elements a page may hold, as parsed from the file.
PAGE_ELEMENTS = 4_000

#: The most bytes a page may weigh.
PAGE_BYTES = 1_500_000

SESSIONS = 300
LONG_REQUESTS = 2_000
LONG_COMPACTIONS = 3
LONG_SUBAGENTS = 30

_T0 = datetime(2026, 8, 1, 9, 0, tzinfo=UTC)

_skip_perf = pytest.mark.skipif(
    os.environ.get("EAWF_SKIP_PERF") == "1",
    reason="EAWF_SKIP_PERF=1 - perf gate skipped",
)


class _Count(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.elements = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.elements += 1


def _stamp(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _session(cwd: Path, index: int, requests: int, compactions: int, agents: int) -> list[str]:
    """Return one session's transcript lines: requests, reads, subagents and compactions."""
    session = f"synthetic-{index:04d}"
    start = _T0 + timedelta(hours=6 * index)
    base: dict[str, Any] = {"sessionId": session, "cwd": str(cwd)}
    rows: list[dict[str, Any]] = [
        base | {"type": "user", "timestamp": _stamp(start), "message": {"content": "do the work"}}
    ]
    cuts = {requests * (cut + 1) // (compactions + 1) for cut in range(compactions)}
    delegated = {requests * (agent + 1) // (agents + 1) for agent in range(agents)}
    for step in range(1, requests + 1):
        moment = start + timedelta(seconds=20 * step)
        tool = "Agent" if step in delegated else "Read"
        call: dict[str, Any] = {"type": "tool_use", "id": f"{session}-t{step}", "name": tool}
        call["input"] = {"subagent_type": "Explore"} if tool == "Agent" else {"file": "f.py"}
        usage = {
            "input_tokens": 40 + step % 7,
            "output_tokens": 30,
            "cache_read_input_tokens": 2_000 + step,
            "cache_creation_input_tokens": 100,
        }
        rows.append(
            base
            | {
                "type": "assistant",
                "timestamp": _stamp(moment),
                "message": {
                    "id": f"{session}-m{step}",
                    "model": "claude-opus-4-7",
                    "content": [{"type": "text", "text": "reading"}, call],
                    "usage": usage,
                },
            }
        )
        done = moment + timedelta(seconds=90 if tool == "Agent" else 5)
        result = {"type": "tool_result", "tool_use_id": call["id"], "content": "x" * 400}
        rows.append(
            base | {"type": "user", "timestamp": _stamp(done), "message": {"content": [result]}}
        )
        if step in cuts:
            meta = {"trigger": "auto", "preTokens": 150_000}
            rows.append(
                base
                | {
                    "type": "system",
                    "subtype": "compact_boundary",
                    "timestamp": _stamp(done),
                    "compactMetadata": meta,
                }
            )
            rows.append(base | {"type": "summary", "summary": "s" * 8_000})
    return [json.dumps(row) for row in rows]


def _corpus(repo: Path, projects: Path) -> None:
    folder = projects / "-synthetic-repo"
    folder.mkdir(parents=True)
    cwd = repo.resolve()
    for index in range(SESSIONS):
        long = index == SESSIONS // 2
        lines = _session(
            cwd,
            index,
            LONG_REQUESTS if long else 4 + index % 9,
            LONG_COMPACTIONS if long else 0,
            LONG_SUBAGENTS if long else index % 2,
        )
        (folder / f"session-{index:04d}.jsonl").write_text("\n".join(lines) + "\n", "utf-8")


@_skip_perf
def test_reflect_pages_stay_inside_the_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("EA_STATE", raising=False)
    repo = tmp_path / "repo"
    repo.mkdir()
    tree = native_tree(repo, [run_row("RUN-00000001")], [])
    projects = tmp_path / "harness" / "projects"
    monkeypatch.setenv("EAWF_CLAUDE_PROJECTS_DIR", str(projects))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    _corpus(repo, projects)
    out = tmp_path / "out"

    started = time.perf_counter()
    result = CliRunner().invoke(
        app,
        ["-w", str(tree.parent), "reflect", "run", "--local-only", "--out", str(out / "r.txt")],
    )
    elapsed = time.perf_counter() - started

    assert result.exit_code == 0, result.output
    assert elapsed < REFLECT_CEILING_SECONDS
    pages = sorted(out.glob("*.html"))
    assert len(pages) == 2 + SESSIONS
    texts = {page: page.read_text("utf-8") for page in pages}
    long = max(texts, key=lambda page: texts[page].count('class="tl-row"'))
    # The long session really drew its compactions and its subagent lanes.
    assert texts[long].count('class="tl-mark"') == LONG_COMPACTIONS
    assert texts[long].count('class="strip"') == LONG_COMPACTIONS + 1
    assert texts[long].count('class="tl-row"') > LONG_SUBAGENTS // 2
    for page in pages:
        data = page.read_bytes()
        counter = _Count()
        counter.feed(data.decode("utf-8"))
        counter.close()
        assert len(data) <= PAGE_BYTES, f"{page.name}: {len(data)} bytes"
        assert counter.elements <= PAGE_ELEMENTS, f"{page.name}: {counter.elements} elements"
