"""The skill bundle's publication unit: every digest a shipped bundle is checked by.

A bundle ships the catalog, each skill's prompt, argument schema, RPC
allowlist and output schema, and the rendered files built from them. They
are only correct together: a prompt re-rendered from a changed catalog, a
hand-edited ``SKILL.md`` or a hook left over from an older render each
leaves the bundle saying one thing and doing another. The packagers write
one :class:`Publication` beside the rendered files, and
:func:`publication_findings` re-derives every digest from the installed
catalog and from the files on disk and compares them as one unit.

The publication also records the engine schema epoch the bundle targets.
An installed bundle whose epoch differs from its repository refuses to
operate: :func:`require_bundle_epoch` names the migration instead of letting
the bundle fail obscurely against records it does not understand.

Run as ``python -m eawf.workflow.skills.publication <bundle-root>`` to check a
rendered bundle before it is archived.
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import sys
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Final, Literal

import orjson
from pydantic import BaseModel, ConfigDict, ValidationError

from eawf.workflow.skills.census import epoch1_nouns

logger = logging.getLogger(__name__)

#: The engine schema epoch every bundle this build renders targets.
BUNDLE_TARGET_EPOCH: Final = 2

#: Where the publication sits, relative to the bundle root.
PUBLICATION_FILENAME: Final = "eawf-publication.json"

#: The command that moves a repository to the epoch a bundle targets.
MIGRATION_COMMAND: Final = "eawf migrate status"

#: The file kinds the epoch-1 noun census covers.
_CENSUSED_KINDS: Final = frozenset({"skill", "manifest", "hook"})

FileKind = Literal["skill", "manifest", "hook", "agent", "doc"]


class BundleFile(BaseModel):
    """One rendered file of a bundle and the digest it was written with.

    Attributes:
        path: The file, relative to the bundle root, POSIX-spelled.
        kind: What the file is, which decides whether the census covers it.
        digest: The SHA-256 of its bytes as written.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str
    kind: FileKind
    digest: str


class SkillDigests(BaseModel):
    """The four digests of one shipped skill.

    Attributes:
        skill_id: The catalog id.
        prompt: The rendered page.
        arguments: The argument schema.
        rpc_allowlist: The routes, verbs and Run tools the skill may drive.
        output_schema: The typed report's JSON schema.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    skill_id: str
    prompt: str
    arguments: str
    rpc_allowlist: str
    output_schema: str


class Publication(BaseModel):
    """Every digest of one bundle, checked together.

    Attributes:
        target_epoch: The engine schema epoch the bundle targets.
        catalog: The skill catalog's digest.
        verb_catalog: The verb catalog's digest the skills were joined to.
        skills: The per-skill digests, in catalog order.
        files: The rendered files, in path order.
        bundle: The digest over every other field.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    target_epoch: int
    catalog: str
    verb_catalog: str
    skills: tuple[SkillDigests, ...]
    files: tuple[BundleFile, ...]
    bundle: str


class BundleEpochMismatchError(RuntimeError):
    """An installed bundle targets another epoch than its repository.

    Attributes:
        bundle_epoch: The epoch the bundle targets.
        repository_epoch: The epoch the repository resolves to.
    """

    def __init__(self, *, bundle_epoch: int, repository_epoch: int) -> None:
        """Name both epochs and the migration that reconciles them.

        Args:
            bundle_epoch: The epoch the bundle targets.
            repository_epoch: The epoch the repository resolves to.
        """
        self.bundle_epoch = bundle_epoch
        self.repository_epoch = repository_epoch
        if repository_epoch < bundle_epoch:
            guidance = (
                f"migrate the repository to epoch {bundle_epoch} first; `{MIGRATION_COMMAND}`"
                " shows where the migration stands"
            )
        else:
            guidance = (
                f"install the bundle this eawf version renders (`eawf plugin update`), which"
                f" targets epoch {repository_epoch}"
            )
        super().__init__(
            f"this eawf bundle targets schema epoch {bundle_epoch} but the repository is at"
            f" epoch {repository_epoch}, so it does not operate here: {guidance}"
        )


def _sha256(data: bytes) -> str:
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def _json_digest(value: object) -> str:
    return _sha256(orjson.dumps(value, option=orjson.OPT_SORT_KEYS))


def _kind(path: str) -> FileKind:
    """Return the kind of a bundle file from its bundle-relative path."""
    name = path.rsplit("/", 1)[-1]
    if name.endswith("SKILL.md") or path.startswith("commands/"):
        return "skill"
    if path.startswith("hooks/") and name.endswith(".sh"):
        return "hook"
    if name.endswith(".json"):
        return "manifest"
    if path.startswith("agents/"):
        return "agent"
    return "doc"


def skill_digests() -> tuple[SkillDigests, ...]:
    """Return the four digests of every catalog skill, from the installed catalog."""
    from eawf.workflow.skills.arguments import argument_schema
    from eawf.workflow.skills.catalog import SKILL_CATALOG, shipped_skill_specs

    pages = {spec.skill_name: spec.body for spec in shipped_skill_specs()}
    return tuple(
        SkillDigests(
            skill_id=entry.skill_id,
            prompt=_sha256(pages[entry.skill_id].encode()),
            arguments=_json_digest(argument_schema(entry).model_dump(mode="json")),
            rpc_allowlist=_json_digest(
                entry.effects.model_dump(mode="json", include={"rpcs", "verbs", "tools"})
            ),
            output_schema=_json_digest(entry.output.model_for(entry.skill_id).model_json_schema()),
        )
        for entry in SKILL_CATALOG.entries
    )


def build_publication(root: Path, paths: Iterable[Path]) -> Publication:
    """Build the publication of the rendered files *paths* under *root*.

    Args:
        root: The bundle root.
        paths: Every file the packager rendered, absolute or root-relative.

    Returns:
        The publication, digesting the files as they are on disk now.
    """
    from eawf.surfaces.cli.verb_catalog import verb_catalog
    from eawf.workflow.skills.catalog import SKILL_CATALOG

    relative = sorted({(root / p).relative_to(root).as_posix() for p in paths})
    files = tuple(
        BundleFile(path=rel, kind=_kind(rel), digest=_sha256((root / rel).read_bytes()))
        for rel in relative
    )
    draft = Publication(
        target_epoch=BUNDLE_TARGET_EPOCH,
        catalog=_json_digest(SKILL_CATALOG.model_dump(mode="json")),
        verb_catalog=f"sha256:{verb_catalog().digest()}",
        skills=skill_digests(),
        files=files,
        bundle="",
    )
    bundle = _json_digest(draft.model_dump(mode="json", exclude={"bundle"}))
    return draft.model_copy(update={"bundle": bundle})


def write_publication(root: Path, paths: Iterable[Path]) -> Path:
    """Write the publication of *paths* to ``<root>/eawf-publication.json``.

    Args:
        root: The bundle root.
        paths: Every file the packager rendered.

    Returns:
        The path written.
    """
    from eawf.surfaces.render._atomic import atomic_write_text

    publication = build_publication(root, paths)
    target = root / PUBLICATION_FILENAME
    payload = orjson.dumps(
        publication.model_dump(mode="json"), option=orjson.OPT_INDENT_2 | orjson.OPT_SORT_KEYS
    )
    atomic_write_text(target, payload.decode() + "\n")
    logger.info(f"write_publication files={len(publication.files)} epoch={BUNDLE_TARGET_EPOCH}")
    return target


def read_publication(root: Path) -> Publication:
    """Read and validate the publication a bundle root carries.

    Args:
        root: The bundle root.

    Returns:
        The validated publication.

    Raises:
        FileNotFoundError: The bundle carries no publication.
        pydantic.ValidationError: The publication does not parse.
    """
    raw = (root / PUBLICATION_FILENAME).read_bytes()
    return Publication.model_validate(orjson.loads(raw))


def publication_findings(root: Path) -> tuple[str, ...]:
    """Check a bundle's publication as one unit against the catalog and its files.

    Args:
        root: The bundle root.

    Returns:
        One message per finding: a missing or unparseable publication, an
        epoch other than the one this build targets, a catalog, verb, prompt,
        argument, allowlist or output digest that no longer matches, a
        rendered file that is missing or was edited after it was written, or
        an epoch-1 noun in a skill, manifest or hook. Empty when it checks.
    """
    try:
        recorded = read_publication(root)
    except FileNotFoundError:
        return (f"{PUBLICATION_FILENAME} is missing; the bundle was not rendered by eawf",)
    except (ValidationError, orjson.JSONDecodeError) as exc:
        return (f"{PUBLICATION_FILENAME} does not parse: {exc}",)
    findings: list[str] = []
    if recorded.target_epoch != BUNDLE_TARGET_EPOCH:
        findings.append(
            f"the bundle targets epoch {recorded.target_epoch}, this build epoch"
            f" {BUNDLE_TARGET_EPOCH}"
        )
    present = [Path(row.path) for row in recorded.files if (root / row.path).is_file()]
    findings.extend(
        f"{row.path} is missing" for row in recorded.files if not (root / row.path).is_file()
    )
    current = build_publication(root, present)
    if current.catalog != recorded.catalog:
        findings.append("the skill catalog changed since the bundle was rendered")
    if current.verb_catalog != recorded.verb_catalog:
        findings.append("the verb catalog changed since the bundle was rendered")
    now = {row.skill_id: row for row in current.skills}
    for row in recorded.skills:
        fresh = now.get(row.skill_id)
        if fresh is None:
            findings.append(f"skill {row.skill_id} is no longer in the catalog")
            continue
        findings.extend(
            f"skill {row.skill_id} {part} digest no longer matches"
            for part in ("prompt", "arguments", "rpc_allowlist", "output_schema")
            if getattr(fresh, part) != getattr(row, part)
        )
    on_disk = {row.path: row.digest for row in current.files}
    findings.extend(
        f"{row.path} was edited after it was rendered"
        for row in recorded.files
        if row.path in on_disk and on_disk[row.path] != row.digest
    )
    for rendered in current.files:
        if rendered.kind in _CENSUSED_KINDS:
            nouns = epoch1_nouns((root / rendered.path).read_text(encoding="utf-8"))
            findings.extend(f"{rendered.path} names epoch-1 noun {noun!r}" for noun in nouns)
    return tuple(findings)


def require_bundle_epoch(bundle_epoch: int, repository_root: Path) -> None:
    """Refuse to operate a bundle whose target epoch differs from the repository.

    Args:
        bundle_epoch: The epoch the invoking bundle targets.
        repository_root: The repository the bundle is operating in.

    Raises:
        BundleEpochMismatchError: The repository resolves to another epoch.
    """
    from eawf.kernel.state.epoch2.authority import resolve_authority

    repository_epoch = resolve_authority(repository_root / ".ea").epoch
    if repository_epoch != bundle_epoch:
        raise BundleEpochMismatchError(bundle_epoch=bundle_epoch, repository_epoch=repository_epoch)


def main(argv: Sequence[str] | None = None) -> int:
    """Check the publication of the bundle named on the command line.

    Args:
        argv: Argument vector; defaults to the process arguments.

    Returns:
        ``0`` when the publication checks, ``1`` after printing every
        finding otherwise.
    """
    parser = argparse.ArgumentParser(prog="eawf-publication")
    parser.add_argument("root", help="rendered bundle root to check")
    args = parser.parse_args(argv)
    findings = publication_findings(Path(args.root))
    for finding in findings:
        print(f"publication: {finding}", file=sys.stderr)
    return 1 if findings else 0


__all__ = [
    "BUNDLE_TARGET_EPOCH",
    "MIGRATION_COMMAND",
    "PUBLICATION_FILENAME",
    "BundleEpochMismatchError",
    "BundleFile",
    "Publication",
    "SkillDigests",
    "build_publication",
    "main",
    "publication_findings",
    "read_publication",
    "require_bundle_epoch",
    "skill_digests",
    "write_publication",
]


if __name__ == "__main__":
    raise SystemExit(main())
