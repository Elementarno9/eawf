"""The tag build reproduces the dry-run receipt, and the dry run renders every asset.

A release manifest can only be frozen before the tag if the tag build
produces the bytes the dry run described. Three defects broke that: the
tag's ``build-wheel`` ran without ``SOURCE_DATE_EPOCH``, so its wheel
digest differed from the receipt of the same commit; the PyPI
publication receipt digested the publish action's attestation files
alongside the dists; and the plugin bundle was a plain ``tar -czf``
rendered only on the tag, so neither it nor the ``SHA256SUMS`` over it
existed before the tag did.

The workflows run only on a tag push or a manual dispatch, so these
checks read their source and, where a step's behaviour is the claim,
run that step: the epoch export under ``bash`` in a scratch repository,
the digest assertion and the receipt writer over fixture dists, and the
render step over a fixture tree. Each check ships a companion that feeds
it the defective shape to prove it reds.
"""

from __future__ import annotations

import contextlib
import copy
import hashlib
import io
import json
import os
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest import mock

import pytest
import yaml

from eawf.workflow.release.pipeline_receipts import RECEIPT_FILENAMES
from eawf.workflow.release.reproducibility import (
    SOURCE_DATE_EPOCH_ENV,
    compare_builds,
    digest_build_output,
)
from eawf.workflow.release.source_host_assets import (
    CHECKSUMS_FILENAME,
    RELEASE_NOTES_FILENAME,
    source_host_assets,
)

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[2]
_WORKFLOWS = _REPO_ROOT / ".github" / "workflows"
_RELEASE = _WORKFLOWS / "release.yaml"
_PLUGIN_RELEASE = _WORKFLOWS / "plugin-release.yaml"

_BUILD_JOB = "build-wheel"
_RECEIPT_JOB = "inventory-and-reproducibility"
_RECEIPT_ARTIFACT = "reproducible-build-receipt"
_RENDER_JOB = "render-release-assets"
_RENDER_ARTIFACT = "release-assets"
_BUNDLE_MODULE = "eawf.workflow.release.plugin_bundle"
_SHA_EXPRESSION = "${{ github.sha }}"

#: The render step's builder call, and the ``tar`` call it replaced.
_BUILDER_CALL = (
    f'uv run python -m {_BUNDLE_MODULE} \\\n  build/eawf-plugin "release-assets/${{bundle}}"'
)
_TAR_CALL = 'tar -czf "release-assets/${bundle}" -C build/eawf-plugin .'

_PY_HEREDOC = re.compile(r"<<'PY'\n(?P<body>.*?)^PY$", flags=re.DOTALL | re.MULTILINE)
_TAR_CREATE = re.compile(r"\btar\s+-[A-Za-z]*c")
_PUBLISHING_COMMANDS: dict[str, re.Pattern[str]] = {
    "npm publish": re.compile(r"\bnpm\s+publish\b"),
    "gh release": re.compile(r"\bgh\s+release\b"),
    "git push": re.compile(r"\bgit\s+push\b"),
}

VERSION = "1.0.0.dev3"
WHEEL = "eawf-1.0.0.dev3-py3-none-any.whl"
SDIST = "eawf-1.0.0.dev3.tar.gz"
EPOCH = 1_789_571_840
SHA = "a" * 40

#: Committer and author times of the scratch commit. They differ so an
#: export of the author time reds as surely as one of the wall clock.
_COMMITTER_DATE = datetime(2026, 1, 1, tzinfo=UTC)
_AUTHOR_DATE = datetime(2020, 1, 1, tzinfo=UTC)


def _load(path: Path) -> dict[str, Any]:
    workflow: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
    return workflow


def _steps(job: dict[str, Any]) -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = job.get("steps", [])
    return steps


def _run_text(step: dict[str, Any]) -> str:
    return str(step.get("run", ""))


def _needs(job: dict[str, Any]) -> list[str]:
    needs = job.get("needs", [])
    return [needs] if isinstance(needs, str) else list(needs)


def _first(steps: list[dict[str, Any]], needle: str) -> int | None:
    """Return the index of the first step whose ``run`` contains *needle*."""
    return next((i for i, step in enumerate(steps) if needle in _run_text(step)), None)


def _download(steps: list[dict[str, Any]], artifact: str) -> tuple[int, dict[str, Any]] | None:
    """Return the index and ``with`` block of the step downloading *artifact*."""
    for index, step in enumerate(steps):
        with_block = step.get("with") or {}
        if str(step.get("uses", "")).startswith("actions/download-artifact") and (
            with_block.get("name") == artifact
        ):
            return index, with_block
    return None


def _write_dist(root: Path, *, wheel: bytes = b"wheel", attestations: bool = False) -> Path:
    """Write a fixture ``dist/`` under *root* and return it."""
    dist = root / "dist"
    dist.mkdir(parents=True)
    (dist / WHEEL).write_bytes(wheel)
    (dist / SDIST).write_bytes(b"sdist")
    if attestations:
        (dist / f"{WHEEL}.publish.attestation").write_bytes(b"{}")
        (dist / f"{SDIST}.publish.attestation").write_bytes(b"{}")
    return dist


def _run_heredoc(step: dict[str, Any], *, cwd: Path, env: dict[str, str]) -> int:
    """Execute the step's Python heredoc in *cwd* and return its exit code."""
    match = _PY_HEREDOC.search(_run_text(step))
    if match is None:
        return -1
    sink = io.StringIO()
    with (
        contextlib.chdir(cwd),
        mock.patch.dict(os.environ, env),
        contextlib.redirect_stdout(sink),
        contextlib.redirect_stderr(sink),
    ):
        try:
            exec(match.group("body"), {"__name__": "__main__"})
        except SystemExit as exited:
            return exited.code if isinstance(exited.code, int) else 1
    return 0


def _clean_env(**overrides: str) -> dict[str, str]:
    """Return the process env without ``GIT_*``, which would retarget git at the repo."""
    inherited = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    return {**inherited, **overrides}


def _run_shell(step: dict[str, Any], *, cwd: Path, env: dict[str, str]) -> int:
    """Run the step's ``run`` script under bash in *cwd* and return its exit code."""
    completed = subprocess.run(
        ["bash", "-c", _run_text(step)],
        cwd=cwd,
        env=_clean_env(**env),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    return completed.returncode


# --- build-wheel pins the epoch and reproduces the receipt -------------------


def _scratch_commit(repo: Path) -> dict[str, str]:
    """Create a one-commit repository at *repo* and return a git env for it."""
    repo.mkdir(parents=True)
    env = {
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "Release Fixture",
        "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
        "GIT_COMMITTER_NAME": "Release Fixture",
        "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
        "GIT_AUTHOR_DATE": _AUTHOR_DATE.isoformat(),
        "GIT_COMMITTER_DATE": _COMMITTER_DATE.isoformat(),
    }
    for argv in (
        ["git", "init", "-q"],
        ["git", "-c", "core.hooksPath=/dev/null", "commit", "-q", "--allow-empty", "-m", "r"],
    ):
        subprocess.run(argv, cwd=repo, env=_clean_env(**env), check=True, timeout=30)
    return env


def _exported_epoch(step: dict[str, Any], scratch: Path) -> str | None:
    """Run the export step against a scratch commit and return what it exported."""
    repo = scratch / "repo"
    git_env = _scratch_commit(repo)
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        env=_clean_env(**git_env),
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    ).stdout.strip()
    github_env = scratch / "github_env"
    step_env = {
        str(key): str(value).replace(_SHA_EXPRESSION, sha)
        for key, value in (step.get("env") or {}).items()
    }
    _run_shell(step, cwd=repo, env={**git_env, **step_env, "GITHUB_ENV": str(github_env)})
    lines = github_env.read_text(encoding="utf-8").splitlines() if github_env.exists() else []
    prefix = f"{SOURCE_DATE_EPOCH_ENV}="
    return next((line.removeprefix(prefix) for line in lines if line.startswith(prefix)), None)


def epoch_export_problems(
    job_name: str, job: dict[str, Any], *, consumer: str, scratch: Path
) -> list[str]:
    """Report every way *job* could run *consumer* without the commit's epoch.

    Args:
        job_name: The job, for the messages.
        job: The parsed job.
        consumer: Text of the command that must see the pinned epoch.
        scratch: Empty directory the export step is run in.

    Returns:
        One problem per violation; empty when a step exports the built
        commit's committer time into ``GITHUB_ENV`` before *consumer*.
    """
    steps = _steps(job)
    export = _first(steps, f"{SOURCE_DATE_EPOCH_ENV}=")
    if export is None:
        return [f"no {job_name} step exports {SOURCE_DATE_EPOCH_ENV}"]
    problems: list[str] = []
    used = _first(steps, consumer)
    if used is None:
        problems.append(f"no {job_name} step runs {consumer!r}")
    elif used < export:
        problems.append(f"{job_name} runs {consumer!r} before {SOURCE_DATE_EPOCH_ENV} is exported")
    expected = str(int(_COMMITTER_DATE.timestamp()))
    exported = _exported_epoch(steps[export], scratch)
    if exported != expected:
        problems.append(
            f"{job_name} exports {SOURCE_DATE_EPOCH_ENV}={exported!r}, not the built "
            f"commit's committer time {expected!r}"
        )
    return problems


def _write_receipt(root: Path, *, source_sha: str, wheel: bytes) -> Path:
    """Write a reproduced receipt for a dist whose wheel holds *wheel*."""
    built = digest_build_output(_write_dist(root, wheel=wheel), attempt=1, source_date_epoch=EPOCH)
    attempts = [built, built.model_copy(update={"attempt": 2})]
    receipt = compare_builds(attempts, source_sha=source_sha, pre_upload=built.artifacts)
    path = root / RECEIPT_FILENAMES[_RECEIPT_ARTIFACT]
    path.write_text(receipt.model_dump_json(), encoding="utf-8")
    return path


def _assertion_verdicts(step: dict[str, Any], scratch: Path) -> dict[str, int]:
    """Run the digest assertion over one matching and three diverging builds."""
    build = scratch / "build"
    _write_dist(build)
    cases = {
        "matching": (SHA, b"wheel", EPOCH),
        "diverging": (SHA, b"other wheel", EPOCH),
        "other-commit": ("b" * 40, b"wheel", EPOCH),
        "other-epoch": (SHA, b"wheel", EPOCH + 1),
    }
    verdicts: dict[str, int] = {}
    for case, (source_sha, wheel, epoch) in cases.items():
        receipt = _write_receipt(scratch / case, source_sha=source_sha, wheel=wheel)
        env = {"RECEIPT": str(receipt), "SHA": SHA, SOURCE_DATE_EPOCH_ENV: str(epoch)}
        verdicts[case] = _run_heredoc(step, cwd=build, env=env)
    return verdicts


def receipt_assertion_problems(job: dict[str, Any], scratch: Path) -> list[str]:
    """Report every way build-wheel could upload dists the receipt does not attest.

    Args:
        job: The parsed build-wheel job.
        scratch: Empty directory the assertion is exercised in.

    Returns:
        One problem per violation; empty when the receipt is downloaded
        outside the workspace, and the step right before the dist upload
        passes a matching build and fails a diverging wheel, a receipt
        for another commit, and a receipt pinned to another epoch.
    """
    steps = _steps(job)
    download = _download(steps, _RECEIPT_ARTIFACT)
    if download is None:
        return [f"{_BUILD_JOB} downloads no {_RECEIPT_ARTIFACT!r} artifact"]
    problems: list[str] = []
    location = str(download[1].get("path", ""))
    if "runner.temp" not in location:
        problems.append(
            f"{_BUILD_JOB} downloads the receipt into {location!r}, inside the "
            f"workspace the sdist and dist/ are built from"
        )
    check = next((i for i, step in enumerate(steps) if "RECEIPT" in (step.get("env") or {})), None)
    if check is None:
        return [*problems, f"no {_BUILD_JOB} step asserts the digests against the receipt"]
    step = steps[check]
    receipt_ref = str(step["env"]["RECEIPT"])
    if location not in receipt_ref or RECEIPT_FILENAMES[_RECEIPT_ARTIFACT] not in receipt_ref:
        problems.append("the digest assertion does not read the downloaded receipt")
    if str((step.get("env") or {}).get("SHA", "")) != _SHA_EXPRESSION:
        problems.append("the digest assertion does not compare against the built commit")
    upload = _download_upload_index(steps)
    build = _first(steps, "uv build")
    if build is None or upload is None or not build < check == upload - 1:
        problems.append("the digest assertion is not the last step before the dist upload")
    verdicts = _assertion_verdicts(step, scratch)
    if verdicts["matching"] != 0:
        problems.append("the digest assertion rejects a build that matches the receipt")
    for case, what in (
        ("diverging", "a wheel the receipt does not attest"),
        ("other-commit", "a receipt for another commit"),
        ("other-epoch", "a receipt pinned to another epoch"),
    ):
        if verdicts[case] == 0:
            problems.append(f"the digest assertion accepts {what}")
    return problems


def _download_upload_index(steps: list[dict[str, Any]]) -> int | None:
    """Return the index of the step uploading the ``dist`` artifact."""
    return next(
        (
            index
            for index, step in enumerate(steps)
            if str(step.get("uses", "")).startswith("actions/upload-artifact")
            and (step.get("with") or {}).get("name") == "dist"
        ),
        None,
    )


def source_date_epoch_violations(workflow: dict[str, Any], scratch: Path) -> list[str]:
    """Report every way release.yaml could publish a build the receipt did not attest.

    Args:
        workflow: The parsed release workflow.
        scratch: Empty directory the steps are exercised in.

    Returns:
        One problem per violation; empty when build-wheel needs the
        receipt job, pins the committer epoch before building, and
        asserts its digests equal the receipt's before uploading.
    """
    job = workflow.get("jobs", {}).get(_BUILD_JOB)
    if job is None:
        return [f"release.yaml declares no {_BUILD_JOB!r} job"]
    problems: list[str] = []
    if _RECEIPT_JOB not in _needs(job):
        problems.append(f"{_BUILD_JOB} does not need {_RECEIPT_JOB}, so it has no receipt to match")
    problems += epoch_export_problems(
        _BUILD_JOB, job, consumer="uv build", scratch=scratch / "epoch"
    )
    return problems + receipt_assertion_problems(job, scratch / "assert")


def pypi_receipt_digest_violations(workflow: dict[str, Any], scratch: Path) -> list[str]:
    """Report every way the PyPI receipt could digest files PyPI does not serve.

    Args:
        workflow: The parsed release workflow.
        scratch: Empty directory the receipt writer is run in.

    Returns:
        One problem per violation; empty when the writer, run over a
        dist holding a wheel, an sdist and their attestation files,
        digests exactly the wheel and the sdist.
    """
    job = workflow.get("jobs", {}).get("publish-pypi")
    if job is None:
        return ["release.yaml declares no 'publish-pypi' job"]
    writer = _first(_steps(job), "publication-receipt-")
    if writer is None:
        return ["publish-pypi writes no publication receipt"]
    _write_dist(scratch, attestations=True)
    env = {
        "TARGET_ID": "pypi",
        "RECEIPT_VERSION": f"v{VERSION}",
        "JOB_CONCLUSION": "success",
        "RUN_ID": "1",
    }
    code = _run_heredoc(_steps(job)[writer], cwd=scratch, env=env)
    written = scratch / "publication-receipt-pypi.json"
    if code != 0 or not written.is_file():
        return [f"the PyPI receipt writer exits {code} without writing its receipt"]
    digests = json.loads(written.read_text(encoding="utf-8"))["artifact_digests"]
    expected = {
        name: f"sha256:{hashlib.sha256((scratch / 'dist' / name).read_bytes()).hexdigest()}"
        for name in (WHEEL, SDIST)
    }
    problems = [
        f"the PyPI receipt digests {name!r}, which PyPI does not serve"
        for name in sorted(set(digests) - set(expected))
    ]
    problems += [
        f"the PyPI receipt records {name!r} as {digests.get(name)!r}, not {digest!r}"
        for name, digest in sorted(expected.items())
        if digests.get(name) != digest
    ]
    return problems


def test_source_date_epoch_pinned_in_build_wheel(tmp_path: Path) -> None:
    """The live build pins the committer epoch and reproduces the receipt."""
    assert source_date_epoch_violations(_load(_RELEASE), tmp_path) == []


def test_source_date_epoch_receipt_skips_attestations(tmp_path: Path) -> None:
    """The live PyPI receipt digests the wheel and the sdist, nothing else."""
    assert pypi_receipt_digest_violations(_load(_RELEASE), tmp_path) == []


def _unpinned_release() -> dict[str, Any]:
    """Return release.yaml as it was before the build was pinned.

    The export, the receipt download and the assertion are dropped, the
    job needs nothing, and the receipt writer digests all of ``dist/``.
    """
    workflow = copy.deepcopy(_load(_RELEASE))
    job = workflow["jobs"][_BUILD_JOB]
    job.pop("needs")
    job["steps"] = [
        step
        for step in _steps(job)
        if SOURCE_DATE_EPOCH_ENV not in _run_text(step)
        and "RECEIPT" not in (step.get("env") or {})
        and _download([step], _RECEIPT_ARTIFACT) is None
    ]
    writer = workflow["jobs"]["publish-pypi"]["steps"]
    index = _first(writer, "publication-receipt-")
    assert index is not None
    writer[index]["run"] = re.sub(
        r"if path\.is_file\(\).*$",
        "if path.is_file()",
        writer[index]["run"],
        flags=re.MULTILINE,
    )
    return workflow


def test_source_date_epoch_gate_reds_on_an_unpinned_build(tmp_path: Path) -> None:
    """The gate fires on the shape that shipped dev2 with a foreign wheel digest."""
    workflow = _unpinned_release()
    problems = source_date_epoch_violations(workflow, tmp_path / "build")
    assert problems == [
        f"{_BUILD_JOB} does not need {_RECEIPT_JOB}, so it has no receipt to match",
        f"no {_BUILD_JOB} step exports {SOURCE_DATE_EPOCH_ENV}",
        f"{_BUILD_JOB} downloads no {_RECEIPT_ARTIFACT!r} artifact",
    ]
    receipt_problems = pypi_receipt_digest_violations(workflow, tmp_path / "receipt")
    assert receipt_problems == [
        f"the PyPI receipt digests '{WHEEL}.publish.attestation', which PyPI does not serve",
        f"the PyPI receipt digests '{SDIST}.publish.attestation', which PyPI does not serve",
    ]


def test_source_date_epoch_gate_reds_on_a_clock_epoch_and_a_blind_assertion(
    tmp_path: Path,
) -> None:
    """Each piece present but wrong still reds.

    The export reads the author time, the receipt lands in ``dist/``,
    the assertion is not the last step before the upload and passes
    anything, and it compares against the tag name rather than the sha.
    """
    workflow = copy.deepcopy(_load(_RELEASE))
    steps = _steps(workflow["jobs"][_BUILD_JOB])
    export = _first(steps, f"{SOURCE_DATE_EPOCH_ENV}=")
    assert export is not None
    steps[export]["run"] = steps[export]["run"].replace("%ct", "%at")
    download = _download(steps, _RECEIPT_ARTIFACT)
    assert download is not None
    download[1]["path"] = "dist/receipt"
    check = next(i for i, step in enumerate(steps) if "RECEIPT" in (step.get("env") or {}))
    blind = steps.pop(check)
    blind["env"]["RECEIPT"] = "dist/receipt/reproducible-build-receipt.json"
    blind["env"]["SHA"] = "${{ github.ref_name }}"
    blind["run"] = "uv run python - <<'PY'\nprint('digests look fine')\nPY\n"
    steps.insert(check - 1, blind)

    problems = source_date_epoch_violations(workflow, tmp_path)

    committer = str(int(_COMMITTER_DATE.timestamp()))
    author = str(int(_AUTHOR_DATE.timestamp()))
    assert problems == [
        f"{_BUILD_JOB} exports {SOURCE_DATE_EPOCH_ENV}={author!r}, not the built "
        f"commit's committer time {committer!r}",
        f"{_BUILD_JOB} downloads the receipt into 'dist/receipt', inside the "
        "workspace the sdist and dist/ are built from",
        "the digest assertion does not compare against the built commit",
        "the digest assertion is not the last step before the dist upload",
        "the digest assertion accepts a wheel the receipt does not attest",
        "the digest assertion accepts a receipt for another commit",
        "the digest assertion accepts a receipt pinned to another epoch",
    ]


def test_source_date_epoch_gate_reds_on_a_missing_job(tmp_path: Path) -> None:
    """A release workflow with neither job cannot vouch for either claim."""
    empty = yaml.safe_load("jobs: {}\n")
    assert source_date_epoch_violations(empty, tmp_path) == [
        f"release.yaml declares no {_BUILD_JOB!r} job"
    ]
    assert pypi_receipt_digest_violations(empty, tmp_path) == [
        "release.yaml declares no 'publish-pypi' job"
    ]


# --- the dry run renders the npm tarball, the bundle and SHA256SUMS ----------


def _uv_shim(bin_dir: Path) -> Path:
    """Write a ``uv`` that runs ``uv run python ...`` with this interpreter."""
    bin_dir.mkdir(parents=True)
    shim = bin_dir / "uv"
    shim.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = run ] && [ "$2" = python ]; then\n'
        "  shift 2\n"
        f'  exec "{sys.executable}" "$@"\n'
        "fi\n"
        'echo "unexpected uv call: $*" >&2\n'
        "exit 97\n",
        encoding="utf-8",
    )
    shim.chmod(0o755)
    return bin_dir


def _render_fixture(root: Path, *, tree_mtime: int) -> Path:
    """Lay out what the render step finds: the tree, the dist and the asset dir."""
    tree = root / "build" / "eawf-plugin"
    (tree / ".claude-plugin").mkdir(parents=True)
    (tree / ".claude-plugin" / "plugin.json").write_text('{"name": "eawf"}\n', encoding="utf-8")
    hook = tree / "hooks" / "session-start.sh"
    hook.parent.mkdir()
    hook.write_text("#!/bin/sh\n", encoding="utf-8")
    hook.chmod(0o755)
    for path in [tree, *tree.rglob("*")]:
        os.utime(path, (tree_mtime, tree_mtime))
    _write_dist(root)
    (root / _RENDER_ARTIFACT).mkdir()
    return root


def _render_once(step: dict[str, Any], root: Path, *, shim: Path) -> dict[str, bytes] | None:
    """Run the render step in *root* and return the asset files it wrote."""
    env = {
        "PATH": f"{shim}{os.pathsep}{os.environ.get('PATH', '')}",
        "PLUGIN_VERSION": VERSION,
        "NPM_VERSION": "1.0.0-dev.3",
        SOURCE_DATE_EPOCH_ENV: str(EPOCH),
    }
    if _run_shell(step, cwd=root, env=env) != 0:
        return None
    return {path.name: path.read_bytes() for path in sorted((root / _RENDER_ARTIFACT).iterdir())}


def _checksums(sums: bytes) -> dict[str, str]:
    """Parse ``SHA256SUMS`` into ``filename -> digest``."""
    pairs = (line.split(maxsplit=1) for line in sums.decode("utf-8").splitlines() if line)
    return {name.lstrip("*"): digest for digest, name in pairs}


def rendered_asset_problems(step: dict[str, Any], scratch: Path) -> list[str]:
    """Report what the render step fails to produce, twice, over one fixture.

    Args:
        step: The step that builds the bundle, notes and checksums.
        scratch: Empty directory the step is run in.

    Returns:
        One problem per violation; empty when two runs over trees with
        different mtimes each write the three source-host assets, the
        bundle bytes agree, the checksums cover the wheel, the sdist and
        the bundle at their real digests, and the notes name the tag the
        version implies.
    """
    shim = _uv_shim(scratch / "bin")
    renders = [
        _render_once(step, _render_fixture(scratch / name, tree_mtime=mtime), shim=shim)
        for name, mtime in (("first", 1_000_000_000), ("second", 1_900_000_000))
    ]
    first, second = renders
    if first is None or second is None:
        return ["the render step exits non-zero over a complete fixture"]
    assets = source_host_assets(VERSION)
    problems = [f"the dry run renders no {name!r}" for name in assets.values() if name not in first]
    bundle = next(name for name in assets.values() if name.endswith(".tar.gz"))
    if bundle in first and first[bundle] != second.get(bundle):
        problems.append("two renders of one tree write different plugin bundles")
    if CHECKSUMS_FILENAME in first:
        expected = {
            name: hashlib.sha256(content).hexdigest()
            for name, content in {
                WHEEL: b"wheel",
                SDIST: b"sdist",
                bundle: first.get(bundle, b""),
            }.items()
        }
        if _checksums(first[CHECKSUMS_FILENAME]) != expected:
            problems.append(f"{CHECKSUMS_FILENAME} does not cover the dist and the bundle")
    if f"v{VERSION}" not in first.get(RELEASE_NOTES_FILENAME, b"").decode("utf-8"):
        problems.append("the release notes do not name the tag the version implies")
    return problems


def _job_publishing_problems(name: str, job: dict[str, Any]) -> list[str]:
    """Report the ways the render job could publish instead of rendering."""
    problems = [
        f"the {name} job runs {command}"
        for command, pattern in _PUBLISHING_COMMANDS.items()
        if any(pattern.search(_run_text(step)) for step in _steps(job))
    ]
    granted = job.get("permissions")
    if not isinstance(granted, dict) or granted.get("contents") != "read":
        problems.append(f"the {name} job's token is not read-only")
    elif granted.get("id-token") == "write":
        problems.append(f"the {name} job can mint a publishing OIDC token")
    return problems


def _consumer_problems(jobs: dict[str, Any]) -> list[str]:
    """Report publish jobs that re-render instead of shipping the rendered files."""
    problems: list[str] = []
    for name in ("publish-claude-npm", "publish-source-host"):
        job = jobs.get(name, {})
        if _RENDER_JOB not in _needs(job) or _download(_steps(job), _RENDER_ARTIFACT) is None:
            problems.append(f"{name} does not publish the files {_RENDER_JOB} rendered")
        for needle in ("npm pack", _BUNDLE_MODULE, "sha256sum"):
            if _first(_steps(job), needle) is not None:
                problems.append(f"{name} re-renders with {needle!r}")
    tarball = _steps_env(jobs.get("publish-claude-npm", {}), "TARBALL")
    if f"needs.{_RENDER_JOB}.outputs.npm_tarball" not in tarball:
        problems.append("the npm publish does not name the tarball the render packed")
    return problems


def _steps_env(job: dict[str, Any], key: str) -> str:
    """Return the first value any step of *job* binds *key* to, or ``""``."""
    return next(
        (str(step["env"][key]) for step in _steps(job) if key in (step.get("env") or {})), ""
    )


def dry_run_render_violations(workflow: dict[str, Any], scratch: Path) -> list[str]:
    """Report every way the dry run could leave the manifest unrenderable.

    Args:
        workflow: The parsed plugin-release workflow.
        scratch: Empty directory the render step is run in.

    Returns:
        One problem per violation; empty when a job that also runs on
        ``workflow_dispatch`` packs the npm tarball, builds the bundle
        with the stdlib builder under the pinned epoch, writes the notes
        and ``SHA256SUMS``, uploads them, cannot publish, and is the one
        source the publish jobs ship from, with no ``tar`` left anywhere.
    """
    jobs = workflow.get("jobs", {})
    problems = [
        f"job {name!r} still builds an archive with tar"
        for name, job in jobs.items()
        if any(_TAR_CREATE.search(_run_text(step)) for step in _steps(job))
    ]
    job = jobs.get(_RENDER_JOB)
    if job is None:
        return [*problems, f"plugin-release.yaml declares no {_RENDER_JOB!r} job"]
    if "if" in job:
        problems.append(f"the {_RENDER_JOB} job is conditional, so a dry run can skip it")
    steps = _steps(job)
    pack = _first(steps, "npm pack")
    if pack is None or _RENDER_ARTIFACT not in _run_text(steps[pack]):
        problems.append(f"the dry run packs no npm tarball into {_RENDER_ARTIFACT}/")
    elif "steps.pack.outputs.npm_tarball" not in str(job.get("outputs", {})):
        problems.append("the packed tarball's name is not a job output")
    render = _first(steps, CHECKSUMS_FILENAME)
    if render is None:
        problems.append(f"the dry run renders no {CHECKSUMS_FILENAME}")
    else:
        if _BUNDLE_MODULE not in _run_text(steps[render]):
            problems.append("the dry run renders no plugin bundle with the stdlib builder")
        problems += epoch_export_problems(
            _RENDER_JOB, job, consumer=_BUNDLE_MODULE, scratch=scratch / "epoch"
        )
        problems += rendered_asset_problems(steps[render], scratch / "render")
    uploads = [
        step
        for step in steps
        if str(step.get("uses", "")).startswith("actions/upload-artifact")
        and (step.get("with") or {}).get("name") == _RENDER_ARTIFACT
    ]
    if not uploads or not str(uploads[0]["with"].get("path", "")).startswith(_RENDER_ARTIFACT):
        problems.append(f"the dry run uploads no {_RENDER_ARTIFACT!r} artifact")
    return problems + _job_publishing_problems(_RENDER_JOB, job) + _consumer_problems(jobs)


def test_dry_run_renders_pack_bundle_and_sums(tmp_path: Path) -> None:
    """The live dry run renders all three and the publish jobs ship them."""
    assert dry_run_render_violations(_load(_PLUGIN_RELEASE), tmp_path) == []


def _bundleless_dry_run() -> dict[str, Any]:
    """Return plugin-release.yaml as it was before the dry run rendered anything.

    The render job is gone, and the source-host leg tars the tree and
    writes the checksums itself, on the tag push only.
    """
    workflow = copy.deepcopy(_load(_PLUGIN_RELEASE))
    jobs = workflow["jobs"]
    render = jobs.pop(_RENDER_JOB)
    for name in ("publish-claude-npm", "publish-source-host"):
        jobs[name]["needs"] = [n for n in _needs(jobs[name]) if n != _RENDER_JOB]
    source_host = jobs["publish-source-host"]
    source_host["steps"] = [
        step for step in _steps(source_host) if _download([step], _RENDER_ARTIFACT) is None
    ]
    assemble = copy.deepcopy(_steps(render)[_first(_steps(render), CHECKSUMS_FILENAME) or 0])
    assemble["run"] = assemble["run"].replace(_BUILDER_CALL, _TAR_CALL)
    source_host["steps"].insert(1, assemble)
    return workflow


def test_dry_run_gate_reds_on_a_bundleless_dry_run(tmp_path: Path) -> None:
    """The gate fires on the shape that rendered nothing before the tag."""
    problems = dry_run_render_violations(_bundleless_dry_run(), tmp_path)
    assert problems == [
        "job 'publish-source-host' still builds an archive with tar",
        f"plugin-release.yaml declares no {_RENDER_JOB!r} job",
    ]


def test_dry_run_gate_reds_on_a_render_without_bundle_or_epoch(tmp_path: Path) -> None:
    """A render job that skips the builder, the epoch and the upload reds on each."""
    workflow = copy.deepcopy(_load(_PLUGIN_RELEASE))
    job = workflow["jobs"][_RENDER_JOB]
    steps = _steps(job)
    render = _first(steps, CHECKSUMS_FILENAME)
    assert render is not None
    steps[render]["run"] = steps[render]["run"].replace(_BUILDER_CALL, 'echo "bundle skipped"')
    job["steps"] = [
        step
        for step in steps
        if f"{SOURCE_DATE_EPOCH_ENV}=" not in _run_text(step)
        and str(step.get("uses", "")).split("@")[0] != "actions/upload-artifact"
    ]

    problems = dry_run_render_violations(workflow, tmp_path)

    assert problems == [
        "the dry run renders no plugin bundle with the stdlib builder",
        f"no {_RENDER_JOB} step exports {SOURCE_DATE_EPOCH_ENV}",
        "the render step exits non-zero over a complete fixture",
        f"the dry run uploads no {_RENDER_ARTIFACT!r} artifact",
    ]


def test_dry_run_gate_reds_on_a_tar_bundle_in_the_render(tmp_path: Path) -> None:
    """Rendering on the dry run is not enough: a tar bundle differs per checkout."""
    workflow = copy.deepcopy(_load(_PLUGIN_RELEASE))
    steps = _steps(workflow["jobs"][_RENDER_JOB])
    render = _first(steps, CHECKSUMS_FILENAME)
    assert render is not None
    steps[render]["run"] = steps[render]["run"].replace(_BUILDER_CALL, _TAR_CALL)

    problems = dry_run_render_violations(workflow, tmp_path)

    assert problems == [
        f"job {_RENDER_JOB!r} still builds an archive with tar",
        "the dry run renders no plugin bundle with the stdlib builder",
        f"no {_RENDER_JOB} step runs {_BUNDLE_MODULE!r}",
        "two renders of one tree write different plugin bundles",
    ]


def test_dry_run_gate_reds_on_a_publishing_render(tmp_path: Path) -> None:
    """A render job that can publish, and publish jobs that repack, both red."""
    workflow = copy.deepcopy(_load(_PLUGIN_RELEASE))
    jobs = workflow["jobs"]
    render = jobs[_RENDER_JOB]
    render["if"] = "github.event_name == 'push'"
    render["permissions"] = {"contents": "read", "id-token": "write"}
    render["steps"].append({"name": "Ship it", "run": 'npm publish "${TARBALL}"\n'})
    npm = jobs["publish-claude-npm"]
    step = next(s for s in _steps(npm) if "TARBALL" in (s.get("env") or {}))
    step["env"].pop("TARBALL")
    step["run"] = "tarball=$(npm pack --silent)\n" + step["run"]

    problems = dry_run_render_violations(workflow, tmp_path)

    assert problems == [
        f"the {_RENDER_JOB} job is conditional, so a dry run can skip it",
        f"the {_RENDER_JOB} job runs npm publish",
        f"the {_RENDER_JOB} job can mint a publishing OIDC token",
        "publish-claude-npm re-renders with 'npm pack'",
        "the npm publish does not name the tarball the render packed",
    ]


def test_dry_run_gate_reds_on_a_missing_workflow_body(tmp_path: Path) -> None:
    """A plugin-release workflow with no jobs cannot render anything."""
    problems = dry_run_render_violations(yaml.safe_load("jobs: {}\n"), tmp_path)
    assert problems == [f"plugin-release.yaml declares no {_RENDER_JOB!r} job"]
