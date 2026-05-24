"""TOFU (trust-on-first-use) ledger for non-bundled profile bodies (P14-W05 / D19).

Behaviour:

- Built-in profiles bundled under ``eawf.platform.profiles.data`` are implicitly
  trusted (the package is the source of truth — its SHA cannot drift).
- Non-bundled profiles (workspace or user overlay) must be granted trust
  before they are composed into a project's effective profile set. The
  first load prompts the operator; the granted ``sha256`` is persisted
  into ``<workspace>/.ea/config.yaml`` under
  ``profiles.trusted: {<name>: <hex-sha>}``.
- Subsequent loads recompute the sha and compare. A match returns ``True``
  silently; a divergence raises :class:`TrustDriftError` so the caller can
  re-prompt.

Public API:

    profile_sha256(path)                            -> str
    is_bundled(profile_id)                          -> bool
    load_trust_ledger(config_path)                  -> dict[str, str]
    save_trust_ledger(config_path, ledger)          -> None
    verify_trust(profile_id, path, ledger, no_input) -> TrustStatus
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)


_DATA_PACKAGE: str = "eawf.platform.profiles.data"
_TRUSTED_KEY: str = "trusted"
_PROFILES_KEY: str = "profiles"


class TrustDriftError(Exception):
    """Raised when the stored sha for *profile_id* no longer matches disk."""


class UntrustedProfileError(Exception):
    """Raised when a non-bundled profile has no entry in the trust ledger."""


@dataclass(frozen=True)
class TrustStatus:
    """Outcome of a :func:`verify_trust` call."""

    profile_id: str
    sha256: str
    is_bundled: bool
    was_already_trusted: bool


def profile_sha256(path: Path) -> str:
    """Stable hex SHA-256 of *path*'s bytes."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def is_bundled(profile_id: str) -> bool:
    """``True`` iff ``data/<profile_id>.yaml`` ships with the package."""
    bundle = files(_DATA_PACKAGE)
    return bundle.joinpath(f"{profile_id}.yaml").is_file()


def load_trust_ledger(config_path: Path) -> dict[str, str]:
    """Return ``profiles.trusted`` from *config_path* or an empty mapping.

    A missing or unreadable file is treated as an empty ledger — the
    caller (e.g. ``profile validate``) decides whether to fall back to
    untrusted or to grant trust on first run.
    """
    if not config_path.is_file():
        return {}
    try:
        data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return {}
    if not isinstance(data, dict):
        return {}
    profiles = data.get(_PROFILES_KEY)
    if not isinstance(profiles, dict):
        return {}
    trusted = profiles.get(_TRUSTED_KEY)
    if not isinstance(trusted, dict):
        return {}
    return {str(k): str(v) for k, v in trusted.items()}


def save_trust_ledger(config_path: Path, ledger: dict[str, str]) -> None:
    """Persist *ledger* into ``profiles.trusted`` on *config_path*.

    Reads the existing YAML body (or starts from an empty mapping when
    missing), merges the ledger under ``profiles.trusted``, and writes the
    result back via a tempfile + ``os.replace`` rename. The caller is
    expected to hold whatever portalock is appropriate; this helper does
    not take its own lock to keep the surface easy to call from CLI
    handlers that already own a transaction-level lock.
    """
    body: dict[str, Any]
    if config_path.is_file():
        loaded = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        body = loaded if isinstance(loaded, dict) else {}
    else:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        body = {}
    profiles = body.setdefault(_PROFILES_KEY, {})
    if not isinstance(profiles, dict):
        profiles = {}
        body[_PROFILES_KEY] = profiles
    profiles[_TRUSTED_KEY] = dict(ledger)
    rendered = yaml.safe_dump(body, sort_keys=True, default_flow_style=False)
    tmp = config_path.with_name(f"{config_path.name}.tmp")
    tmp.write_text(rendered, encoding="utf-8")
    tmp.replace(config_path)


def verify_trust(
    profile_id: str,
    *,
    path: Path | None,
    ledger: dict[str, str],
    no_input: bool,
) -> TrustStatus:
    """Resolve trust for *profile_id* against *ledger*.

    Args:
        profile_id: Profile name.
        path: On-disk path to the profile body. ``None`` for bundled
            profiles (the trust check is short-circuited).
        ledger: Already-loaded ``profiles.trusted`` mapping.
        no_input: When ``True``, an untrusted profile or sha drift fails
            closed (the CLI cannot prompt). When ``False``, the caller
            takes responsibility for prompting; this helper returns a
            status indicating which path applies.

    Raises:
        TrustDriftError: Stored sha for *profile_id* does not match the
            recomputed sha. The caller re-prompts the operator.
        UntrustedProfileError: ``no_input`` is true and the profile has no
            ledger entry; refuse to load implicitly.
    """
    if path is None or is_bundled(profile_id):
        return TrustStatus(
            profile_id=profile_id,
            sha256="",
            is_bundled=True,
            was_already_trusted=True,
        )
    sha = profile_sha256(path)
    stored = ledger.get(profile_id)
    if stored is None:
        if no_input:
            raise UntrustedProfileError(
                f"profile {profile_id!r} is not in the trust ledger; "
                f"--no-input mode refuses implicit trust grants"
            )
        return TrustStatus(
            profile_id=profile_id,
            sha256=sha,
            is_bundled=False,
            was_already_trusted=False,
        )
    if stored != sha:
        raise TrustDriftError(
            f"profile {profile_id!r}: stored sha {stored!r} differs from on-disk sha {sha!r}"
        )
    return TrustStatus(
        profile_id=profile_id,
        sha256=sha,
        is_bundled=False,
        was_already_trusted=True,
    )
