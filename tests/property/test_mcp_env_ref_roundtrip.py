"""Hypothesis property test: env-ref values never reach disk.

Invariant: for any (env_var_name, secret_value) drawn from the
strategy, monkeypatching ``os.environ[env_var_name] = secret_value``
and running the full ``add → install`` pipeline produces a
settings.json on disk where ``secret_value`` does not appear in the
raw bytes.

This is the secret-leak smoke test. The Eä installer never reads
``os.environ`` for env-ref names — the literal ``${ENV:NAME}`` token
is what gets persisted — but a regression elsewhere (e.g. an
accidental ``os.environ[NAME]`` substitution in a future helper)
would reach disk. The property pins that invariant.

Strategy:

- ``env_var_name`` matches ``[A-Z_][A-Z0-9_]{0,30}`` (uppercase /
  underscore / digits — the env-ref grammar).
- ``secret_value`` matches ``sec-[a-z]{16,64}`` — a stable prefix
  (``sec-``) followed by lowercase letters. The disjoint alphabet
  guarantees the secret can never be a substring of a var name
  (which is uppercase) or of any JSON keyword (``"command"``,
  ``"args"``, ...). Without the disjoint alphabet, Hypothesis
  trivially finds collisions where the secret appears verbatim
  inside the env-var name, raising false positives.
"""

from __future__ import annotations

import os
from pathlib import Path

import orjson
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from eawf.kernel.state.enums import McpRisk, McpStatus
from eawf.kernel.state.models import McpServer
from eawf.runtime.mcp.installer import install_runtime_entry

pytestmark = pytest.mark.property


_NAME_STRATEGY = st.from_regex(r"\A[A-Z_][A-Z0-9_]{0,30}\Z", fullmatch=True)
_SECRET_STRATEGY = st.from_regex(r"\Asec-[a-z]{16,64}\Z", fullmatch=True)


def _make_state(server_id: str, env_ref: str) -> McpServer:
    return McpServer(
        id=server_id,
        owner="eawf",
        command="/usr/bin/probe",
        args=[],
        env_refs=[env_ref],
        risk=McpRisk.READ,
        write_capable=False,
        status=McpStatus.CONFIGURED,
        installed_targets=[],
    )


@given(env_var_name=_NAME_STRATEGY, secret_value=_SECRET_STRATEGY)
@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_install_never_writes_secret_value_to_disk(
    tmp_path_factory: pytest.TempPathFactory,
    env_var_name: str,
    secret_value: str,
) -> None:
    tmp_path: Path = tmp_path_factory.mktemp("mcp_env_ref_roundtrip")
    # We rely on orjson's deterministic dump only for assertion
    # framing; the installer itself uses ``json.dumps``.
    _ = orjson

    # Set the secret in the ambient environment. The installer must
    # never read it back out — the property is precisely that the
    # secret never reaches the on-disk settings.json.
    prior = os.environ.get(env_var_name)
    os.environ[env_var_name] = secret_value
    try:
        token = f"${{ENV:{env_var_name}}}"
        server = _make_state(server_id="probe", env_ref=token)
        install_runtime_entry(
            server=server,
            runtime="claude",
            target_dir=tmp_path,
            force=False,
            timestamp="1970-01-01T00:00:00+00:00",
        )
        settings_path = tmp_path / ".claude" / "settings.json"
        raw = settings_path.read_bytes()
        # The secret value, encoded as UTF-8, must not appear
        # anywhere in the persisted bytes. The literal token *should*
        # appear (we wrote it deliberately).
        assert secret_value.encode("utf-8") not in raw
        assert token.encode("utf-8") in raw
    finally:
        if prior is None:
            os.environ.pop(env_var_name, None)
        else:
            os.environ[env_var_name] = prior
