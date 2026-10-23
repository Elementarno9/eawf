"""Release tags come from the release verb, and dev tags publish as prereleases.

A release object created without the prerelease flag advertises a dev
checkpoint as the latest release. The defect sits in workflow source that
runs only after a tag push, so these checks read the source, and each ships
a companion that feeds the checker the defective shape to prove it reds.

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
_PLUGIN_RELEASE = _WORKFLOWS / "plugin-release.yaml"
_CORE_PROFILE = _REPO_ROOT / "src" / "eawf" / "platform" / "profiles" / "data" / "core.yaml"
_RULE_IDS = ("release-process", "ship-process")

_RELEASE_TAG_VERB = re.compile(r"eawf release pipeline\b[^`\n]*--publish")

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


def release_rule_mismatches(body: str) -> list[str]:
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
        problems.append("does not name eawf release pipeline --publish")
    if any("release train" not in line for line in verb_lines):
        problems.append(
            "names eawf release pipeline --publish without scoping it to release-train repos"
        )
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


def test_rule_text_names_the_release_pipeline_verb() -> None:
    """Both rules, authored and rendered, scope the verb to release-train repos."""
    bodies = _core_rule_bodies()
    assert sorted(bodies) == sorted(_RULE_IDS)
    for rule_id, body in bodies.items():
        assert release_rule_mismatches(body) == [], rule_id
        rendered = (_REPO_ROOT / "docs" / "rules" / f"{rule_id}.md").read_text(encoding="utf-8")
        assert release_rule_mismatches(rendered) == [], f"re-render docs/rules/{rule_id}.md"


def test_rule_text_check_reds_on_the_workflow_tagging_text() -> None:
    """The check fires on the text that sent every repo to the workflow tag."""
    old = (
        "5. **Tag the release.** Under the ``per-phase`` cadence the post-merge "
        "``.github/workflows/phase-release.yaml`` reads the ``(release=v<X.Y.Z>)`` "
        "annotation off the phase-close commit, tags the merge commit, and publishes "
        "notes from the PR body.\n"
    )
    assert release_rule_mismatches(old) == [
        "does not name eawf release pipeline --publish",
        "does not keep the repo's own tag flow for every other repo",
        "still says phase-release.yaml tags the merge commit",
    ]


def test_rule_text_check_reds_on_an_unscoped_tag_verb() -> None:
    """Naming the verb for every repo is the other half of the defect."""
    unscoped = "Every repo releases with ``eawf release pipeline <version> --publish``.\n"
    assert release_rule_mismatches(unscoped) == [
        "names eawf release pipeline --publish without scoping it to release-train repos",
        "does not keep the repo's own tag flow for every other repo",
    ]
