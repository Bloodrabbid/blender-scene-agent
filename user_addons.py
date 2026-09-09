# SPDX-FileCopyrightText: 2026 Higgsfield Inc.
# SPDX-License-Identifier: GPL-3.0-or-later

"""Install Supercomputer-authored single-file legacy Blender add-ons.

Writes ``{slug}.py`` into Blender's user scripts/addons, derives the module
name from ``bl_info['name']``, and always hot-reloads (disable → replace →
purge ``sys.modules`` → enable).
"""

from __future__ import annotations

import ast
import contextlib
import re
import sys
from pathlib import Path
from typing import Any

import addon_utils
import bpy

# Well under the TCP bridge 10 MiB request cap; large enough for fat single-file add-ons.
MAX_SOURCE_BYTES = 5 * 1024 * 1024


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", name.strip().lower()).strip("_")
    if not slug or not slug[0].isalpha():
        slug = "hf_" + (slug or "addon")
    return slug[:64]


def _parse_bl_info(source: str) -> dict[str, Any]:
    try:
        tree = ast.parse(source)
    except SyntaxError as error:
        raise ValueError(f"source is not valid Python: {error}") from error
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id == "bl_info":
                try:
                    value = ast.literal_eval(node.value)
                except (ValueError, TypeError) as error:
                    raise ValueError("bl_info must be a literal dict") from error
                if not isinstance(value, dict):
                    raise ValueError("bl_info must be a dict")
                return value
    raise ValueError("source must define bl_info = {...}")


def _validate_register_hooks(source: str) -> None:
    try:
        tree = ast.parse(source)
    except SyntaxError as error:
        raise ValueError(f"source is not valid Python: {error}") from error
    names = {
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    missing = [name for name in ("register", "unregister") if name not in names]
    if missing:
        raise ValueError(
            "source must define " + " and ".join(f"{name}()" for name in missing)
        )


def _jsonable_bl_info(bl_info: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in bl_info.items():
        if isinstance(value, tuple):
            out[key] = list(value)
        else:
            out[key] = value
    return out


def _purge_modules(module: str) -> None:
    prefix = module + "."
    for key in list(sys.modules):
        if key == module or key.startswith(prefix):
            del sys.modules[key]


def _disable_addon(module: str) -> None:
    if module in bpy.context.preferences.addons:
        with contextlib.suppress(Exception):
            bpy.ops.preferences.addon_disable(module=module)
    with contextlib.suppress(Exception):
        addon_utils.disable(module, default_set=False)


def _refresh_addon_modules() -> None:
    if hasattr(addon_utils, "modules_refresh"):
        with contextlib.suppress(Exception):
            addon_utils.modules_refresh()
            return
    with contextlib.suppress(Exception):
        addon_utils.modules(refresh=True)


def install(source: str) -> dict[str, Any]:
    """Install or update a single-file legacy add-on from source. Always reloads."""
    if not isinstance(source, str) or not source.strip():
        raise ValueError("source is required")
    if len(source.encode("utf-8")) > MAX_SOURCE_BYTES:
        raise ValueError(f"source exceeds {MAX_SOURCE_BYTES} bytes")

    bl_info = _parse_bl_info(source)
    _validate_register_hooks(source)
    display_name = bl_info.get("name")
    if not isinstance(display_name, str) or not display_name.strip():
        raise ValueError("bl_info['name'] must be a non-empty string")

    module = _slugify(display_name)
    addons_dir = Path(bpy.utils.user_resource("SCRIPTS", path="addons", create=True))
    target = addons_dir / f"{module}.py"

    _disable_addon(module)
    _purge_modules(module)
    target.write_text(source, encoding="utf-8")
    _refresh_addon_modules()

    enabled_mod = addon_utils.enable(module, default_set=True, persistent=True)
    if enabled_mod is None:
        return {
            "ok": False,
            "module": module,
            "path": str(target),
            "enabled": False,
            "needs_restart": True,
            "error": f"Failed to enable add-on '{module}' after install",
            "bl_info": _jsonable_bl_info(bl_info),
        }

    return {
        "ok": True,
        "module": module,
        "path": str(target),
        "enabled": True,
        "reloaded": True,
        "bl_info": _jsonable_bl_info(bl_info),
    }
