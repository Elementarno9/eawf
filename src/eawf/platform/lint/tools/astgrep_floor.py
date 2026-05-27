"""Generate the deterministic ast-grep floor used by PR reviewdog.

The floor is intentionally small and high-signal. It gives Mode A work a
visible, non-blocking review surface without turning ast-grep into the
authoritative lint layer.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml

GENERATED_SENTINEL = "eawf-astgrep-floor: generated"
DEFAULT_OUTPUT_DIR = Path(".ast-grep-floor")
DEFAULT_CONFIG_PATH = Path("sgconfig.yml")

AstGrepLanguage = Literal["JavaScript", "Python", "TypeScript", "Yaml"]
ReviewdogReporter = Literal["github-pr-review"]
ReviewdogFailLevel = Literal["none"]


@dataclass(frozen=True)
class AstGrepRule:
    """One deterministic ast-grep floor rule."""

    id: str
    language: AstGrepLanguage
    message: str
    pattern: str
    files: tuple[str, ...]
    note: str
    severity: str = "warning"

    @property
    def filename(self) -> str:
        """Return stable rule filename."""
        return f"{self.id}.yml"

    def as_mapping(self) -> dict[str, object]:
        """Return ast-grep YAML mapping for this rule."""
        return {
            "id": self.id,
            "language": self.language,
            "severity": self.severity,
            "message": self.message,
            "note": self.note,
            "files": list(self.files),
            "rule": {"pattern": self.pattern},
        }


@dataclass(frozen=True)
class ReviewdogFloorPolicy:
    """Reviewdog policy paired with the deterministic floor."""

    ceremony_mode: Literal["A"]
    floor_only: bool
    reporter: ReviewdogReporter
    fail_level: ReviewdogFailLevel
    level: Literal["warning"]


@dataclass(frozen=True)
class AstGrepFloorPaths:
    """Paths written by :func:`write_floor`."""

    config_path: Path
    rule_dir: Path
    rule_paths: tuple[Path, ...]


REVIEWDOG_FLOOR_POLICY = ReviewdogFloorPolicy(
    ceremony_mode="A",
    floor_only=True,
    reporter="github-pr-review",
    fail_level="none",
    level="warning",
)

FLOOR_RULES: tuple[AstGrepRule, ...] = (
    AstGrepRule(
        id="eawf-js-no-debugger",
        language="JavaScript",
        message="Remove JavaScript debugger statements before merge",
        pattern="debugger",
        files=("**/*.js", "**/*.mjs", "**/*.cjs"),
        note="Mode A floor-only rule: visible reviewdog warning, non-blocking CI.",
    ),
    AstGrepRule(
        id="eawf-py-no-breakpoint",
        language="Python",
        message="Remove interactive Python breakpoint before merge",
        pattern="breakpoint()",
        files=("src/**/*.py", "tests/**/*.py"),
        note="Mode A floor-only rule: visible reviewdog warning, non-blocking CI.",
    ),
    AstGrepRule(
        id="eawf-py-no-eval",
        language="Python",
        message="Avoid dynamic Python eval in committed code",
        pattern="eval($$$ARGS)",
        files=("src/**/*.py", "tests/**/*.py"),
        note="Mode A floor-only rule: visible reviewdog warning, non-blocking CI.",
    ),
    AstGrepRule(
        id="eawf-ts-no-debugger",
        language="TypeScript",
        message="Remove TypeScript debugger statements before merge",
        pattern="debugger",
        files=("**/*.ts", "**/*.tsx"),
        note="Mode A floor-only rule: visible reviewdog warning, non-blocking CI.",
    ),
    AstGrepRule(
        id="eawf-yaml-no-pr-target",
        language="Yaml",
        message="Avoid pull_request_target for untrusted PR code paths",
        pattern="pull_request_target:",
        files=(".github/workflows/**/*.yml", ".github/workflows/**/*.yaml"),
        note="Mode A floor-only rule: visible reviewdog warning, non-blocking CI.",
    ),
)


def render_rule(rule: AstGrepRule) -> str:
    """Render one ast-grep rule YAML document."""
    body = yaml.safe_dump(rule.as_mapping(), sort_keys=False)
    return f"# {GENERATED_SENTINEL}\n{body}"


def render_config(rule_dir: str) -> str:
    """Render ast-grep project config for *rule_dir*."""
    body = yaml.safe_dump({"ruleDirs": [rule_dir]}, sort_keys=False)
    policy = REVIEWDOG_FLOOR_POLICY
    return (
        f"# {GENERATED_SENTINEL}\n"
        f"# policy: mode={policy.ceremony_mode} floor_only={policy.floor_only} "
        f"reporter={policy.reporter} fail_level={policy.fail_level}\n"
        f"{body}"
    )


def _repo_relative(path: Path, repo_root: Path) -> str:
    resolved = path.resolve()
    root = repo_root.resolve()
    try:
        return resolved.relative_to(root).as_posix()
    except ValueError as exc:
        raise ValueError(f"path must live under repo root: {path!s}") from exc


def _write_generated(path: Path, text: str) -> None:
    if path.exists() and GENERATED_SENTINEL not in path.read_text(encoding="utf-8"):
        raise FileExistsError(f"refusing to overwrite non-generated file: {path!s}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def write_floor(
    repo_root: Path,
    *,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    config_path: Path = DEFAULT_CONFIG_PATH,
) -> AstGrepFloorPaths:
    """Write sgconfig + rule files for the deterministic ast-grep floor.

    Args:
        repo_root: Repository root used to keep generated paths repo-relative.
        output_dir: Directory for generated rule files.
        config_path: Generated ast-grep config path.

    Returns:
        Written config path, rule directory, and individual rule paths.

    Raises:
        FileExistsError: if a target file exists without the generated sentinel.
        ValueError: if generated paths would sit outside *repo_root*.
    """
    root = repo_root.resolve()
    rule_dir = (root / output_dir).resolve() / "rules"
    config = (root / config_path).resolve()
    rule_dir_text = _repo_relative(rule_dir, root)

    _write_generated(config, render_config(rule_dir_text))
    rule_paths: list[Path] = []
    for rule in FLOOR_RULES:
        rule_path = rule_dir / rule.filename
        _write_generated(rule_path, render_rule(rule))
        rule_paths.append(rule_path)
    return AstGrepFloorPaths(
        config_path=config,
        rule_dir=rule_dir,
        rule_paths=tuple(rule_paths),
    )


def main(argv: list[str] | None = None) -> int:
    """CLI entry point for generating the ast-grep floor."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--config-path", type=Path, default=DEFAULT_CONFIG_PATH)
    args = parser.parse_args(argv)

    paths = write_floor(
        args.repo_root,
        output_dir=args.output_dir,
        config_path=args.config_path,
    )
    print(
        f"astgrep-floor: wrote config={_repo_relative(paths.config_path, args.repo_root)} "
        f"rules={len(paths.rule_paths)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "FLOOR_RULES",
    "GENERATED_SENTINEL",
    "REVIEWDOG_FLOOR_POLICY",
    "AstGrepFloorPaths",
    "AstGrepRule",
    "ReviewdogFloorPolicy",
    "main",
    "render_config",
    "render_rule",
    "write_floor",
]
