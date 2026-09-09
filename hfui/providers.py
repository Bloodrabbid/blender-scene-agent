"""Which brand mark stands for a catalog model.

Ported from the Adobe plugin's ``model-icon.tsx`` so the two surfaces show the
same glyph for the same model. The catalog does carry a ``provider`` string,
but it is empty on roughly a third of the entries — and where it is set it is
sometimes the lab rather than the mark the model is known by (the lab for
Image Auto, which the design gives a sparkle). So the id and display name are
matched first, exactly as Adobe does, and ``provider`` only answers what that
leaves over: it is what recognises Hailuo as MiniMax.

The 3D catalog is the gap — Meshy, Tripo, Tencent and Meta have no mark in the
Adobe set, so those models fall back to the neutral icon the caller passes.
"""

import os
import re

DIR = "providers"

# Only the labs we actually have a glyph for. Keys are the catalog's `provider`
# with everything but letters and digits stripped, since it is free text.
_BY_PROVIDER = {
    "blackforestlabs": "black-forest-labs",
    "bytedance": "seedance",
    "elevenlabs": "eleven-labs",
    "google": "google",
    "hailuo": "minimax",
    "scene_agent": "scene_agent",
    "kling": "kling",
    "kuaishou": "kling",
    "minimax": "minimax",
    "openai": "openai",
    "recraft": "recraft",
    # Z Image is Alibaba's Tongyi lab, the same family as Wan.
    "alibaba": "wan",
    "tongyimai": "wan",
    "wan": "wan",
    "xai": "grok",
}


def _from_text(source):
    """Adobe's `getModelIconType`. Order is load-bearing — first match wins."""
    if "eleven" in source:
        return "eleven-labs"
    if "minimax" in source or "hailuo" in source:
        return "minimax"
    if "grok" in source:
        return "grok"
    if "recraft" in source:
        return "recraft"
    if "kling" in source:
        return "kling"
    if (
        "seedance" in source
        or "seedream" in source
        or "seed_audio" in source
        or "seed speech" in source
        or "bytedance" in source
    ):
        return "seedance"
    if (
        "nano_banana" in source
        or "veo" in source
        or "gemini" in source
        or "google" in source
    ):
        return "google"
    if "openai" in source or "imagegen" in source or "gpt image" in source:
        return "openai"
    if "flux" in source:
        return "black-forest-labs"
    # The two short ones have to start a word or they swallow their neighbours:
    # a plain substring reads Topaz Image as z_image, and "wan" is three
    # letters. Adobe leans on match order for this; the boundary is cheaper.
    if re.search(r"\b(wan|z[_ ]image)", source):
        return "wan"
    if "image_auto" in source or "image auto" in source:
        return "image-auto"
    if (
        "scene_agent" in source
        or "cinematic" in source
        or "cinema studio" in source
        or "soul" in source
        or "marketing studio" in source
        or "thumbnail" in source
    ):
        return "scene_agent"
    return None


def name(job_type="", display_name="", provider=""):
    """``"providers/google"`` for a catalog entry, or ``None`` if unbranded."""
    mark = _from_text(f"{job_type or ''} {display_name or ''}".lower())
    if mark is None:
        key = "".join(ch for ch in str(provider or "").lower() if ch.isalnum())
        mark = _BY_PROVIDER.get(key)
    return f"{DIR}/{mark}" if mark else None


def for_model(model, fallback=None):
    """Same, from a catalog dict as the SDK hands it back."""
    if not model:
        return fallback
    return (
        name(
            model.get("job_type"),
            model.get("display_name"),
            model.get("provider"),
        )
        or fallback
    )


# -- Scene Builder (claudesfield) models ----------------------------------
#
# Ported from fnf-web's `model-grouping.ts` so the Scene Builder picker shows
# the same marks as the web's agent model select. Unlike the media catalog
# above, this catalog is provider-first: the backend sets `provider` on every
# row, so it is matched first and the id/label text only rescues rows cached
# on disk before the provider started being stored.

_AGENT_BY_PROVIDER = {
    "scene_agent": "scene_agent",
    "anthropic": "claude",
    "openai": "openai",
    "google": "google",
    "googleai": "google",
    "xai": "grok",
    "deepseek": "deepseek",
    "alibaba": "alibaba",
    "qwen": "alibaba",
    "moonshot": "moonshot",
    "moonshotai": "moonshot",
    "kimi": "moonshot",
    "bytedance": "bytedance",
    "bytedanceseed": "bytedance",
    "byteplus": "bytedance",
    "seed": "bytedance",
    "zai": "zai",
}


def _agent_from_text(source):
    """Provider read off a model's id and label, for provider-less rows."""
    if "claude" in source:
        return "claude"
    if "gpt" in source or "openai" in source:
        return "openai"
    if "gemini" in source or "google" in source:
        return "google"
    if "kimi" in source or "moonshot" in source:
        return "moonshot"
    if "grok" in source:
        return "grok"
    if "deepseek" in source:
        return "deepseek"
    if "qwen" in source:
        return "alibaba"
    if "doubao" in source or "seed" in source:
        return "bytedance"
    if "glm" in source:
        return "zai"
    return None


def agent(model_id="", label="", provider=""):
    """Icon name for a Scene Builder model row. Never ``None`` — the picker
    always draws something, so unknowns get the web's sparkle fallback."""
    model_id = str(model_id or "")
    if model_id == "google/gemini-orchestrator":
        return f"{DIR}/gemini-sparkle"
    key = "".join(ch for ch in str(provider or "").lower() if ch.isalnum())
    mark = _AGENT_BY_PROVIDER.get(key) or _agent_from_text(
        f"{model_id} {label or ''}".lower()
    )
    return f"{DIR}/{mark}" if mark else "sparkles-3"


def path(mark):
    """Absolute path of a mark returned by `name`, for a non-Skia consumer."""
    if not mark:
        return None
    resources = os.path.join(os.path.dirname(__file__), "..", "resources", "icons")
    return os.path.normpath(os.path.join(resources, f"{mark}.svg"))
