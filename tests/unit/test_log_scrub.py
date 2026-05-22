"""Tests for the emit-time :class:`SensitiveScrubber` log filter."""

from __future__ import annotations

import logging
import tomllib
from importlib.resources import files
from pathlib import Path

import pytest

from eawf.logging.scrub import REDACTION, SensitiveScrubber


def _scrubber() -> SensitiveScrubber:
    """Return a scrubber with the default (pyproject-derived) allowlist."""
    return SensitiveScrubber()


def _canonical_author_email() -> str:
    """Read the canonical author email from eawf's pyproject.toml.

    Kept out of the test as a literal so no PII lands in committed
    source; the value is sourced from the same file the scrubber
    allowlists at runtime.
    """
    package_root = Path(str(files("eawf"))).resolve()
    for candidate in [package_root, *package_root.parents]:
        pyproject = candidate / "pyproject.toml"
        if not pyproject.is_file():
            continue
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        project = data.get("project", {})
        if project.get("name") != "eawf":
            continue
        authors = project.get("authors") or []
        if authors and isinstance(authors[0], dict):
            email = authors[0].get("email")
            if isinstance(email, str) and email:
                return email
    pytest.skip("canonical author email not resolvable from pyproject.toml")


# --- one test per PATTERN -------------------------------------------------


def test_scrub_macos_home_path() -> None:
    scrubber = _scrubber()
    line = "create_worktree path=/Users/jdoe/Workspace/repo done"  # pragma: allowlist secret
    out = scrubber.scrub(line)
    assert "/Users/jdoe" not in out  # pragma: allowlist secret
    assert REDACTION in out
    # Trailing path segments are split on '/', so only the home root is
    # masked; the redaction marker must be present.
    assert out.startswith("create_worktree path=<scrubbed>")


def test_scrub_windows_home_path() -> None:
    scrubber = _scrubber()
    out = scrubber.scrub(r"open path=C:\Users\jdoe done")  # pragma: allowlist secret
    assert r"C:\Users\jdoe" not in out  # pragma: allowlist secret
    assert REDACTION in out


def test_scrub_linux_home_path() -> None:
    scrubber = _scrubber()
    out = scrubber.scrub("read path=/home/jdoe done")  # pragma: allowlist secret
    assert "/home/jdoe" not in out  # pragma: allowlist secret
    assert REDACTION in out


def test_scrub_email() -> None:
    scrubber = SensitiveScrubber(allowed_emails=frozenset())
    out = scrubber.scrub("notify addr=stranger@example.com queued")
    assert "stranger@example.com" not in out
    assert REDACTION in out


def test_scrub_openai_key() -> None:
    scrubber = _scrubber()
    key = "sk-" + "a1B2c3D4e5F6g7H8i9J0"
    out = scrubber.scrub(f"auth token={key}")
    assert key not in out
    assert REDACTION in out


def test_scrub_anthropic_key() -> None:
    scrubber = _scrubber()
    key = "sk-ant-" + "a1B2c3D4e5F6g7H8i9J0K1"
    out = scrubber.scrub(f"auth token={key}")
    assert key not in out
    # The Anthropic prefix must not be left dangling in front of the
    # redaction marker.
    assert "sk-ant-<scrubbed>" not in out
    assert REDACTION in out


def test_scrub_github_pat() -> None:
    scrubber = _scrubber()
    pat = "ghp_" + "A" * 36
    out = scrubber.scrub(f"clone token={pat}")
    assert pat not in out
    assert REDACTION in out


def test_scrub_ipv4_address() -> None:
    scrubber = _scrubber()
    out = scrubber.scrub("peer connect addr=192.168.0.1 ok")
    assert "192.168.0.1" not in out
    assert REDACTION in out


def test_scrub_ipv6_address() -> None:
    scrubber = _scrubber()
    out = scrubber.scrub("peer connect addr=2001:0db8:85a3:0000:0000:8a2e:0370:7334 ok")
    assert "2001:0db8" not in out
    assert REDACTION in out


def test_scrub_tilde_home_path() -> None:
    scrubber = _scrubber()
    out = scrubber.scrub("load cfg=~/.eawf/state.json done")
    assert "~/.eawf" not in out
    assert REDACTION in out


def test_scrub_dotted_version_string_not_clobbered_by_ipv4() -> None:
    # A three-segment dotted version is not an IPv4 dotted-quad, so the
    # IPv4 pattern's word-boundary anchors must leave it intact.
    scrubber = _scrubber()
    msg = "boot version=0.3.0 ready"
    assert scrubber.scrub(msg) == msg


# --- allowlist ------------------------------------------------------------


def test_canonical_author_email_preserved_by_allowlist() -> None:
    email = _canonical_author_email()
    scrubber = _scrubber()
    out = scrubber.scrub(f"author email={email} ok")
    assert email in out
    assert REDACTION not in out


def test_non_canonical_email_scrubbed_even_with_default_allowlist() -> None:
    scrubber = _scrubber()
    out = scrubber.scrub("author email=outsider@elsewhere.org ok")  # pragma: allowlist secret
    assert "outsider@elsewhere.org" not in out  # pragma: allowlist secret
    assert REDACTION in out


def test_explicit_allowlist_preserves_listed_email() -> None:
    scrubber = SensitiveScrubber(allowed_emails=frozenset({"keep@allow.test"}))
    out = scrubber.scrub("a x=keep@allow.test b x=drop@deny.test")
    assert "keep@allow.test" in out
    assert "drop@deny.test" not in out
    assert REDACTION in out


def test_noreply_coauthor_addresses_preserved_by_default() -> None:
    scrubber = _scrubber()
    out = scrubber.scrub("trailer co=noreply@anthropic.com other=noreply@openai.com")
    assert "noreply@anthropic.com" in out
    assert "noreply@openai.com" in out
    assert REDACTION not in out


def test_scrub_allowlisted_email_after_path_survives_in_position() -> None:
    # Regression: a redacted path occupies the first ``<scrubbed>`` slot,
    # so first-occurrence restoration would drop the allowlisted email
    # into the path's slot. Positional placeholders keep each token put.
    scrubber = SensitiveScrubber(allowed_emails=frozenset({"keep@allow.test"}))
    out = scrubber.scrub("path=/Users/jdoe email=keep@allow.test end")  # pragma: allowlist secret
    assert out == "path=<scrubbed> email=keep@allow.test end"


def test_scrub_path_redacted_while_allowlisted_email_preserved() -> None:
    scrubber = SensitiveScrubber(allowed_emails=frozenset({"keep@allow.test"}))
    out = scrubber.scrub("open path=/Users/secret mail=keep@allow.test")  # pragma: allowlist secret
    assert "/Users/secret" not in out  # pragma: allowlist secret
    assert "keep@allow.test" in out
    assert REDACTION in out


def test_scrub_multiple_paths_and_one_email_each_restored_to_right_slot() -> None:
    scrubber = SensitiveScrubber(allowed_emails=frozenset({"keep@allow.test"}))
    out = scrubber.scrub("a=/Users/x b=keep@allow.test c=/home/y")  # pragma: allowlist secret
    assert out == "a=<scrubbed> b=keep@allow.test c=<scrubbed>"


def test_scrub_two_allowlisted_emails_interleaved_with_path() -> None:
    scrubber = SensitiveScrubber(allowed_emails=frozenset({"a@allow.test", "b@allow.test"}))
    out = scrubber.scrub("p=/Users/z m1=a@allow.test m2=b@allow.test")  # pragma: allowlist secret
    assert out == "p=<scrubbed> m1=a@allow.test m2=b@allow.test"


def test_scrub_allowlisted_preserved_while_other_email_scrubbed() -> None:
    scrubber = SensitiveScrubber(allowed_emails=frozenset({"keep@allow.test"}))
    out = scrubber.scrub("good=keep@allow.test bad=stranger@evil.test")
    assert "keep@allow.test" in out
    assert "stranger@evil.test" not in out
    assert REDACTION in out


# --- filter() integration -------------------------------------------------


def _record(msg: str, *args: object) -> logging.LogRecord:
    return logging.LogRecord(
        name="eawf.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=msg,
        args=args,
        exc_info=None,
    )


def test_filter_returns_true_and_rewrites_record_in_place() -> None:
    scrubber = _scrubber()
    record = _record("auth token=%s", "sk-" + "Z" * 24)
    assert scrubber.filter(record) is True
    assert record.getMessage() == "auth token=<scrubbed>"
    # args are cleared so the formatter cannot re-interpolate the secret.
    assert record.args == ()


def test_filter_scrubs_after_arg_interpolation() -> None:
    scrubber = SensitiveScrubber(allowed_emails=frozenset())
    record = _record("notify addr=%s", "leak@secret.test")
    scrubber.filter(record)
    assert "leak@secret.test" not in record.getMessage()
    assert REDACTION in record.getMessage()


def test_filter_attaches_to_handler_and_scrubs_emitted_output() -> None:
    import io

    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(message)s"))
    handler.addFilter(_scrubber())

    log = logging.getLogger("eawf.test.scrub.handler")
    log.handlers = [handler]
    log.setLevel(logging.INFO)
    log.propagate = False

    pat_value = "ghp_" + "B" * 40
    log.info("clone token=%s", pat_value)
    handler.flush()

    emitted = stream.getvalue()
    assert pat_value not in emitted
    assert REDACTION in emitted


# --- error / boundary paths ----------------------------------------------


def test_scrub_empty_message_is_noop() -> None:
    assert _scrubber().scrub("") == ""


def test_scrub_clean_message_unchanged() -> None:
    msg = "phase_activate phase=P27 base=main"
    assert _scrubber().scrub(msg) == msg


def test_email_pattern_index_raises_when_no_email_pattern() -> None:
    import re

    from eawf.logging.scrub import _email_pattern_index

    with pytest.raises(ValueError, match="no email pattern"):
        _email_pattern_index((re.compile(r"\d+"),))
