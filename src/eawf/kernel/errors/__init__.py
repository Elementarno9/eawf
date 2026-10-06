"""Exception types shared across layers.

An exception that callers in several layers catch lives here rather than in
the module that first raised it, so catching it never imports that module's
layer.
"""

from __future__ import annotations
