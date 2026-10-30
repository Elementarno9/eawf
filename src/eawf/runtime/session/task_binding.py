"""A machine-local record of the Task each host session serves.

A host session the operator starts is not a Run, so no state row says which Task it
worked on, yet its transcript belongs with that Task's. When the session starts on a
Task worktree branch, the start hook binds it to the Task the branch names; the
operator re-scopes a session with ``eawf session bind``. Each binding is a row
appended under the tree's ``local/`` directory, beside the transcript index, and the
latest row of a session is the one that counts, so a re-scope supersedes and nothing
is rewritten.

A branch binding fills only a session no row binds yet, checked under the lock the
binding is appended under: a session resumed on its start branch, or a start hook
racing ``eawf session bind``, must not undo the operator's re-scope.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Annotated, Final, Literal

from pydantic import BaseModel, ConfigDict, StringConstraints, field_validator

from eawf.kernel.state.epoch2.measurement import VendorSessionDigest
from eawf.kernel.state.types import UtcDatetime
from eawf.kernel.store.append import append_json_line, append_json_line_unless
from eawf.kernel.store.paths import LOCAL_DIRNAME
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.runtime.session.vendor_id import hash_vendor_session_id

logger = logging.getLogger(__name__)

#: The binding file, inside the tree's ``local/`` directory.
SESSION_BINDING_FILENAME: Final = "session-binding.jsonl"

#: Each environment variable a host runtime exports its own session id in, checked in
#: order, so ``eawf session bind`` run from inside a session finds that session.
SESSION_ID_ENV: Final[tuple[str, ...]] = ("CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID")

#: Who wrote a binding: the start hook reading the branch, or the operator.
BindingSource = Literal["branch", "operator"]

_TaskKey = Annotated[str, StringConstraints(strict=True, pattern=r"^[A-Z][A-Z0-9_-]{1,15}-\d{4,}$")]


class SessionTaskBinding(BaseModel):
    """One binding of a host session to a Task.

    Attributes:
        vendor_session_digest: The session's id, hashed on validation, so the
            raw id never reaches the file.
        task_key: The Task the session serves.
        source: Who bound it.
        recorded_at: When the row was written.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    vendor_session_digest: VendorSessionDigest
    task_key: _TaskKey
    source: BindingSource
    recorded_at: UtcDatetime

    @field_validator("vendor_session_digest", mode="before")
    @classmethod
    def _hash_raw_id(cls, value: object) -> object:
        """Hash a raw vendor session id; pass anything else to validation."""
        if isinstance(value, str) and value:
            return hash_vendor_session_id(value)
        return value


def session_binding_path(tree_root: Path) -> Path:
    """Return the binding file of the tree at *tree_root* (not created here).

    Args:
        tree_root: The tree's ``.ea`` directory.

    Returns:
        ``<tree_root>/local/session-binding.jsonl``.
    """
    return tree_root / LOCAL_DIRNAME / SESSION_BINDING_FILENAME


def session_task_keys(tree_root: Path) -> dict[str, str]:
    """Return the Task each bound session serves, by its latest binding.

    Args:
        tree_root: The tree's ``.ea`` directory.

    Returns:
        Each session digest mapped to its Task key; empty when nothing was bound.

    Raises:
        pydantic.ValidationError: A line does not validate, so the file is corrupt.
    """
    try:
        text = session_binding_path(tree_root).read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    bound: dict[str, str] = {}
    for line in text.splitlines():
        if line.strip():
            row = SessionTaskBinding.model_validate_json(line)
            bound[row.vendor_session_digest] = row.task_key
    return bound


def _logged(row: SessionTaskBinding) -> SessionTaskBinding:
    logger.info(f"bind_session task={row.task_key} source={row.source}")
    return row


def bind_session_to_branch_task(
    tree_root: Path, *, session_id: str, task_key: str, now: datetime
) -> SessionTaskBinding | None:
    """Bind a starting session to the Task its branch names, unless it is bound already.

    Args:
        tree_root: The tree's ``.ea`` directory.
        session_id: The session's raw id.
        task_key: The Task key the checked-out branch names.
        now: When the row is written.

    Returns:
        The appended row, or ``None`` when a row already binds the session.

    Raises:
        pydantic.ValidationError: The session id is empty or the Task key is
            malformed, or the file holds a corrupt line.
        StateConflict: The file's append lock could not be acquired.
    """
    row = SessionTaskBinding(
        vendor_session_digest=session_id, task_key=task_key, source="branch", recorded_at=now
    )
    if not append_json_line_unless(
        session_binding_path(tree_root),
        row.model_dump_json(),
        lambda: row.vendor_session_digest in session_task_keys(tree_root),
    ):
        return None
    return _logged(row)


def current_session_id(env: Mapping[str, str]) -> str:
    """Return the session ``eawf session bind`` re-scopes when none is named.

    Only the session the command runs inside is taken. The host session the
    index recorded last is not: with sessions running side by side it may be
    another one, which would be re-scoped in this one's place.

    Args:
        env: The process environment; a host runtime exports its session id there.

    Returns:
        The id the environment names.

    Raises:
        LookupError: The environment names no host session.
    """
    for variable in SESSION_ID_ENV:
        if env.get(variable):
            return env[variable]
    raise LookupError("this command runs in no host session; name one with --session")


def rebind_session(
    tree_root: Path, *, task_key: str, session_id: str, now: datetime
) -> SessionTaskBinding:
    """Bind a host session to *task_key*, superseding any binding it had.

    Args:
        tree_root: The tree's ``.ea`` directory.
        task_key: The Task the session serves from now on.
        session_id: The session's raw id or its digest.
        now: When the row is written.

    Returns:
        The appended row.

    Raises:
        LookupError: The tree's generation holds no Task keyed *task_key*.
        GenerationUnavailableError: The tree is not at authority epoch 2.
        pydantic.ValidationError: The session id is empty or the Task key is
            malformed.
        StateConflict: The file's append lock could not be acquired.
    """
    row = SessionTaskBinding(
        vendor_session_digest=session_id, task_key=task_key, source="operator", recorded_at=now
    )
    # Imported here: the session-start hook imports this module, and only the
    # operator's re-scope reads the generation.
    from eawf.observability.reflect.entities import read_standing_rows

    if task_key not in read_standing_rows(tree_root, Epoch2Collection.TASK):
        raise LookupError(f"the tree holds no Task {task_key}")
    append_json_line(session_binding_path(tree_root), row.model_dump_json())
    return _logged(row)


__all__ = [
    "SESSION_BINDING_FILENAME",
    "SESSION_ID_ENV",
    "BindingSource",
    "SessionTaskBinding",
    "bind_session_to_branch_task",
    "current_session_id",
    "rebind_session",
    "session_binding_path",
    "session_task_keys",
]
