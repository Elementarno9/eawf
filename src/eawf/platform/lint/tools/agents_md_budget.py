"""Check that AGENTS.md tier-0 render blocks stay within token budget.

A repository that authors ``.ea/rules.yaml`` renders AGENTS.md (and
AGENTS.override.md, when it composes a workspace layer) from the rule
graph instead of these profile blocks, so the tier-0 weight below no
longer reflects what a session actually loads and this gate could never
fail on an over-budget rule render. :func:`check_rendered_projection_budget`
is the check that measures those rendered files directly, against the
same certified byte cap the render transaction and the doctor check hold
them to; :func:`main` runs it instead of the tier-0 report whenever a rule
source is present.
"""

from __future__ import annotations

import argparse
import re
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

from eawf.platform.profiles.loader import list_profiles, load_profile
from eawf.platform.render_block import DEFAULT_TIER0_TOKEN_CAP
from eawf.platform.rules.host_facts import ProjectionKind, load_host_facts, smallest_certified_cap
from eawf.platform.rules.render import CARD_TARGET, POLICY_TARGET, rule_source_present

_TOKEN_RE = re.compile(r"\S+")


@dataclass(frozen=True)
class Tier0BudgetReport:
    """Budget-check result for tier-0 AGENTS.md render blocks."""

    cap: int
    tokens: int
    blocks: tuple[str, ...]

    @property
    def clean(self) -> bool:
        """Return whether the token count is at or under the cap."""
        return self.tokens <= self.cap


def count_tokens(text: str) -> int:
    """Return the stable approximate token count used by the budget gate."""
    return len(_TOKEN_RE.findall(text))


def _block_text(block: object) -> str:
    body = getattr(block, "body_template", "")
    if body:
        return str(body)
    parts = [
        getattr(block, "rationale", None),
        getattr(block, "mechanism", None),
        getattr(block, "verification", None),
    ]
    return "\n".join(str(part) for part in parts if part)


def _load_cap(repo_root: Path) -> int:
    pyproject = repo_root / "pyproject.toml"
    if not pyproject.exists():
        return DEFAULT_TIER0_TOKEN_CAP
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    table = data.get("tool", {}).get("eawf", {}).get("agents_md_budget", {})
    return int(table.get("max-tier0-tokens", DEFAULT_TIER0_TOKEN_CAP))


def check_budget(repo_root: Path, *, workspace: Path | None = None) -> Tier0BudgetReport:
    """Return the tier-0 AGENTS.md budget report for the visible profiles.

    Args:
        repo_root: Repository root whose ``pyproject.toml`` supplies the
            ``[tool.eawf.agents_md_budget] max-tier0-tokens`` cap (falling
            back to :data:`DEFAULT_TIER0_TOKEN_CAP` when unset).
        workspace: Optional workspace root forwarded to the profile
            loader. When given, its ``.ea/profiles/`` overlay participates
            in discovery, so a synthesized over-cap tier-0 profile can be
            measured without mutating the bundled set.

    Returns:
        :class:`Tier0BudgetReport` naming each tier-0 block id and its
        token weight, the cap, and the summed token count.
    """
    cap = _load_cap(repo_root)
    tokens = 0
    blocks: list[str] = []
    for profile_id in sorted(list_profiles(workspace=workspace)):
        profile = load_profile(profile_id, workspace=workspace)
        for block in profile.render_blocks:
            if block.target != "AGENTS.md" or block.tier != "tier0":
                continue
            block_tokens = count_tokens(_block_text(block))
            tokens += block_tokens
            blocks.append(f"{profile_id}:{block.id}:{block_tokens}")
    return Tier0BudgetReport(cap=cap, tokens=tokens, blocks=tuple(blocks))


@dataclass(frozen=True)
class ProjectionBudgetEntry:
    """One rendered projection's byte size against its certified cap.

    Attributes:
        target: Repository-relative path of the rendered file.
        byte_count: Its UTF-8 byte size on disk.
        cap_bytes: The smallest certified cap among its readers.
    """

    target: str
    byte_count: int
    cap_bytes: int

    @property
    def over(self) -> bool:
        """Return whether this projection exceeds its certified cap."""
        return self.byte_count > self.cap_bytes


@dataclass(frozen=True)
class RenderedProjectionBudgetReport:
    """Byte-budget report for the rendered card and policy projections.

    Empty when the repository has not switched to authoring
    ``.ea/rules.yaml``, since nothing renders these files from that source.
    """

    entries: tuple[ProjectionBudgetEntry, ...]

    @property
    def clean(self) -> bool:
        """Return whether every measured projection is within its cap."""
        return not any(entry.over for entry in self.entries)


def check_rendered_projection_budget(repo_root: Path) -> RenderedProjectionBudgetReport:
    """Measure the rendered card and policy files against their certified cap.

    Unlike :func:`check_budget`, this reads the files a repository's rule
    graph actually renders rather than the profile render blocks that fed
    the legacy renderer, so it is the check that can fail on an over-budget
    rule render.

    Args:
        repo_root: Repository root holding ``.ea/rules.yaml`` and the
            rendered ``AGENTS.md`` (and ``AGENTS.override.md``, when the
            repository composes a workspace layer).

    Returns:
        One entry per rendered projection found on disk; empty when the
        repository authors no rule source.
    """
    if not rule_source_present(repo_root):
        return RenderedProjectionBudgetReport(entries=())
    registry = load_host_facts()
    entries: list[ProjectionBudgetEntry] = []
    targets: tuple[tuple[str, ProjectionKind], ...] = (
        (CARD_TARGET, "card"),
        (POLICY_TARGET, "policy"),
    )
    for target, kind in targets:
        path = repo_root / target
        if not path.is_file():
            continue
        cap = smallest_certified_cap(registry, kind)
        if cap is None:
            continue
        entries.append(
            ProjectionBudgetEntry(
                target=target,
                byte_count=len(path.read_bytes()),
                cap_bytes=cap.cap_bytes,
            )
        )
    return RenderedProjectionBudgetReport(entries=tuple(entries))


def main(argv: list[str] | None = None) -> int:
    """CLI entry point for the AGENTS.md budget gate.

    A repository authoring ``.ea/rules.yaml`` is held to
    :func:`check_rendered_projection_budget`'s verdict over the rendered
    card and policy files; a repository without one falls back to
    :func:`check_budget`'s tier-0 profile-block report.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument(
        "--workspace",
        type=Path,
        default=None,
        help="Workspace root whose .ea/profiles overlay participates in discovery.",
    )
    args = parser.parse_args(argv)
    repo_root = args.repo_root.resolve()
    workspace = args.workspace.resolve() if args.workspace is not None else None

    if rule_source_present(repo_root):
        projection_report = check_rendered_projection_budget(repo_root)
        if projection_report.clean:
            summary = ", ".join(
                f"{entry.target}={entry.byte_count}B/{entry.cap_bytes}B"
                for entry in projection_report.entries
            )
            print(f"agents-md-budget: clean rendered_projections={summary}")
            return 0
        over = ", ".join(
            f"{entry.target}={entry.byte_count}B>{entry.cap_bytes}B cap"
            for entry in projection_report.entries
            if entry.over
        )
        print(f"agents-md-budget: rendered projection(s) over cap: {over}", file=sys.stderr)
        return 1

    report = check_budget(repo_root, workspace=workspace)
    if report.clean:
        print(
            f"agents-md-budget: clean tier0_tokens={report.tokens} "
            f"cap={report.cap} blocks={len(report.blocks)}"
        )
        return 0
    joined = ", ".join(report.blocks)
    print(
        f"agents-md-budget: tier0_tokens={report.tokens} exceeds cap={report.cap}; blocks={joined}",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "ProjectionBudgetEntry",
    "RenderedProjectionBudgetReport",
    "Tier0BudgetReport",
    "check_budget",
    "check_rendered_projection_budget",
    "count_tokens",
    "main",
]
