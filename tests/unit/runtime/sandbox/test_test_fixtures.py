"""Fixture declarations bind exact read-only data without arbitrary environment access."""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from pathlib import Path

import pytest
from pydantic import ValidationError

from eawf.kernel.delivery.receipts import canonical_digest
from eawf.runtime.sandbox.test_fixtures import (
    TestFixtureBinding as FixtureBinding,
)
from eawf.runtime.sandbox.test_fixtures import (
    TestFixtureInput as FixtureInput,
)
from eawf.runtime.sandbox.test_fixtures import (
    resolve_test_fixtures,
)
from eawf.runtime.sandbox.test_fixtures import (
    test_fixture_context as fixture_context,
)
from eawf.runtime.sandbox.test_fixtures import (
    test_fixture_environment as fixture_environment,
)

pytestmark = pytest.mark.unit


def declaration(content: bytes = b"fixture") -> FixtureInput:
    """Declare one file without a machine-local path."""
    files = {".zip": hashlib.sha256(content).hexdigest()}
    return FixtureInput(
        id="example-data",
        environment_variable="CP_TEST_GAME_DATA",
        files=files,
        digest=canonical_digest(files),
    )


@pytest.fixture
def readonly(tmp_path: Path) -> Iterator[Path]:
    """Provide a tiny immutable directory and restore modes for cleanup."""
    directory = tmp_path / "data"
    directory.mkdir()
    (directory / ".zip").write_bytes(b"fixture")
    (directory / ".zip").chmod(0o444)
    directory.chmod(0o555)
    yield directory
    directory.chmod(0o755)
    (directory / ".zip").chmod(0o644)


def test_exact_readonly_fixture_supplies_only_its_declared_path(readonly: Path) -> None:
    item = declaration()
    bound = resolve_test_fixtures([item], [FixtureBinding(id=item.id, directory=readonly)])
    with fixture_context(bound):
        assert fixture_environment([item]) == {"CP_TEST_GAME_DATA": str(readonly)}
        assert fixture_environment([]) == {}
    with pytest.raises(ValueError, match="not bound"):
        fixture_environment([item])


@pytest.mark.parametrize(
    "variable",
    ["PATH", "LD_PRELOAD", "PYTHONPATH", "GH_TOKEN", "DOTNET_ROOT", "CP_TEST_DATA;GH_TOKEN"],
)
def test_runtime_or_credential_environment_names_are_not_fixture_inputs(variable: str) -> None:
    payload = declaration().model_dump()
    payload["environment_variable"] = variable
    with pytest.raises(ValidationError):
        FixtureInput.model_validate(payload)


@pytest.mark.parametrize(
    "name",
    ["../escape", "/absolute", "a/../escape", "a//file", "./file", "C:/file", "a\\file", ".", ""],
)
def test_manifest_paths_cannot_escape_or_change_spelling(name: str) -> None:
    files = {name: "a" * 64}
    with pytest.raises(ValidationError):
        FixtureInput(
            id="data",
            environment_variable="CP_TEST_GAME_DATA",
            files=files,
            digest=canonical_digest(files),
        )


def test_writable_directory_is_refused_before_hashing(tmp_path: Path) -> None:
    item = declaration()
    with pytest.raises(ValueError, match="read-only directory"):
        resolve_test_fixtures([item], [FixtureBinding(id=item.id, directory=tmp_path)])


def test_writable_file_symlink_and_extra_content_are_refused(
    readonly: Path, tmp_path: Path
) -> None:
    item = declaration()
    binding = FixtureBinding(id=item.id, directory=readonly)
    (readonly / ".zip").chmod(0o644)
    with pytest.raises(ValueError, match="writable or symlinked"):
        resolve_test_fixtures([item], [binding])
    (readonly / ".zip").chmod(0o444)
    readonly.chmod(0o755)
    (readonly / "extra").symlink_to(tmp_path)
    readonly.chmod(0o555)
    with pytest.raises(ValueError, match="symlinked"):
        resolve_test_fixtures([item], [binding])
    readonly.chmod(0o755)
    (readonly / "extra").unlink()
    (readonly / "extra").write_bytes(b"extra")
    (readonly / "extra").chmod(0o444)
    readonly.chmod(0o555)
    with pytest.raises(ValueError, match="manifest"):
        resolve_test_fixtures([item], [binding])


def test_missing_extra_and_duplicate_bindings_are_refused(readonly: Path) -> None:
    item = declaration()
    binding = FixtureBinding(id=item.id, directory=readonly)
    for declarations, bindings in [
        ([item], []),
        ([], [binding]),
        ([item], [binding, binding]),
        ([item, item], [binding]),
    ]:
        with pytest.raises(ValueError, match="same unique inputs"):
            resolve_test_fixtures(declarations, bindings)


def test_content_mismatch_and_manifest_digest_mismatch_are_refused(readonly: Path) -> None:
    item = declaration(b"different")
    with pytest.raises(ValueError, match="content does not match"):
        resolve_test_fixtures([item], [FixtureBinding(id=item.id, directory=readonly)])
    with pytest.raises(ValidationError, match="digest"):
        FixtureInput.model_validate({**item.model_dump(), "digest": canonical_digest("wrong")})
