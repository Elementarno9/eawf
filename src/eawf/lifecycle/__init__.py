"""Lifecycle helpers (project/subproject/phase/iter/wave).

The lifecycle package houses pure-functional helpers that the
:mod:`eawf.cli.commands.lifecycle` Typer handlers compose under a held
sibling lock. The split keeps the CLI handlers thin (parse → emit) and lets
the allocator/transition logic be unit-tested without spinning up Typer.
"""

from __future__ import annotations
