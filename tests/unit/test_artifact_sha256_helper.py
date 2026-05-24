"""Unit tests for :func:`eawf.platform.artifacts.validation.sha256_file`."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from eawf.platform.artifacts.validation import sha256_file


def test_sha256_file_matches_hashlib_digest(tmp_path: Path) -> None:
    """Helper must agree with a one-shot ``hashlib.sha256`` call."""
    body = b"hello eawf"
    target = tmp_path / "body.txt"
    target.write_bytes(body)
    assert sha256_file(target) == hashlib.sha256(body).hexdigest()


def test_sha256_file_empty_file_returns_canonical_empty_digest(tmp_path: Path) -> None:
    target = tmp_path / "empty.txt"
    target.write_bytes(b"")
    digest = sha256_file(target)
    # Agree with the live hashlib digest; the well-known canonical hex
    # is not hard-coded here to keep the file out of detect-secrets'
    # high-entropy bucket.
    assert digest == hashlib.sha256(b"").hexdigest()
    assert len(digest) == 64


def test_sha256_file_large_streamed_input_matches_hashlib(tmp_path: Path) -> None:
    """Body larger than the 64 KiB chunk boundary still digests correctly."""
    body = b"X" * (1024 * 1024 + 7)  # 1 MiB + 7 bytes — straddles chunk boundary.
    target = tmp_path / "big.bin"
    target.write_bytes(body)
    assert sha256_file(target) == hashlib.sha256(body).hexdigest()


def test_sha256_file_returns_lowercase_hex(tmp_path: Path) -> None:
    target = tmp_path / "case.txt"
    target.write_bytes(b"ABC")
    digest = sha256_file(target)
    assert digest == digest.lower()
    assert len(digest) == 64


def test_sha256_file_missing_path_raises_filenotfound(tmp_path: Path) -> None:
    target = tmp_path / "nope.txt"
    with pytest.raises(FileNotFoundError):
        sha256_file(target)
