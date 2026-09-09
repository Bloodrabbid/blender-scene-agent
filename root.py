"""One way to reach the add-on root, from any depth.

Panels, surfaces and features all take the root module as a handle — `hb` —
and read everything off it with `getattr`. Getting hold of it turned out to be
a small recurring hazard, and this module exists to have exactly one answer.

**Counting dots does not survive a move.** Three modules resolved the root by
trimming a fixed number of components off their own `__package__`:
`rsplit(".", 1)` in `props`, `rsplit(".", 2)` in `features.overlays`,
`rpartition(".")` in `sidebar`. Each is correct only at the depth it was
written at, and a file that moves one directory keeps working right up until
it silently resolves to the wrong module — `sys.modules[...]` still answers,
the attribute reads still parse, and the failure arrives later as a draw that
raises or, worse, a `getattr` default that quietly answers `None` forever.
Both of those have happened here.

This module lives *in* the root package, so `__package__` already is the
answer and there is nothing to count. The import that reaches it is relative
and therefore checked by `scripts/audit_modules.py`, which is the difference
that matters: a wrong number of dots here fails loudly at import instead of
quietly at draw.
"""

import sys

#: The root package's dotted name. `blender_scene_agent` from a source
#: checkout, `bl_ext.user_default.blender_scene_agent` once installed — which is why
#: nothing may hardcode it. Blender keys add-on preferences on this string.
NAME = __package__


def addon():
    """The add-on root module, the `hb` every panel and surface is handed."""
    return sys.modules[NAME]
