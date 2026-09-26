"""Builtin rule modules: the registered catalog, repository selection, and the index.

A rule module is an immutable, versioned rule set on one topic that ships with
the tool as package data. A repository activates a module only by naming its
registered reference under ``modules`` in ``.ea/rules.yaml``; the module's
prose is never copied into the repository, and a repository rule that repeats
a builtin module's prose is refused.

:func:`select_rule_modules` is the single entry point. It resolves a loaded
repository source's selection against the registered catalog, refuses copied
builtin prose, and returns the selection whose ``records`` feed
:func:`~eawf.platform.rules.compile.compile_rule_records` and whose entries
:func:`render_module_index` turns into the module index. The index is derived
from the selection alone, so adding or removing a reference changes it with no
second edit.

Two module kinds exist:

- ``core`` modules carry obligations that bind every activity; they may place
  rules in any zone and may leave a rule unscoped.
- ``catalog`` modules carry topic craft; their rules stay out of the
  constitution and each declares an activity or role selector, so a module
  surfaces only where it applies.
"""

from __future__ import annotations

import functools
import hashlib
import logging
import re
from collections.abc import Mapping
from importlib.resources import files
from types import MappingProxyType
from typing import Annotated, ClassVar, Final, Literal

import yaml
from pydantic import (
    AfterValidator,
    Field,
    PositiveInt,
    StringConstraints,
    ValidationError,
    model_validator,
)

from eawf.platform.rules.loader import RuleSourceError
from eawf.platform.rules.records import (
    AuthoredRule,
    LoadedRuleSource,
    QualifiedId,
    RuleModel,
    RuleRecord,
    RuleSourceIdentity,
    RuleTitle,
    SelectorToken,
)

logger = logging.getLogger(__name__)

#: The namespace every builtin module reference lives under.
BUILTIN_MODULE_NAMESPACE: Final[str] = "eawf"

#: The command an index entry names to read a module's full view.
MODULE_VIEW_COMMAND: Final[str] = "eawf rules view"

#: Stated once above the index, because loading a module is a retrieval and
#: not an opt-in: its rules carry the same force as any other rule once read.
MODULE_BINDING_NOTICE: Final[str] = (
    "Read a module when its trigger applies. Once read, its rules bind for the rest of "
    "the activity with the same force as any other rule; they are not advisory."
)

# Package-data location of the registered module files, one per reference.
_DATA_PACKAGE: Final[str] = "eawf.platform.rules"
_DATA_PARTS: Final[tuple[str, ...]] = ("data", "modules")
_MODULE_SUFFIX: Final[str] = ".yaml"

# Words that carry no condition, so a trigger made only of these plus title
# words restates the title instead of saying when to load.
_FILLER_WORDS: Final[frozenset[str]] = frozenset(
    {"a", "an", "and", "any", "for", "in", "of", "on", "or", "rules", "the", "to", "when", "with"}
)
_WORD: Final[re.Pattern[str]] = re.compile(r"[a-z0-9]+")
_SPACE: Final[re.Pattern[str]] = re.compile(r"\s+")

RuleModuleKind = Literal["core", "catalog"]


def _refuse_trailing_period(text: str) -> str:
    """Keep a trigger joinable into one index line.

    Args:
        text: The already length-bounded trigger.

    Returns:
        ``text`` unchanged.

    Raises:
        ValueError: When ``text`` ends with a period.
    """
    if text.endswith("."):
        raise ValueError("a when_to_use trigger must not end with a period")
    return text


#: The load decision: the condition under which a module's rules apply.
TriggerStr = Annotated[
    str,
    StringConstraints(strict=True, strip_whitespace=True, min_length=16, max_length=200),
    AfterValidator(_refuse_trailing_period),
]


class RuleModuleError(ValueError):
    """Base class for a refusal raised while registering or selecting modules.

    Attributes:
        code: The stable failure code, so tooling matches on a code rather
            than parsing an English message.
    """

    code: ClassVar[str] = "rule_module_error"


class RuleModuleCatalogError(RuleModuleError):
    """A shipped module file is unreadable or breaks the module contract."""

    code: ClassVar[str] = "rule_module_catalog"


class RuleModuleCopyError(RuleSourceError):
    """A repository rule repeats a builtin module's prose instead of selecting it."""

    code: ClassVar[str] = "rule_module_copy"


class RuleModuleDocument(RuleModel):
    """The on-disk shape of one builtin rule module.

    Attributes:
        schema_version: Gates unknown future formats.
        module_id: The registered reference a repository selects.
        version: Bumped on any content change; bound into the source locator.
        kind: ``core`` or ``catalog``; see the module docstring.
        topic: The short topic name the index shows.
        title: At most 72 characters, no trailing period.
        when_to_use: The concrete condition under which the rules apply.
        rules: The module's rules; every identifier sits under ``module_id``.
    """

    schema_version: Literal[1]
    module_id: QualifiedId
    version: PositiveInt
    kind: RuleModuleKind
    topic: SelectorToken
    title: RuleTitle
    when_to_use: TriggerStr
    rules: tuple[AuthoredRule, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _enforce_module_contract(self) -> RuleModuleDocument:
        """Refuse a module that breaks namespace, zone, scope or trigger rules.

        Returns:
            The validated module.

        Raises:
            ValueError: When the module sits outside the builtin namespace, a
                rule identifier sits outside the module, a catalog rule is in
                the constitution or unscoped, or the trigger restates the
                title.
        """
        if self.module_id.split(".", 1)[0] != BUILTIN_MODULE_NAMESPACE:
            raise ValueError(
                f"module_id {self.module_id!r} must start with {BUILTIN_MODULE_NAMESPACE!r}"
            )
        prefix = f"{self.module_id}."
        for rule in self.rules:
            if not rule.rule_id.startswith(prefix):
                raise ValueError(f"rule_id {rule.rule_id!r} must start with {prefix!r}")
            if self.kind != "catalog":
                continue
            if rule.zone == "constitution":
                raise ValueError(
                    f"{rule.rule_id}: a catalog module cannot place a rule in the constitution"
                )
            if not (rule.scope.activities or rule.scope.roles):
                raise ValueError(
                    f"{rule.rule_id}: a catalog module rule must declare an activity or role"
                )
        title_words = set(_WORD.findall(self.title.casefold()))
        trigger_words = set(_WORD.findall(self.when_to_use.casefold())) - _FILLER_WORDS
        if trigger_words <= title_words:
            raise ValueError("when_to_use restates the title instead of naming a condition")
        return self


class RuleModule(RuleModel):
    """A registered module with its rules stamped as builtin records.

    Attributes:
        document: The validated module file.
        source: The builtin source identity every record carries.
        records: The module's rules, ready for graph compilation.
    """

    document: RuleModuleDocument
    source: RuleSourceIdentity
    records: tuple[RuleRecord, ...]

    @property
    def scope(self) -> tuple[str, ...]:
        """Return the sorted activity and role selectors across the module's rules."""
        return tuple(
            sorted(
                {token for record in self.records for token in record.scope.activities}
                | {token for record in self.records for token in record.scope.roles}
            )
        )


class RuleModuleEntry(RuleModel):
    """One selected reference and what it resolved to.

    Attributes:
        reference: The reference as selected.
        module: The resolved module, or ``None`` when it is unavailable.
        unavailable_reason: Why the reference did not resolve.
    """

    reference: QualifiedId
    module: RuleModule | None = None
    unavailable_reason: str | None = None

    @model_validator(mode="after")
    def _module_xor_reason(self) -> RuleModuleEntry:
        """Require exactly one of a module and an unavailability reason.

        Returns:
            The validated entry.

        Raises:
            ValueError: When both or neither are set.
        """
        if (self.module is None) == (self.unavailable_reason is None):
            raise ValueError("an entry carries either a module or an unavailable_reason")
        return self


class RuleModuleSelection(RuleModel):
    """The resolved module selection of one repository.

    Attributes:
        entries: One entry per distinct selected reference, sorted by
            reference so selection order carries no meaning.
    """

    entries: tuple[RuleModuleEntry, ...]

    @property
    def records(self) -> tuple[RuleRecord, ...]:
        """Return the builtin records of every available selected module."""
        return tuple(
            record
            for entry in self.entries
            if entry.module is not None
            for record in entry.module.records
        )


def builtin_rule_modules() -> Mapping[str, RuleModule]:
    """Return the registered builtin modules keyed by reference.

    The catalog is package data pinned by the installed version, so it is
    read once per process and returned as a read-only mapping of frozen
    modules.

    Returns:
        Every registered module, keyed by ``module_id``.

    Raises:
        RuleModuleCatalogError: When a shipped module file is not valid YAML,
            breaks the module contract, or is named differently from its
            ``module_id``.
    """
    return _load_catalog()


@functools.cache
def _load_catalog() -> Mapping[str, RuleModule]:
    """Read every module file under the package data directory.

    Returns:
        The registered modules keyed by ``module_id``.

    Raises:
        RuleModuleCatalogError: See :func:`builtin_rule_modules`.
    """
    directory = files(_DATA_PACKAGE).joinpath(*_DATA_PARTS)
    catalog: dict[str, RuleModule] = {}
    for item in sorted(directory.iterdir(), key=lambda entry: entry.name):
        if not item.name.endswith(_MODULE_SUFFIX):
            continue
        reference = item.name.removesuffix(_MODULE_SUFFIX)
        module = parse_rule_module(item.read_bytes(), where=item.name)
        if module.document.module_id != reference:
            raise RuleModuleCatalogError(
                f"{item.name}: module_id {module.document.module_id!r} must match the file name"
            )
        catalog[reference] = module
    logger.debug(f"builtin rule modules registered count={len(catalog)}")
    return MappingProxyType(catalog)


def parse_rule_module(raw: bytes, *, where: str) -> RuleModule:
    """Validate one module file and stamp its rules with a builtin source.

    Args:
        raw: The exact bytes of the module file.
        where: The file name, for refusal messages.

    Returns:
        The module; each record's source locator is
        ``<module_id>@<version>`` and its digest covers ``raw``.

    Raises:
        RuleModuleCatalogError: When ``raw`` is not a YAML mapping or breaks
            the module contract.
    """
    try:
        payload = yaml.safe_load(raw.decode("utf-8"))
        document = RuleModuleDocument.model_validate(payload)
    except (UnicodeDecodeError, yaml.YAMLError, ValidationError) as exc:
        raise RuleModuleCatalogError(f"{where}: {exc}") from exc
    source = RuleSourceIdentity(
        kind="builtin",
        locator=f"{document.module_id}@{document.version}",
        digest=f"sha256:{hashlib.sha256(raw).hexdigest()}",
    )
    records = tuple(
        RuleRecord.model_validate({**rule.model_dump(), "source": source})
        for rule in document.rules
    )
    return RuleModule(document=document, source=source, records=records)


def select_rule_modules(loaded: LoadedRuleSource) -> RuleModuleSelection:
    """Resolve a repository's module selection against the builtin catalog.

    Args:
        loaded: The repository rule source, as :func:`load_rule_source`
            returns it.

    Returns:
        One entry per distinct selected reference, sorted. A reference with
        no registered module is kept as an unavailable entry so the index
        stays total.

    Raises:
        RuleModuleCopyError: When a repository rule's instruction or
            rationale repeats a builtin module's prose.
        RuleModuleCatalogError: When the shipped catalog is invalid.
    """
    catalog = builtin_rule_modules()
    _refuse_copied_prose(loaded.rules, catalog)
    entries = tuple(
        RuleModuleEntry(reference=reference, module=catalog[reference])
        if reference in catalog
        else RuleModuleEntry(
            reference=reference,
            unavailable_reason="no registered builtin module has this reference",
        )
        for reference in sorted(set(loaded.modules))
    )
    logger.debug(
        f"rule modules selected count={len(entries)} "
        f"unavailable={sum(entry.module is None for entry in entries)}"
    )
    return RuleModuleSelection(entries=entries)


def render_module_index(selection: RuleModuleSelection) -> str:
    """Render the module index: one line per selected reference.

    Args:
        selection: The resolved selection.

    Returns:
        The binding notice followed by one markdown list line per entry, or
        an empty string when nothing is selected.
    """
    if not selection.entries:
        return ""
    lines = [MODULE_BINDING_NOTICE, ""]
    for entry in selection.entries:
        if entry.module is None:
            lines.append(f"- `{entry.reference}` unavailable: {entry.unavailable_reason}")
            continue
        document = entry.module.document
        count = len(entry.module.records)
        noun = "rule" if count == 1 else "rules"
        lines.append(
            f"- `{entry.reference}` {document.topic} [{', '.join(entry.module.scope)}] "
            f"{count} {noun}; load when: {document.when_to_use}; "
            f"read via: `{MODULE_VIEW_COMMAND} {entry.reference}`"
        )
    return "\n".join(lines) + "\n"


def _refuse_copied_prose(rules: tuple[RuleRecord, ...], catalog: Mapping[str, RuleModule]) -> None:
    """Refuse a repository rule that repeats any builtin module's prose.

    Every registered module counts, selected or not: copying prose out of the
    catalog is what selection replaces.

    Args:
        rules: The repository rules.
        catalog: The registered modules.

    Raises:
        RuleModuleCopyError: On the first repeated instruction or rationale.
    """
    owners: dict[str, str] = {}
    for module in catalog.values():
        for record in module.records:
            for text in (record.instruction, record.rationale):
                if text is not None:
                    owners.setdefault(_normalize(text), record.rule_id)
    for rule in rules:
        for field in ("instruction", "rationale"):
            text = getattr(rule, field)
            if text is None:
                continue
            owner = owners.get(_normalize(text))
            if owner is not None:
                raise RuleModuleCopyError(
                    f"{rule.rule_id}.{field} copies builtin rule {owner}; select its module "
                    f"under modules in .ea/rules.yaml instead of copying its prose"
                )


def _normalize(text: str) -> str:
    """Fold case, whitespace and a trailing period so a copy cannot hide.

    Args:
        text: Rule prose.

    Returns:
        The comparison key.
    """
    return _SPACE.sub(" ", text.casefold()).strip().rstrip(".")


__all__ = [
    "BUILTIN_MODULE_NAMESPACE",
    "MODULE_BINDING_NOTICE",
    "MODULE_VIEW_COMMAND",
    "RuleModule",
    "RuleModuleCatalogError",
    "RuleModuleCopyError",
    "RuleModuleDocument",
    "RuleModuleEntry",
    "RuleModuleError",
    "RuleModuleSelection",
    "builtin_rule_modules",
    "parse_rule_module",
    "render_module_index",
    "select_rule_modules",
]
