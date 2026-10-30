"""Read a state-document fixture into the in-memory contract view.

The measured-contract and release suites keep their contracts in
state-document fixtures; the code under test reads a
:class:`~eawf.workflow.evidence._io.ContractView`, which holds only the
project code and the artifacts.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import orjson

from eawf.kernel.state.models import Artifact
from eawf.workflow.evidence._io import ContractView


def contract_view(path: Path) -> ContractView:
    """Return the project code and artifacts of the state document at *path*.

    Args:
        path: A state-document fixture.

    Returns:
        A fresh view; mutating it never touches *path*.

    Raises:
        FileNotFoundError: *path* does not exist.
        pydantic.ValidationError: An artifact row violates its model.
    """
    payload = orjson.loads(path.read_bytes())
    project = payload.get("project")
    return ContractView(
        project_code=project["code"] if isinstance(project, dict) else None,
        artifacts={
            key: Artifact.model_validate(row)
            for key, row in (payload.get("artifacts") or {}).items()
        },
        updated_at=datetime.now(UTC),
    )
