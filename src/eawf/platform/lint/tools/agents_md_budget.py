"""Check that AGENTS.md tier-0 render blocks stay within token budget."""

from __future__ import annotations

import argparse
import re
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

from eawf.platform.profiles.loader import list_profiles, load_profile
from eawf.platform.render_block import DEFAULT_TIER0_TOKEN_CAP

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


def check_budget(repo_root: Path) -> Tier0BudgetReport:
    """Return the tier-0 AGENTS.md budget report for bundled profiles."""
    cap = _load_cap(repo_root)
    tokens = 0
    blocks: list[str] = []
    for profile_id in sorted(list_profiles()):
        profile = load_profile(profile_id)
        for block in profile.render_blocks:
            if block.target != "AGENTS.md" or block.tier != "tier0":
                continue
            block_tokens = count_tokens(_block_text(block))
            tokens += block_tokens
            blocks.append(f"{profile_id}:{block.id}:{block_tokens}")
    return Tier0BudgetReport(cap=cap, tokens=tokens, blocks=tuple(blocks))


def main(argv: list[str] | None = None) -> int:
    """CLI entry point for the AGENTS.md budget gate."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    report = check_budget(args.repo_root.resolve())
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


__all__ = ["Tier0BudgetReport", "check_budget", "count_tokens", "main"]
