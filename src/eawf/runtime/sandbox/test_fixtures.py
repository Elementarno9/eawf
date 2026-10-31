"""Explicit, content-bound read-only data supplied to test command gates."""

from __future__ import annotations

import hashlib
import os
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from eawf.kernel.delivery.receipts import canonical_digest


class TestFixtureInput(BaseModel):
    """Portable declaration of every file in a test-data directory."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]{0,63}$")]
    environment_variable: Annotated[
        str, Field(pattern=r"^[A-Z][A-Z0-9_]*_TEST_(?:[A-Z0-9_]+_)?DATA$")
    ]
    files: dict[str, Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]] = Field(min_length=1)
    digest: Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]

    @model_validator(mode="after")
    def _portable_manifest(self) -> Self:
        """Reject escaping paths and a digest that does not bind this manifest."""
        for name in self.files:
            path = Path(name)
            if (
                not name
                or name == "."
                or path.is_absolute()
                or "\\" in name
                or ":" in name
                or ".." in path.parts
                or path.as_posix() != name
            ):
                raise ValueError("fixture files must use normalized relative paths")
        if self.digest != canonical_digest(self.files):
            raise ValueError("fixture digest does not match its declared file manifest")
        return self


class TestFixtureBinding(BaseModel):
    """Machine-local directory binding; never part of a gate or proof ledger."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]{0,63}$")]
    directory: Path

    @field_validator("directory")
    @classmethod
    def _absolute_directory(cls, value: Path) -> Path:
        """Require an explicit local root rather than a cwd-dependent lookup."""
        if not value.is_absolute():
            raise ValueError("fixture directory must be absolute")
        return value


class ResolvedTestFixture(BaseModel):
    """A verified input carried only through the local child execution channel."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    declaration: TestFixtureInput
    directory: Path


def resolve_test_fixtures(
    declarations: Sequence[TestFixtureInput], bindings: Sequence[TestFixtureBinding]
) -> tuple[ResolvedTestFixture, ...]:
    """Verify exact read-only content once before a native invocation can reuse proof.

    Raises:
        ValueError: A binding is absent, repeated, undeclared, writable,
            symlinked, incomplete or does not match its declared content.
    """
    named = {item.id: item for item in declarations}
    bound = {item.id: item for item in bindings}
    if (
        len(named) != len(declarations)
        or len(bound) != len(bindings)
        or named.keys() != bound.keys()
    ):
        raise ValueError("fixture declarations and bindings must name the same unique inputs")
    resolved = []
    for key, declaration in named.items():
        directory = bound[key].directory
        if directory.is_symlink() or not directory.is_dir() or os.access(directory, os.W_OK):
            raise ValueError(f"fixture {key!r} requires a read-only directory")
        files = {}
        for path in directory.rglob("*"):
            if path.is_symlink() or os.access(path, os.W_OK):
                raise ValueError(f"fixture {key!r} contains a writable or symlinked entry")
            if path.is_dir():
                continue
            if not path.is_file():
                raise ValueError(f"fixture {key!r} contains a non-file entry")
            with path.open("rb") as handle:
                files[path.relative_to(directory).as_posix()] = hashlib.file_digest(
                    handle, "sha256"
                ).hexdigest()
        if files != declaration.files:
            raise ValueError(f"fixture {key!r} content does not match its declared manifest")
        resolved.append(ResolvedTestFixture(declaration=declaration, directory=directory.resolve()))
    return tuple(resolved)


_FIXTURES: ContextVar[tuple[ResolvedTestFixture, ...]] = ContextVar("test_fixtures", default=())


@contextmanager
def test_fixture_context(fixtures: tuple[ResolvedTestFixture, ...]) -> Iterator[None]:
    """Bind already verified local inputs for the command runner in this child."""
    token = _FIXTURES.set(fixtures)
    try:
        yield
    finally:
        _FIXTURES.reset(token)


def test_fixture_environment(declarations: Sequence[TestFixtureInput]) -> dict[str, str]:
    """Return only requested verified fixture paths, refusing unbound inputs."""
    available = {item.declaration.id: item for item in _FIXTURES.get()}
    environment: dict[str, str] = {}
    for declaration in declarations:
        fixture = available.get(declaration.id)
        if fixture is None or fixture.declaration != declaration:
            raise ValueError(f"fixture {declaration.id!r} is not bound to verified content")
        variable = declaration.environment_variable
        if variable in environment:
            raise ValueError("fixture environment variables must be unique per gate")
        environment[variable] = str(fixture.directory)
    return environment
