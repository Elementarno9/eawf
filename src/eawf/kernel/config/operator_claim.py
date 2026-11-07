"""The operator principal this machine's user claims to be.

The claim lives in the user config layer and nowhere else: a claim of identity is the
person's, and a principal named in a repository's committed config would make everyone
who clones it act as that one principal. The reader lives here rather than with a
surface because the daemon, which cannot import surfaces, reads it too.
"""

from __future__ import annotations

from pydantic import ValidationError as PydValidationError

from eawf.kernel.config import layered
from eawf.kernel.config.loader import load_yaml_layer
from eawf.kernel.config.schema import OperatorConfig
from eawf.surfaces.cli.errors import ValidationError


def claimed_operator() -> str | None:
    """Return the principal this machine's operator claims in the user config, if any.

    Returns:
        The claimed principal key; ``None`` when no claim is recorded.

    Raises:
        ValueError: The user layer cannot be read or parsed, or its ``operator`` block
            is not one, or its claim is not a principal key.
    """
    try:
        block = load_yaml_layer(layered.global_config_path()).get("operator") or {}
        return OperatorConfig.model_validate(block).principal
    except (ValidationError, PydValidationError) as error:
        raise ValueError(
            f"operator.principal in the user config is not a principal key (e.g. OP-0001): {error}"
        ) from error


__all__ = ["claimed_operator"]
