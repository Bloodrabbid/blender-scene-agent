"""Analytics, kept as a shape and nothing else.

This module used to send the add-on's whole event taxonomy to Amplitude, with
a device id persisted beside the auth token and a user id taken from the
Higgsfield session's JWT. Both are gone: there is no account to identify and
nothing here leaves the machine. `diagnostics.py` next door still writes the
local session log, which is what support actually reads.

The seventy-odd `analytics.track(...)` call sites are left as they are. They
cost nothing, they document what the add-on considers an event, and keeping
them means the removal is one file rather than a diff across the whole tree.
"""

from __future__ import annotations


class AnalyticsEvent:
    """The old taxonomy, still named so call sites read unchanged."""

    PluginOpened = "Plugin Opened"
    RouteViewed = "Route Viewed"
    SignInStarted = "Sign In Started"
    SignInSucceeded = "Sign In Succeeded"
    SignInFailed = "Sign In Failed"
    SignOut = "Sign Out"
    GenerationSubmitted = "Generation Submitted"
    GenerationSucceeded = "Generation Succeeded"
    GenerationFailed = "Generation Failed"
    GenerationValidationFailed = "Generation Validation Failed"
    ModelSelected = "Model Selected"
    SettingsChanged = "Settings Changed"
    ReferenceAdded = "Reference Added"
    ReferenceRemoved = "Reference Removed"
    MediaImported = "Media Imported"
    MediaImportFailed = "Media Import Failed"
    UpdateAvailable = "Update Available"
    UIPerformance = "UI Performance"
    UISurfaceDisabled = "UI Surface Disabled"


def init_analytics(*, plugin_version="", host_app_version="", environment=""):
    return None


def shutdown_analytics():
    return None


def identify_user(user_id, properties=None):
    return None


def reset_user():
    return None


def track(event_name, properties=None):
    return None


def flush():
    return None


def error_name(error):
    """Still used by log lines, so it still answers."""
    if error is None:
        return ""
    name = type(error).__name__
    detail = getattr(error, "status", None)
    if detail is not None:
        return f"{name}:{detail}"
    return name


def reference_count(media_paths):
    if not media_paths:
        return 0
    total = 0
    for value in media_paths.values():
        if isinstance(value, (list, tuple)):
            total += len(value)
        elif value:
            total += 1
    return total


def generation_properties(**kwargs):
    return {key: value for key, value in kwargs.items() if value is not None}
