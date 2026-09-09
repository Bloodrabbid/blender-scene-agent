"""The property group: every value the user sets, on one object.

It hangs off `bpy.types.Scene` and both surfaces read and write it — the
sidebar as Blender widgets, the composer as chips. That is the whole reason the
two can never disagree: there is one value, not two copies of it, and a change
on either side runs the same `update=` callback.

There used to be fifty-odd properties here, and they were the account's: a
workspace, a model per generation kind, dynamic parameter collections decoded
from each model's JSON schema, image references, motion shots, realtime
prerender settings, the phone camera's sensitivity, the Results pager. All of
it went with the backend that gave those values meaning.

Three are left, and only three are needed: what is typed in the prompt, the
identity that ties a .blend to its saved chat threads, and the sidebar's tab
enum — which no longer has more than one tab, but `sidebar/base.py` still gates
panels on it and an enum with no items cannot be read.
"""

import bpy


class SceneAgentProps(bpy.types.PropertyGroup):
    prompt: bpy.props.StringProperty(
        name="Prompt",
        description="What to ask the agent",
        default="",
    )
    scene_builder_id: bpy.props.StringProperty(
        name="Scene Builder ID",
        description="Stable identity used to restore this scene's chat threads",
        default="",
        options={"HIDDEN"},
    )
    ui_section: bpy.props.EnumProperty(
        name="Section",
        items=[
            ("AGENT", "Agent", "The agent, the bridge, and how to reach both"),
        ],
        default="AGENT",
    )
