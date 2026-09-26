"""Compose the builtin, workspace and repository rule layers.

Rule sources resolve ``builtin < workspace < repository``, the same order as
configuration precedence. Composition only gathers records: precedence,
exact supersession and the one-owner-per-obligation check are evaluated
over the combined set by graph compilation, so no layer wins by arriving
later.

The workspace layer is never discovered. The repository rule source names a
registered workspace by key and pins the digest of the bytes it accepted;
the registry must list the repository as a member of that workspace; and
the workspace source is read from one fixed location under the user's eawf
home. A repository that names no workspace has no workspace layer.

The workspace layer is machine-local, so it feeds only generated policy.
The committed project card composes committed inputs only, the builtin and
repository layers, and :func:`require_committed_inputs` refuses anything
else, because two machines rendering one commit must produce the same
bytes.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import ClassVar, Final

from pydantic import ValidationError

from eawf.platform.registry.models import RegistryReadError, default_registry_path, read_registry
from eawf.platform.registry.workspace import WorkspaceResolutionError, resolve_member_workspace
from eawf.platform.rules.loader import (
    RuleSourceAbsolutePathError,
    RuleSourceError,
    RuleSourceNamespaceError,
    RuleSourceSchemaError,
    RuleSourceTraversalError,
    load_rule_source,
    screen_rule_document,
)
from eawf.platform.rules.records import (
    REGISTERED_LOCATOR_PREFIX,
    WORKSPACE_NAMESPACE,
    QualifiedId,
    RuleModel,
    RuleRecord,
    RuleSourceIdentity,
    RuleSourceKind,
    WorkspaceRuleDocument,
    WorkspaceRuleRef,
)

logger = logging.getLogger(__name__)

#: Source kinds whose content is committed, the only kinds a committed
#: projection may render from.
COMMITTED_SOURCE_KINDS: Final[frozenset[RuleSourceKind]] = frozenset({"builtin", "repository"})

#: Returns the builtin rule records for the selected module references.
BuiltinRuleProvider = Callable[[tuple[str, ...]], Iterable[RuleRecord]]


class RuleWorkspaceUnresolvedError(RuleSourceError):
    """The named workspace is unregistered, or the repository is not a member."""

    code: ClassVar[str] = "rule_workspace_unresolved"


class RuleWorkspaceMissingError(RuleSourceError):
    """The named workspace has no rule source."""

    code: ClassVar[str] = "rule_workspace_missing"


class RuleWorkspaceDigestError(RuleSourceError):
    """The workspace rule source differs from the bytes the repository pinned."""

    code: ClassVar[str] = "rule_workspace_digest"


class RuleSourceLayerError(RuleSourceError):
    """A record was supplied for a layer other than the one it resolved from."""

    code: ClassVar[str] = "rule_source_layer"


class RuleCommittedInputError(RuleSourceError):
    """A machine-local record was offered as input to a committed projection."""

    code: ClassVar[str] = "rule_committed_input"


class RuleLayers(RuleModel):
    """The records every layer contributes, before compilation.

    Attributes:
        builtin: Records of the builtin layer.
        workspace: Records of the workspace layer; empty when the repository
            names no workspace or the caller asked for committed inputs.
        repository: Records of the repository layer.
        modules: Builtin module references the repository selects.
    """

    builtin: tuple[RuleRecord, ...]
    workspace: tuple[RuleRecord, ...]
    repository: tuple[RuleRecord, ...]
    modules: tuple[QualifiedId, ...]

    def records(self) -> tuple[RuleRecord, ...]:
        """Return every layer's records, lowest layer first.

        Returns:
            Builtin, then workspace, then repository records.
        """
        return (*self.builtin, *self.workspace, *self.repository)

    def committed_records(self) -> tuple[RuleRecord, ...]:
        """Return only the records a committed projection may render.

        Returns:
            Builtin, then repository records.
        """
        return (*self.builtin, *self.repository)


def no_builtin_rules(modules: tuple[str, ...]) -> tuple[RuleRecord, ...]:
    """Contribute no builtin records, whatever modules are selected.

    Args:
        modules: The selected module references; unused.

    Returns:
        An empty tuple.
    """
    del modules
    return ()


def workspace_rule_locator(key: str) -> str:
    """Return the home-relative locator of a workspace's rule source.

    Args:
        key: The registered workspace key.

    Returns:
        ``workspaces/<key>/rules.yaml``, relative to the eawf home.
    """
    return f"workspaces/{key}/rules.yaml"


def load_rule_layers(
    repo_root: Path,
    *,
    builtin_rules: BuiltinRuleProvider = no_builtin_rules,
    workspace: bool = True,
    home: Path | None = None,
) -> RuleLayers:
    """Gather the builtin, workspace and repository layers for a repository.

    Args:
        repo_root: The repository root holding ``.ea/rules.yaml``.
        builtin_rules: Resolves the selected module references to builtin
            records.
        workspace: ``False`` gathers committed inputs only and never reads
            the registry or a workspace source.
        home: The directory holding the ``.eawf`` home; ``None`` for the
            user's home directory.

    Returns:
        The layers, each record checked against the layer it came from.

    Raises:
        RuleSourceError: When the repository or workspace source fails to
            load, or a provider returns a record from another layer.
    """
    loaded = load_rule_source(repo_root)
    builtin = _require_layer(builtin_rules(loaded.modules), kind="builtin")
    workspace_records: tuple[RuleRecord, ...] = ()
    if workspace and loaded.workspace is not None:
        workspace_records = load_workspace_rules(loaded.workspace, repo_root=repo_root, home=home)
    layers = RuleLayers(
        builtin=builtin,
        workspace=workspace_records,
        repository=_require_layer(loaded.rules, kind="repository"),
        modules=loaded.modules,
    )
    logger.debug(
        f"rule layers composed builtin={len(layers.builtin)} "
        f"workspace={len(layers.workspace)} repository={len(layers.repository)}"
    )
    return layers


def load_workspace_rules(
    ref: WorkspaceRuleRef, *, repo_root: Path, home: Path | None = None
) -> tuple[RuleRecord, ...]:
    """Resolve and read the workspace rule source a repository names.

    Args:
        ref: The repository's reference to the workspace.
        repo_root: The naming repository's root; it must be a registered
            member of the workspace.
        home: The directory holding the ``.eawf`` home; ``None`` for the
            user's home directory.

    Returns:
        The workspace rules stamped with the workspace source identity.

    Raises:
        RuleWorkspaceUnresolvedError: When the registry cannot be read, the
            workspace is unregistered, or the repository is not a member.
        RuleWorkspaceMissingError: When the workspace has no rule source.
        RuleWorkspaceDigestError: When the source bytes differ from the pin.
        RuleSourceSchemaError: When the source fails the closed schema.
        RuleSourceIncludeError: When the source carries an include or URL.
        RuleSourceLeakError: When the source carries a credential, a
            home-directory path or a personal email.
        RuleSourceNamespaceError: When a rule sits outside the workspace
            namespace.
        RuleSourceAbsolutePathError: When a path field is absolute.
        RuleSourceTraversalError: When a path field climbs out.
    """
    try:
        registry = read_registry(home=home)
        resolve_member_workspace(registry, key=ref.key, repo_root=repo_root)
    except RegistryReadError as exc:
        raise RuleWorkspaceUnresolvedError(
            f"workspace {ref.key!r} cannot be resolved: the registry is unreadable"
        ) from exc
    except WorkspaceResolutionError as exc:
        raise RuleWorkspaceUnresolvedError(f"{exc.code}: {exc}") from exc
    locator = workspace_rule_locator(ref.key)
    source_path = default_registry_path(home=home).parent.joinpath(*locator.split("/"))
    if not source_path.is_file():
        raise RuleWorkspaceMissingError(f"workspace {ref.key!r} has no rule source at {locator}")
    raw = source_path.read_bytes()
    digest = f"sha256:{hashlib.sha256(raw).hexdigest()}"
    if digest != ref.digest:
        raise RuleWorkspaceDigestError(
            f"{locator} is at {digest}, but the repository pinned {ref.digest}; "
            f"review the workspace rules and re-pin the digest"
        )
    payload = screen_rule_document(raw, where=locator)
    try:
        document = WorkspaceRuleDocument.model_validate(payload)
    except ValidationError as exc:
        raise RuleSourceSchemaError(f"{locator}: {exc}") from exc
    for index, rule in enumerate(document.rules):
        where = f"{locator} rules[{index}]"
        if rule.rule_id.split(".", 1)[0] != WORKSPACE_NAMESPACE:
            raise RuleSourceNamespaceError(
                f"{where}.rule_id {rule.rule_id!r} must start with {WORKSPACE_NAMESPACE!r}."
            )
        paths = [*rule.scope.paths]
        if rule.procedure_ref is not None and not rule.procedure_ref.startswith(
            REGISTERED_LOCATOR_PREFIX
        ):
            paths.append(rule.procedure_ref)
        for path in paths:
            _check_relative(path, where=where)
    identity = RuleSourceIdentity(kind="workspace", locator=locator, digest=digest)
    return tuple(
        RuleRecord.model_validate({**rule.model_dump(), "source": identity})
        for rule in document.rules
    )


def require_committed_inputs(records: Iterable[RuleRecord]) -> tuple[RuleRecord, ...]:
    """Refuse any record a committed projection must not render.

    Args:
        records: The records offered to a committed projection.

    Returns:
        The records, unchanged, when every one is from a committed source.

    Raises:
        RuleCommittedInputError: When a record resolved from a machine-local
            source.
    """
    accepted = tuple(records)
    for record in accepted:
        if record.source.kind not in COMMITTED_SOURCE_KINDS:
            raise RuleCommittedInputError(
                f"{record.rule_id} resolved from a {record.source.kind} source "
                f"({record.source.locator}); a committed projection renders builtin and "
                f"repository rules only"
            )
    return accepted


def _require_layer(
    records: Iterable[RuleRecord], *, kind: RuleSourceKind
) -> tuple[RuleRecord, ...]:
    """Require every record to have resolved from the ``kind`` layer.

    Args:
        records: The records supplied for one layer.
        kind: The layer they were supplied for.

    Returns:
        The records as a tuple.

    Raises:
        RuleSourceLayerError: When a record resolved from another layer.
    """
    accepted = tuple(records)
    for record in accepted:
        if record.source.kind != kind:
            raise RuleSourceLayerError(
                f"{record.rule_id} resolved from a {record.source.kind} source but was "
                f"supplied for the {kind} layer"
            )
    return accepted


def _check_relative(value: str, *, where: str) -> None:
    """Require a workspace path field to be relative and non-climbing.

    A workspace rule applies to several repositories, so its paths are
    checked for shape only; there is no single root to confine them to.

    Args:
        value: The authored path or path glob.
        where: The rule location, for the refusal message.

    Raises:
        RuleSourceAbsolutePathError: When ``value`` is absolute.
        RuleSourceTraversalError: When ``value`` has a ``..`` segment.
    """
    if value.startswith(("/", "~", "\\")) or (len(value) > 1 and value[1] == ":"):
        raise RuleSourceAbsolutePathError(f"{where}: an absolute path is refused")
    if ".." in value.replace("\\", "/").split("/"):
        raise RuleSourceTraversalError(f"{where}: a parent traversal is refused")


__all__ = [
    "COMMITTED_SOURCE_KINDS",
    "BuiltinRuleProvider",
    "RuleCommittedInputError",
    "RuleLayers",
    "RuleSourceLayerError",
    "RuleWorkspaceDigestError",
    "RuleWorkspaceMissingError",
    "RuleWorkspaceUnresolvedError",
    "load_rule_layers",
    "load_workspace_rules",
    "no_builtin_rules",
    "require_committed_inputs",
    "workspace_rule_locator",
]
