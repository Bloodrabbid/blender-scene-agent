"""The add-on's own state on the scene.

Everything the user sets lives on one `PropertyGroup` hung off `bpy.types.Scene`
as `scene_agent`, and both surfaces read and write it — the sidebar as Blender
widgets, the composer as chips. That is the whole reason the two can never
disagree: there is one value, not two copies of it.

This module is the *accessor* and imports nothing, so everything in the add-on
can reach for it. `SceneAgentProps` itself is `props/group.py`, which imports
the world — every callback it names lives with the feature that owns it — and
is imported only by the root.
"""

import bpy

from .. import root

# Preferences are keyed on the add-on's own package. `__package__` was right
# when this was `props.py` in the root and became the *props* package the moment
# it grew an `__init__` — silently, because the lookup just answers None and
# every caller treats that as "still registering". The Viewport UI switch
# stopped working and nothing said so. `root` is the one place that knows, and
# it knows by living there rather than by counting dots.
_ADDON = root.NAME


def props(context):
    """The add-on's property group on a context's scene."""
    return context.scene.scene_agent


def addon_preferences():
    """This add-on's preferences, or None while it is still registering."""
    try:
        return bpy.context.preferences.addons[_ADDON].preferences
    except (KeyError, AttributeError):
        return None
