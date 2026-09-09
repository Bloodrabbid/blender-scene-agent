"""Design tokens, inherited from the upstream add-on's Figma variables.

Source: Folders / New Prompt Input, node 13787:64706. Names follow the Figma
variable they came from so a design change can be traced back:

    transparent/dark/05  ->  SURFACE
    button/brand         ->  BRAND
    caption/sm/medium    ->  CAPTION

Values only. Anything that composes them into a widget lives in ``controls``.
"""

from __future__ import annotations

import os
import sys

from ..skui import Font, Shadow, hexa, rgba

# -- color ---------------------------------------------------------------

BACKGROUND = hexa("#1c1e21")  # background/secondary
SURFACE_SUBTLE = hexa("#1b1b1b")  # background/surface/neutral-subtle

SURFACE = rgba(1, 1, 1, 0.05)  # transparent/dark/05
SURFACE_HOVER = rgba(1, 1, 1, 0.09)
SURFACE_PRESSED = rgba(1, 1, 1, 0.13)

# A toggle that is on. The same neutral family carried up until it separates
# from the chips around it — brand green is the Generate button's alone, and a
# lit chip in it read as the primary action rather than as a setting.
SELECTED = rgba(1, 1, 1, 0.24)
SELECTED_HOVER = rgba(1, 1, 1, 0.3)
SELECTED_PRESSED = rgba(1, 1, 1, 0.36)


def _over(top, bottom):
    """``top`` composited onto an opaque ``bottom``."""
    alpha = top[3]
    return rgba(*[t * alpha + b * (1 - alpha) for t, b in zip(top, bottom)])


# What the inset panel actually reads as on screen. A gradient that has to end
# in "nothing left to see" needs the composite as an opaque colour: fading to
# translucent SURFACE would tint whatever is behind it instead of hiding it.
PANEL = _over(SURFACE, BACKGROUND)
# The same colour, emptied. Skia interpolates gradients unpremultiplied, so a
# ramp to plain ``TRANSPARENT`` — which is transparent *black* — drags a grey
# smear through its middle. Keeping the RGB and dropping only the alpha is the
# fade the eye expects.
PANEL_CLEAR = rgba(*PANEL[:3], 0.0)

TEXT_PRIMARY = hexa("#ffffff")  # text/primary
TEXT_SECONDARY = hexa("#828282")  # text/secondary
ICON_PRIMARY = hexa("#ffffff")  # icon/primary
ICON_SECONDARY = hexa("#828282")  # icon/secondary

BRAND = hexa("#d1fe17")  # button/brand
BRAND_BEVEL = hexa("#829b19")  # the inset bottom edge on the Generate button
BRAND_DIM = hexa("#6f7f2a")
ON_BRAND = hexa("#131517")  # Font/PrimaryReverted

# A job that came back failed, cancelled or rejected. Not a Figma variable —
# the plugin's own surfaces never had to draw one — but the brand colour cannot
# stand in for it: green means "this is working" everywhere else here, so a
# failed generation wearing it says the opposite of what happened.
DANGER = hexa("#ff5b52")

# Controls that sit on top of imagery rather than on the panel, where a 5%
# white would disappear into whatever the picture happens to be.
SCRIM = rgba(0, 0, 0, 0.55)
SCRIM_HOVER = rgba(0, 0, 0, 0.72)
SCRIM_PRESSED = rgba(0, 0, 0, 0.85)

# The ring around a selected tile. The brand hue at three fifths of brand's
# luminance, which is the most an accent can be here: full-strength green is
# the composer's Generate button and nothing else, and a ring of it around a
# thumbnail came out at 12.3:1 against the panel — the brightest thing in the
# viewport, for a state that only says "this is the one the inspector is on".
# This lands at 7.8:1, still nearly twice the weight of RING_HOVER below, which
# is what keeps a selected tile from reading as merely hovered.
RING_SELECTED = hexa("#a9cc2b")

# The ring around a hovered tile. Read this alpha as a means, not as the
# appearance: it is drawn on the explorer's hover plane, which the GPU blits and
# therefore blends in *linear* light, while every other token here is authored
# for the sRGB compositing the rest of the design system does. The number lands
# about three stops below where it reads — 45% here paints a mid grey on the
# panel, roughly as loud as TEXT_SECONDARY, which is the intent. Left at
# BORDER_STRONG's 20% it arrived at 3% and, measured, moved the ring band by
# -3.6 levels: the affordance darkening the thing it was meant to light up.
RING_HOVER = rgba(1, 1, 1, 0.45)

# The ring's geometry, here rather than in one widget module because two of
# them draw it and it is the agreement between them that makes a lit tile in
# the launcher's panel and a lit tile in the explorer's grid the same thing.
#
# How heavy: an outset stroke reaches its full width outward, so this doubles
# as the clearance it asks for, and 2.5 is the most either grid can pay. In the
# explorer, half of ``CAPTION_GAP`` is still clear under the tile and two lit
# neighbours — one selected, one hovered — still have half of ``GAP`` between
# their strokes; in the launcher's panel, ``LG`` of padding and ``MD`` between
# tiles. A third lighter and it read as an edge of the tile rather than as a
# ring around it, a line on a tile-sized square no heavier than the hairline
# under a text field. The half point is deliberate and survives the pixel grid
# at both scales: 2.5 px at 1×, 5 at 2×.
RING_WIDTH = 2.5
# Clearance between a tile and the ring around it, hover's or the selection's.
# None: the ring sits on the square's edge. Held off it by even a couple of
# points it stopped reading as this tile's ring and started reading as a box
# drawn around it — the corners are where it shows, since a 16pt radius turns a
# uniform gap into a visible wedge of panel at each one.
RING_GAP = 0.0

# The filled part of a slider chip. Brand green would shout across a strip of
# a dozen chips, so the fill is the pressed surface carried further.
TRACK_FILL = rgba(1, 1, 1, 0.16)
TRACK_FILL_HOVER = rgba(1, 1, 1, 0.22)

BORDER_STRONG = rgba(1, 1, 1, 0.2)  # border/strong
BORDER_SUBTLE = rgba(1, 1, 1, 0.05)  # border/subtle
BORDER_FAINT = rgba(217 / 255, 217 / 255, 217 / 255, 0.04)
SEPARATOR = hexa("#d9d9d9")  # Separator/Card

# -- space ---------------------------------------------------------------
#
# **These are device pixels at 1×, and they are meant to be whole ones.** The
# surfaces rasterize a layer at the display's scale and blit it one texel to
# one pixel, so a token that is not an integer cannot land on the pixel grid on
# a standard display and every edge built from it is a two-pixel blend. The
# Figma ladder these come from — 4 / 6 / 8 / 12 / 16 — was drawn for a web page
# and read a third too large in a viewport; it used to be brought down by a 0.8
# applied at raster time, which is exactly what made them fractional. The
# reduction lives here now instead, so the numbers below *are* the design.

ZERO = 0.0
XS = 3.0
SM = 5.0
MD = 6.0
LG = 10.0
XL = 13.0

# -- radius --------------------------------------------------------------

RADIUS_CARD = 19.0
RADIUS_SURFACE = 16.0
RADIUS_BUTTON = 10.0
RADIUS_CHIP = 6.0
RADIUS_PILL = 26.0  # the collapsed composer
RADIUS_ROUND = 999.0

# -- type ----------------------------------------------------------------
#
# Sizes and line heights are whole pixels at 1× for the same reason the spacing
# is: Skia rasterizes a glyph at whatever size the canvas transform lands it
# on, and a caption asked for at 9.6 px is a caption with no crisp stem in it.

BODY = Font("Inter", 11, 400, tracking=0.08, line_height=16)  # body/sm/regular
BODY_MEDIUM = Font("Inter", 11, 500, tracking=0.08, line_height=16)  # body/sm/medium
# body/sm/semi-bold. Costs no font asset: the bundled Inter is variable and the
# weight axis is pinned per size, so this is the same face at 600.
BODY_STRONG = Font("Inter", 11, 600, tracking=0.08, line_height=16)
# Prompt rows may contain an 18 px reference chip. Keep the body face but give
# each row one pixel of air above and below the chip so adjacent wrapped rows
# never paint their pill backgrounds into one another.
PROMPT = Font("Inter", 11, 400, tracking=0.08, line_height=20)
CAPTION = Font("Inter", 10, 500, tracking=0.0, line_height=13)  # caption/sm/medium
LABEL = Font("Inter", 8, 700, tracking=0.16, line_height=10)  # caption/sm/semi-bold
# Not a design-system face — the product has no monospace, and a fenced code
# block in an agent reply needs one. Asked for by the host's own name for it,
# because `FontBook` resolves a family through the system font manager and
# falls back to a proportional face without saying so; all three of these ship
# with their platform.
CODE = Font(
    {"darwin": "Menlo", "win32": "Consolas"}.get(sys.platform, "DejaVu Sans Mono"),
    10,
    400,
    line_height=14,
)
BRAND_LABEL = Font("Space Grotesk", 10, 700, line_height=14)  # BrandBody/xs/bold-upper

PLACEHOLDER = rgba(1, 1, 1, 0.5)  # the collapsed pill's prompt colour
# Selected text sits under the glyphs, so it has to stay dark enough to read
# white type through the brand colour.
SELECTION = rgba(209 / 255, 254 / 255, 23 / 255, 0.28)

# -- elevation -----------------------------------------------------------

CARD_SHADOW = (
    Shadow(0, 3, 5, rgba(0, 0, 0, 0.16)),
    Shadow(0, 3, 13, rgba(0, 0, 0, 0.08)),
)

# The Generate button carries a five-layer stack in the design; the two tight
# ones read as a contact shadow and the wide one as lift.
BUTTON_SHADOW = (
    Shadow(0, 1, 2, rgba(0, 0, 0, 0.49)),
    Shadow(1, 2, 3, rgba(0, 0, 0, 0.43)),
    Shadow(2, 6, 4, rgba(0, 0, 0, 0.25)),
    Shadow(5, 16, 18, rgba(0, 0, 0, 0.15)),
)

# -- assets --------------------------------------------------------------

ICON_DIR = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "resources", "icons")
)


def icon(name):
    """``icon("plus")`` -> the bundled SVG path."""
    return os.path.join(ICON_DIR, f"{name}.svg")
