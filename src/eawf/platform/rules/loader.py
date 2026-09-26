"""Load ``.ea/rules.yaml``, the only repository-authored rule source.

The repository layer reads exactly one file. There is no directory scan, no
layout inference and no include directive, so nothing the file does not
spell out can contribute policy. Before any rule is accepted the loader
refuses, with a typed :class:`RuleSourceError`:

- a source file or a named path that resolves outside the repository through
  a symlink;
- an absolute path or a parent traversal in a path field;
- an include directive, or a value that is a URL;
- a credential-shaped token, a home-directory path or a personal email
  anywhere in the document; and
- a rule identifier outside the repository namespace.

:func:`load_rule_source` is the single entry point. Graph compilation and the
project-card render consume its result.
"""

from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Iterator
from pathlib import Path, PurePosixPath
from typing import ClassVar, Final

import yaml
from pydantic import ValidationError

from eawf.observability.logging.state_leak import (
    default_allowed_emails,
    describe_state_leaks,
    diff_state_leaks,
)
from eawf.platform.rules.records import (
    REGISTERED_LOCATOR_PREFIX,
    REPOSITORY_NAMESPACE,
    LoadedRuleSource,
    RuleRecord,
    RuleSourceDocument,
    RuleSourceIdentity,
)

logger = logging.getLogger(__name__)

#: Where the repository rule source lives, relative to the repository root.
RULE_SOURCE_PATH: Final[PurePosixPath] = PurePosixPath(".ea/rules.yaml")

# Keys that would pull policy from somewhere other than this one file.
_INCLUDE_KEYS: Final[frozenset[str]] = frozenset(
    {"include", "includes", "import", "imports", "extends", "$ref"}
)

# A whole value that is a URL or an scp-style remote is a reference to remote
# content. A URL mentioned inside prose is not an include and is left alone.
_REMOTE_VALUE: Final[re.Pattern[str]] = re.compile(
    r"^\s*(?:[A-Za-z][A-Za-z0-9+.-]*://\S*|[\w.-]+@[\w.-]+:\S+)\s*$"
)

_GLOB_CHARS: Final[frozenset[str]] = frozenset("*?[")


class RuleSourceError(ValueError):
    """Base class for a refusal raised while loading a rule source.

    Attributes:
        code: The stable failure code, so tooling matches on a code rather
            than parsing an English message.
    """

    code: ClassVar[str] = "rule_source_error"


class RuleSourceMissingError(RuleSourceError):
    """The repository has no ``.ea/rules.yaml``."""

    code: ClassVar[str] = "rule_source_missing"


class RuleSourceSchemaError(RuleSourceError):
    """The source is not YAML, or does not match the closed schema."""

    code: ClassVar[str] = "rule_source_schema"


class RuleSourceIncludeError(RuleSourceError):
    """The source names an include directive or a remote reference."""

    code: ClassVar[str] = "rule_source_include"


class RuleSourceLeakError(RuleSourceError):
    """The source carries a credential, a home-directory path or an email."""

    code: ClassVar[str] = "rule_source_leak"


class RuleSourceNamespaceError(RuleSourceError):
    """A repository rule identifier sits outside the repository namespace."""

    code: ClassVar[str] = "rule_source_namespace"


class RuleSourcePathError(RuleSourceError):
    """A path field is not a normalized repository-relative path."""

    code: ClassVar[str] = "rule_source_path"


class RuleSourceAbsolutePathError(RuleSourcePathError):
    """A path field is absolute or home-anchored."""

    code: ClassVar[str] = "rule_source_absolute_path"


class RuleSourceTraversalError(RuleSourcePathError):
    """A path field climbs out through a ``..`` segment."""

    code: ClassVar[str] = "rule_source_traversal"


class RuleSourceSymlinkEscapeError(RuleSourcePathError):
    """A path resolves outside the repository root through a symlink."""

    code: ClassVar[str] = "rule_source_symlink_escape"


def load_rule_source(repo_root: Path) -> LoadedRuleSource:
    """Read and validate the repository rule source.

    Args:
        repo_root: The repository root. ``.ea/rules.yaml`` is read from it.

    Returns:
        The selected module references and the repository rules, each rule
        stamped with the identity of the file it was read from.

    Raises:
        RuleSourceMissingError: When ``.ea/rules.yaml`` does not exist.
        RuleSourceSymlinkEscapeError: When the source file, or a path a rule
            names, resolves outside ``repo_root``.
        RuleSourceSchemaError: When the file is not a YAML mapping or fails
            the closed schema.
        RuleSourceIncludeError: When the file carries an include directive or
            a URL-valued field.
        RuleSourceLeakError: When the file carries a credential-shaped token,
            a home-directory path or a personal email.
        RuleSourceNamespaceError: When a rule identifier is outside the
            repository namespace.
        RuleSourceAbsolutePathError: When a path field is absolute.
        RuleSourceTraversalError: When a path field has a ``..`` segment.
        RuleSourcePathError: When a path field is not normalized.
    """
    root = repo_root.resolve()
    source_path = root.joinpath(*RULE_SOURCE_PATH.parts)
    if not source_path.is_file():
        raise RuleSourceMissingError(f"{RULE_SOURCE_PATH} not found under the repository root")
    _require_confined(root, source_path, where=str(RULE_SOURCE_PATH))
    raw = source_path.read_bytes()
    identity = RuleSourceIdentity(
        kind="repository",
        locator=RULE_SOURCE_PATH.as_posix(),
        digest=f"sha256:{hashlib.sha256(raw).hexdigest()}",
    )
    payload = screen_rule_document(raw, where=RULE_SOURCE_PATH.as_posix())
    try:
        document = RuleSourceDocument.model_validate(payload)
    except ValidationError as exc:
        raise RuleSourceSchemaError(f"{RULE_SOURCE_PATH}: {exc}") from exc
    for index, rule in enumerate(document.rules):
        where = f"rules[{index}]"
        if rule.rule_id.split(".", 1)[0] != REPOSITORY_NAMESPACE:
            raise RuleSourceNamespaceError(
                f"{where}.rule_id {rule.rule_id!r} must start with {REPOSITORY_NAMESPACE!r}."
            )
        for path_index, path in enumerate(rule.scope.paths):
            _check_repo_path(root, path, where=f"{where}.scope.paths[{path_index}]")
        if rule.procedure_ref is not None and not rule.procedure_ref.startswith(
            REGISTERED_LOCATOR_PREFIX
        ):
            _check_repo_path(root, rule.procedure_ref, where=f"{where}.procedure_ref")
    records = tuple(
        RuleRecord.model_validate({**rule.model_dump(), "source": identity})
        for rule in document.rules
    )
    logger.debug(
        f"rule source loaded digest={identity.digest} rules={len(records)} "
        f"modules={len(document.modules)}"
    )
    return LoadedRuleSource(
        source=identity,
        workspace=document.workspace,
        modules=document.modules,
        rules=records,
    )


def screen_rule_document(raw: bytes, *, where: str) -> object:
    """Decode rule source bytes and refuse what no rule source may carry.

    Every rule source, whatever its layer, passes this screen before its
    schema is checked, so a layer cannot become the route around it.

    Args:
        raw: The exact bytes of the source file.
        where: The source's display locator, for refusal messages. Never
            an absolute path.

    Returns:
        The decoded top-level mapping.

    Raises:
        RuleSourceSchemaError: When the bytes are not UTF-8 YAML or the top
            level is not a mapping.
        RuleSourceIncludeError: When the document carries an include
            directive or a URL-valued field.
        RuleSourceLeakError: When the document carries a credential-shaped
            token, a home-directory path or a personal email.
    """
    try:
        payload = yaml.safe_load(raw.decode("utf-8"))
    except (UnicodeDecodeError, yaml.YAMLError) as exc:
        raise RuleSourceSchemaError(f"{where} is not valid YAML: {exc}") from exc
    if not isinstance(payload, dict):
        raise RuleSourceSchemaError(f"{where} must hold a YAML mapping")
    _refuse_includes(payload)
    leaks = diff_state_leaks(None, payload, allowed_emails=default_allowed_emails())
    if leaks:
        raise RuleSourceLeakError(f"{where}: {describe_state_leaks(leaks)}")
    return payload


def _walk(value: object, field_path: str) -> Iterator[tuple[str, object, object]]:
    """Yield ``(field_path, key, value)`` for every mapping entry and list item.

    Args:
        value: The decoded YAML node to walk.
        field_path: The location of ``value``; empty for the document root.

    Yields:
        One triple per nested node. ``key`` is ``None`` for a list item.
    """
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{field_path}.{key}" if field_path else str(key)
            yield child_path, key, child
            yield from _walk(child, child_path)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            child_path = f"{field_path}[{index}]"
            yield child_path, None, child
            yield from _walk(child, child_path)


def _refuse_includes(payload: object) -> None:
    """Refuse any include directive and any URL-valued field.

    Args:
        payload: The decoded document.

    Raises:
        RuleSourceIncludeError: On the first include key or remote value.
    """
    for field_path, key, value in _walk(payload, ""):
        if isinstance(key, str) and key.casefold() in _INCLUDE_KEYS:
            raise RuleSourceIncludeError(
                f"{field_path}: include directives are refused; "
                f"a rule source contributes only the rules it spells out"
            )
        if isinstance(value, str) and _REMOTE_VALUE.match(value):
            raise RuleSourceIncludeError(f"{field_path}: a remote reference is refused")


def _check_repo_path(root: Path, value: str, *, where: str) -> None:
    """Require ``value`` to be a normalized path confined under ``root``.

    A glob is checked up to its first wildcard segment, the longest prefix
    that names a concrete location.

    Args:
        root: The resolved repository root.
        value: The authored path or path glob.
        where: The field location, for the refusal message.

    Raises:
        RuleSourceAbsolutePathError: When ``value`` is absolute or
            home-anchored.
        RuleSourceTraversalError: When ``value`` has a ``..`` segment.
        RuleSourcePathError: When ``value`` has a backslash, an empty
            segment or a ``.`` segment.
        RuleSourceSymlinkEscapeError: When the concrete prefix resolves
            outside ``root``.
    """
    if value.startswith(("/", "~")) or re.match(r"^[A-Za-z]:", value):
        raise RuleSourceAbsolutePathError(f"{where}: an absolute path is refused")
    if "\\" in value:
        raise RuleSourcePathError(f"{where}: use forward slashes in repository paths")
    segments = value.split("/")
    if ".." in segments:
        raise RuleSourceTraversalError(f"{where}: a parent traversal is refused")
    if "" in segments or "." in segments:
        raise RuleSourcePathError(f"{where}: {value!r} is not a normalized repository path")
    concrete: list[str] = []
    for segment in segments:
        if _GLOB_CHARS.intersection(segment):
            break
        concrete.append(segment)
    _require_confined(root, root.joinpath(*concrete), where=where)


def _require_confined(root: Path, path: Path, *, where: str) -> None:
    """Require ``path`` to resolve inside ``root`` after following symlinks.

    Args:
        root: The resolved repository root.
        path: A path under ``root``; it need not exist.
        where: The field or file location, for the refusal message.

    Raises:
        RuleSourceSymlinkEscapeError: When ``path`` resolves outside ``root``.
    """
    if not path.resolve().is_relative_to(root):
        raise RuleSourceSymlinkEscapeError(
            f"{where}: resolves outside the repository through a symlink"
        )


__all__ = [
    "RULE_SOURCE_PATH",
    "RuleSourceAbsolutePathError",
    "RuleSourceError",
    "RuleSourceIncludeError",
    "RuleSourceLeakError",
    "RuleSourceMissingError",
    "RuleSourceNamespaceError",
    "RuleSourcePathError",
    "RuleSourceSchemaError",
    "RuleSourceSymlinkEscapeError",
    "RuleSourceTraversalError",
    "load_rule_source",
    "screen_rule_document",
]
