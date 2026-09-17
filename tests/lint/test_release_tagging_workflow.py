"""Release tags come from the release verb, and dev tags publish as prereleases.

A tag pushed with a workflow's default ``GITHUB_TOKEN`` starts no workflow
run, so a post-merge job that tags the merge commit looks green while
release.yaml and plugin-release.yaml never fire. A release object created
without the prerelease flag advertises a dev checkpoint as the latest
release. Both defects sit in workflow source that runs only after a merge or
a tag push, so these checks read the source, and each ships a companion that
feeds the checker the defective shape to prove it reds.

The rule text is checked alongside the workflows: the core profile renders it
into every managed repo, and most of those repos tag through their own flow
rather than an eawf release train.
"""

from __future__ import annotations

import contextlib
import copy
import io
import re
from pathlib import Path
from typing import Any
from unittest import mock

import pytest
import yaml

import eawf
from eawf.platform.install.dist_tag import DIST_TAG_NEXT, dist_tag_for_version

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[2]
_WORKFLOWS = _REPO_ROOT / ".github" / "workflows"
_PHASE_RELEASE = _WORKFLOWS / "phase-release.yaml"
_PLUGIN_RELEASE = _WORKFLOWS / "plugin-release.yaml"
_CORE_PROFILE = _REPO_ROOT / "src" / "eawf" / "platform" / "profiles" / "data" / "core.yaml"
_RULE_IDS = ("release-process", "ship-process")

#: Shell commands that create a tag, move one, or publish a release object.
_TAGGING_COMMANDS: dict[str, re.Pattern[str]] = {
    "git push": re.compile(r"\bgit\s+push\b"),
    "git tag": re.compile(r"\bgit\s+tag\b"),
    "gh release create": re.compile(r"\bgh\s+release\s+create\b"),
}

_RELEASE_TAG_VERB = re.compile(r"eawf release tag\b[^`\n]*--push")

#: Versions whose tag must publish as a prerelease, one per pre segment.
_PRERELEASE_VERSIONS = ("0.7.0.dev3", "0.7.0rc1", "0.8.0a1", "0.8.0b2", "0.8.0.dev12")
_FINAL_VERSIONS = ("0.7.0", "1.0.0")

_PY_HEREDOC = re.compile(r"<<'PY'\n(?P<body>.*?)^PY$", flags=re.DOTALL | re.MULTILINE)
_UNFLAGGED_RUN_LINE = re.compile(r"gh release (?:create|edit)\b(?!.*\$\{flag\})")
_STALE_RELEASE_COMMENT = re.compile(r"#.*phase-release\.yaml creates")


def _load(path: Path) -> dict[str, Any]:
    workflow: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
    return workflow


def _find_step(job: dict[str, Any], name_fragment: str) -> dict[str, Any] | None:
    """Return the first step of *job* whose name contains *name_fragment*."""
    steps: list[dict[str, Any]] = job.get("steps", [])
    for step in steps:
        if name_fragment.lower() in str(step.get("name", "")).lower():
            return step
    return None


# --- no workflow tags the phase merge ---------------------------------------


def tag_push_violations(workflow: dict[str, Any]) -> list[str]:
    """Report every way the phase-release *workflow* could tag or publish.

    Args:
        workflow: The parsed phase-release workflow.

    Returns:
        One problem per violation; empty when no step tags, pushes or
        creates a release, the token cannot write, the annotation and
        version checks remain, and the tag command is printed for the
        annotated version.
    """
    job = workflow.get("jobs", {}).get("phase-release")
    if job is None:
        return ["phase-release.yaml declares no 'phase-release' job"]
    problems = [
        f"step {step.get('name')!r} runs {command}"
        for step in job.get("steps", [])
        for command, pattern in _TAGGING_COMMANDS.items()
        if pattern.search(str(step.get("run", "")))
    ]
    for scope in (workflow, job):
        granted = scope.get("permissions")
        if granted == "write-all" or (
            isinstance(granted, dict) and granted.get("contents") == "write"
        ):
            problems.append("the workflow token keeps contents: write, so a tag push still lands")
    annotation = _find_step(job, "release annotation")
    if annotation is None or "(release=" not in str(annotation.get("run", "")):
        problems.append("the release annotation check is gone")
    version = _find_step(job, "version source")
    if version is None or "version mismatch" not in str(version.get("run", "")):
        problems.append("the version source check is gone")
    return problems + _tag_command_problems(job)


def _tag_command_problems(job: dict[str, Any]) -> list[str]:
    """Report the ways the job could finish without naming the tag command."""
    printer = next(
        (step for step in job.get("steps", []) if "eawf release tag" in str(step.get("run", ""))),
        None,
    )
    if printer is None:
        return ["no step prints the eawf release tag --push command"]
    run = str(printer.get("run", ""))
    problems: list[str] = []
    if not re.search(r"eawf release tag \$\{VERSION\} --push", run):
        problems.append("the printed command is not eawf release tag ${VERSION} --push")
    if "steps.annotation.outputs.version" not in str(printer.get("env", {})):
        problems.append("the printed command is not bound to the annotated version")
    if "release_required == 'true'" not in str(printer.get("if", "")):
        problems.append("the tag command prints even when the merge carries no annotation")
    return problems


def test_tag_push_absent_from_phase_release() -> None:
    """The live workflow only checks the annotation and prints the tag verb."""
    assert tag_push_violations(_load(_PHASE_RELEASE)) == []


def test_tag_push_gate_reds_on_a_tagging_workflow() -> None:
    """The gate fires on the shape that shipped: tag, push, then release.

    The defective workflow is the live one with the print step swapped for
    the two steps it replaced, and the write token they needed.
    """
    old = copy.deepcopy(_load(_PHASE_RELEASE))
    old["permissions"]["contents"] = "write"
    job = old["jobs"]["phase-release"]
    job["steps"] = [s for s in job["steps"] if "eawf release tag" not in str(s.get("run", ""))]
    job["steps"] += [
        {
            "name": "Create annotated tag",
            "if": "steps.annotation.outputs.release_required == 'true'",
            "run": 'git tag -a "$TAG" "$MERGE_SHA" -m "Release $TAG"\ngit push origin "$TAG"\n',
        },
        {
            "name": "Publish phase release notes",
            "run": 'gh release create "$TAG" --title "$TAG" --notes-file release-notes.md\n',
        },
    ]
    problems = tag_push_violations(old)
    assert any("runs git tag" in problem for problem in problems), problems
    assert any("runs git push" in problem for problem in problems), problems
    assert any("runs gh release create" in problem for problem in problems), problems
    assert any("contents: write" in problem for problem in problems), problems
    assert any("no step prints" in problem for problem in problems), problems


def test_tag_push_gate_reds_on_a_workflow_without_its_checks() -> None:
    """Dropping the checks is a violation even when nothing is tagged."""
    problems = tag_push_violations(yaml.safe_load("jobs:\n  phase-release:\n    steps: []\n"))
    assert "the release annotation check is gone" in problems
    assert "the version source check is gone" in problems
    assert tag_push_violations(yaml.safe_load("jobs: {}\n")) == [
        "phase-release.yaml declares no 'phase-release' job"
    ]


# --- every dev and rc tag publishes as a prerelease -------------------------


def _resolved_outputs(step: dict[str, Any], version: str) -> dict[str, str]:
    """Run the version step's Python for *version* and parse its outputs."""
    match = _PY_HEREDOC.search(str(step.get("run", "")))
    if match is None:
        return {}
    buffer = io.StringIO()
    with mock.patch.object(eawf, "__version__", version), contextlib.redirect_stdout(buffer):
        exec(match.group("body"), {})
    return dict(line.split("=", 1) for line in buffer.getvalue().splitlines() if "=" in line)


def prerelease_flag_violations(workflow: dict[str, Any], text: str) -> list[str]:
    """Report every way plugin-release could publish a dev tag as the latest release.

    Args:
        workflow: The parsed plugin-release workflow.
        text: The raw workflow source, whose comments the parse drops.

    Returns:
        One problem per violation; empty when every dev and rc version
        resolves ``prerelease=true`` off the dist-tag and the
        create-or-edit step passes the flag on both branches.
    """
    jobs = workflow.get("jobs", {})
    problems: list[str] = []
    resolver = _find_step(jobs.get("package-validate", {}), "npm dist-tag")
    if resolver is None:
        problems.append("package-validate has no step resolving the dist-tag")
    else:
        for version in (*_PRERELEASE_VERSIONS, *_FINAL_VERSIONS):
            expected = "true" if dist_tag_for_version(version) == DIST_TAG_NEXT else "false"
            actual = _resolved_outputs(resolver, version).get("prerelease")
            if actual != expected:
                problems.append(f"version {version} resolves prerelease={actual}, not {expected}")
    release = _find_step(jobs.get("publish-source-host", {}), "tag release")
    if release is None:
        return [*problems, "publish-source-host has no create-or-edit release step"]
    run = str(release.get("run", ""))
    if "needs.package-validate.outputs.prerelease" not in str(release.get("env", {})):
        problems.append("the prerelease flag is not read from the resolved dist-tag")
    if not re.search(r'"\$\{PRERELEASE\}" = "true" \]; then\s+flag="--prerelease"', run):
        problems.append("a true prerelease output never selects --prerelease")
    if "gh release create" not in run or "gh release edit" not in run:
        problems.append("the release step does not create-or-edit the tag release")
    problems += [
        f"release call drops the prerelease flag: {line.strip()}"
        for line in run.splitlines()
        if _UNFLAGGED_RUN_LINE.search(line)
    ]
    if _STALE_RELEASE_COMMENT.search(text):
        problems.append("a comment still says phase-release.yaml creates the release")
    return problems


def test_prerelease_flag_on_dev_tag_release() -> None:
    """The live workflow flags every dev and rc tag, and only those."""
    text = _PLUGIN_RELEASE.read_text(encoding="utf-8")
    assert prerelease_flag_violations(yaml.safe_load(text), text) == []


def test_prerelease_gate_reds_on_an_unflagged_dev_release() -> None:
    """The gate fires when a dev tag would publish as the latest release.

    The defective workflow hardcodes the output, drops the flag from both
    release calls, and carries the comment that credited phase-release.
    """
    text = _PLUGIN_RELEASE.read_text(encoding="utf-8")
    broken = copy.deepcopy(yaml.safe_load(text))
    resolver = _find_step(broken["jobs"]["package-validate"], "npm dist-tag")
    release = _find_step(broken["jobs"]["publish-source-host"], "tag release")
    assert resolver is not None
    assert release is not None
    resolver["run"] = "uv run python - <<'PY'\nprint('prerelease=false')\nPY\n"
    release["env"].pop("PRERELEASE")
    release["run"] = (
        'if gh release view "${TAG}"; then\n'
        '  gh release edit "${TAG}" --notes-file RELEASE_NOTES.md\n'
        "else\n"
        '  gh release create "${TAG}" --title "${TAG}" --notes-file RELEASE_NOTES.md\n'
        "fi\n"
    )
    stale = "        # phase-release.yaml creates the release for a phase-close merge,\n"
    problems = prerelease_flag_violations(broken, text + stale)
    assert any("0.7.0.dev3 resolves prerelease=false" in p for p in problems), problems
    assert any("0.7.0rc1 resolves prerelease=false" in p for p in problems), problems
    assert not any("0.7.0 resolves" in p for p in problems), problems
    assert any("not read from the resolved dist-tag" in p for p in problems), problems
    assert any("never selects --prerelease" in p for p in problems), problems
    assert sum("drops the prerelease flag" in p for p in problems) == 2, problems
    assert any("phase-release.yaml creates the release" in p for p in problems), problems


def test_prerelease_gate_reds_on_missing_steps() -> None:
    """A workflow with neither step cannot vouch for the flag."""
    assert prerelease_flag_violations(yaml.safe_load("jobs: {}\n"), "") == [
        "package-validate has no step resolving the dist-tag",
        "publish-source-host has no create-or-edit release step",
    ]


# --- the rule text names the tag verb for release-train repos only ----------


def release_tag_rule_mismatches(body: str) -> list[str]:
    """Return every way a release or ship rule *body* misstates the tagging path.

    Args:
        body: The rule body, as authored in the profile or as rendered.

    Returns:
        One message per disagreement; empty when the verb is named only
        for release-train repos, every other repo keeps its own tag flow,
        and no sentence credits phase-release.yaml with tagging.
    """
    lines = body.splitlines()
    verb_lines = [line for line in lines if _RELEASE_TAG_VERB.search(line)]
    problems: list[str] = []
    if not verb_lines:
        problems.append("does not name eawf release tag --push")
    if any("release train" not in line for line in verb_lines):
        problems.append("names eawf release tag --push without scoping it to release-train repos")
    if not any("own tag flow" in line and "every other repo" in line.lower() for line in lines):
        problems.append("does not keep the repo's own tag flow for every other repo")
    sentences = (s for line in lines for s in re.split(r"(?<=[.;])\s+", line))
    if any("phase-release.yaml" in s and "tags the merge commit" in s for s in sentences):
        problems.append("still says phase-release.yaml tags the merge commit")
    return problems


def _core_rule_bodies() -> dict[str, str]:
    profile = yaml.safe_load(_CORE_PROFILE.read_text(encoding="utf-8"))
    return {
        block["id"]: block["body_template"]
        for block in profile["render_blocks"]
        if block["id"] in _RULE_IDS
    }


def test_rule_text_names_the_release_tag_verb() -> None:
    """Both rules, authored and rendered, scope the verb to release-train repos."""
    bodies = _core_rule_bodies()
    assert sorted(bodies) == sorted(_RULE_IDS)
    for rule_id, body in bodies.items():
        assert release_tag_rule_mismatches(body) == [], rule_id
        rendered = (_REPO_ROOT / "docs" / "rules" / f"{rule_id}.md").read_text(encoding="utf-8")
        assert release_tag_rule_mismatches(rendered) == [], f"re-render docs/rules/{rule_id}.md"


def test_rule_text_check_reds_on_the_workflow_tagging_text() -> None:
    """The check fires on the text that sent every repo to the workflow tag."""
    old = (
        "5. **Tag the release.** Under the ``per-phase`` cadence the post-merge "
        "``.github/workflows/phase-release.yaml`` reads the ``(release=v<X.Y.Z>)`` "
        "annotation off the phase-close commit, tags the merge commit, and publishes "
        "notes from the PR body.\n"
    )
    assert release_tag_rule_mismatches(old) == [
        "does not name eawf release tag --push",
        "does not keep the repo's own tag flow for every other repo",
        "still says phase-release.yaml tags the merge commit",
    ]


def test_rule_text_check_reds_on_an_unscoped_tag_verb() -> None:
    """Naming the verb for every repo is the other half of the defect."""
    unscoped = "Every repo tags with ``eawf release tag --push``.\n"
    assert release_tag_rule_mismatches(unscoped) == [
        "names eawf release tag --push without scoping it to release-train repos",
        "does not keep the repo's own tag flow for every other repo",
    ]
