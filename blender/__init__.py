"""Thin helpers over Blender's own API, owned by nobody in particular.

What belongs here is the code that is *about Blender* rather than about this
product: finding a region, tagging a redraw, the small operations every feature
needs and none of them should define. Nothing in here may import a feature, a
UI package or the add-on root — that direction is what makes it safe for a
feature package to depend on it.
"""
