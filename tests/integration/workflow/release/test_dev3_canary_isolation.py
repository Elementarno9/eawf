"""The canary left the production tree alone, and left nothing of itself behind.

This is the proof command the ``canary_isolation`` gate resolves to, so what it
reads has to be a record that could say the opposite. Two claims, each checked
against a recorded fact rather than against a promise.

**The production root did not move.** One digest over the tracked ``.ea`` tree,
taken before the canary was provisioned and again after it was torn down. Equal
digests are the whole claim, and unequal ones are the finding; a record carrying
one digest, or a digest that is not a digest, fails at the loader instead of
comparing something to itself.

**Nothing of the canary survives.** Teardown removed its registry row, its
runtime directory and its tree, and the registry it was registered in holds no
rows at all afterwards. A teardown that removed a row without naming it, or that
named a code the provisioning never added, is refused by the record's own rules.

The operator's own repository registry is described by booleans rather than by
its contents, because a record that listed it would leak one machine's projects
into a committed file for no gain.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Annotated, Any, Literal, Self

import pytest
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

pytestmark = pytest.mark.integration

#: Four levels up from this file lands on the repository root.
_REPO_ROOT = Path(__file__).resolve().parents[4]

#: Where the isolation record is committed.
RECORD = _REPO_ROOT / ".ea/artifacts/evidence/2026-09-18-canary-rehearsal/isolation-record.json"

#: A sha256 digest as every record in this repository spells one.
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")

#: The path prefixes that name one machine, and the pattern built from them.
LEAK_SHAPES: tuple[str, ...] = (
    "/Users/",  # pragma: allowlist secret
    "/home/",  # pragma: allowlist secret
    "/private/",
    "/var/folders/",
    "/tmp/",
    "~/",
    "C:\\",
)
_ABSOLUTE_PATH = re.compile("|".join(re.escape(shape) for shape in LEAK_SHAPES))


class ProductionRoot(BaseModel):
    """The tree the rehearsal had to leave alone, digested twice.

    Attributes:
        label: Which tree was digested, in words.
        tracked_file_count: How many files the digest covers. A digest over
            nothing is equal to itself, so the count is what makes the pair
            mean something.
        before_digest: The digest taken before the canary was provisioned.
        after_digest: The digest taken after it was torn down.
        digest_method: How the digest is computed, so a reader can redo it.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    label: Annotated[str, Field(min_length=1)]
    tracked_file_count: Annotated[int, Field(ge=1)]
    before_digest: Annotated[str, Field(pattern=_DIGEST.pattern)]
    after_digest: Annotated[str, Field(pattern=_DIGEST.pattern)]
    digest_method: Annotated[str, Field(min_length=1)]

    @property
    def unchanged(self) -> bool:
        """Return whether the tree digested the same before and after."""
        return self.before_digest == self.after_digest


class CanaryRegistration(BaseModel):
    """Where the canary was registered while it existed.

    Attributes:
        project_code: The code it took.
        registry: Which registry file held the row, in words.
        rows_after_provision: The codes that registry held once it was
            provisioned, which has to include the canary's own.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    project_code: Annotated[str, Field(pattern=r"^[A-Z][A-Z0-9_-]{1,15}$")]
    registry: Annotated[str, Field(min_length=1)]
    rows_after_provision: Annotated[tuple[str, ...], Field(min_length=1)]

    @model_validator(mode="after")
    def _the_canary_was_actually_registered(self) -> Self:
        """Refuse a record whose provisioning did not register the canary.

        Raises:
            ValueError: The rows after provisioning do not include the code,
                which would make the teardown's removal unfalsifiable.
        """
        if self.project_code not in self.rows_after_provision:
            raise ValueError(
                f"canary {self.project_code!r} is not among the rows its provisioning left"
            )
        return self


class Teardown(BaseModel):
    """What the teardown removed.

    Attributes:
        removed_registry_codes: The registry rows it removed, in code order.
        registry_rows_after: The codes the registry held afterwards.
        runtime_dir_removed: Whether the recorded runtime directory was removed.
        runtime_dir_exists: Whether it is still on disk. Recorded separately
            because a teardown that reports a removal is not the same fact as
            a directory that is gone.
        root_exists: Whether the canary's tree is still on disk.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    removed_registry_codes: tuple[str, ...]
    registry_rows_after: tuple[str, ...]
    runtime_dir_removed: bool
    runtime_dir_exists: bool
    root_exists: bool

    @model_validator(mode="after")
    def _a_removal_is_not_also_a_survival(self) -> Self:
        """Refuse a teardown whose two halves contradict each other.

        Raises:
            ValueError: A directory is reported removed and also present, or a
                code is reported removed and also still held.
        """
        problems: list[str] = []
        if self.runtime_dir_removed and self.runtime_dir_exists:
            problems.append("the runtime directory is reported removed and still present")
        still_held = sorted(set(self.removed_registry_codes) & set(self.registry_rows_after))
        if still_held:
            problems.append(f"codes {still_held} are reported removed and still registered")
        if problems:
            raise ValueError("; ".join(problems))
        return self


class OperatorRegistry(BaseModel):
    """What the rehearsal did to the operator's own registry, as booleans.

    Attributes:
        addressed: Whether any command in the rehearsal wrote it.
        unchanged: Whether it digested the same before and after.
        holds_canary_code: Whether it carries a row under the canary's code.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    addressed: bool
    unchanged: bool
    holds_canary_code: bool


class IsolationRecord(BaseModel):
    """The whole isolation record, as it is committed.

    Attributes:
        schema_version: The record shape this suite reads.
        rehearsed_on: The day the walk was driven.
        production_root: The digest pair over the tree that had to stay still.
        canary: Where the canary was registered.
        teardown: What was removed.
        operator_registry: What the operator's own registry did.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["canary-isolation/v1"]
    rehearsed_on: Annotated[str, Field(pattern=r"^\d{4}-\d{2}-\d{2}$")]
    production_root: ProductionRoot
    canary: CanaryRegistration
    teardown: Teardown
    operator_registry: OperatorRegistry


def load_record(document: Any = None) -> IsolationRecord:
    """Return the validated record, from ``document`` or from the committed file.

    Args:
        document: A decoded record to validate instead of the committed one.

    Returns:
        The validated record.

    Raises:
        pydantic.ValidationError: The document is not an isolation record.
    """
    if document is None:
        document = json.loads(RECORD.read_text(encoding="utf-8"))
    return IsolationRecord.model_validate(document)


def isolation_defects(record: IsolationRecord) -> tuple[str, ...]:
    """Return every way the record fails to show an isolated rehearsal.

    Args:
        record: The validated record.

    Returns:
        One message per failure: a production root that moved, a canary row
        the teardown did not remove, a registry still holding rows, a tree or
        runtime directory still on disk, and an operator registry that was
        touched. Empty when the rehearsal was isolated.
    """
    defects: list[str] = []
    if not record.production_root.unchanged:
        defects.append("the production root digested differently before and after the rehearsal")
    if record.canary.project_code not in record.teardown.removed_registry_codes:
        defects.append(f"the teardown left a registry row for {record.canary.project_code!r}")
    if record.teardown.registry_rows_after:
        defects.append(
            f"the registry still holds {sorted(record.teardown.registry_rows_after)} after teardown"
        )
    if record.teardown.root_exists:
        defects.append("the canary tree is still on disk")
    if record.teardown.runtime_dir_exists:
        defects.append("the canary runtime directory is still on disk")
    if record.operator_registry.addressed or not record.operator_registry.unchanged:
        defects.append("the operator's own registry was addressed by the rehearsal")
    if record.operator_registry.holds_canary_code:
        defects.append("the operator's own registry holds a row under the canary's code")
    return tuple(defects)


def _recorded() -> dict[str, Any]:
    """Return the committed record, decoded and mutable."""
    parsed: dict[str, Any] = json.loads(RECORD.read_text(encoding="utf-8"))
    return parsed


def _with(section: str, **changes: Any) -> dict[str, Any]:
    """Return the committed record with ``changes`` applied inside ``section``."""
    document = _recorded()
    document[section] = {**document[section], **changes}
    return document


# ---------- the recorded run was isolated ----------


def test_the_recorded_isolation_record_validates() -> None:
    """The committed file is an isolation record, not merely well-formed JSON."""
    record = load_record()
    assert record.schema_version == "canary-isolation/v1"
    assert record.rehearsed_on == "2026-09-18"


def test_the_production_root_digests_are_equal() -> None:
    """The claim the gate exists to settle: the rehearsal moved nothing outside itself."""
    root = load_record().production_root
    assert root.before_digest == root.after_digest
    assert root.unchanged


def test_the_production_root_digest_covers_a_real_tree() -> None:
    """A digest over nothing equals itself, so the file count is asserted too."""
    root = load_record().production_root
    assert root.tracked_file_count >= 1
    assert root.digest_method
    assert _DIGEST.match(root.before_digest)


def test_the_teardown_removed_the_canary_registry_row() -> None:
    """The canary was registered, and the row it took is gone."""
    record = load_record()
    assert record.canary.project_code in record.canary.rows_after_provision
    assert record.canary.project_code in record.teardown.removed_registry_codes


def test_no_registry_row_survives_the_teardown() -> None:
    """A row left behind is the canary leaking into a registry that outlives it."""
    assert load_record().teardown.registry_rows_after == ()


def test_neither_the_canary_tree_nor_its_runtime_directory_survives() -> None:
    """Teardown removes the tree it created and the runtime directory it allocated."""
    teardown = load_record().teardown
    assert teardown.runtime_dir_removed
    assert not teardown.runtime_dir_exists
    assert not teardown.root_exists


def test_the_operator_registry_was_never_addressed() -> None:
    """The canary took a scratch registry, so the real one has nothing to undo."""
    registry = load_record().operator_registry
    assert not registry.addressed
    assert registry.unchanged
    assert not registry.holds_canary_code


def test_the_recorded_run_has_no_isolation_defects() -> None:
    """Every claim above, taken together, through the one function that judges them."""
    assert isolation_defects(load_record()) == ()


def test_the_isolation_record_names_no_absolute_path() -> None:
    """The record is committed, so it must say nothing about the host that made it."""
    text = RECORD.read_text(encoding="utf-8")
    assert not [match.group(0) for match in _ABSOLUTE_PATH.finditer(text)]


@pytest.mark.parametrize("shape", LEAK_SHAPES)
def test_the_path_scan_catches_every_shape_it_looks_for(shape: str) -> None:
    """A scan nobody proved reads nothing; each shape is caught in a sentence."""
    sentence = f"the canary tree was at {shape}somebody/scratch"
    assert [match.group(0) for match in _ABSOLUTE_PATH.finditer(sentence)] == [shape]


# ---------- the checks have teeth ----------


def test_a_production_root_that_moved_is_reported() -> None:
    """The failure the gate exists for: a rehearsal that wrote outside its canary."""
    document = _with("production_root", after_digest=f"sha256:{'e' * 64}")
    defects = isolation_defects(load_record(document))
    assert defects == ("the production root digested differently before and after the rehearsal",)


def test_a_registry_row_the_teardown_kept_is_reported() -> None:
    """A canary that survives in a registry is one a later run will collide with."""
    document = _with("teardown", removed_registry_codes=[])
    defects = isolation_defects(load_record(document))
    assert defects[0] == "the teardown left a registry row for 'W84CANARY'"


def test_a_registry_still_holding_rows_is_reported() -> None:
    """Rows under other codes matter too: the scratch registry held only the canary."""
    document = _with("teardown", registry_rows_after=["LEFTOVER"])
    assert "still holds ['LEFTOVER'] after teardown" in isolation_defects(load_record(document))[0]


def test_a_surviving_canary_tree_is_reported() -> None:
    """A tree left on disk is a disposable repository nobody disposed of."""
    document = _with("teardown", root_exists=True)
    assert isolation_defects(load_record(document)) == ("the canary tree is still on disk",)


def test_a_surviving_runtime_directory_is_reported() -> None:
    """The runtime directory outlives the tree unless teardown removes it too."""
    document = _with("teardown", runtime_dir_removed=False, runtime_dir_exists=True)
    assert isolation_defects(load_record(document)) == (
        "the canary runtime directory is still on disk",
    )


def test_an_addressed_operator_registry_is_reported() -> None:
    """Writing the operator's registry is the second way a canary escapes."""
    document = _with("operator_registry", addressed=True)
    assert isolation_defects(load_record(document)) == (
        "the operator's own registry was addressed by the rehearsal",
    )


def test_an_operator_registry_holding_the_canary_code_is_reported() -> None:
    """The row itself, not just the act of writing, is what a later run collides with."""
    document = _with("operator_registry", holds_canary_code=True)
    assert isolation_defects(load_record(document)) == (
        "the operator's own registry holds a row under the canary's code",
    )


def test_a_teardown_claiming_to_remove_a_directory_that_is_present_is_refused() -> None:
    """The record's own rule: a removal and a survival cannot both be recorded."""
    document = _with("teardown", runtime_dir_exists=True)
    with pytest.raises(ValidationError, match="reported removed and still present"):
        load_record(document)


def test_a_teardown_removing_a_code_it_still_holds_is_refused() -> None:
    """The same contradiction on the registry side."""
    document = _with("teardown", registry_rows_after=["W84CANARY"])
    with pytest.raises(ValidationError, match="reported removed and still registered"):
        load_record(document)


def test_a_canary_that_was_never_registered_is_refused() -> None:
    """Removing a row nobody added proves nothing, so the record refuses the claim."""
    document = _with("canary", rows_after_provision=["SOMETHINGELSE"])
    with pytest.raises(ValidationError, match="is not among the rows its provisioning left"):
        load_record(document)


def test_a_provisioning_that_left_no_rows_is_refused() -> None:
    """The empty boundary: an empty row list cannot contain the canary's code."""
    document = _with("canary", rows_after_provision=[])
    with pytest.raises(ValidationError):
        load_record(document)


def test_a_digest_that_is_not_a_digest_is_refused() -> None:
    """A free-text digest could be made equal to anything, including itself."""
    document = _with("production_root", before_digest="unchanged", after_digest="unchanged")
    with pytest.raises(ValidationError):
        load_record(document)


def test_a_truncated_digest_is_refused() -> None:
    """The off-by-one boundary on digest width, refused at the loader."""
    document = _with("production_root", after_digest=f"sha256:{'a' * 63}")
    with pytest.raises(ValidationError):
        load_record(document)


def test_a_digest_over_no_files_is_refused() -> None:
    """A pair over zero files is two digests of nothing, which always agree."""
    document = _with("production_root", tracked_file_count=0)
    with pytest.raises(ValidationError):
        load_record(document)


def test_an_unknown_key_is_refused() -> None:
    """The record forbids extras, so a typo fails rather than being dropped."""
    document = _recorded()
    document["torn_down_by"] = "somebody"
    with pytest.raises(ValidationError):
        load_record(document)


def test_a_record_of_another_schema_version_is_refused() -> None:
    """A version this suite does not read is refused rather than read as if it were."""
    document = _recorded()
    document["schema_version"] = "canary-isolation/v2"
    with pytest.raises(ValidationError):
        load_record(document)


def test_a_record_missing_its_teardown_is_refused() -> None:
    """A rehearsal with no recorded teardown has not shown it cleaned up."""
    document = _recorded()
    del document["teardown"]
    with pytest.raises(ValidationError):
        load_record(document)


def test_a_record_that_is_not_a_mapping_is_refused() -> None:
    """The wrong-type boundary, refused at the loader rather than downstream."""
    with pytest.raises(ValidationError):
        load_record([])
